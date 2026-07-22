import json
import logging
import sqlite3
from pathlib import Path, PurePosixPath

from ctx_engine.intelligence.confidence import LOW_CONFIDENCE_THRESHOLD, LIKELY_STALE_THRESHOLD
from ctx_engine.mcp_server.tools.centrality import compute_centrality
from ctx_engine.mcp_server.tools.folding import format_folded_directory_tree

logger = logging.getLogger("ctx.assembly")


def get_confidence_tag(confidence: float, is_stale: int) -> str:
    if is_stale:
        return "[STALE - run 'ctx update <file>' to refresh]"
    if confidence < LIKELY_STALE_THRESHOLD:
        return "[LIKELY STALE - metadata may not reflect current code]"
    if confidence < LOW_CONFIDENCE_THRESHOLD:
        return "[LOW CONFIDENCE - code has changed since last summary]"
    return ""


def format_file_header(row: sqlite3.Row, long: bool = True) -> str:
    lines = [f"FILE: {row['path']}"]
    if row["system"]:
        lines.append(f"SYSTEM: {row['system']}")
    if row["purpose"]:
        lines.append(f"PURPOSE: {row['purpose']}")
    elif row["summary"]:
        lines.append(f"PURPOSE: {row['summary']}")
    else:
        lines.append("PURPOSE: (not yet summarized)")
    if long and row["exports"]:
        exports = json.loads(row["exports"])
        if exports:
            lines.append(f"EXPORTS: {', '.join(exports[:10])}")
    if long and row["imports"]:
        imports = json.loads(row["imports"])
        if imports:
            lines.append(f"IMPORTS: {', '.join(imports[:8])}")
    if row["danger"]:
        lines.append(f"DANGER: {row['danger']}")
    if row["last_change"]:
        lines.append(f"LAST CHANGE: {row['last_change']}")
    confidence_tag = get_confidence_tag(row["confidence"], row["is_stale"])
    if confidence_tag:
        lines.append(confidence_tag)
    return "\n".join(lines)


def format_function_record(row: sqlite3.Row, long: bool = True) -> str:
    lines = [f"FUNC: {row['id']}"]
    lines.append(f"SIG: {row['signature']}")
    if long and row["summary_long"]:
        lines.append(f"DOES: {row['summary_long']}")
    elif row["summary"]:
        lines.append(f"DOES: {row['summary']}")
    if row["mutates"]:
        mutates = json.loads(row["mutates"])
        if mutates:
            lines.append(f"MUTATES: {', '.join(mutates)}")
    if row["danger"]:
        lines.append(f"DANGER: {row['danger']}")
    tags = []
    ct = get_confidence_tag(row["confidence"], row["is_stale"])
    if ct:
        tags.append(ct)
    if row["is_tainted"]:
        tags.append("[TAINTED - see warning above]")
    if tags:
        lines.append(" ".join(tags))
    return "\n".join(lines)


def get_import_paths(conn: sqlite3.Connection, target_file: str) -> list[str]:
    row = conn.execute(
        "SELECT imports, used_by FROM files WHERE path = ?", (target_file,)
    ).fetchone()
    if row is None:
        return []
    imports = json.loads(row["imports"] or "[]")
    used_by = json.loads(row["used_by"] or "[]")
    seen = list(dict.fromkeys(imports + used_by))
    return seen


def select_relevant_functions(
    functions: list[sqlite3.Row],
    target_line: int | None,
    limit: int = 20,
) -> list[sqlite3.Row]:
    if target_line is not None:
        nearby = [f for f in functions if f["line_start"] <= target_line <= f["line_end"]]
        if nearby:
            result = nearby[:limit]
            remaining = [f for f in functions if f not in result]
            result.extend(remaining[:limit - len(result)])
            return result
    return functions[:limit]


