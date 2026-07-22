import hashlib
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("ctx")


def _gen_id(*parts: str) -> str:
    combined = "".join(parts)
    return hashlib.sha256(combined.encode()).hexdigest()[:12]


def handle_add_danger(conn: sqlite3.Connection, repo_root: Path, arguments: dict) -> str:
    scope = arguments.get("scope", "*")
    description = arguments.get("description", "")
    reason = arguments.get("reason", "")

    if not description:
        return "Error: 'description' argument is required."

    danger_id = _gen_id(scope, description)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO dangers (id, scope, description, reason, added_by, created_at) "
        "VALUES (?, ?, ?, ?, 'model', ?)",
        (danger_id, scope, description, reason, now),
    )
    conn.commit()
    return f"Danger zone added: {danger_id}"


def handle_remove_danger(conn: sqlite3.Connection, repo_root: Path, arguments: dict) -> str:
    danger_id = arguments.get("id", "")
    if not danger_id:
        return "Error: 'id' argument is required."

    row = conn.execute(
        "SELECT added_by FROM dangers WHERE id = ?", (danger_id,)
    ).fetchone()
    if row is None:
        return f"Danger zone not found: {danger_id}"
    if row["added_by"] == "human":
        return (
            f"Cannot remove human-added danger {danger_id} via MCP. "
            "Use 'ctx danger remove {danger_id}' from the CLI."
        )
    conn.execute("DELETE FROM dangers WHERE id = ?", (danger_id,))
    conn.commit()
    return f"Danger zone removed: {danger_id}"


def handle_add_decision(conn: sqlite3.Connection, repo_root: Path, arguments: dict) -> str:
    scope = arguments.get("scope", "*")
    decision = arguments.get("decision", "")
    alternatives = arguments.get("alternatives", "")
    reason = arguments.get("reason", "")

    if not decision:
        return "Error: 'decision' argument is required."

    decision_id = _gen_id(scope, decision)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO decisions (id, scope, decision, alternatives, reason, added_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'model', ?)",
        (decision_id, scope, decision, alternatives, reason, now),
    )
    conn.commit()
    return f"Decision recorded: {decision_id}"


def handle_log_session(conn: sqlite3.Connection, repo_root: Path, arguments: dict) -> str:
    entry = arguments.get("entry", "")
    files_touched_raw = arguments.get("files_touched", "")
    files_touched = ", ".join(files_touched_raw) if isinstance(files_touched_raw, list) else str(files_touched_raw)

    if not entry:
        return "Error: 'entry' argument is required."

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO session_log (entry, files_touched, timestamp) VALUES (?, ?, ?)",
        (entry, files_touched, now),
    )
    conn.execute(
        "DELETE FROM session_log WHERE id NOT IN ("
        "SELECT id FROM session_log ORDER BY timestamp DESC LIMIT 10"
        ")"
    )
    conn.commit()
    count = conn.execute("SELECT count(*) FROM session_log").fetchone()[0]
    return f"Session log updated ({count} entries total)"


def handle_log_change(conn: sqlite3.Connection, repo_root: Path, arguments: dict) -> str:
    file_path = arguments.get("file", "")
    summary = arguments.get("summary", "")

    if not file_path:
        return "Error: 'file' argument is required."
    if not summary:
        return "Error: 'summary' argument is required."

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO changes (file, commit_hash, summary, author, timestamp) "
        "VALUES (?, ?, ?, 'model', ?)",
        (file_path, uuid.uuid4().hex[:12], summary, now),
    )
    conn.execute(
        "DELETE FROM changes WHERE id NOT IN ("
        "SELECT id FROM changes WHERE file = ? ORDER BY timestamp DESC LIMIT 20"
        ")",
        (file_path,),
    )
    conn.commit()
    return f"Change logged for {file_path}"


def handle_mark_tainted(conn: sqlite3.Connection, repo_root: Path, arguments: dict) -> str:
    function_id = arguments.get("function_id", "")
    taint_source = arguments.get("taint_source", "")

    if not function_id:
        return "Error: 'function_id' argument is required."
    if not taint_source:
        return "Error: 'taint_source' argument is required."

    fn_row = conn.execute(
        "SELECT id, is_tainted FROM functions WHERE id = ?", (function_id,)
    ).fetchone()
    if fn_row is None:
        return f"Function not found: {function_id}"

    if fn_row["is_tainted"]:
        return f"Function {function_id} is already tainted."

    conn.execute(
        "UPDATE functions SET is_tainted = 1, taint_source = ? WHERE id = ?",
        (taint_source, function_id),
    )
    conn.execute(
        "INSERT OR IGNORE INTO taint_queue (function_id, taint_source) VALUES (?, ?)",
        (function_id, taint_source),
    )
    conn.commit()
    return f"Marked {function_id} as tainted (source: {taint_source}). Use ctx.clear_taint after review."


def handle_clear_taint(conn: sqlite3.Connection, repo_root: Path, arguments: dict) -> str:
    function_id = arguments.get("function_id", "")

    if not function_id:
        return "Error: 'function_id' argument is required."

    fn_row = conn.execute(
        "SELECT id, is_tainted FROM functions WHERE id = ?", (function_id,)
    ).fetchone()
    if fn_row is None:
        return f"Function not found: {function_id}"
    if not fn_row["is_tainted"]:
        return f"Function {function_id} is not currently tainted."

    conn.execute(
        "UPDATE functions SET is_tainted = 0, taint_source = NULL WHERE id = ?",
        (function_id,),
    )
    conn.execute(
        "DELETE FROM taint_queue WHERE function_id = ?",
        (function_id,),
    )
    conn.commit()
    return f"Cleared taint on {function_id}."


