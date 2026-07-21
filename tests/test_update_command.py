import pytest
import sqlite3
import json
from unittest.mock import patch, MagicMock
import click
from ctx_engine.db import init_schema
from ctx_engine.commands.update import run_update


@pytest.fixture
def mock_update_env(tmp_path):
    """Set up a temporary SQLite DB indexed with a file and two functions."""
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    conn.execute("""
        INSERT INTO files (path, purpose, summary, semantic_hash, content_hash, is_stale)
        VALUES ('test.py', 'Original purpose', 'Original summary', 'hash1', 'content1', 0)
    """)
    conn.execute("""
        INSERT INTO functions (id, file, name, signature, line_start, line_end, semantic_hash,
                               is_stale, is_tainted, taint_source, summary)
        VALUES ('test.py::func_a', 'test.py', 'func_a', 'def func_a()', 1, 5, 'hf_a',
                0, 0, NULL, 'old func_a summary')
    """)
    conn.execute("""
        INSERT INTO functions (id, file, name, signature, line_start, line_end, semantic_hash,
                               is_stale, is_tainted, taint_source, summary)
        VALUES ('test.py::func_b', 'test.py', 'func_b', 'def func_b()', 6, 10, 'hf_b',
                1, 1, 'some_source.py::s', 'old func_b summary')
    """)

    conn.commit()
    conn.close()
    return tmp_path


def _get_assert_conn(mock_update_env):
    """Open a separate connection for assertions (run_update closes its own)."""
    c = sqlite3.connect(str(mock_update_env / ".ctx" / "index.db"))
    c.row_factory = sqlite3.Row
    return c


@patch("ctx_engine.commands.update.run_reindex_pipeline")
@patch("ctx_engine.commands.update.call_llm_with_retry")
@patch("ctx_engine.commands.update.get_model_name")
@patch("ctx_engine.commands.update.get_anthropic_client")
@patch("ctx_engine.commands.update.connect")
def test_update_needs_summary_true_for_all(
    mock_connect, mock_get_client, mock_get_model, mock_call_llm, mock_reindex, mock_update_env
):
    """ctx update sets needs_summary=True for every function, including stale=0 ones."""
    conn = sqlite3.connect(str(mock_update_env / ".ctx" / "index.db"))
    conn.row_factory = sqlite3.Row
    mock_connect.return_value = conn
    mock_get_client.return_value = MagicMock()
    mock_get_model.return_value = "claude-test"
    mock_reindex.return_value = (0, [], set())

    mock_call_llm.return_value = (json.dumps([
        {
            "path": "test.py",
            "purpose": "Updated purpose",
            "summary": "Updated summary",
            "danger": None,
            "functions": [
                {"id": "test.py::func_a", "summary": "new func_a", "summary_long": "new long a", "danger": None},
                {"id": "test.py::func_b", "summary": "new func_b", "summary_long": "new long b", "danger": None},
            ],
        }
    ]), 100, 50)

    run_update(mock_update_env, "test.py")

    ac = _get_assert_conn(mock_update_env)
    r_a = ac.execute(
        "SELECT summary, is_stale, is_tainted, taint_source FROM functions WHERE id = 'test.py::func_a'"
    ).fetchone()
    assert r_a["summary"] == "new func_a"
    assert r_a["is_stale"] == 0

    r_b = ac.execute(
        "SELECT summary, is_stale, is_tainted, taint_source FROM functions WHERE id = 'test.py::func_b'"
    ).fetchone()
    assert r_b["summary"] == "new func_b"
    assert r_b["is_stale"] == 0
    ac.close()

    user_content = mock_call_llm.call_args[0][3]
    payload = json.loads(user_content)
    assert len(payload) == 1
    assert all(f["needs_summary"] is True for f in payload[0]["functions"])


