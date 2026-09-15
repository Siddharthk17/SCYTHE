"""`ctx impact` — transitive impact analysis over the call and import graphs.

Before changing a function, know the blast radius: every function that
depends on it, directly or transitively, grouped by severity (critical /
major / minor) and call distance, with danger zones and system-boundary
crossings called out.
"""
import json
import sqlite3
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from ctx_engine.db import connect


@dataclass
class ImpactReport:
    target_ids: list[str] = field(default_factory=list)
    target_label: str = ""
    affected_functions: dict[str, int] = field(default_factory=dict)
    affected_files: dict[str, int] = field(default_factory=dict)
    import_affected_files: dict[str, int] = field(default_factory=dict)
    edges: list[tuple[str, str]] = field(default_factory=list)


def compute_transitive_impact(
    conn: sqlite3.Connection,
    target_ids: list[str],
    max_depth: int = 5,
) -> ImpactReport:
    """BFS over the call graph (caller direction) and the import graph.

    Call-graph traversal finds every function that depends on the target,
    directly or transitively. Import-graph traversal then marks files that
    import affected files as weakly affected. The visited sets are checked
    before enqueueing, so circular call graphs always terminate.
    """
    visited_functions: dict[str, int] = {}
    visited_files: dict[str, int] = {}
    edges: list[tuple[str, str]] = []

    queue: deque[tuple[str, int]] = deque((fn_id, 0) for fn_id in target_ids)
    for fn_id in target_ids:
        fn_row = conn.execute(
            "SELECT file FROM functions WHERE id = ?", (fn_id,)
        ).fetchone()
        if fn_row:
            visited_files.setdefault(fn_row["file"], 0)

    if max_depth <= 0:
        # Depth 0: the target itself, no traversal.
        for fn_id in target_ids:
            visited_functions[fn_id] = 0
        return ImpactReport(
            target_ids=list(target_ids),
            affected_functions=visited_functions,
            affected_files=visited_files,
            import_affected_files={},
            edges=edges,
        )

    if max_depth <= 0:
        # Depth 0: the target itself, no traversal.
        for fn_id in target_ids:
            visited_functions[fn_id] = 0
        return ImpactReport(
            target_ids=list(target_ids),
            affected_functions=visited_functions,
            affected_files=visited_files,
            import_affected_files={},
            edges=edges,
        )

    while queue:
        fn_id, depth = queue.popleft()
        if fn_id in visited_functions:
            continue
        visited_functions[fn_id] = depth
        if depth >= max_depth:
            continue

        callers = conn.execute(
            "SELECT caller_id FROM call_graph WHERE callee_id = ?", (fn_id,)
        ).fetchall()

        for row in callers:
            caller_id = row["caller_id"]
            edges.append((fn_id, caller_id))
            if caller_id not in visited_functions:
                queue.append((caller_id, depth + 1))
                caller_file = conn.execute(
                    "SELECT file FROM functions WHERE id = ?", (caller_id,)
                ).fetchone()
                if caller_file and caller_file["file"] not in visited_files:
                    visited_files[caller_file["file"]] = depth + 1

    file_queue: deque[tuple[str, int]] = deque(
        (f, d) for f, d in visited_files.items() if d > 0
    )
    visited_import_files: dict[str, int] = {}

    while file_queue:
        file_path, depth = file_queue.popleft()
        if depth >= max_depth:
            continue
        if file_path in visited_import_files:
            continue
        visited_import_files[file_path] = depth

        importers = conn.execute(
            "SELECT path FROM files "
            "WHERE used_by IS NOT NULL AND used_by LIKE ?",
            (f'%"{file_path}"%',),
        ).fetchall()

        for row in importers:
            if row["path"] not in visited_import_files:
                file_queue.append((row["path"], depth + 1))

    return ImpactReport(
        target_ids=list(target_ids),
        affected_functions=visited_functions,
        affected_files=visited_files,
        import_affected_files=visited_import_files,
        edges=edges,
    )


def resolve_impact_targets(
    conn: sqlite3.Connection, target: str
) -> tuple[list[str], str]:
    """Resolve a target string to function ids.

    Accepts a function id (`path::Name`), a file path (union of every
    function in the file), or a system name (union of every function in
    files assigned to that system).
    """
    row = conn.execute(
        "SELECT id FROM functions WHERE id = ?", (target,)
    ).fetchone()
    if row:
        return [target], target

    file_row = conn.execute(
        "SELECT path FROM files WHERE path = ?", (target,)
    ).fetchone()
    if file_row:
        ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM functions WHERE file = ? ORDER BY line_start",
                (target,),
            ).fetchall()
        ]
        if not ids:
            raise ValueError(
                f"File '{target}' is indexed but contains no functions."
            )
        return ids, target

    sys_rows = conn.execute(
        "SELECT id FROM functions WHERE file IN "
        "(SELECT path FROM files WHERE system = ?) ORDER BY id",
        (target,),
    ).fetchall()
    if sys_rows:
        return [r["id"] for r in sys_rows], f"system:{target}"

    raise ValueError(
        f"Target not found: '{target}'. "
        "Pass a function id (path::Name), an indexed file path, or a system name."
    )