def assemble_zone3(
    conn: sqlite3.Connection,
    repo_root: Path,
    target_file: str,
    compact: bool = False,
) -> str:
    parts = []

    repo_name = repo_root.name
    total_files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]

    system_rows = conn.execute(
        "SELECT system, COUNT(*) as cnt FROM files WHERE system IS NOT NULL GROUP BY system ORDER BY cnt DESC"
    ).fetchall()
    systems_str = ""
    if system_rows:
        sys_parts = [f"{r['system']}: {r['cnt']}" for r in system_rows]
        systems_str = f"  Systems: {', '.join(sys_parts)}"

    parts.append(
        f"PROJECT: {repo_name}\n"
        f"  Total files: {total_files}\n"
        f"{systems_str}"
    )

    parts.append(format_folded_directory_tree(conn, repo_root, target_file))

    global_dangers = conn.execute(
        "SELECT description, reason, added_by FROM dangers WHERE scope = '*'"
    ).fetchall()
    if global_dangers:
        parts.append(format_dangers_block("GLOBAL DANGER ZONES", global_dangers))

    dep_paths = get_import_paths(conn, target_file)
    scoped_dangers = []
    if dep_paths:
        placeholders = ",".join("?" for _ in dep_paths)
        query = (
            f"SELECT scope, description, reason, added_by FROM dangers "
            f"WHERE (scope = ? OR scope IN ({placeholders})) AND scope != '*'"
        )
        scoped_dangers = conn.execute(query, [target_file] + dep_paths).fetchall()
    else:
        scoped_dangers = conn.execute(
            "SELECT scope, description, reason, added_by FROM dangers "
            "WHERE scope = ? AND scope != '*'",
            (target_file,),
        ).fetchall()
    if scoped_dangers:
        parts.append(format_dangers_block("FILE-SCOPED DANGER ZONES", scoped_dangers))

    decisions = conn.execute(
        "SELECT scope, decision, alternatives, reason FROM decisions "
        "WHERE scope = ? OR scope IS NULL OR scope = '*'",
        (target_file,),
    ).fetchall()
    if decisions:
        if compact:
            parts.append(format_decisions_compact(decisions))
        else:
            parts.append(format_decisions_block(decisions))

    if not compact:
        session_entries = conn.execute(
            "SELECT entry, files_touched, timestamp FROM session_log "
            "ORDER BY timestamp DESC LIMIT 3"
        ).fetchall()
        if session_entries:
            parts.append(format_session_log(session_entries))

    return "\n\n".join(parts)


def assemble_zone3_compact(
    conn: sqlite3.Connection,
    repo_root: Path,
    target_file: str,
) -> str:
    return assemble_zone3(conn, repo_root, target_file, compact=True)


def assemble_zone0(
    conn: sqlite3.Connection,
    target_file: str,
    target_line: int | None,
    long_summaries: bool = True,
) -> str:
    parts = []

    file_row = conn.execute(
        "SELECT * FROM files WHERE path = ?", (target_file,)
    ).fetchone()
    if file_row is None:
        return (
            f"FILE: {target_file}\n"
            "(not indexed)\n\n"
            f"Run 'ctx update {target_file}' to index this file, "
            "or 'ctx init' to index the entire repo."
        )

    parts.append(format_file_header(file_row))

    all_functions = conn.execute(
        "SELECT * FROM functions WHERE file = ? ORDER BY line_start",
        (target_file,),
    ).fetchall()

    if len(all_functions) > 30:
        functions = select_relevant_functions(all_functions, target_line, limit=20)
        pagination_note = (
            f"Showing 20 of {len(all_functions)} functions. "
            "Call ctx.get_function(id) for others."
        )
    else:
        functions = all_functions
        pagination_note = None

    for fn in functions:
        parts.append(format_function_record(fn, long=long_summaries))

    if pagination_note:
        parts.append(pagination_note)

    tainted = [fn for fn in functions if fn["is_tainted"]]
    for fn in tainted:
        parts.append(
            f"WARNING: TAINTED: {fn['id']}\n"
            f"  Dependency changed: {fn['taint_source']}\n"
            "  Update this function's summary after reviewing the change."
        )

    recent = conn.execute(
        "SELECT commit_hash, summary, author, timestamp FROM changes "
        "WHERE file = ? ORDER BY timestamp DESC LIMIT 3",
        (target_file,),
    ).fetchall()
    if recent:
        parts.append(format_recent_changes(recent))

    return "\n\n".join(parts)


def assemble_zone1(
    conn: sqlite3.Connection,
    target_file: str,
    centrality_scores: dict[str, float],
    long_mode: bool = True,
) -> str:
    parts = []

    row = conn.execute(
        "SELECT imports, used_by FROM files WHERE path = ?", (target_file,)
    ).fetchone()
    if row is None:
        return ""

    imports = json.loads(row["imports"] or "[]")
    used_by = json.loads(row["used_by"] or "[]")
    all_deps = list(dict.fromkeys(imports + used_by))

    ranked = sorted(all_deps, key=lambda p: centrality_scores.get(p, 0.0), reverse=True)[:12]

    for dep_path in ranked:
        dep_row = conn.execute(
            "SELECT * FROM files WHERE path = ?", (dep_path,)
        ).fetchone()
        if dep_row is None:
            continue

        if dep_row["used_by_count"] > 15:
            summary_text = dep_row["summary"] or dep_row["purpose"] or "(not summarized)"
            parts.append(
                f"FILE: {dep_path} [HIGH-FREQUENCY: {dep_row['used_by_count']} dependents]\n"
                f"  PURPOSE: {summary_text}"
            )
        else:
            parts.append(format_file_header(dep_row, long=long_mode))

    return "\n\n".join(parts)


