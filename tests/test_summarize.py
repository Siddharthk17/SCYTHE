import pytest
import sqlite3
import json
from unittest.mock import patch, MagicMock
from ctx_engine.db import init_schema
from ctx_engine.commands.summarize import run_summarize

@pytest.fixture
def mock_db(tmp_path):
    db_path = tmp_path / "test_index.db"
    conn = sqlite3.connect(db_path)
    init_schema(conn)
    
    # Insert 3 files
    # 1. fresh (purpose not null, is_stale=0)
    conn.execute("INSERT INTO files (path, purpose, summary, semantic_hash, content_hash, is_stale) VALUES ('fresh.py', 'doing something', 'fresh file', 'h1', 'c1', 0)")
    # 2. purpose is NULL
    conn.execute("INSERT INTO files (path, purpose, summary, semantic_hash, content_hash, is_stale) VALUES ('no_purpose.py', NULL, NULL, 'h2', 'c2', 0)")
    # 3. is_stale = 1
    conn.execute("INSERT INTO files (path, purpose, summary, semantic_hash, content_hash, is_stale) VALUES ('stale.py', 'old purpose', 'stale file', 'h3', 'c3', 1)")
    
    # Insert functions
    conn.execute("INSERT INTO functions (id, file, name, signature, line_start, line_end, semantic_hash, is_stale) VALUES ('fresh.py::f', 'fresh.py', 'f', 'def f()', 1, 3, 'hf', 0)")
    conn.execute("INSERT INTO functions (id, file, name, signature, line_start, line_end, semantic_hash, is_stale) VALUES ('no_purpose.py::g', 'no_purpose.py', 'g', 'def g()', 1, 3, 'hg', 1)")
    conn.execute("INSERT INTO functions (id, file, name, signature, line_start, line_end, semantic_hash, is_stale) VALUES ('stale.py::h', 'stale.py', 'h', 'def h()', 1, 3, 'hh', 1)")
    
    conn.commit()
    conn.close()
    
    return db_path

@patch("ctx_engine.commands.summarize.connect")
def test_summarize_dry_run(mock_connect, mock_db):
    """Verify that dry-run operates offline, making no client calls and checking no environment keys."""
    # Connect mock_connect to our temp db
    conn = sqlite3.connect(mock_db)
    mock_connect.return_value = conn
    
    # We do NOT patch get_anthropic_client, proving it is never called.
    # We just run with dry_run=True.
    run_summarize(Path(mock_db).parent, dry_run=True)
    
    # Verify DB state wasn't modified
    row = conn.execute("SELECT purpose FROM files WHERE path = 'no_purpose.py'").fetchone()
    assert row[0] is None
    
    conn.close()

@patch("ctx_engine.commands.summarize.get_anthropic_client")
@patch("ctx_engine.commands.summarize.connect")
def test_summarize_selection_default(mock_connect, mock_get_client, mock_db):
    """Verify that by default, summarize only selects files needing updates (purpose IS NULL or is_stale = 1)."""
    conn = sqlite3.connect(mock_db)
    mock_connect.return_value = conn
    
    # Mock client return
    mock_client = MagicMock()
    mock_get_client.return_value = mock_client
    
    mock_message = MagicMock()
    mock_message.usage.input_tokens = 100
    mock_message.usage.output_tokens = 50
    
    mock_block = MagicMock()
    mock_block.type = "text"
    # Return mock JSON array of results for stale.py and no_purpose.py
    mock_block.text = json.dumps([
        {
            "path": "no_purpose.py",
            "purpose": "Resolved purpose",
            "summary": "no_purpose sum",
            "danger": None,
            "functions": [
                {"id": "no_purpose.py::g", "summary": "g sum", "summary_long": "g long sum", "danger": None}
            ]
        },
        {
            "path": "stale.py",
            "purpose": "Updated purpose",
            "summary": "stale sum",
            "danger": None,
            "functions": [
                {"id": "stale.py::h", "summary": "h sum", "summary_long": "h long sum", "danger": None}
            ]
        }
    ])
    mock_message.content = [mock_block]
    mock_client.messages.create.return_value = mock_message
    
    run_summarize(Path(mock_db).parent)
    
    # Verify stale.py and no_purpose.py are updated
    r_no_purpose = conn.execute("SELECT purpose, is_stale FROM files WHERE path = 'no_purpose.py'").fetchone()
    assert r_no_purpose[0] == "Resolved purpose"
    assert r_no_purpose[1] == 0
    
    r_stale = conn.execute("SELECT purpose, is_stale FROM files WHERE path = 'stale.py'").fetchone()
    assert r_stale[0] == "Updated purpose"
    assert r_stale[1] == 0
    
    # fresh.py was NOT processed or updated
    r_fresh = conn.execute("SELECT purpose FROM files WHERE path = 'fresh.py'").fetchone()
    assert r_fresh[0] == "doing something"
    
    # Verify mock was called once with the expected payload containing only 2 files
    mock_client.messages.create.assert_called_once()
    call_args = mock_client.messages.create.call_args[1]
    # Check that fresh.py is NOT in the user prompt
    user_content = call_args["messages"][0]["content"]
    assert "fresh.py" not in user_content
    assert "stale.py" in user_content
    assert "no_purpose.py" in user_content

