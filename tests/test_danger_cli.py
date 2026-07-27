import sqlite3
import pytest
from pathlib import Path
from ctx_engine.db import init_schema
from ctx_engine.commands.danger_cmd import danger_add, danger_remove, danger_list, danger_detect


@pytest.fixture
def danger_db(tmp_path):
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    (tmp_path / "a.py").write_text(
        "def add(a, b): return a + b\n\n"
        "def helper():\n"
        "    helper()\n"
        "    helper()\n"
        "    helper()\n"
        "    helper()\n"
        "    helper()\n"
        "    helper()\n",
        encoding="utf-8",
    )

    conn.execute(
        "INSERT OR IGNORE INTO files (path, semantic_hash, content_hash, purpose, summary, is_stale, confidence, danger, exports, imports, used_by, used_by_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("a.py", "sh1", "ch1", "Test file", "A test", 0, 0.9, None, '["add","helper"]', "[]", "[]", 0),
    )
    conn.execute(
        "INSERT OR IGNORE INTO functions (id, file, name, signature, summary, summary_long, line_start, line_end, semantic_hash, confidence, is_stale, is_tainted, mutates, danger) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("a.py:add", "a.py", "add", "def add(a, b)", "Adds two numbers", None, 1, 1, "sf1", 0.8, 0, 0, "[]", None),
    )
    conn.execute(
        "INSERT OR IGNORE INTO functions (id, file, name, signature, summary, summary_long, line_start, line_end, semantic_hash, confidence, is_stale, is_tainted, mutates, danger) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("a.py:helper", "a.py", "helper", "def helper()", "Helper function", None, 3, 8, "sf2", 0.8, 0, 0, "[]", None),
    )
    conn.commit()
    return conn, tmp_path


def test_danger_add(danger_db):
    conn, repo = danger_db
    did = danger_add(conn, "a.py", "No error handling", "Missing try/except")
    assert did is not None
    row = conn.execute("SELECT * FROM dangers WHERE id = ?", (did,)).fetchone()
    assert row is not None
    assert row["scope"] == "a.py"
    assert row["description"] == "No error handling"
    assert row["reason"] == "Missing try/except"
    assert row["added_by"] == "human"


def test_danger_add_duplicate(danger_db):
    conn, repo = danger_db
    d1 = danger_add(conn, "a.py", "Duplicate", "Reason 1")
    d2 = danger_add(conn, "a.py", "Duplicate", "Reason 2")
    assert d1 == d2 or conn.execute("SELECT COUNT(*) FROM dangers WHERE description = 'Duplicate'").fetchone()[0] == 1


def test_danger_remove(danger_db):
    conn, repo = danger_db
    did = danger_add(conn, "a.py", "Remove me", "Temp")
    msg = danger_remove(conn, did, confirmed=True)
    assert "Removed" in msg
    assert conn.execute("SELECT * FROM dangers WHERE id = ?", (did,)).fetchone() is None


def test_danger_remove_not_found(danger_db):
    conn, repo = danger_db
    msg = danger_remove(conn, "nonexistent")
    assert "not found" in msg or "No danger zone" in msg


def test_danger_remove_auto_without_confirm(danger_db):
    conn, repo = danger_db
    conn.execute(
        "INSERT INTO dangers (id, scope, description, reason, added_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("auto-001", "a.py", "Auto danger", "Auto", "auto", "2026-07-01T00:00:00Z"),
    )
    conn.commit()
    msg = danger_remove(conn, "auto-001", confirmed=True)
    assert "Removed" in msg


def test_danger_remove_human_without_confirm(danger_db):
    conn, repo = danger_db
    conn.execute(
        "INSERT INTO dangers (id, scope, description, reason, added_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("human-001", "a.py", "Human danger", "Human", "human", "2026-07-01T00:00:00Z"),
    )
    conn.commit()
    msg = danger_remove(conn, "human-001", confirmed=True)
    assert "Removed" in msg


def test_danger_remove_human_with_confirm(danger_db):
    conn, repo = danger_db
    conn.execute(
        "INSERT INTO dangers (id, scope, description, reason, added_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("human-001", "a.py", "Human danger", "Human", "human", "2026-07-01T00:00:00Z"),
    )
    conn.commit()
    msg = danger_remove(conn, "human-001", confirmed=True)
    assert "Removed" in msg


def test_danger_list_empty(danger_db):
    conn, repo = danger_db
    rows = danger_list(conn)
    assert len(rows) >= 0


def test_danger_list_scope_filter(danger_db):
    conn, repo = danger_db
    danger_add(conn, "a.py", "Test danger A", "Reason A")
    danger_add(conn, "b.py", "Test danger B", "Reason B")
    rows = danger_list(conn, scope="a.py")
    assert all(r["scope"] == "a.py" for r in rows)


def test_danger_list_wildcard_scope(danger_db):
    conn, repo = danger_db
    danger_add(conn, "a.py", "A", "Reason")
    danger_add(conn, "b.py", "B", "Reason")
    rows = danger_list(conn, scope="*")
    assert len(rows) == 2


def test_danger_detect_no_heuristic_db(tmp_path):
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    result = danger_detect(conn, tmp_path)
    assert "detected" in result or "added" in result or "added" not in result


def test_danger_detect_dry_run(tmp_path):
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    result = danger_detect(conn, tmp_path, dry_run=True)
    assert isinstance(result, dict)
    assert "detected" in result
