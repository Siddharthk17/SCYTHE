import pytest
import sqlite3
from ctx_engine.db import init_schema
from ctx_engine.commands.status import run_status


@pytest.fixture
def populated_db(tmp_path):
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    init_schema(conn)

    conn.execute(
        "INSERT INTO files (path, purpose, summary, semantic_hash, content_hash, is_stale, exports, imports) "
        "VALUES ('a.py', 'A module', 'does a', 'h1', 'c1', 0, '[]', '[\"b.py\"]')"
    )
    conn.execute(
        "INSERT INTO files (path, purpose, summary, semantic_hash, content_hash, is_stale) "
        "VALUES ('b.py', 'B module', 'does b', 'h2', 'c2', 1)"
    )
    conn.execute(
        "INSERT INTO files (path, purpose, summary, semantic_hash, content_hash, is_stale) "
        "VALUES ('c.js', 'C module', 'does c', 'h3', 'c3', 0)"
    )

    conn.execute(
        "INSERT INTO functions (id, file, name, signature, line_start, line_end, semantic_hash, is_stale, is_tainted, confidence) "
        "VALUES ('a.py::run', 'a.py', 'run', 'def run()', 1, 5, 'hf1', 0, 1, 1.0)"
    )
    conn.execute(
        "INSERT INTO functions (id, file, name, signature, line_start, line_end, semantic_hash, is_stale, confidence) "
        "VALUES ('a.py::setup', 'a.py', 'setup', 'def setup()', 6, 10, 'hf2', 0, 1.0)"
    )
    conn.execute(
        "INSERT INTO functions (id, file, name, signature, line_start, line_end, semantic_hash, is_stale, confidence) "
        "VALUES ('b.py::compute', 'b.py', 'compute', 'def compute()', 1, 5, 'hf3', 1, 0.3)"
    )
    conn.execute(
        "INSERT INTO functions (id, file, name, signature, line_start, line_end, semantic_hash, is_stale, confidence) "
        "VALUES ('c.js::serve', 'c.js', 'serve', 'function serve()', 1, 5, 'hf4', 0, 1.0)"
    )

    conn.execute(
        "INSERT INTO call_graph (caller_id, callee_id, callee_name, callee_file) "
        "VALUES ('a.py::run', 'b.py::compute', 'compute', 'b.py')"
    )
    conn.execute(
        "INSERT INTO call_graph (caller_id, callee_id, callee_name, callee_file, is_ambiguous) "
        "VALUES ('a.py::setup', NULL, 'unknown_func', NULL, 1)"
    )

    conn.execute(
        "INSERT INTO taint_queue (function_id, taint_source, queued_at, priority) "
        "VALUES ('a.py::run', 'b.py::compute', '2025-01-01T00:00:00', 0)"
    )

    conn.execute("INSERT INTO directories (path, file_count, updated_at) VALUES ('.', 3, '2025-01-01')")

    conn.commit()
    conn.close()
    return tmp_path


def test_status_basic_output(populated_db, capsys):
    """Verify that run_status prints all expected sections with correct counts."""
    run_status(populated_db)
    out = capsys.readouterr().out

    assert "ctx status" in out
    assert "journal mode:" in out.lower()
    assert "fts5 search:" in out

    assert "table records:" in out
    assert "files" in out
    assert "functions" in out
    assert "call_graph" in out
    assert "taint_queue" in out
    assert "directories" in out

    assert "b.py" in out
    assert "call graph:" in out
    assert "confidence distribution:" in out
    assert "is_stale:" in out
    assert "is_tainted:" in out
    assert "taint_queue:" in out


def test_status_language_breakdown(populated_db, capsys):
    """Verify language counts appear in output."""
    run_status(populated_db)
    out = capsys.readouterr().out

    assert "python" in out
    assert "javascript" in out


def test_status_stale_file_listing(populated_db, capsys):
    """Verify stale files appear in the stale files section."""
    run_status(populated_db)
    out = capsys.readouterr().out

    assert "b.py" in out
    assert "stale files:" in out.lower()


def test_status_confidence_distribution(populated_db, capsys):
    """Verify confidence distribution reflects seeded function data."""
    run_status(populated_db)
    out = capsys.readouterr().out

    assert "fresh" in out
    assert "LOW CONFIDENCE" in out
    assert "LIKELY STALE" in out


def test_status_file_not_found(tmp_path, capsys):
    """Verify FileNotFoundError when DB does not exist."""
    with pytest.raises(FileNotFoundError, match="Database not found"):
        run_status(tmp_path)