def assemble_zone2(
    conn: sqlite3.Connection,
    target_file: str,
    long_mode: bool = False,
) -> str:
    parts = []

    callees = conn.execute(
        """SELECT DISTINCT cg.callee_id, cg.callee_name, cg.callee_file,
                  cg.is_ambiguous, cg.candidates
           FROM call_graph cg
           JOIN functions fn ON fn.id = cg.caller_id
           WHERE fn.file = ? AND cg.callee_id IS NOT NULL
           LIMIT 15""",
        (target_file,),
    ).fetchall()

    callers = conn.execute(
        """SELECT DISTINCT cg.caller_id
           FROM call_graph cg
           JOIN functions fn ON fn.id = cg.callee_id
           WHERE fn.file = ? AND cg.caller_id NOT IN (
               SELECT id FROM functions WHERE file = ?
           )
           LIMIT 8""",
        (target_file, target_file),
    ).fetchall()

    if callees:
        parts.append("CALL TARGETS (functions this file calls):")
        for row in callees:
            fn_row = conn.execute(
                "SELECT * FROM functions WHERE id = ?", (row["callee_id"],)
            ).fetchone()
            if fn_row:
                parts.append(format_function_record(fn_row, long=long_mode))

    if callers:
        parts.append("CALLERS (external functions that call into this file):")
        for row in callers:
            fn_row = conn.execute(
                "SELECT * FROM functions WHERE id = ?", (row["caller_id"],)
            ).fetchone()
            if fn_row:
                parts.append(format_function_record(fn_row, long=False))

    return "\n\n".join(parts)


def assemble_context(
    conn: sqlite3.Connection,
    repo_root: Path,
    target_file: str,
    target_line: int | None = None,
    budget: int = 8000,
) -> str:
    centrality = compute_centrality(conn, target_file)

    zone3 = assemble_zone3(conn, repo_root, target_file)
    zone0 = assemble_zone0(conn, target_file, target_line, long_summaries=True)
    zone1 = assemble_zone1(conn, target_file, centrality, long_mode=True)
    zone2 = assemble_zone2(conn, target_file, long_mode=False)

    def total_tokens(*zones: str) -> int:
        return sum(len(z) // 4 for z in zones if z)

    if total_tokens(zone3, zone0, zone1, zone2) > budget:
        zone0 = assemble_zone0(conn, target_file, target_line, long_summaries=False)
        zone2 = assemble_zone2(conn, target_file, long_mode=False)

    if total_tokens(zone3, zone0, zone1, zone2) > budget:
        zone1 = assemble_zone1(conn, target_file, centrality, long_mode=False)

    if total_tokens(zone3, zone0, zone1) > budget:
        zone2 = ""

    if total_tokens(zone3, zone0, zone1) > budget:
        zone3 = assemble_zone3_compact(conn, repo_root, target_file)

    sections = [s for s in [zone3, zone0, zone1, zone2] if s]
    result = "\n\n" + ("\u2500" * 60 + "\n\n").join(sections)

    actual = len(result) // 4
    if actual > budget:
        logger.warning(
            "Context for %s is %d tokens (budget %d) after all pruning steps",
            target_file, actual, budget,
        )

    return result


def format_dangers_block(title: str, dangers: list[sqlite3.Row]) -> str:
    lines = [f"{title}:"]
    for d in dangers:
        scope_prefix = f"[{d['scope']}]" if d["scope"] else ""
        lines.append(f"  {scope_prefix} {d['description']}")
        if d["reason"]:
            lines.append(f"    Reason: {d['reason']}")
        if d["added_by"]:
            lines.append(f"    Added by: {d['added_by']}")
    return "\n".join(lines)


def format_decisions_block(decisions: list[sqlite3.Row]) -> str:
    lines = ["ARCHITECTURAL DECISIONS:"]
    for d in decisions:
        scope_prefix = f"[{d['scope'] or '*'}]"
        lines.append(f"  {scope_prefix} {d['decision']}")
        if d["alternatives"]:
            lines.append(f"    Alternatives: {d['alternatives']}")
        if d["reason"]:
            lines.append(f"    Reason: {d['reason']}")
    return "\n".join(lines)


def format_decisions_compact(decisions: list[sqlite3.Row]) -> str:
    lines = ["ARCHITECTURAL DECISIONS:"]
    for d in decisions:
        scope_prefix = f"[{d['scope'] or '*'}]"
        lines.append(f"  {scope_prefix} {d['decision']}")
    return "\n".join(lines)


def format_session_log(entries: list[sqlite3.Row]) -> str:
    lines = ["RECENT SESSION LOG:"]
    for e in entries:
        ts = e["timestamp"] or ""
        entry_text = e["entry"]
        lines.append(f"  [{ts}] {entry_text}")
    return "\n".join(lines)


def format_recent_changes(entries: list[sqlite3.Row]) -> str:
    lines = ["RECENT CHANGES:"]
    for e in entries:
        h = (e["commit_hash"] or "?")[:7]
        s = e["summary"] or ""
        a = e["author"] or ""
        t = e["timestamp"] or ""
        lines.append(f"  {h} \"{s}\" ({a}) {t}")
    return "\n".join(lines)
