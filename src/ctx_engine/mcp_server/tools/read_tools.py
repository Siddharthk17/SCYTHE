import json
import logging
import sqlite3
from pathlib import Path

from ctx_engine.db.connection import connect
from ctx_engine.intelligence.confidence import LOW_CONFIDENCE_THRESHOLD, LIKELY_STALE_THRESHOLD
from ctx_engine.mcp_server.tools.assembly import (
    assemble_context,
    format_file_header,
    format_function_record,
)
from ctx_engine.hashing import file_content_hash

logger = logging.getLogger("ctx")


def _get_conn(db_path: Path) -> sqlite3.Connection:
    return connect(db_path)


def handle_get_context(conn: sqlite3.Connection, repo_root: Path, arguments: dict) -> str:
    target_file = arguments.get("file", "")
    target_line = arguments.get("line")

    if not target_file:
        return "Error: 'file' argument is required."

    file_exists = conn.execute(
        "SELECT 1 FROM files WHERE path = ?", (target_file,)
    ).fetchone()

    if file_exists is None:
        return (
            f"FILE: {target_file}\n"
            "(not indexed)\n\n"
            f"Run 'ctx update {target_file}' to index this file, "
            "or 'ctx init' to index the entire repo."
        )

    return assemble_context(conn, repo_root, target_file, target_line)


def handle_get_function(conn: sqlite3.Connection, repo_root: Path, arguments: dict) -> str:
    func_id = arguments.get("id", "")
    if not func_id:
        return "Error: 'id' argument is required."

    fn_row = conn.execute(
        "SELECT * FROM functions WHERE id = ?", (func_id,)
    ).fetchone()
    if fn_row is None:
        return f"Function not found: {func_id}"

    parts = [format_function_record(fn_row, long=True)]

    file_path = fn_row["file"]
    file_abspath = repo_root / file_path
    try:
        source_lines = file_abspath.read_text(encoding="utf-8").splitlines()
        func_lines = source_lines[fn_row["line_start"] - 1:fn_row["line_end"]]
        source_text = "\n".join(func_lines)
        parts.append(f"LINES: {fn_row['line_start']}-{fn_row['line_end']}")
        parts.append("SOURCE:")
        parts.append(source_text)

        current_hash = file_content_hash(file_abspath.read_bytes())
        db_hash_row = conn.execute(
            "SELECT content_hash FROM files WHERE path = ?", (file_path,)
        ).fetchone()
        if db_hash_row and db_hash_row["content_hash"] != current_hash:
            parts.append("[STALE - file has changed since index; run 'ctx update <file>' to refresh]")
    except Exception as err:
        logger.warning("Cannot read source for %s: %s", file_path, err)

    return "\n\n".join(parts)


def handle_search(conn: sqlite3.Connection, repo_root: Path, arguments: dict) -> str:
    query = arguments.get("query", "")
    limit = min(arguments.get("limit", 10), 50)
    if not query:
        return "Error: 'query' argument is required."

    results = []

    fts_ok = False
    try:
        conn.execute("SELECT count(*) FROM files_fts").fetchone()
        fts_ok = True
    except sqlite3.OperationalError:
        pass

    if fts_ok:
        try:
            file_rows = conn.execute(
                """SELECT path, purpose, summary, rank
                   FROM files_fts WHERE files_fts MATCH ? ORDER BY rank LIMIT ?""",
                (query, limit),
            ).fetchall()
            for row in file_rows:
                text = row["purpose"] or row["summary"] or ""
                results.append(f"FILE: {row['path']}\n  \"{text}\"")
        except sqlite3.OperationalError:
            fts_ok = False

    if not fts_ok:
        like_q = f"%{query}%"
        file_rows = conn.execute(
            "SELECT path, purpose, summary FROM files "
            "WHERE purpose LIKE ? OR summary LIKE ? LIMIT ?",
            (like_q, like_q, limit),
        ).fetchall()
        for row in file_rows:
            text = row["purpose"] or row["summary"] or ""
            results.append(f"FILE: {row['path']}\n  \"{text}\"")

    func_rows = conn.execute(
        "SELECT id, summary, summary_long FROM functions "
        "WHERE summary LIKE ? OR summary_long LIKE ? LIMIT ?",
        (f"%{query}%", f"%{query}%", limit),
    ).fetchall()
    for row in func_rows:
        text = row["summary"] or row["summary_long"] or ""
        results.append(f"FUNC: {row['id']}\n  \"{text}\"")

    func_rows2 = conn.execute(
        "SELECT id, summary, signature FROM functions "
        "WHERE signature LIKE ? LIMIT ?",
        (f"%{query}%", limit),
    ).fetchall()
    for row in func_rows2:
        text = row["signature"]
        results.append(f"FUNC: {row['id']}\n  SIG: \"{text}\"")

    seen = list(dict.fromkeys(results))
    top = seen[:limit]

    if not top:
        return f"SEARCH RESULTS for \"{query}\":\n  (no matches found)"

    header = f"SEARCH RESULTS for \"{query}\":"
    numbered = []
    for i, r in enumerate(top, 1):
        numbered.append(f"  {i}. {r}")
    return header + "\n" + "\n\n".join(numbered)


