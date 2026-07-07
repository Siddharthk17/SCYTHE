import pytest
import sqlite3
import json

from ctx_engine.db import init_schema
from ctx_engine.intelligence.llm_client import (
    batch_files,
    parse_response,
    apply_summary_batch,
)
from ctx_engine.commands.summarize import get_taint_warning

def test_batch_files_size_boundary():
    """Verify that 25 files are split into two batches at the 20-file boundary."""
    files = [{"path": f"file_{i}.py", "functions": []} for i in range(25)]
    batches = batch_files(files, max_files_per_batch=20)
    assert len(batches) == 2
    assert len(batches[0]) == 20
    assert len(batches[1]) == 5

def test_batch_files_token_boundary():
    """Verify that a large file exceeding token limit forms its own batch."""
    # A payload that is roughly 40,000 characters (~10,000 tokens)
    small_file = {"path": "small.py", "content": "x" * 100}
    large_file = {"path": "large.py", "content": "x" * 160000} # 160,000 chars ~40,000 tokens
    
    # Batch size limit 20, token limit 10,000 tokens (40,000 chars)
    files = [small_file, large_file, small_file]
    batches = batch_files(files, max_files_per_batch=20, max_tokens_per_batch=10000)
    
    # Since large_file has ~40,000 tokens (which is > 10,000 token limit), it must start/be in its own batch.
    assert len(batches) == 3

def test_parse_response_formats():
    """Verify that parse_response handles raw JSON and fenced JSON markdown."""
    raw_json = '[{"path": "a.py"}]'
    fenced_json = '```json\n[{"path": "a.py"}]\n```'
    
    assert parse_response(raw_json) == [{"path": "a.py"}]
    assert parse_response(fenced_json) == [{"path": "a.py"}]
    
    # Test malformed JSON
    with pytest.raises(json.JSONDecodeError):
        parse_response("[invalid json")

def test_get_taint_warning():
    """Verify that get_taint_warning resolves the source function ID to a warning note."""
    conn = sqlite3.connect(":memory:")
    init_schema(conn)
    
    # Insert source
    conn.execute("INSERT INTO files (path, semantic_hash, content_hash) VALUES ('b.py', 'hb', 'cb')")
    conn.execute(
        """
        INSERT INTO functions (id, file, class_name, name, signature, line_start, line_end, semantic_hash)
        VALUES ('b.py::B', 'b.py', 'ClassB', 'B', 'def B()', 1, 5, 'hB')
        """
    )
    
    warning = get_taint_warning(conn, "b.py::B")
    assert "depends on ClassB.B() in b.py, which changed" in warning
    
    # Missing source ID fallback
    assert "depends on b.py::Missing, which changed" in get_taint_warning(conn, "b.py::Missing")
    conn.close()

def test_apply_summary_batch():
    """Verify that apply_summary_batch correctly updates files, functions, and clears taint queue."""
    conn = sqlite3.connect(":memory:")
    init_schema(conn)
    
    # Seed db
    conn.execute("INSERT INTO files (path, semantic_hash, content_hash, is_stale) VALUES ('a.py', 'ha', 'ca', 1)")
    conn.execute(
        """
        INSERT INTO functions (id, file, name, signature, line_start, line_end, semantic_hash, is_stale, is_tainted, taint_source)
        VALUES ('a.py::A', 'a.py', 'A', 'def A()', 1, 5, 'hA', 1, 1, 'b.py::B')
        """
    )
    conn.execute("INSERT INTO taint_queue (function_id, taint_source) VALUES ('a.py::A', 'b.py::B')")
    conn.commit()
    
    parsed = [
        {
            "path": "a.py",
            "purpose": "A test purpose",
            "summary": "A test summary",
            "danger": "A test danger",
            "functions": [
                {
                    "id": "a.py::A",
                    "summary": "new function summary",
                    "summary_long": "new function summary long",
                    "danger": "func danger"
                }
            ]
        }
    ]
    
    files_up, funcs_up = apply_summary_batch(conn, parsed)
    assert files_up == 1
    assert funcs_up == 1
    
    # Check file updates
    file_row = conn.execute("SELECT purpose, summary, danger, confidence, is_stale FROM files WHERE path = 'a.py'").fetchone()
    assert file_row[0] == "A test purpose"
    assert file_row[1] == "A test summary"
    assert file_row[2] == "A test danger"
    assert file_row[3] == 1.0
    assert file_row[4] == 0
    
    # Check function updates
    func_row = conn.execute("SELECT summary, summary_long, danger, confidence, is_stale, is_tainted, taint_source FROM functions WHERE id = 'a.py::A'").fetchone()
    assert func_row[0] == "new function summary"
    assert func_row[1] == "new function summary long"
    assert func_row[2] == "func danger"
    assert func_row[3] == 1.0
    assert func_row[4] == 0
    assert func_row[5] == 0
    assert func_row[6] is None
    
    # Check taint queue cleared
    assert conn.execute("SELECT count(*) FROM taint_queue").fetchone()[0] == 0
    conn.close()
