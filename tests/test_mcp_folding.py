import sqlite3
import pytest
from pathlib import Path
from ctx_engine.db import init_schema
from ctx_engine.mcp_server.tools.folding import format_folded_directory_tree


@pytest.fixture
def folding_db(tmp_path):
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    conn.execute(
        "INSERT INTO files (path, semantic_hash, content_hash) VALUES (?, ?, ?)",
        ("src/main.py", "sh1", "h1"),
    )
    conn.execute(
        "INSERT INTO files (path, semantic_hash, content_hash) VALUES (?, ?, ?)",
        ("src/utils.py", "sh2", "h2"),
    )
    conn.execute(
        "INSERT INTO directories (path, file_count, summary) VALUES (?, ?, ?)",
        ("src", 2, "Source code"),
    )
    conn.execute(
        "INSERT INTO directories (path, file_count, summary) VALUES (?, ?, ?)",
        ("tests", 5, "Test suite"),
    )
    conn.execute(
        "INSERT INTO directories (path, file_count, summary) VALUES (?, ?, ?)",
        ("docs", 10, "Documentation"),
    )
    conn.commit()
    return conn, tmp_path


@pytest.fixture
def empty_dirs_db(tmp_path):
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    conn.execute(
        "INSERT INTO files (path, semantic_hash, content_hash) VALUES (?, ?, ?)",
        ("main.py", "sh1", "h1"),
    )
    conn.commit()
    return conn, tmp_path


@pytest.fixture
def nested_db(tmp_path):
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    conn.execute(
        "INSERT INTO files (path, semantic_hash, content_hash) VALUES (?, ?, ?)",
        ("src/core/mcts.py", "sh1", "h1"),
    )
    conn.execute(
        "INSERT INTO files (path, semantic_hash, content_hash) VALUES (?, ?, ?)",
        ("src/core/node.py", "sh2", "h2"),
    )
    conn.execute(
        "INSERT INTO files (path, semantic_hash, content_hash) VALUES (?, ?, ?)",
        ("src/training/train.py", "sh3", "h3"),
    )
    conn.execute(
        "INSERT INTO directories (path, file_count, summary) VALUES (?, ?, ?)",
        ("src", 3, "Source code"),
    )
    conn.execute(
        "INSERT INTO directories (path, file_count, summary) VALUES (?, ?, ?)",
        ("src/core", 2, "Core MCTS implementation"),
    )
    conn.execute(
        "INSERT INTO directories (path, file_count, summary) VALUES (?, ?, ?)",
        ("src/training", 1, "Training pipeline"),
    )
    conn.commit()
    return conn, tmp_path


def test_folding_shows_active_directory(folding_db):
    conn, repo = folding_db
    result = format_folded_directory_tree(conn, repo, "src/main.py")
    assert "DIRECTORY TREE:" in result
    assert "src/" in result
    assert "active" in result.lower()


def test_folding_marks_target(folding_db):
    conn, repo = folding_db
    result = format_folded_directory_tree(conn, repo, "src/main.py")
    assert "TARGET" in result


def test_folding_collapsed_dirs(folding_db):
    conn, repo = folding_db
    result = format_folded_directory_tree(conn, repo, "src/main.py")
    assert "tests/" in result
    assert "docs/" in result
    assert "5 files" in result or "5" in result


def test_folding_file_count_matches(folding_db):
    conn, repo = folding_db
    result = format_folded_directory_tree(conn, repo, "src/main.py")
    assert "main.py (TARGET)" in result
    assert "utils.py" in result
    assert "[5 files" in result
    assert "[10 files" in result


def test_folding_empty_directories_table(empty_dirs_db):
    conn, repo = empty_dirs_db
    result = format_folded_directory_tree(conn, repo, "main.py")
    assert "DIRECTORY TREE:" in result
    assert "main.py" in result


def test_folding_root_level_target(empty_dirs_db):
    conn, repo = empty_dirs_db
    result = format_folded_directory_tree(conn, repo, "main.py")
    assert "main.py" in result
    assert "TARGET" in result


def test_folding_nested_active_dir(nested_db):
    conn, repo = nested_db
    result = format_folded_directory_tree(conn, repo, "src/core/mcts.py")
    assert "src/core/" in result
    assert "mcts.py (TARGET)" in result
    assert "node.py" in result
    assert "src/" in result


def test_folding_no_crash_missing_dir(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    conn.execute(
        "INSERT INTO files (path, semantic_hash, content_hash) VALUES (?, ?, ?)",
        ("orphan.py", "sh1", "h1"),
    )
    conn.commit()
    result = format_folded_directory_tree(conn, tmp_path, "orphan.py")
    assert "DIRECTORY TREE:" in result
