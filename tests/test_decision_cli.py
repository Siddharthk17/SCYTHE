import sqlite3
import pytest
from ctx_engine.db import init_schema
from ctx_engine.commands.decision_cmd import decision_add, decision_remove, decision_list


@pytest.fixture
def decision_db(tmp_path):
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    conn.commit()
    return conn, tmp_path


def test_decision_add(decision_db):
    conn, repo = decision_db
    did = decision_add(conn, "a.py", "Use plain functions", "Classes", "Simplicity")
    assert did is not None
    row = conn.execute("SELECT * FROM decisions WHERE id = ?", (did,)).fetchone()
    assert row is not None
    assert row["scope"] == "a.py"
    assert row["decision"] == "Use plain functions"
    assert row["alternatives"] == "Classes"
    assert row["reason"] == "Simplicity"
    assert row["added_by"] == "human"


def test_decision_add_global(decision_db):
    conn, repo = decision_db
    did = decision_add(conn, None, "Use Python 3.14", "3.12", "Latest")
    assert did is not None
    row = conn.execute("SELECT * FROM decisions WHERE id = ?", (did,)).fetchone()
    assert row["scope"] is None


def test_decision_add_duplicate(decision_db):
    conn, repo = decision_db
    d1 = decision_add(conn, "a.py", "Same decision", "A", "Reason 1")
    d2 = decision_add(conn, "a.py", "Same decision", "A", "Reason 2")
    count = conn.execute(
        "SELECT COUNT(*) FROM decisions WHERE decision = 'Same decision'"
    ).fetchone()[0]
    assert count == 1


def test_decision_remove(decision_db):
    conn, repo = decision_db
    did = decision_add(conn, "a.py", "Remove me", "B", "Temp")
    msg = decision_remove(conn, did, confirmed=True)
    assert "Removed" in msg
    assert conn.execute("SELECT * FROM decisions WHERE id = ?", (did,)).fetchone() is None


def test_decision_remove_not_found(decision_db):
    conn, repo = decision_db
    msg = decision_remove(conn, "nonexistent")
    assert "not found" in msg or "No decision" in msg


def test_decision_remove_auto_without_confirm(decision_db):
    conn, repo = decision_db
    conn.execute(
        "INSERT INTO decisions (id, scope, decision, alternatives, reason, added_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("auto-001", "a.py", "Auto decision", "None", "Auto", "auto", "2026-07-01T00:00:00Z"),
    )
    conn.commit()
    msg = decision_remove(conn, "auto-001", confirmed=True)
    assert "Removed" in msg


def test_decision_remove_human_without_confirm(decision_db):
    conn, repo = decision_db
    conn.execute(
        "INSERT INTO decisions (id, scope, decision, alternatives, reason, added_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("human-001", "a.py", "Human decision", "None", "Human", "human", "2026-07-01T00:00:00Z"),
    )
    conn.commit()
    msg = decision_remove(conn, "human-001", confirmed=True)
    assert "Removed" in msg


def test_decision_remove_human_with_confirm(decision_db):
    conn, repo = decision_db
    conn.execute(
        "INSERT INTO decisions (id, scope, decision, alternatives, reason, added_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("human-001", "a.py", "Human decision", "None", "Human", "human", "2026-07-01T00:00:00Z"),
    )
    conn.commit()
    msg = decision_remove(conn, "human-001", confirmed=True)
    assert "Removed" in msg


def test_decision_list_empty(decision_db):
    conn, repo = decision_db
    rows = decision_list(conn)
    assert rows == []


def test_decision_list_scope_filter(decision_db):
    conn, repo = decision_db
    decision_add(conn, "a.py", "Decision A", "Alt A", "Reason A")
    decision_add(conn, "b.py", "Decision B", "Alt B", "Reason B")
    rows = decision_list(conn, scope="a.py")
    assert all(r["scope"] == "a.py" for r in rows)


def test_decision_list_wildcard_scope(decision_db):
    conn, repo = decision_db
    decision_add(conn, None, "Global decision", "Alt A", "Reason A")
    decision_add(conn, "a.py", "File decision", "Alt B", "Reason B")
    rows = decision_list(conn, scope="*")
    assert len(rows) == 1
    assert rows[0]["scope"] is None


def test_decision_list_all(decision_db):
    conn, repo = decision_db
    decision_add(conn, "a.py", "Decision A", "Alt A", "Reason A")
    decision_add(conn, "b.py", "Decision B", "Alt B", "Reason B")
    rows = decision_list(conn)
    assert len(rows) == 2
