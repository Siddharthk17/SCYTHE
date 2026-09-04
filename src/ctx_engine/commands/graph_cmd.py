"""`ctx graph` — export the import/call graph as DOT (Graphviz) or Mermaid.

Two render targets: DOT for Graphviz (`dot -Tpng`) and Mermaid for GitHub/PR
rendering. Supports neighborhood focus (--focus with --depth) and optional
call-graph edges (--with-calls).
"""
import json
import sqlite3
from pathlib import Path

from ctx_engine.db import connect

# Color scheme (same hex values work in DOT and Mermaid).
COLOR_DEFAULT = "#AED6F1"
COLOR_FOCUS = "#85C1E9"
COLOR_HIGH_FANIN = "#F1948A"
COLOR_STALE = "#F9E79F"
COLOR_TAINTED = "#FAD7A0"
HIGH_FANIN_THRESHOLD = 15
LARGE_GRAPH_NODES = 100


def _short_label(path: str, system: str | None) -> str:
    """Node label: `filename (system)`, or just `filename` without a system.

    Deep paths collapse to the bare filename to keep diagrams readable.
    """
    name = Path(path).name
    return f"{name} ({system})" if system else name


def _load_graph(
    conn: sqlite3.Connection,
    focus: str | None,
    depth: int,
) -> dict[str, dict]:
    """Load nodes and edges, optionally narrowed to a focus neighborhood.

    Returns {path: {"system", "used_by_count", "is_stale", "tainted", "imports", "used_by"}}.
    """
    rows = conn.execute(
        """SELECT f.path, f.system, f.used_by_count, f.is_stale, f.imports, f.used_by,
                  (SELECT COUNT(*) FROM functions fn
                   WHERE fn.file = f.path AND fn.is_tainted = 1) AS tainted
           FROM files f"""
    ).fetchall()

    nodes: dict[str, dict] = {}
    for r in rows:
        try:
            imports = json.loads(r["imports"] or "[]")
        except json.JSONDecodeError:
            imports = []
        try:
            used_by = json.loads(r["used_by"] or "[]")
        except json.JSONDecodeError:
            used_by = []
        nodes[r["path"]] = {
            "system": r["system"],
            "used_by_count": r["used_by_count"] or 0,
            "is_stale": bool(r["is_stale"]),
            "tainted": bool(r["tainted"]),
            "imports": imports,
            "used_by": used_by,
        }

    if focus is None:
        return nodes

    if focus not in nodes:
        raise ValueError(f"Focus file not in index: {focus}")

    # BFS over import edges in both directions, up to depth hops.
    selected: set[str] = {focus}
    frontier = {focus}
    for _ in range(depth):
        next_frontier: set[str] = set()
        for node in frontier:
            for neighbor in nodes[node]["imports"] + nodes[node]["used_by"]:
                if neighbor in nodes and neighbor not in selected:
                    next_frontier.add(neighbor)
        selected |= next_frontier
        frontier = next_frontier

    return {p: info for p, info in nodes.items() if p in selected}


def _node_color(path: str, info: dict, focus: str | None) -> str:
    if focus is not None and path == focus:
        return COLOR_FOCUS
    if info["used_by_count"] > HIGH_FANIN_THRESHOLD:
        return COLOR_HIGH_FANIN
    if info["is_stale"]:
        return COLOR_STALE
    if info["tainted"]:
        return COLOR_TAINTED
    return COLOR_DEFAULT


def render_dot(graph: dict[str, dict], focus: str | None, with_calls: bool) -> str:
    """Render the import graph as Graphviz DOT.

    All node ids are double-quoted (paths contain `/` and `.`), and literal
    `"` characters in labels are escaped.
    """
    lines = [
        "digraph ctx_import_graph {",
        "    rankdir=LR;",
        "    node [shape=box, style=filled, fillcolor=lightblue];",
        "",
        "    /* File nodes */",
    ]
    for path, info in sorted(graph.items()):
        label = _short_label(path, info["system"]).replace('"', '\\"')
        if info["used_by_count"] > HIGH_FANIN_THRESHOLD:
            label += f"\\n★ imported by {info['used_by_count']}"
        color = _node_color(path, info, focus)
        lines.append(f'    "{path}" [label="{label}", fillcolor="{color}"];')

    lines.append("")
    lines.append("    /* Import edges */")
    for path in sorted(graph):
        for target in sorted(graph[path]["imports"]):
            if target in graph:
                lines.append(f'    "{path}" -> "{target}";')

    if with_calls:
        lines.append("")
        lines.append("    /* Call edges (dashed) */")
        for path in sorted(graph):
            for target in sorted(graph[path].get("calls", [])):
                lines.append(
                    f'    "{path}" -> "{target}" [style=dashed];'
                )
    lines.append("}")
    return "\n".join(lines)