def severity_of(depth: int) -> str:
    if depth <= 1:
        return "critical"
    if depth <= 3:
        return "major"
    return "minor"


def group_by_severity(report: ImpactReport) -> dict[str, list[tuple[str, int]]]:
    """Split affected functions (excluding the targets) into severity tiers."""
    groups: dict[str, list[tuple[str, int]]] = {
        "critical": [], "major": [], "minor": [],
    }
    targets = set(report.target_ids)
    for fn_id, depth in sorted(report.affected_functions.items(), key=lambda kv: (kv[1], kv[0])):
        if fn_id in targets:
            continue
        groups[severity_of(depth)].append((fn_id, depth))
    return groups


def _function_file(conn: sqlite3.Connection, fn_id: str) -> str:
    row = conn.execute(
        "SELECT file FROM functions WHERE id = ?", (fn_id,)
    ).fetchone()
    return row["file"] if row else "?"


def _file_system(conn: sqlite3.Connection, path: str) -> str | None:
    row = conn.execute(
        "SELECT system FROM files WHERE path = ?", (path,)
    ).fetchone()
    return row["system"] if row else None


def dangers_in_blast_radius(
    conn: sqlite3.Connection, report: ImpactReport
) -> list[dict]:
    """Danger zones scoped to affected functions, files, or everything."""
    scopes = set(report.target_ids) | set(report.affected_functions) | set(report.affected_files) | {"*"}
    placeholders = ",".join("?" for _ in scopes)
    rows = conn.execute(
        "SELECT id, scope, description, reason, added_by FROM dangers "
        f"WHERE scope IN ({placeholders}) ORDER BY scope",
        list(scopes),
    ).fetchall()
    return [dict(r) for r in rows]


def tainted_in_blast_radius(
    conn: sqlite3.Connection, report: ImpactReport
) -> list[str]:
    rows = conn.execute(
        "SELECT id FROM functions WHERE is_tainted = 1"
    ).fetchall()
    tainted = {r["id"] for r in rows}
    return sorted(set(report.affected_functions) & tainted)


def system_crossings(
    conn: sqlite3.Connection, report: ImpactReport
) -> list[tuple[str, str, str]]:
    """(from_system, to_system, via_function) for cross-system ripples."""
    target_systems = {
        _file_system(conn, f)
        for f in report.affected_files
        if report.affected_files[f] == 0
    }
    target_systems.discard(None)
    crossings: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    targets = set(report.target_ids)
    for fn_id in report.affected_functions:
        if fn_id in targets:
            continue
        to_sys = _file_system(conn, _function_file(conn, fn_id))
        if not to_sys:
            continue
        for from_sys in target_systems:
            if from_sys != to_sys and (from_sys, to_sys) not in seen:
                seen.add((from_sys, to_sys))
                crossings.append((from_sys, to_sys, fn_id))
    return crossings


def print_text_report(
    conn: sqlite3.Connection, report: ImpactReport, repo_name: str
) -> None:
    groups = group_by_severity(report)
    targets = set(report.target_ids)
    n_funcs = max(0, len(report.affected_functions) - len(targets))
    n_files = len(report.affected_files)
    critical = groups["critical"]
    major = groups["major"]
    minor = groups["minor"]

    print(f"ctx impact {report.target_label} — {repo_name}")
    print()
    print(f"  Target: {report.target_label}")
    if len(report.target_ids) == 1:
        only = report.target_ids[0]
        print(f"  File: {_function_file(conn, only)}")
        system = _file_system(conn, _function_file(conn, only))
        if system:
            print(f"  System: {system}")
    else:
        print(f"  Functions in scope: {len(report.target_ids)}")
    print()
    print("  " + "═" * 46)
    print(f"  BLAST RADIUS: {n_funcs} functions, {n_files} files")
    print("  " + "═" * 46)

    if n_funcs == 0:
        print()
        print("  no dependent functions found — no callers in the index.")
    else:
        if critical:
            print()
            print(f"  ▸ CRITICAL (depth 1 — direct callers, {len(critical)} functions)")
            print()
            for fn_id, _ in critical:
                print(f"    {fn_id}  ({_function_file(conn, fn_id)})")
        if major:
            print()
            print(f"  ▸ MAJOR (depth 2–3 — indirect callers, {len(major)} functions)")
            print()
            for fn_id, depth in major:
                print(f"    {fn_id}  ({_function_file(conn, fn_id)})  depth {depth}")
        if minor:
            print()
            print(f"  ▸ MINOR (depth 4+ — deep indirect, {len(minor)} functions)")
            print()
            for fn_id, depth in minor:
                print(f"    {fn_id}  (depth {depth})")

    import_only = {
        p: d for p, d in report.import_affected_files.items()
        if p not in report.affected_files
    }
    if import_only:
        print()
        print(f"  [import-only] {len(import_only)} additional files import affected files:")
        for path in sorted(import_only):
            print(f"    [import-only] {path}  (depth {import_only[path]})")

    print()
    print("  " + "═" * 46)
    print("  DANGER ZONES IN BLAST RADIUS")
    print("  " + "═" * 46)
    print()
    for dz in dangers_in_blast_radius(conn, report):
        print(f"  [{dz['scope']}] {dz['description']} ({dz['added_by']})")
    for fn_id in tainted_in_blast_radius(conn, report):
        print(f"  [TAINTED] {fn_id} — summary may not reflect current behavior.")
    if not dangers_in_blast_radius(conn, report) and not tainted_in_blast_radius(conn, report):
        print("  None — no danger zones or tainted functions in the blast radius.")

    crossings = system_crossings(conn, report)
    print()
    print("  " + "═" * 46)
    print("  SYSTEM BOUNDARY CROSSINGS")
    print("  " + "═" * 46)
    print()
    if crossings:
        print(f"  Change propagates across {len(crossings)} system boundaries:")
        for from_sys, to_sys, via in crossings:
            print(f"    {from_sys} → {to_sys} ({via})")
        print()
        print("  This is a cross-system change. Coordinate with all system owners.")
    else:
        print("  None — the blast radius stays within one system.")

    print()
    print("  " + "═" * 46)
    print("  SUMMARY")
    print("  " + "═" * 46)
    print(
        f"  Changing {report.target_label} affects {n_funcs} functions "
        f"in {n_files} files across {len(crossings)} system boundaries."
    )
    print(f"  Minimum review required for: {len(critical)} critical callers.")


