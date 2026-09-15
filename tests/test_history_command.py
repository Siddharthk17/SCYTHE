"""Tests for ctx history (Week 9)."""
import json
import sqlite3

import pytest

from ctx_engine.commands.history_cmd import (
    build_history_query,
    run_history,
)
from ctx_engine.db import init_schema


@pytest.fixture
def history_db(tmp_path):
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    conn.execute(
        "INSERT INTO files (path, system, semantic_hash, content_hash, "
        "is_stale, updated_at) "
        "VALUES ('src/mcts.py', 'search', 'sh', 'ch', 1, '2026-06-10T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO files (path, system, semantic_hash, content_hash, "
        "is_stale, updated_at) "
        "VALUES ('src/targets.py', 'training', 'sh', 'ch', 0, '2026-06-15T00:00:00Z')"
    )
    entries = [
        ("src/mcts.py", "7b4e1f2", "moved contempt", "model", "2026-06-14T18:00:00Z"),
        ("src/mcts.py", "a3f9c2d", "rename batch fn", "human", "2026-06-14T14:00:00Z"),
        ("src/targets.py", "b2d8c1f", "fix draw value", "human", "2026-06-13T10:00:00Z"),
        ("src/mcts.py", "c9e4f1a", "bump batch size", "model", "2026-06-12T16:00:00Z"),
    ]
    for file, commit, summary, author, ts in entries:
        conn.execute(
            "INSERT INTO changes (file, commit_hash, summary, author, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            (file, commit, summary, author, ts),
        )
    conn.commit()
    yield conn, tmp_path
    conn.close()


def test_default_order_desc(history_db, capsys):
    _, root = history_db
    run_history(root, limit=10)
    out = capsys.readouterr().out
    assert out.index("moved contempt") < out.index("rename batch fn")
    assert out.index("rename batch fn") < out.index("fix draw value")


def test_file_filter(history_db, capsys):
    _, root = history_db
    run_history(root, file="src/targets.py")
    out = capsys.readouterr().out
    assert "fix draw value" in out
    assert "moved contempt" not in out


def test_author_model_filter(history_db, capsys):
    _, root = history_db
    run_history(root, author="model")
    out = capsys.readouterr().out
    assert "moved contempt" in out
    assert "bump batch size" in out
    assert "rename batch fn" not in out


def test_system_filter(history_db, capsys):
    _, root = history_db
    run_history(root, system="training")
    out = capsys.readouterr().out
    assert "fix draw value" in out
    assert "moved contempt" not in out


def test_since_until_filter(history_db):
    conn, _ = history_db
    sql, params = build_history_query(
        None, None, "2026-06-13T00:00:00Z", "2026-06-14T00:00:00Z", None, 50
    )
    rows = conn.execute(sql, params).fetchall()
    assert [r["summary"] for r in rows] == ["fix draw value"]


def test_limit(history_db, capsys):
    _, root = history_db
    run_history(root, limit=2)
    out = capsys.readouterr().out
    assert "Showing 2 of 4" in out


def test_json_format(history_db, capsys):
    _, root = history_db
    run_history(root, system="search", format="json")
    payload = json.loads(capsys.readouterr().out)
    assert payload["total_matching"] == 3
    assert payload["shown"] == 3
    assert payload["query"]["system"] == "search"
    entry = payload["entries"][0]
    assert {"file", "commit_hash", "summary", "author", "timestamp",
            "metadata_updated"} <= set(entry)


def test_empty_history_message(tmp_path, capsys):
    root = tmp_path / "repo"
    (root / ".ctx").mkdir(parents=True)
    conn = sqlite3.connect(root / ".ctx" / "index.db")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    conn.commit()
    conn.close()
    run_history(root)
    assert "No history found" in capsys.readouterr().out


def test_stale_model_entry_annotation(history_db, capsys):
    """Model-authored change on a stale file gets the audit-model note."""
    _, root = history_db
    run_history(root, author="model")
    out = capsys.readouterr().out
    assert "← ctx audit-model shows:" in out
