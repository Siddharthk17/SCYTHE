import json
import sqlite3
import pytest
from pathlib import Path
from ctx_engine.db import init_schema
from ctx_engine.mcp_server.tools.assembly import (
    format_file_header,
    format_function_record,
    get_confidence_tag,
    assemble_zone0,
    assemble_zone3,
    assemble_zone1,
    assemble_zone2,
    assemble_context,
)


@pytest.fixture
def assembly_db(tmp_path):
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    conn.execute(
        "INSERT INTO files (path, semantic_hash, purpose, summary, danger, exports, imports, confidence, is_stale, content_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("src/main.py", "sh1", "Main entry point", "Handles CLI dispatch", "Modifies global state", '["run", "init"]', '["os", "sys"]', 1.0, 0, "abc123"),
    )
    conn.execute(
        "INSERT INTO functions (id, file, name, signature, summary, summary_long, line_start, line_end, semantic_hash, confidence, is_stale, is_tainted, mutates, danger) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("src/main.py:run", "src/main.py", "run", "def run(args)", "Runs the app", "Runs the CLI application with given args", 10, 35, "sf1", 1.0, 0, 0, '["stdout"]', "Modifies global state"),
    )
    conn.execute(
        "INSERT INTO functions (id, file, name, signature, summary, line_start, line_end, semantic_hash, confidence, is_stale, is_tainted, mutates) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("src/main.py:init", "src/main.py", "init", "def init()", "Init the system", 40, 50, "sf2", 0.3, 1, 0, '["config"]'),
    )
    conn.commit()
    return conn, tmp_path


@pytest.fixture
def multi_file_db(tmp_path):
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    conn.execute(
        "INSERT INTO files (path, semantic_hash, purpose, summary, content_hash, imports, used_by, used_by_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("src/core.py", "sh1", "Core logic", "Main core module", "h1", '[]', '["src/main.py"]', 1),
    )
    conn.execute(
        "INSERT INTO files (path, semantic_hash, purpose, summary, content_hash, imports, used_by, used_by_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("src/main.py", "sh2", "Main entry", "Entry point", "h2", '["src/core.py"]', '[]', 0),
    )
    conn.execute(
        "INSERT INTO files (path, semantic_hash, purpose, summary, content_hash, imports, used_by, used_by_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("src/utils.py", "sh3", "Utilities", "Utility functions", "h3", '["src/core.py"]', '[]', 0),
    )
    conn.execute(
        "INSERT INTO directories (path, file_count, summary) VALUES (?, ?, ?)",
        ("src", 3, "Source code"),
    )
    conn.commit()
    return conn, tmp_path


def test_get_confidence_tag_fresh():
    assert get_confidence_tag(1.0, 0) == ""


def test_get_confidence_tag_stale():
    tag = get_confidence_tag(1.0, 1)
    assert "STALE" in tag


def test_get_confidence_tag_low_confidence():
    tag = get_confidence_tag(0.3, 0)
    assert "LOW CONFIDENCE" in tag


def test_get_confidence_tag_very_low():
    tag = get_confidence_tag(0.1, 0)
    assert "LIKELY STALE" in tag


def test_format_file_header(assembly_db):
    conn, _ = assembly_db
    row = conn.execute("SELECT * FROM files WHERE path = 'src/main.py'").fetchone()
    result = format_file_header(row)
    assert "FILE: src/main.py" in result
    assert "Main entry point" in result
    assert "run, init" in result


def test_format_file_header_stale_tag(assembly_db):
    conn, _ = assembly_db
    conn.execute("UPDATE files SET is_stale = 1 WHERE path = 'src/main.py'")
    conn.commit()
    row = conn.execute("SELECT * FROM files WHERE path = 'src/main.py'").fetchone()
    result = format_file_header(row)
    assert "STALE" in result


def test_format_function_record(assembly_db):
    conn, _ = assembly_db
    row = conn.execute("SELECT * FROM functions WHERE id = 'src/main.py:run'").fetchone()
    result = format_function_record(row)
    assert "FUNC: src/main.py:run" in result
    assert "Runs the app" in result or "Runs the CLI" in result


