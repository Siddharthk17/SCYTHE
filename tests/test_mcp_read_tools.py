import json
import sqlite3
import pytest
from pathlib import Path
from ctx_engine.db import init_schema
from ctx_engine.mcp_server.tools.read_tools import (
    handle_get_context,
    handle_get_function,
    handle_search,
    handle_get_dangers,
    handle_get_decisions,
    handle_get_callers,
    handle_get_tainted,
    handle_ctx_status,
)


@pytest.fixture
def read_db(tmp_path):
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    (tmp_path / "a.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")

    conn.execute(
        "INSERT INTO files (path, semantic_hash, purpose, summary, content_hash, confidence, is_stale) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("a.py", "sh1", "Test file", "A test file for unit testing", "abc123", 0.8, 0),
    )
    conn.execute(
        "INSERT INTO functions (id, file, name, signature, summary, line_start, line_end, semantic_hash, confidence, is_stale, is_tainted, mutates) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("a.py:add", "a.py", "add", "def add(a, b)", "Adds two numbers", 1, 1, "sf1", 0.6, 0, 0, "[]"),
    )
    conn.execute(
        "INSERT INTO dangers (scope, description, reason) VALUES (?, ?, ?)",
        ("a.py", "Mutates global state", "No tests"),
    )
    conn.execute(
        "INSERT INTO decisions (scope, decision, alternatives, reason) VALUES (?, ?, ?, ?)",
        ("a.py", "Use plain functions", "Classes", "Simplicity"),
    )
    conn.execute(
        "INSERT INTO session_log (entry, files_touched, timestamp) VALUES (?, ?, ?)",
        ("Fixed bug in add", "a.py", "2026-07-01T00:00:00Z"),
    )
    conn.commit()
    return conn, tmp_path


def test_get_context_returns_assembly(read_db):
    conn, repo = read_db
    result = handle_get_context(conn, repo, {"file": "a.py"})
    assert "FILE: a.py" in result
    assert "Adds two numbers" in result


def test_get_context_requires_file(read_db):
    conn, repo = read_db
    result = handle_get_context(conn, repo, {})
    assert "required" in result


def test_get_context_not_indexed(read_db):
    conn, repo = read_db
    result = handle_get_context(conn, repo, {"file": "nonexistent.py"})
    assert "not indexed" in result


def test_get_context_zone3_before_zone0(read_db):
    conn, repo = read_db
    result = handle_get_context(conn, repo, {"file": "a.py"})
    assert "DIRECTORY TREE" in result
    assert "FILE: a.py" in result
    assert result.index("DIRECTORY TREE") < result.index("FILE: a.py")


def test_get_function_returns_record(read_db):
    conn, repo = read_db
    result = handle_get_function(conn, repo, {"id": "a.py:add"})
    assert "FUNC: a.py:add" in result
    assert "SOURCE:" in result


def test_get_function_not_found(read_db):
    conn, repo = read_db
    result = handle_get_function(conn, repo, {"id": "nonexistent"})
    assert "not found" in result


def test_get_function_stale_warning(read_db):
    conn, repo = read_db
    (repo / "a.py").write_text("def subtract(a, b): return a - b\n", encoding="utf-8")
    result = handle_get_function(conn, repo, {"id": "a.py:add"})
    assert "STALE" in result


def test_search_returns_matches(read_db):
    conn, repo = read_db
    result = handle_search(conn, repo, {"query": "test"})
    assert "SEARCH RESULTS" in result
    assert "a.py" in result


def test_search_no_matches(read_db):
    conn, repo = read_db
    result = handle_search(conn, repo, {"query": "zzz_nonexistent_zzz"})
    assert "no matches" in result


def test_search_requires_query(read_db):
    conn, repo = read_db
    result = handle_search(conn, repo, {})
    assert "required" in result


def test_search_fts5_fallback(read_db):
    conn, repo = read_db
    result = handle_search(conn, repo, {"query": "test"})
    assert "SEARCH RESULTS" in result
    assert "a.py" in result


def test_get_dangers_all(read_db):
    conn, repo = read_db
    result = handle_get_dangers(conn, repo, {})
    assert "DANGER ZONES" in result
    assert "Mutates global state" in result


def test_get_dangers_filtered(read_db):
    conn, repo = read_db
    result = handle_get_dangers(conn, repo, {"scope": "a.py"})
    assert "a.py" in result


def test_get_dangers_no_matches(read_db):
    conn, repo = read_db
    result = handle_get_dangers(conn, repo, {"scope": "nonexistent.py"})
    assert "DANGER ZONES" in result


def test_get_decisions_all(read_db):
    conn, repo = read_db
    result = handle_get_decisions(conn, repo, {})
    assert "ARCHITECTURAL DECISIONS" in result


def test_get_decisions_filtered(read_db):
    conn, repo = read_db
    result = handle_get_decisions(conn, repo, {"scope": "a.py"})
    assert "a.py" in result


def test_get_callers_empty(read_db):
    conn, repo = read_db
    result = handle_get_callers(conn, repo, {"function_id": "a.py:add"})
    assert "no callers" in result


def test_get_tainted_empty(read_db):
    conn, repo = read_db
    result = handle_get_tainted(conn, repo, {})
    assert "TAINTED FUNCTIONS:\n  (none)" in result


def test_get_tainted_with_file_filter(read_db):
    conn, repo = read_db
    result = handle_get_tainted(conn, repo, {"file": "a.py"})
    assert "TAINTED FUNCTIONS:\n  (none)" in result


def test_ctx_status_shows_counts(read_db):
    conn, repo = read_db
    result = handle_ctx_status(conn, repo, {})
    assert "CTX INDEX STATUS" in result
    assert "files indexed: 1" in result
    assert "functions indexed: 1" in result
    assert "hooks:" in result