@patch("ctx_engine.commands.update.run_reindex_pipeline")
@patch("ctx_engine.commands.update.call_llm_with_retry")
@patch("ctx_engine.commands.update.get_model_name")
@patch("ctx_engine.commands.update.get_anthropic_client")
@patch("ctx_engine.commands.update.connect")
def test_update_clears_taint(
    mock_connect, mock_get_client, mock_get_model, mock_call_llm, mock_reindex, mock_update_env
):
    """ctx update clears is_tainted and taint_source for all functions in the file."""
    conn = sqlite3.connect(str(mock_update_env / ".ctx" / "index.db"))
    conn.row_factory = sqlite3.Row
    mock_connect.return_value = conn
    mock_get_client.return_value = MagicMock()
    mock_get_model.return_value = "claude-test"
    mock_reindex.return_value = (0, [], set())

    mock_call_llm.return_value = (json.dumps([
        {
            "path": "test.py",
            "purpose": "Updated purpose",
            "summary": "Updated summary",
            "danger": None,
            "functions": [
                {"id": "test.py::func_a", "summary": "new func_a", "summary_long": "new long a", "danger": None},
                {"id": "test.py::func_b", "summary": "new func_b", "summary_long": "new long b", "danger": None},
            ],
        }
    ]), 100, 50)

    before = conn.execute(
        "SELECT is_tainted, taint_source FROM functions WHERE id = 'test.py::func_b'"
    ).fetchone()
    assert before["is_tainted"] == 1
    assert before["taint_source"] == "some_source.py::s"

    run_update(mock_update_env, "test.py")

    ac = _get_assert_conn(mock_update_env)
    after = ac.execute(
        "SELECT is_tainted, taint_source FROM functions WHERE id = 'test.py::func_b'"
    ).fetchone()
    assert after["is_tainted"] == 0
    assert after["taint_source"] is None
    ac.close()


@patch("ctx_engine.commands.update.run_reindex_pipeline")
@patch("ctx_engine.commands.update.call_llm_with_retry")
@patch("ctx_engine.commands.update.get_model_name")
@patch("ctx_engine.commands.update.get_anthropic_client")
@patch("ctx_engine.commands.update.connect")
def test_update_diff_none_and_unchanged(
    mock_connect, mock_get_client, mock_get_model, mock_call_llm, mock_reindex, mock_update_env, capsys
):
    """ctx update diff shows (none) for NULL summaries and (unchanged) for identical summaries."""
    db_path = mock_update_env / ".ctx" / "index.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    mock_connect.return_value = conn
    mock_get_client.return_value = MagicMock()
    mock_get_model.return_value = "claude-test"
    mock_reindex.return_value = (0, [], set())

    conn.execute("UPDATE files SET purpose = NULL, summary = NULL WHERE path = 'test.py'")
    conn.execute("UPDATE functions SET summary = 'func a unchanged' WHERE id = 'test.py::func_a'")
    conn.execute("UPDATE functions SET summary = NULL WHERE id = 'test.py::func_b'")
    conn.commit()

    mock_call_llm.return_value = (json.dumps([
        {
            "path": "test.py",
            "purpose": "New real purpose",
            "summary": "New real summary",
            "danger": None,
            "functions": [
                {"id": "test.py::func_a", "summary": "func a unchanged", "summary_long": "still same", "danger": None},
                {"id": "test.py::func_b", "summary": "now has summary", "summary_long": "brand new", "danger": None},
            ],
        }
    ]), 100, 50)

    run_update(mock_update_env, "test.py")

    captured = capsys.readouterr().out

    assert "- (none)" in captured
    assert "+ New real purpose" in captured
    assert "func_a" in captured
    assert "(unchanged)" in captured
    assert "func_b" in captured
    assert "- (none)" in captured
    assert "+ now has summary" in captured

    conn.close()


@patch("ctx_engine.commands.update.connect")
def test_update_not_indexed(mock_connect, mock_update_env, capsys):
    """ctx update on a non-indexed file errors with documented message and makes no LLM call."""
    conn = sqlite3.connect(str(mock_update_env / ".ctx" / "index.db"))
    conn.row_factory = sqlite3.Row
    mock_connect.return_value = conn

    with pytest.raises(click.Abort):
        run_update(mock_update_env, "nonexistent.py")

    captured = capsys.readouterr().err
    assert "nonexistent.py is not indexed" in captured
    assert "Run 'ctx init' first" in captured