def handle_update_summary(conn: sqlite3.Connection, repo_root: Path, arguments: dict) -> str:
    target_type = arguments.get("type", "file")
    target_id = arguments.get("id", "")
    summary = arguments.get("summary", "")
    summary_long = arguments.get("summary_long", "")

    if not target_id:
        return "Error: 'id' argument is required."
    if not summary:
        return "Error: 'summary' argument is required."

    now = datetime.now(timezone.utc).isoformat()

    if target_type == "function":
        fn_row = conn.execute(
            "SELECT id FROM functions WHERE id = ?", (target_id,)
        ).fetchone()
        if fn_row is None:
            return f"Function not found: {target_id}"
        conn.execute(
            "UPDATE functions SET summary = ?, summary_long = COALESCE(NULLIF(?, ''), summary), "
            "confidence = 1.0, is_stale = 0, is_tainted = 0, taint_source = NULL, "
            "updated_at = ? WHERE id = ?",
            (summary, summary_long, now, target_id),
        )
        conn.execute(
            "DELETE FROM taint_queue WHERE function_id = ?",
            (target_id,),
        )
        conn.commit()
        return f"Updated summary for function {target_id}."
    else:
        file_row = conn.execute(
            "SELECT path FROM files WHERE path = ?", (target_id,)
        ).fetchone()
        if file_row is None:
            return f"File not found: {target_id}"
        conn.execute(
            "UPDATE files SET summary = ?, purpose = COALESCE(NULLIF(?, ''), purpose), "
            "confidence = 1.0, is_stale = 0, updated_at = ? WHERE path = ?",
            (summary, summary_long, now, target_id),
        )
        conn.commit()
        return f"Updated summary for file {target_id}."


def handle_update_file(conn: sqlite3.Connection, repo_root: Path, arguments: dict) -> str:
    file_path = arguments.get("file", "")
    if not file_path:
        return "Error: 'file' argument is required."

    file_row = conn.execute(
        "SELECT path FROM files WHERE path = ?", (file_path,)
    ).fetchone()
    if file_row is None:
        return f"File not found: {file_path}"

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE files SET "
        "purpose = COALESCE(NULLIF(?, ''), purpose), "
        "summary = COALESCE(NULLIF(?, ''), summary), "
        "danger = COALESCE(NULLIF(?, ''), danger), "
        "last_change = COALESCE(NULLIF(?, ''), last_change), "
        "system = COALESCE(NULLIF(?, ''), system), "
        "confidence = 1.0, is_stale = 0, updated_at = ? "
        "WHERE path = ?",
        (
            arguments.get("purpose", ""),
            arguments.get("summary", ""),
            arguments.get("danger", ""),
            arguments.get("last_change", ""),
            arguments.get("system", ""),
            now,
            file_path,
        ),
    )
    conn.commit()
    return f"Updated file record: {file_path}"


def handle_update_function(conn: sqlite3.Connection, repo_root: Path, arguments: dict) -> str:
    func_id = arguments.get("id", "")
    if not func_id:
        return "Error: 'id' argument is required."

    fn_row = conn.execute(
        "SELECT id FROM functions WHERE id = ?", (func_id,)
    ).fetchone()
    if fn_row is None:
        return f"Function not found: {func_id}"

    now = datetime.now(timezone.utc).isoformat()
    mutates = arguments.get("mutates")
    mutates_json = json.dumps(mutates) if mutates is not None else None

    conn.execute(
        "UPDATE functions SET "
        "summary = COALESCE(NULLIF(?, ''), summary), "
        "summary_long = COALESCE(NULLIF(?, ''), summary_long), "
        "danger = COALESCE(NULLIF(?, ''), danger), "
        "mutates = COALESCE(NULLIF(?, ''), mutates), "
        "confidence = 1.0, is_stale = 0, is_tainted = 0, taint_source = NULL, "
        "updated_at = ? WHERE id = ?",
        (
            arguments.get("summary", ""),
            arguments.get("summary_long", ""),
            arguments.get("danger", ""),
            mutates_json,
            now,
            func_id,
        ),
    )
    conn.execute(
        "DELETE FROM taint_queue WHERE function_id = ?",
        (func_id,),
    )
    conn.commit()
    return f"Updated function record: {func_id} (taint cleared)"


def handle_plan(conn: sqlite3.Connection, repo_root: Path, arguments: dict) -> str:
    plan_text = arguments.get("plan", "")
    files_touched_raw = arguments.get("files_touched", "")
    files_touched = ", ".join(files_touched_raw) if isinstance(files_touched_raw, list) else str(files_touched_raw)

    if not plan_text:
        return "Error: 'plan' argument is required."

    conn.execute(
        "INSERT INTO session_log (entry, files_touched) VALUES (?, ?)",
        (f"PLAN: {plan_text}", files_touched),
    )
    conn.commit()
    return "Plan logged to session history."
