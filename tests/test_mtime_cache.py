import sqlite3
import pytest
from pathlib import Path
from ctx_engine.db import init_schema
from ctx_engine.reindex import can_skip_file


@pytest.fixture
def db_with_file(tmp_path):
    conn = sqlite3.connect(tmp_path / "test.db")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    py_file = tmp_path / "file.py"
    py_file.write_text("x = 1")
    stat = py_file.stat()
    conn.execute(
        "INSERT INTO files (path, mtime, file_size, content_hash, semantic_hash, confidence, is_stale) "
        "VALUES (?, ?, ?, ?, ?, 1.0, 0)",
        ("file.py", stat.st_mtime, stat.st_size, "abc", "def"),
    )
    conn.commit()
    return conn, tmp_path


def test_can_skip_file_matches(db_with_file):
    conn, repo = db_with_file
    assert can_skip_file(conn, repo / "file.py", "file.py") is True


def test_can_skip_file_not_in_db(db_with_file):
    conn, repo = db_with_file
    (repo / "other.py").write_text("y = 2")
    assert can_skip_file(conn, repo / "other.py", "other.py") is False


def test_can_skip_file_modified(db_with_file):
    conn, repo = db_with_file
    (repo / "file.py").write_text("x = 12345\n")
    assert can_skip_file(conn, repo / "file.py", "file.py") is False


def test_can_skip_file_nulls_in_db(tmp_path):
    conn = sqlite3.connect(tmp_path / "test.db")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    conn.execute(
        "INSERT INTO files (path, content_hash, semantic_hash, confidence, is_stale) "
        "VALUES (?, ?, ?, 1.0, 0)",
        ("file.py", "abc", "def"),
    )
    conn.commit()
    py_file = tmp_path / "file.py"
    py_file.write_text("x = 1")
    assert can_skip_file(conn, py_file, "file.py") is False
    conn.close()


def test_can_skip_file_not_on_disk(db_with_file):
    conn, repo = db_with_file
    assert can_skip_file(conn, repo / "missing.py", "missing.py") is False
