import sqlite3
import subprocess
from pathlib import Path
import pytest
from ctx_engine.db import connect, init_schema
from ctx_engine.reindex import reindex_file, run_reindex_pipeline

@pytest.fixture
def temp_repo(tmp_path):
    # Initialize Git repo
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, capture_output=True, check=True)
    
    # Create sample Python file
    (tmp_path / "calc.py").write_text("""
def add(a, b):
    # original logic
    return a + b

def subtract(a, b):
    return a - b
""", encoding="utf-8")
    
    # Commit files
    subprocess.run(["git", "add", "calc.py"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=tmp_path, capture_output=True, check=True)
    
    # Setup DB
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = connect(db_path)
    init_schema(conn)
    
    yield tmp_path, conn
    
    conn.close()

def test_reindex_no_changes(temp_repo):
    """Run reindex_file twice with no changes -> metadata and hashes are preserved exactly."""
    tmp_path, conn = temp_repo
    
    # First reindex
    reindex_file(conn, tmp_path, "calc.py", "python")
    
    # Seed some mock metadata
    conn.execute("UPDATE files SET purpose = 'Calculator helper', summary = 'Calc module', danger = 'None' WHERE path = 'calc.py'")
    conn.execute("UPDATE functions SET summary = 'adds two numbers', confidence = 1.0, is_stale = 0 WHERE id = 'calc.py::add'")
    conn.execute("UPDATE functions SET summary = 'subtracts two numbers', confidence = 1.0, is_stale = 0 WHERE id = 'calc.py::subtract'")
    conn.commit()
    
    # Second reindex (no change on disk)
    reindex_file(conn, tmp_path, "calc.py", "python")
    
    # Assert metadata is preserved
    file_row = conn.execute("SELECT purpose, summary, danger, confidence, is_stale FROM files WHERE path = 'calc.py'").fetchone()
    assert file_row["purpose"] == 'Calculator helper'
    assert file_row["summary"] == 'Calc module'
    assert file_row["danger"] == 'None'
    assert file_row["confidence"] == 1.0
    
    add_row = conn.execute("SELECT summary, confidence, is_stale FROM functions WHERE id = 'calc.py::add'").fetchone()
    assert add_row["summary"] == 'adds two numbers'
    assert add_row["confidence"] == 1.0
    assert add_row["is_stale"] == 0

def test_reindex_semantic_change(temp_repo):
    """Change one function's logic -> confidence decays, stale=1, untouched function is unchanged."""
    tmp_path, conn = temp_repo
    
    # First reindex & seed metadata
    reindex_file(conn, tmp_path, "calc.py", "python")
    conn.execute("UPDATE files SET purpose = 'Calculator helper', confidence = 1.0, is_stale = 0 WHERE path = 'calc.py'")
    conn.execute("UPDATE functions SET summary = 'adds two numbers', confidence = 1.0, is_stale = 0 WHERE id = 'calc.py::add'")
    conn.execute("UPDATE functions SET summary = 'subtracts two numbers', confidence = 1.0, is_stale = 0 WHERE id = 'calc.py::subtract'")
    conn.commit()
    
    # Change logic of add() on disk
    (tmp_path / "calc.py").write_text("""
def add(a, b):
    # logic change
    return a + b + 42

def subtract(a, b):
    return a - b
""", encoding="utf-8")
    
    # Second reindex
    reindex_file(conn, tmp_path, "calc.py", "python")
    
    # Assert calc.py file is stale and confidence decayed
    file_row = conn.execute("SELECT purpose, confidence, is_stale FROM files WHERE path = 'calc.py'").fetchone()
    assert file_row["purpose"] == 'Calculator helper'
    assert file_row["confidence"] == pytest.approx(0.85)
    assert file_row["is_stale"] == 1
    
    # Assert add() confidence decayed, stale=1, old summary preserved
    add_row = conn.execute("SELECT summary, confidence, is_stale FROM functions WHERE id = 'calc.py::add'").fetchone()
    assert add_row["summary"] == 'adds two numbers'
    assert add_row["confidence"] == pytest.approx(0.85)
    assert add_row["is_stale"] == 1
    
    # Assert subtract() remains untouched
    sub_row = conn.execute("SELECT summary, confidence, is_stale FROM functions WHERE id = 'calc.py::subtract'").fetchone()
    assert sub_row["summary"] == 'subtracts two numbers'
    assert sub_row["confidence"] == 1.0
    assert sub_row["is_stale"] == 0

def test_reindex_addition_and_removal(temp_repo):
    """Add a new function and remove an old one -> verify appropriate addition/removal behaviour."""
    tmp_path, conn = temp_repo
    
    # First reindex
    reindex_file(conn, tmp_path, "calc.py", "python")
    
    # Modify disk: remove subtract(), add multiply()
    (tmp_path / "calc.py").write_text("""
def add(a, b):
    return a + b

def multiply(a, b):
    return a * b
""", encoding="utf-8")
    
    # Second reindex
    reindex_file(conn, tmp_path, "calc.py", "python")
    
    # Assert subtract() is gone
    sub_row = conn.execute("SELECT * FROM functions WHERE id = 'calc.py::subtract'").fetchone()
    assert sub_row is None
    
    # Assert multiply() is added as new (summary=None, stale=1, confidence=1.0)
    mult_row = conn.execute("SELECT summary, confidence, is_stale FROM functions WHERE id = 'calc.py::multiply'").fetchone()
    assert mult_row is not None
    assert mult_row["summary"] is None
    assert mult_row["confidence"] == 1.0
    assert mult_row["is_stale"] == 1

def test_reindex_rename(temp_repo):
    """Rename a function -> treated as removal of old id and addition of new id."""
    tmp_path, conn = temp_repo
    
    reindex_file(conn, tmp_path, "calc.py", "python")
    
    # Rename subtract to sub
    (tmp_path / "calc.py").write_text("""
def add(a, b):
    return a + b

def sub(a, b):
    return a - b
""", encoding="utf-8")
    
    reindex_file(conn, tmp_path, "calc.py", "python")
    
    # Old subtract is gone
    assert conn.execute("SELECT count(*) FROM functions WHERE id = 'calc.py::subtract'").fetchone()[0] == 0
    # New sub is added with stale=1 and summary=None
    new_sub = conn.execute("SELECT summary, is_stale, confidence FROM functions WHERE id = 'calc.py::sub'").fetchone()
    assert new_sub is not None
    assert new_sub["summary"] is None
    assert new_sub["is_stale"] == 1

def test_reindex_reformatting_invariance(temp_repo):
    """Change whitespace and quotes -> semantic hashes match, stale stays 0, confidence stays 1.0."""
    tmp_path, conn = temp_repo
    
    reindex_file(conn, tmp_path, "calc.py", "python")
    
    # Seed metadata
    conn.execute("UPDATE files SET purpose = 'Calculator helper', confidence = 1.0, is_stale = 0 WHERE path = 'calc.py'")
    conn.execute("UPDATE functions SET summary = 'adds two numbers', confidence = 1.0, is_stale = 0 WHERE id = 'calc.py::add'")
    conn.commit()
    
    # Reformat calc.py with indentation and single vs double quote literal changes (in comment/docstring or whitespace)
    # The actual AST logic remains identical
    (tmp_path / "calc.py").write_text("""
def add(a, b):
    
    
    return a + b

def subtract(a, b):
        return a - b
""", encoding="utf-8")
    
    reindex_file(conn, tmp_path, "calc.py", "python")
    
    # Assert add() is not stale and confidence is still 1.0
    add_row = conn.execute("SELECT summary, confidence, is_stale FROM functions WHERE id = 'calc.py::add'").fetchone()
    assert add_row["confidence"] == 1.0
    assert add_row["is_stale"] == 0