def handle_get_dangers(conn: sqlite3.Connection, repo_root: Path, arguments: dict) -> str:
    scope = arguments.get("scope")

    if scope:
        rows = conn.execute(
            "SELECT scope, description, reason, added_by FROM dangers WHERE scope = ? ORDER BY added_by DESC",
            (scope,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT scope, description, reason, added_by FROM dangers ORDER BY added_by DESC"
        ).fetchall()

    if not rows:
        return "DANGER ZONES:\n  (none)"

    lines = ["DANGER ZONES:"]
    for r in rows:
        scope_prefix = f"[{r['scope']}]" if r['scope'] else "[*]"
        lines.append(f"  {scope_prefix} {r['description']}")
        if r['reason']:
            lines.append(f"    Reason: {r['reason']}")
        if r['added_by']:
            lines.append(f"    Added by: {r['added_by']}")
    return "\n".join(lines)


def handle_get_decisions(conn: sqlite3.Connection, repo_root: Path, arguments: dict) -> str:
    scope = arguments.get("scope")

    if scope:
        rows = conn.execute(
            "SELECT scope, decision, alternatives, reason FROM decisions WHERE scope = ?",
            (scope,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT scope, decision, alternatives, reason FROM decisions "
            "WHERE scope IS NULL OR scope = '*'"
        ).fetchall()

    if not rows:
        return "ARCHITECTURAL DECISIONS:\n  (none)"

    lines = ["ARCHITECTURAL DECISIONS:"]
    for r in rows:
        scope_prefix = f"[{r['scope'] or '*'}]"
        lines.append(f"  {scope_prefix} {r['decision']}")
        if r['alternatives']:
            lines.append(f"    Alternatives rejected: {r['alternatives']}")
        if r['reason']:
            lines.append(f"    Reason: {r['reason']}")
    return "\n".join(lines)


def handle_get_callers(conn: sqlite3.Connection, repo_root: Path, arguments: dict) -> str:
    function_id = arguments.get("function_id", "")
    if not function_id:
        return "Error: 'function_id' argument is required."

    caller_rows = conn.execute(
        """SELECT DISTINCT cg.caller_id, fn.signature, fn.summary, fn.file
           FROM call_graph cg
           JOIN functions fn ON fn.id = cg.caller_id
           WHERE cg.callee_id = ?
           ORDER BY fn.file""",
        (function_id,),
    ).fetchall()

    if not caller_rows:
        return f"CALLERS OF: {function_id}\n  (no callers found)"

    parts = [f"CALLERS OF: {function_id}"]
    for r in caller_rows:
        lines = [
            f"  FUNC: {r['caller_id']}",
            f"    SIG: {r['signature']}",
        ]
        if r['summary']:
            lines.append(f"    DOES: {r['summary']}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def handle_get_tainted(conn: sqlite3.Connection, repo_root: Path, arguments: dict) -> str:
    file_filter = arguments.get("file")

    if file_filter:
        rows = conn.execute(
            """SELECT id, taint_source, updated_at FROM functions
               WHERE is_tainted = 1 AND file = ?""",
            (file_filter,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, taint_source, updated_at FROM functions
               WHERE is_tainted = 1"""
        ).fetchall()

    if not rows:
        return "TAINTED FUNCTIONS:\n  (none)"

    lines = [f"TAINTED FUNCTIONS ({len(rows)}):"]
    for r in rows:
        taint_source = r['taint_source'] or "unknown"
        updated = r['updated_at'] or "unknown"
        lines.append(f"  {r['id']}")
        lines.append(f"    Tainted by: {taint_source}")
        lines.append(f"    Queued since: {updated}")
    return "\n".join(lines)


def handle_ctx_status(conn: sqlite3.Connection, repo_root: Path, arguments: dict) -> str:
    file_count = conn.execute("SELECT count(*) FROM files").fetchone()[0]
    func_count = conn.execute("SELECT count(*) FROM functions").fetchone()[0]
    stale_funcs = conn.execute("SELECT count(*) FROM functions WHERE is_stale = 1").fetchone()[0]
    stale_files = conn.execute("SELECT count(*) FROM files WHERE is_stale = 1").fetchone()[0]
    tainted = conn.execute("SELECT count(*) FROM functions WHERE is_tainted = 1").fetchone()[0]
    taint_queue = conn.execute("SELECT count(*) FROM taint_queue").fetchone()[0]

    last_commit = conn.execute(
        "SELECT commit_hash, summary, timestamp FROM changes ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()

    mcp_json_path = repo_root / ".mcp.json"
    hooks_pre = repo_root / ".git" / "hooks" / "pre-commit"
    hooks_post = repo_root / ".git" / "hooks" / "post-commit"

    pre_status = "INSTALLED" if hooks_pre.exists() else "NOT INSTALLED"
    post_status = "INSTALLED" if hooks_post.exists() else "NOT INSTALLED"

    lines = [
        "CTX INDEX STATUS:",
        f"  files indexed: {file_count}",
        f"  functions indexed: {func_count}",
        f"  is_stale: {stale_funcs} functions, {stale_files} files",
        f"  is_tainted: {tainted} functions",
        f"  taint_queue: {taint_queue} entries",
    ]

    if last_commit:
        h = (last_commit["commit_hash"] or "?")[:7]
        s = last_commit["summary"] or ""
        t = last_commit["timestamp"] or ""
        lines.append(f"  last commit recorded: {h} \"{s}\" ({t})")
    else:
        lines.append("  last commit recorded: (none)")

    lines.append(f"  hooks: pre-commit {pre_status}, post-commit {post_status}")
    lines.append(f"  mcp config: {'PRESENT' if mcp_json_path.exists() else 'NOT CONFIGURED'}")

    return "\n".join(lines)
