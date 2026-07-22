import json
import sqlite3
import pytest
from pathlib import Path
from ctx_engine.db import init_schema
from ctx_engine.mcp_server.tools.write_tools import (
    handle_add_danger,
    handle_remove_danger,
    handle_add_decision,
    handle_log_session,
    handle_log_change,
    handle_update_summary,
    handle_update_file,
    handle_update_function,
    handle_mark_tainted,
    handle_clear_taint,
    handle_plan,
)


@pytest.fixture
def write_db(tmp_path):
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    (tmp_path / "a.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")

    conn.execute(
        "INSERT INTO files (path, semantic_hash, content_hash, purpose, summary, is_stale, confidence, danger, exports, imports, used_by, used_by_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("a.py", "sh1", "abc123", "Test file", "A test file", 0, 0.8, None, "[]", "[]", "[]", 0),
    )
    conn.execute(
        "INSERT INTO functions (id, file, name, signature, summary, summary_long, line_start, line_end, semantic_hash, confidence, is_stale, is_tainted, mutates, danger) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("a.py:add", "a.py", "add", "def add(a, b)", "Adds two numbers", None, 1, 1, "sf1", 0.6, 0, 0, "[]", None),
    )
    conn.execute(
        "INSERT INTO decisions (id, scope, decision, alternatives, reason, added_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (_gen_id("a.py", "Use plain functions"), "a.py", "Use plain functions", "Classes", "Simplicity", "model", "2026-07-01T00:00:00Z"),
    )
    conn.commit()
    return conn, tmp_path


def _gen_id(*parts):
    import hashlib
    return hashlib.sha256("".join(parts).encode()).hexdigest()[:12]


def test_add_danger_adds_row(write_db):
    conn, repo = write_db
    result = handle_add_danger(conn, repo, {"scope": "a.py", "description": "No error handling", "reason": "Missing try/except"})
    assert "Danger zone added" in result
    row = conn.execute("SELECT * FROM dangers WHERE scope = 'a.py' AND description = 'No error handling'").fetchone()
    assert row is not None
    assert row["reason"] == "Missing try/except"


def test_add_danger_requires_description(write_db):
    conn, repo = write_db
    result = handle_add_danger(conn, repo, {"scope": "a.py"})
    assert "required" in result


def test_add_danger_insert_or_ignore(write_db):
    conn, repo = write_db
    handle_add_danger(conn, repo, {"scope": "a.py", "description": "Same danger", "reason": "Reason 1"})
    count1 = conn.execute("SELECT count(*) FROM dangers").fetchone()[0]
    handle_add_danger(conn, repo, {"scope": "a.py", "description": "Same danger", "reason": "Reason 2"})
    count2 = conn.execute("SELECT count(*) FROM dangers").fetchone()[0]
    assert count1 == count2


def test_remove_danger_removes(write_db):
    conn, repo = write_db
    handle_add_danger(conn, repo, {"scope": "a.py", "description": "Temp danger", "reason": "Temp"})
    row = conn.execute("SELECT * FROM dangers WHERE description = 'Temp danger'").fetchone()
    row_id = row["id"]
    result = handle_remove_danger(conn, repo, {"id": row_id})
    assert "removed" in result
    assert conn.execute("SELECT * FROM dangers WHERE id = ?", (row_id,)).fetchone() is None


def test_remove_danger_not_found(write_db):
    conn, repo = write_db
    result = handle_remove_danger(conn, repo, {"id": "nonexistent"})
    assert "not found" in result


def test_remove_danger_requires_id(write_db):
    conn, repo = write_db
    result = handle_remove_danger(conn, repo, {})
    assert "required" in result


def test_remove_danger_blocks_human_added(write_db):
    conn, repo = write_db
    conn.execute(
        "INSERT INTO dangers (id, scope, description, reason, added_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("human-001", "a.py", "Human danger", "Human added", "human", "2026-07-01T00:00:00Z"),
    )
    conn.commit()
    result = handle_remove_danger(conn, repo, {"id": "human-001"})
    assert "human-added" in result or "Cannot" in result


def test_add_decision_adds_row(write_db):
    conn, repo = write_db
    result = handle_add_decision(conn, repo, {"scope": "a.py", "decision": "Use Python 3.12", "alternatives": "3.11", "reason": "Latest features"})
    assert "Decision recorded" in result
    row = conn.execute("SELECT * FROM decisions WHERE scope = 'a.py' AND decision = 'Use Python 3.12'").fetchone()
    assert row is not None


def test_add_decision_requires_decision(write_db):
    conn, repo = write_db
    result = handle_add_decision(conn, repo, {"scope": "a.py"})
    assert "required" in result


