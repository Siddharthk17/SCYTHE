import subprocess
import gc
import pytest
from ctx_engine.db import connect, init_schema
from ctx_engine.reindex import reindex_file


@pytest.fixture
def temp_repo(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, capture_output=True, check=True)

    (tmp_path / "calc.py").write_text("""
def add(a, b):
    # original logic
    return a + b

def subtract(a, b):
    return a - b
""", encoding="utf-8")

    subprocess.run(["git", "add", "calc.py"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=tmp_path, capture_output=True, check=True)

    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    return tmp_path


def _with_db(temp_repo):
    conn = connect(temp_repo / ".ctx" / "index.db")
    init_schema(conn)
    return conn


def _close(conn):
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    conn.close()
    # Force GC to avoid ResourceWarning from lingering Row references
    gc.collect()
    # If the db file was ":memory:", it doesn't exist. Otherwise, reopen and close.
    if path and path != ":memory:":
        pass


def test_reindex_no_changes(temp_repo):
    """Run reindex_file twice with no changes -> metadata and hashes are preserved exactly."""
    conn = _with_db(temp_repo)
    try:
        reindex_file(conn, temp_repo, "calc.py", "python")

        conn.execute("UPDATE files SET purpose = 'Calculator helper', summary = 'Calc module', danger = 'None' WHERE path = 'calc.py'")
        conn.execute("UPDATE functions SET summary = 'adds two numbers', confidence = 1.0, is_stale = 0 WHERE id = 'calc.py::add'")
        conn.execute("UPDATE functions SET summary = 'subtracts two numbers', confidence = 1.0, is_stale = 0 WHERE id = 'calc.py::subtract'")
        conn.commit()

        reindex_file(conn, temp_repo, "calc.py", "python")

        r = dict(conn.execute("SELECT purpose, summary, danger, confidence, is_stale FROM files WHERE path = 'calc.py'").fetchone())
        assert r["purpose"] == 'Calculator helper'
        assert r["summary"] == 'Calc module'
        assert r["danger"] == 'None'
        assert r["confidence"] == 1.0

        r = dict(conn.execute("SELECT summary, confidence, is_stale FROM functions WHERE id = 'calc.py::add'").fetchone())
        assert r["summary"] == 'adds two numbers'
        assert r["confidence"] == 1.0
        assert r["is_stale"] == 0
    finally:
        conn.close()


def test_reindex_semantic_change(temp_repo):
    """Change one function's logic -> confidence decays, stale=1, untouched function is unchanged."""
    conn = _with_db(temp_repo)
    try:
        reindex_file(conn, temp_repo, "calc.py", "python")

        conn.execute("UPDATE files SET purpose = 'Calculator helper', confidence = 1.0, is_stale = 0 WHERE path = 'calc.py'")
        conn.execute("UPDATE functions SET summary = 'adds two numbers', confidence = 1.0, is_stale = 0 WHERE id = 'calc.py::add'")
        conn.execute("UPDATE functions SET summary = 'subtracts two numbers', confidence = 1.0, is_stale = 0 WHERE id = 'calc.py::subtract'")
        conn.commit()

        (temp_repo / "calc.py").write_text("""
def add(a, b):
    # logic change
    return a + b + 42

def subtract(a, b):
    return a - b
""", encoding="utf-8")

        reindex_file(conn, temp_repo, "calc.py", "python")

        r = dict(conn.execute("SELECT purpose, confidence, is_stale FROM files WHERE path = 'calc.py'").fetchone())
        assert r["purpose"] == 'Calculator helper'
        assert r["confidence"] == pytest.approx(0.85)
        assert r["is_stale"] == 1

        r = dict(conn.execute("SELECT summary, confidence, is_stale FROM functions WHERE id = 'calc.py::add'").fetchone())
        assert r["summary"] == 'adds two numbers'
        assert r["confidence"] == pytest.approx(0.85)
        assert r["is_stale"] == 1

        r = dict(conn.execute("SELECT summary, confidence, is_stale FROM functions WHERE id = 'calc.py::subtract'").fetchone())
        assert r["summary"] == 'subtracts two numbers'
        assert r["confidence"] == 1.0
        assert r["is_stale"] == 0
    finally:
        conn.close()
        del conn
        import gc
        gc.collect()


def test_reindex_addition_and_removal(temp_repo):
    """Add a new function and remove an old one -> verify appropriate addition/removal behaviour."""
    conn = _with_db(temp_repo)
    try:
        reindex_file(conn, temp_repo, "calc.py", "python")

        (temp_repo / "calc.py").write_text("""
def add(a, b):
    return a + b

def multiply(a, b):
    return a * b
""", encoding="utf-8")

        reindex_file(conn, temp_repo, "calc.py", "python")

        assert conn.execute("SELECT * FROM functions WHERE id = 'calc.py::subtract'").fetchone() is None

        r = conn.execute("SELECT summary, confidence, is_stale FROM functions WHERE id = 'calc.py::multiply'").fetchone()
        assert r is not None
        assert r["summary"] is None
        assert r["confidence"] == 1.0
        assert r["is_stale"] == 1
    finally:
        conn.close()


def test_reindex_rename(temp_repo):
    """Rename a function -> treated as removal of old id and addition of new id."""
    conn = _with_db(temp_repo)
    try:
        reindex_file(conn, temp_repo, "calc.py", "python")

        (temp_repo / "calc.py").write_text("""
def add(a, b):
    return a + b

def sub(a, b):
    return a - b
""", encoding="utf-8")

        reindex_file(conn, temp_repo, "calc.py", "python")

        assert conn.execute("SELECT count(*) FROM functions WHERE id = 'calc.py::subtract'").fetchone()[0] == 0
        r = conn.execute("SELECT summary, is_stale, confidence FROM functions WHERE id = 'calc.py::sub'").fetchone()
        assert r is not None
        assert r["summary"] is None
        assert r["is_stale"] == 1
    finally:
        conn.close()


def test_reindex_reformatting_invariance(temp_repo):
    """Change whitespace and quotes -> semantic hashes match, stale stays 0, confidence stays 1.0."""
    conn = _with_db(temp_repo)
    try:
        reindex_file(conn, temp_repo, "calc.py", "python")

        conn.execute("UPDATE files SET purpose = 'Calculator helper', confidence = 1.0, is_stale = 0 WHERE path = 'calc.py'")
        conn.execute("UPDATE functions SET summary = 'adds two numbers', confidence = 1.0, is_stale = 0 WHERE id = 'calc.py::add'")
        conn.commit()

        (temp_repo / "calc.py").write_text("""
def add(a, b):
    
    
    return a + b

def subtract(a, b):
        return a - b
""", encoding="utf-8")

        reindex_file(conn, temp_repo, "calc.py", "python")

        r = conn.execute("SELECT summary, confidence, is_stale FROM functions WHERE id = 'calc.py::add'").fetchone()
        assert r["confidence"] == 1.0
        assert r["is_stale"] == 0
    finally:
        conn.close()
