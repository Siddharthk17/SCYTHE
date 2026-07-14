import sqlite3
import pytest
from ctx_engine.db import init_schema
from ctx_engine.commands.summarize import get_summarize_selection


@pytest.fixture
def taint_env(tmp_path):
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    init_schema(conn)

    conn.execute(
        "INSERT INTO files (path, semantic_hash, content_hash, purpose, summary) VALUES ('mod.py', 'h1', 'c1', 'old purpose', 'old summary')",
    )
    conn.execute(
        "INSERT INTO files (path, semantic_hash, content_hash, purpose, summary) VALUES ('util.py', 'h2', 'c2', 'util purpose', 'util summary')",
    )

    conn.execute(
        "INSERT INTO functions (id, file, name, signature, line_start, line_end, is_tainted, semantic_hash, summary) VALUES "
        "('mod.py::add', 'mod.py', 'add', 'def add(a,b)', 1, 3, 1, 'hf1', 'sum1'),"
        "('mod.py::sub', 'mod.py', 'sub', 'def sub(a,b)', 5, 7, 0, 'hf2', 'sum2'),"
        "('util.py::helper', 'util.py', 'helper', 'def helper()', 1, 2, 1, 'hf3', 'sum3')",
    )

    conn.execute(
        "INSERT INTO call_graph (caller_id, callee_id, callee_name) VALUES ('mod.py::add', 'util.py::helper', 'helper')",
    )
    conn.commit()
    conn.close()
    return tmp_path, db_path


def test_taint_selection_only_is_tainted(taint_env):
    """get_summarize_selection includes tainted functions."""
    repo_root, db_path = taint_env
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    payloads, count, batches = get_summarize_selection(conn, repo_root)
    conn.close()
    assert len(payloads) >= 1

    all_func_ids = set()
    for p in payloads:
        all_func_ids.update(f["id"] for f in p.get("functions", []))
    assert "mod.py::add" in all_func_ids


def test_taint_selection_includes_all_funcs_for_tainted_file(taint_env):
    """When a file has any tainted function, all its functions appear in selection."""
    repo_root, db_path = taint_env
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    payloads, count, batches = get_summarize_selection(conn, repo_root)
    conn.close()
    all_func_ids = set()
    for p in payloads:
        all_func_ids.update(f["id"] for f in p.get("functions", []))
    assert "mod.py::sub" in all_func_ids


def test_taint_selection_purpose_needs_update(taint_env):
    """Payloads have purpose_needs_update field."""
    repo_root, db_path = taint_env
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    payloads, count, batches = get_summarize_selection(conn, repo_root)
    conn.close()
    assert all("purpose_needs_update" in p for p in payloads)


def test_taint_selection_all_tainted_when_forced(taint_env):
    """With --force flag, all functions are included."""
    repo_root, db_path = taint_env
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    payloads, count, batches = get_summarize_selection(conn, repo_root, force=True)
    conn.close()
    all_func_ids = set()
    for p in payloads:
        all_func_ids.update(f["id"] for f in p.get("functions", []))
    assert "mod.py::add" in all_func_ids
    assert "mod.py::sub" in all_func_ids
    assert "util.py::helper" in all_func_ids