def test_log_session_adds_row(write_db):
    conn, repo = write_db
    result = handle_log_session(conn, repo, {"entry": "Worked on a.py", "files_touched": "a.py"})
    assert "Session log" in result
    row = conn.execute("SELECT * FROM session_log WHERE entry = 'Worked on a.py'").fetchone()
    assert row is not None
    assert row["timestamp"] is not None


def test_log_session_rolling_cap(write_db):
    conn, repo = write_db
    for i in range(12):
        handle_log_session(conn, repo, {"entry": f"Session {i}", "files_touched": "a.py"})
    rows = conn.execute("SELECT * FROM session_log ORDER BY timestamp").fetchall()
    assert len(rows) == 10
    assert rows[0]["entry"] == "Session 2"


def test_log_session_requires_entry(write_db):
    conn, repo = write_db
    result = handle_log_session(conn, repo, {})
    assert "required" in result


def test_log_change_adds_row(write_db):
    conn, repo = write_db
    result = handle_log_change(conn, repo, {"file": "a.py", "summary": "Added error handling"})
    assert "Change logged" in result
    row = conn.execute("SELECT * FROM changes WHERE file = 'a.py' AND summary = 'Added error handling'").fetchone()
    assert row is not None


def test_log_change_rolling_cap(write_db):
    conn, repo = write_db
    for i in range(25):
        handle_log_change(conn, repo, {"file": "a.py", "summary": f"Change {i}"})
    rows = conn.execute("SELECT * FROM changes WHERE file = 'a.py' ORDER BY timestamp").fetchall()
    assert len(rows) == 20, f"Expected 20, got {len(rows)}"
    assert rows[0]["summary"] == "Change 5"


def test_log_change_requires_file(write_db):
    conn, repo = write_db
    result = handle_log_change(conn, repo, {})
    assert "required" in result


def test_log_change_requires_summary(write_db):
    conn, repo = write_db
    result = handle_log_change(conn, repo, {"file": "a.py"})
    assert "required" in result


def test_update_summary_updates_file(write_db):
    conn, repo = write_db
    result = handle_update_summary(conn, repo, {"type": "file", "id": "a.py", "summary": "New summary"})
    assert "Updated summary" in result
    row = conn.execute("SELECT * FROM files WHERE path = 'a.py'").fetchone()
    assert row["summary"] == "New summary"
    assert row["confidence"] == 1.0


def test_update_summary_updates_purpose_via_summary_long(write_db):
    conn, repo = write_db
    result = handle_update_summary(conn, repo, {"type": "file", "id": "a.py", "summary": "New summary", "summary_long": "New purpose"})
    assert "Updated summary" in result
    row = conn.execute("SELECT * FROM files WHERE path = 'a.py'").fetchone()
    assert row["purpose"] == "New purpose"


def test_update_summary_clears_stale(write_db):
    conn, repo = write_db
    conn.execute("UPDATE files SET is_stale = 1 WHERE path = 'a.py'")
    conn.commit()
    handle_update_summary(conn, repo, {"type": "file", "id": "a.py", "summary": "New summary"})
    row = conn.execute("SELECT is_stale FROM files WHERE path = 'a.py'").fetchone()
    assert row["is_stale"] == 0


def test_update_summary_not_indexed(write_db):
    conn, repo = write_db
    result = handle_update_summary(conn, repo, {"type": "file", "id": "nonexistent.py", "summary": "Nope"})
    assert "not found" in result


def test_update_summary_requires_id(write_db):
    conn, repo = write_db
    result = handle_update_summary(conn, repo, {"type": "file"})
    assert "required" in result


def test_update_summary_function_updates(write_db):
    conn, repo = write_db
    result = handle_update_summary(conn, repo, {"type": "function", "id": "a.py:add", "summary": "New summary"})
    assert "Updated summary" in result
    row = conn.execute("SELECT * FROM functions WHERE id = 'a.py:add'").fetchone()
    assert row["summary"] == "New summary"
    assert row["confidence"] == 1.0
    assert row["is_stale"] == 0
    assert row["is_tainted"] == 0


def test_update_file_updates_purpose(write_db):
    conn, repo = write_db
    result = handle_update_file(conn, repo, {"file": "a.py", "purpose": "Updated purpose"})
    assert "Updated file" in result
    row = conn.execute("SELECT * FROM files WHERE path = 'a.py'").fetchone()
    assert row["purpose"] == "Updated purpose"


def test_update_file_updates_summary(write_db):
    conn, repo = write_db
    result = handle_update_file(conn, repo, {"file": "a.py", "summary": "Updated summary"})
    assert "Updated file" in result
    row = conn.execute("SELECT * FROM files WHERE path = 'a.py'").fetchone()
    assert row["summary"] == "Updated summary"