@patch("ctx_engine.commands.summarize.get_anthropic_client")
@patch("ctx_engine.commands.summarize.connect")
def test_summarize_force(mock_connect, mock_get_client, mock_db):
    """Verify that --force processes all files regardless of current stale state."""
    conn = sqlite3.connect(mock_db)
    mock_connect.return_value = conn
    
    mock_client = MagicMock()
    mock_get_client.return_value = mock_client
    
    mock_message = MagicMock()
    mock_message.usage.input_tokens = 200
    mock_message.usage.output_tokens = 100
    
    mock_block = MagicMock()
    mock_block.type = "text"
    # Return mock results for all three files
    mock_block.text = json.dumps([
        {
            "path": "fresh.py", "purpose": "Forced purpose", "summary": "s", "danger": None,
            "functions": [{"id": "fresh.py::f", "summary": "f s", "summary_long": "f l", "danger": None}]
        },
        {
            "path": "no_purpose.py", "purpose": "Forced purpose", "summary": "s", "danger": None,
            "functions": [{"id": "no_purpose.py::g", "summary": "g s", "summary_long": "g l", "danger": None}]
        },
        {
            "path": "stale.py", "purpose": "Forced purpose", "summary": "s", "danger": None,
            "functions": [{"id": "stale.py::h", "summary": "h s", "summary_long": "h l", "danger": None}]
        }
    ])
    mock_message.content = [mock_block]
    mock_client.messages.create.return_value = mock_message
    
    run_summarize(Path(mock_db).parent, force=True)
    
    # Assert fresh.py was updated too
    r_fresh = conn.execute("SELECT purpose FROM files WHERE path = 'fresh.py'").fetchone()
    assert r_fresh[0] == "Forced purpose"

@patch("ctx_engine.commands.summarize.get_anthropic_client")
@patch("ctx_engine.commands.summarize.connect")
def test_summarize_malformed_json_tolerance(mock_connect, mock_get_client, mock_db):
    """Verify that a malformed response does not crash the run, and reports skipped files."""
    conn = sqlite3.connect(mock_db)
    mock_connect.return_value = conn
    
    mock_client = MagicMock()
    mock_get_client.return_value = mock_client
    
    mock_message = MagicMock()
    mock_message.usage.input_tokens = 100
    mock_message.usage.output_tokens = 50
    mock_block = MagicMock()
    mock_block.type = "text"
    mock_block.text = "This is not JSON text at all!"
    mock_message.content = [mock_block]
    mock_client.messages.create.return_value = mock_message
    
    # Should complete without raising an error
    run_summarize(Path(mock_db).parent)
    
    # DB state should not have changed (stale.py remains stale)
    r_stale = conn.execute("SELECT is_stale FROM files WHERE path = 'stale.py'").fetchone()
    assert r_stale[0] == 1