def test_format_function_record_low_confidence(assembly_db):
    conn, _ = assembly_db
    conn.execute("UPDATE functions SET is_stale = 0 WHERE id = 'src/main.py:init'")
    conn.commit()
    row = conn.execute("SELECT * FROM functions WHERE id = 'src/main.py:init'").fetchone()
    result = format_function_record(row)
    assert "LOW CONFIDENCE" in result


def test_format_function_record_tainted(assembly_db):
    conn, _ = assembly_db
    conn.execute("UPDATE functions SET is_tainted = 1 WHERE id = 'src/main.py:run'")
    conn.commit()
    row = conn.execute("SELECT * FROM functions WHERE id = 'src/main.py:run'").fetchone()
    result = format_function_record(row)
    assert "TAINTED" in result


def test_assemble_zone0(assembly_db):
    conn, repo = assembly_db
    result = assemble_zone0(conn, "src/main.py", None, long_summaries=True)
    assert "FILE: src/main.py" in result
    assert "FUNC: src/main.py:run" in result


def test_assemble_zone0_not_indexed(assembly_db):
    conn, repo = assembly_db
    result = assemble_zone0(conn, "nonexistent.py", None)
    assert "not indexed" in result


def test_assemble_zone3(assembly_db):
    conn, repo = assembly_db
    result = assemble_zone3(conn, repo, "src/main.py")
    assert "PROJECT:" in result
    assert "DIRECTORY TREE:" in result


def test_assemble_zone1_ordering(multi_file_db):
    conn, repo = multi_file_db
    centrality = {
        "src/core.py": 0.5,
        "src/main.py": 1.0,
        "src/utils.py": 0.3,
    }
    result = assemble_zone1(conn, "src/main.py", centrality, long_mode=False)
    assert "FILE: src/core.py" in result
    assert "PURPOSE: Core logic" in result


def test_assemble_zone1_fan_in_compression(multi_file_db):
    conn, repo = multi_file_db
    conn.execute(
        "UPDATE files SET used_by_count = 20 WHERE path = 'src/core.py'"
    )
    conn.commit()
    centrality = {
        "src/core.py": 0.5,
        "src/main.py": 1.0,
        "src/utils.py": 0.3,
    }
    result = assemble_zone1(conn, "src/main.py", centrality, long_mode=False)
    assert "HIGH-FREQUENCY" in result
    assert "20 dependents" in result


def test_assemble_zone2_empty(multi_file_db):
    conn, repo = multi_file_db
    result = assemble_zone2(conn, "src/main.py", long_mode=False)
    assert result == "" or "CALL TARGETS" not in result


def test_assemble_context_under_budget(multi_file_db):
    conn, repo = multi_file_db
    result = assemble_context(conn, repo, "src/main.py", budget=8000)
    tokens = len(result) // 4
    assert tokens <= 8000, f"Token budget exceeded: {tokens} > 8000"


def test_assemble_context_zone3_before_zone0(multi_file_db):
    conn, repo = multi_file_db
    result = assemble_context(conn, repo, "src/main.py")
    assert "DIRECTORY TREE" in result
    assert "FILE:" in result
    assert result.index("DIRECTORY TREE") < result.index("FILE:")


def test_assemble_context_pruning_step3(multi_file_db):
    conn, repo = multi_file_db
    result = assemble_context(conn, repo, "src/main.py", budget=1)
    assert "budget" in result or "prun" in result


def test_pagination_with_many_functions():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    conn.execute(
        "INSERT INTO files (path, semantic_hash, purpose, content_hash) VALUES (?, ?, ?, ?)",
        ("big.py", "sh1", "Many funcs", "h1"),
    )
    for i in range(35):
        conn.execute(
            "INSERT INTO functions (id, file, name, signature, summary, line_start, line_end, semantic_hash, confidence, is_stale, is_tainted, mutates) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"big.py:f{i}", "big.py", f"f{i}", f"def f{i}()", f"Function {i}", i * 10, i * 10 + 5, f"sf{i}", 1.0, 0, 0, "[]"),
        )
    conn.commit()

    result = assemble_zone0(conn, "big.py", None, long_summaries=True)
    assert "Showing 20 of 35" in result
    assert "ctx.get_function" in result

    conn.close()