def test_update_file_updates_danger(write_db):
    conn, repo = write_db
    result = handle_update_file(conn, repo, {"file": "a.py", "danger": "Mutates global state"})
    assert "Updated file" in result
    row = conn.execute("SELECT * FROM files WHERE path = 'a.py'").fetchone()
    assert row["danger"] == "Mutates global state"


def test_update_file_coalesce(write_db):
    conn, repo = write_db
    result = handle_update_file(conn, repo, {"file": "a.py", "purpose": "Keep this", "summary": ""})
    assert "Updated file" in result
    row = conn.execute("SELECT * FROM files WHERE path = 'a.py'").fetchone()
    assert row["purpose"] == "Keep this"
    assert row["summary"] == "A test file"


def test_update_file_not_indexed(write_db):
    conn, repo = write_db
    result = handle_update_file(conn, repo, {"file": "nonexistent.py", "purpose": "Nope"})
    assert "not found" in result


def test_update_file_requires_file(write_db):
    conn, repo = write_db
    result = handle_update_file(conn, repo, {})
    assert "required" in result


def test_update_function_updates_summary(write_db):
    conn, repo = write_db
    result = handle_update_function(conn, repo, {"id": "a.py:add", "summary": "New summary"})
    assert "Updated function" in result
    row = conn.execute("SELECT * FROM functions WHERE id = 'a.py:add'").fetchone()
    assert row["summary"] == "New summary"


def test_update_function_updates_danger(write_db):
    conn, repo = write_db
    result = handle_update_function(conn, repo, {"id": "a.py:add", "danger": "No error handling"})
    assert "Updated function" in result
    row = conn.execute("SELECT * FROM functions WHERE id = 'a.py:add'").fetchone()
    assert row["danger"] == "No error handling"


def test_update_function_updates_mutates_as_list(write_db):
    conn, repo = write_db
    result = handle_update_function(conn, repo, {"id": "a.py:add", "mutates": ["stdout"]})
    assert "Updated function" in result
    row = conn.execute("SELECT * FROM functions WHERE id = 'a.py:add'").fetchone()
    assert row["mutates"] == '["stdout"]'


def test_update_function_clears_taint_and_stale(write_db):
    conn, repo = write_db
    conn.execute("UPDATE functions SET is_tainted = 1, is_stale = 1 WHERE id = 'a.py:add'")
    conn.commit()
    handle_update_function(conn, repo, {"id": "a.py:add", "summary": "New summary"})
    row = conn.execute("SELECT * FROM functions WHERE id = 'a.py:add'").fetchone()
    assert row["is_tainted"] == 0
    assert row["is_stale"] == 0


def test_update_function_resets_confidence(write_db):
    conn, repo = write_db
    handle_update_function(conn, repo, {"id": "a.py:add", "summary": "New summary"})
    row = conn.execute("SELECT * FROM functions WHERE id = 'a.py:add'").fetchone()
    assert row["confidence"] == 1.0


def test_update_function_not_found(write_db):
    conn, repo = write_db
    result = handle_update_function(conn, repo, {"id": "nonexistent", "summary": "Nope"})
    assert "not found" in result


def test_update_function_requires_id(write_db):
    conn, repo = write_db
    result = handle_update_function(conn, repo, {"summary": "Nope"})
    assert "required" in result


def test_mark_tainted_updates_db(write_db):
    conn, repo = write_db
    result = handle_mark_tainted(conn, repo, {"function_id": "a.py:add", "taint_source": "test"})
    assert "tainted" in result
    row = conn.execute("SELECT * FROM functions WHERE id = 'a.py:add'").fetchone()
    assert row["is_tainted"] == 1


def test_mark_tainted_not_found(write_db):
    conn, repo = write_db
    result = handle_mark_tainted(conn, repo, {"function_id": "nonexistent", "taint_source": "test"})
    assert "not found" in result


def test_clear_taint_clears(write_db):
    conn, repo = write_db
    handle_mark_tainted(conn, repo, {"function_id": "a.py:add", "taint_source": "test"})
    result = handle_clear_taint(conn, repo, {"function_id": "a.py:add"})
    assert "Cleared taint" in result
    row = conn.execute("SELECT * FROM functions WHERE id = 'a.py:add'").fetchone()
    assert row["is_tainted"] == 0


def test_plan_adds_to_session(write_db):
    conn, repo = write_db
    result = handle_plan(conn, repo, {"plan": "Fix the bug", "files_touched": "a.py"})
    assert "Plan logged" in result
    row = conn.execute("SELECT * FROM session_log WHERE entry LIKE 'PLAN:%'").fetchone()
    assert row is not None