def _mermaid_id(path: str) -> str:
    """Stable Mermaid node id: munged path (letters, digits, underscore only)."""
    return "".join(c if c.isalnum() else "_" for c in path)


def render_mermaid(graph: dict[str, dict], focus: str | None, with_calls: bool) -> str:
    """Render the import graph as Mermaid, wrapped in a code fence."""
    lines = ["graph LR"]
    for path in sorted(graph):
        label = _short_label(path, graph[path]["system"]).replace('"', "'")
        lines.append(f'    {_mermaid_id(path)}["{label}"]')
    for path in sorted(graph):
        for target in sorted(graph[path]["imports"]):
            if target in graph:
                edge = f"    {_mermaid_id(path)} --> {_mermaid_id(target)}"
                if edge not in lines:
                    lines.append(edge)
    if with_calls:
        for path in sorted(graph):
            for target in sorted(graph[path].get("calls", [])):
                lines.append(
                    f"    {_mermaid_id(path)} -->|calls| {_mermaid_id(target)}"
                )
    for path in sorted(graph):
        color = _node_color(path, graph[path], focus)
        if color != COLOR_DEFAULT:
            lines.append(f"    style {_mermaid_id(path)} fill:{color}")
    return "```mermaid\n" + "\n".join(lines) + "\n```"


def collect_call_edges(conn: sqlite3.Connection, graph: dict[str, dict]) -> None:
    """Attach function-level call edges (as file pairs) to graph nodes.

    Only edges whose caller file is in the graph are collected; duplicates
    between the same file pair collapse to one edge.
    """
    for path in graph:
        graph[path]["calls"] = []
    rows = conn.execute(
        """SELECT DISTINCT cf.file AS caller_file, tf.file AS callee_file
           FROM call_graph cg
           JOIN functions cf ON cf.id = cg.caller_id
           JOIN functions tf ON tf.id = cg.callee_id
           WHERE cg.callee_id IS NOT NULL"""
    ).fetchall()
    for row in rows:
        if row["caller_file"] in graph and row["callee_file"] in graph:
            if row["callee_file"] not in graph[row["caller_file"]]["calls"]:
                graph[row["caller_file"]]["calls"].append(row["callee_file"])


def run_graph(
    repo_root: Path,
    focus: str | None = None,
    depth: int = 2,
    format: str = "dot",
    output: str | None = None,
    with_calls: bool = False,
) -> None:
    db_path = repo_root / ".ctx" / "index.db"
    if not db_path.exists():
        raise FileNotFoundError("Database not found. Run 'ctx init' first.")

    conn = connect(db_path)
    try:
        graph = _load_graph(conn, focus, depth)

        if with_calls:
            collect_call_edges(conn, graph)

        if focus is None and len(graph) > LARGE_GRAPH_NODES:
            print(
                f"  ⚠ Large graph: {len(graph)} nodes. "
                f"Consider --focus <file> --depth <n> for a readable neighborhood.",
                flush=True,
            )
        if focus is not None and depth >= 3:
            print(
                f"  ⚠ Depth {depth} is usually too large to be readable.",
                flush=True,
            )

        # --output extension overrides --format.
        out_format = format
        if output:
            ext = Path(output).suffix.lower()
            if ext == ".dot":
                out_format = "dot"
            elif ext in (".md", ".mermaid"):
                out_format = "mermaid"

        if out_format == "mermaid":
            rendered = render_mermaid(graph, focus, with_calls)
        else:
            rendered = render_dot(graph, focus, with_calls)

        if output:
            out_path = Path(output)
            if not out_path.is_absolute():
                out_path = repo_root / out_path
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(rendered, encoding="utf-8")
            print(f"  Written: {out_path}")
        else:
            print(rendered)
    finally:
        conn.close()