def _mermaid_node_id(fn_id: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in fn_id)


def render_mermaid_impact(
    conn: sqlite3.Connection, report: ImpactReport
) -> str:
    """Render the caller-direction impact graph as Mermaid."""
    lines = ["graph TD"]
    nodes = set(report.target_ids)
    for callee, caller in report.edges:
        if callee in report.affected_functions and caller in report.affected_functions:
            nodes.add(callee)
            nodes.add(caller)
    targets = set(report.target_ids)
    for fn_id in sorted(nodes):
        short = fn_id.split("::")[-1].replace(".", "\\n")
        lines.append(f'    {_mermaid_node_id(fn_id)}["{short}"]')
    seen_edges: set[tuple[str, str]] = set()
    for callee, caller in report.edges:
        if callee in nodes and caller in nodes and (callee, caller) not in seen_edges:
            seen_edges.add((callee, caller))
            lines.append(
                f"    {_mermaid_node_id(callee)} --> {_mermaid_node_id(caller)}"
            )
    for fn_id in sorted(nodes):
        if fn_id in targets:
            lines.append(f"    {_mermaid_node_id(fn_id)}:::target")
        else:
            lines.append(f"    {_mermaid_node_id(fn_id)}:::{severity_of(report.affected_functions[fn_id])}")
    lines += [
        "    classDef target fill:#E74C3C,color:#fff",
        "    classDef critical fill:#F1948A",
        "    classDef major fill:#FAD7A0",
        "    classDef minor fill:#D5F5E3",
    ]
    return "```mermaid\n" + "\n".join(lines) + "\n```"


def report_to_json(
    conn: sqlite3.Connection, report: ImpactReport
) -> dict:
    groups = group_by_severity(report)
    return {
        "target": report.target_label,
        "target_ids": report.target_ids,
        "affected_functions": dict(report.affected_functions),
        "affected_files": dict(report.affected_files),
        "import_affected_files": dict(report.import_affected_files),
        "severity": {
            tier: [{"id": fn_id, "depth": depth} for fn_id, depth in items]
            for tier, items in groups.items()
        },
        "danger_zones": dangers_in_blast_radius(conn, report),
        "tainted": tainted_in_blast_radius(conn, report),
        "system_crossings": [
            {"from": a, "to": b, "via": v} for a, b, v in system_crossings(conn, report)
        ],
        "summary": {
            "functions": max(0, len(report.affected_functions) - len(set(report.target_ids))),
            "files": len(report.affected_files),
            "critical": len(groups["critical"]),
            "major": len(groups["major"]),
            "minor": len(groups["minor"]),
        },
    }


def run_impact(
    repo_root: Path,
    target: str,
    depth: int = 5,
    format: str = "text",
) -> None:
    db_path = repo_root / ".ctx" / "index.db"
    if not db_path.exists():
        raise FileNotFoundError("Database not found. Run 'ctx init' first.")

    conn = connect(db_path)
    try:
        target_ids, label = resolve_impact_targets(conn, target)
        report = compute_transitive_impact(conn, target_ids, max_depth=depth)
        report.target_label = label
        if format == "json":
            print(json.dumps(report_to_json(conn, report), indent=2))
        elif format == "mermaid":
            print(render_mermaid_impact(conn, report))
        else:
            print_text_report(conn, report, repo_root.name)
    finally:
        conn.close()
