"""Tests for ctx refactor plan / apply (Week 9)."""
import sqlite3

import pytest

from ctx_engine.commands.refactor_cmd import (
    apply_refactor,
    plan_refactor,
    run_refactor_apply,
    run_refactor_plan,
    signatures_differ,
)
from ctx_engine.db import init_schema


@pytest.fixture
def refactor_db(tmp_path):
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    def add_file(path, system="misc"):
        conn.execute(
            "INSERT INTO files (path, system, semantic_hash, content_hash, "
            "exports, imports, used_by, used_by_count, is_stale) "
            "VALUES (?, ?, 'sh', 'ch', '[]', '[]', '[]', 0, 0)",
            (path, system),
        )

    def add_func(fid, path, name, sig="def old(a, b)"):
        conn.execute(
            "INSERT INTO functions (id, file, name, signature, line_start, "
            "line_end, semantic_hash) VALUES (?, ?, ?, ?, 10, 20, 'sh')",
            (fid, path, name, sig),
        )

    def add_edge(caller, callee, name, callee_file=None):
        conn.execute(
            "INSERT INTO call_graph (caller_id, callee_id, callee_name, callee_file) "
            "VALUES (?, ?, ?, ?)",
            (caller, callee, name, callee_file),
        )

    add_file("src/mod.py")
    add_file("tests/test_mod.py")
    add_func("src/mod.py::old", "src/mod.py", "old")
    add_func("src/mod.py::helper", "src/mod.py", "helper")
    add_func("tests/test_mod.py::test_old", "tests/test_mod.py", "test_old")
    add_edge("src/mod.py::helper", "src/mod.py::old", "old", "src/mod.py")
    add_edge("tests/test_mod.py::test_old", "src/mod.py::old", "old", "src/mod.py")
    conn.commit()
    yield conn, tmp_path
    conn.close()


def test_plan_lists_call_sites(refactor_db, capsys):
    conn, _ = refactor_db
    plan_refactor(conn, "src/mod.py::old", new_name="new")
    out = capsys.readouterr().out
    assert "src/mod.py" in out
    assert "tests/test_mod.py" in out
    assert "lines 10–20" in out
    assert "CALL SITES TO UPDATE (2 direct)" in out
    assert "ctx update src/mod.py" in out


def test_plan_no_callers(refactor_db, capsys):
    conn, _ = refactor_db
    conn.execute("DELETE FROM call_graph")
    conn.commit()
    plan_refactor(conn, "src/mod.py::old")
    out = capsys.readouterr().out
    assert "No call sites found — function appears to be unused or entry point." in out


def test_plan_new_name_in_change_lines(refactor_db, capsys):
    conn, _ = refactor_db
    plan_refactor(conn, "src/mod.py::old", new_name="shiny_new")
    out = capsys.readouterr().out
    assert "Change to: shiny_new(...) (was old(...))" in out
    assert "New function id will be: src/mod.py::shiny_new" in out


def test_plan_signature_change_flags_call_sites(refactor_db, capsys):
    conn, _ = refactor_db
    plan_refactor(conn, "src/mod.py::old", new_signature="def old(a, b, c)")
    out = capsys.readouterr().out
    assert "may need argument update" in out


def test_signatures_differ():
    assert not signatures_differ("def old(a, b)", "def new(a, b)")
    assert signatures_differ("def old(a, b)", "def new(a, b, c)")
    assert signatures_differ("def old(a, b)", "def new(a, renamed)")
    assert signatures_differ(None, "def new(a)")


def test_apply_refuses_existing_id(refactor_db, capsys):
    _, root = refactor_db
    run_refactor_apply(root, "src/mod.py::old")
    out = capsys.readouterr().out
    assert "Old function still exists — run ctx init first." in out


def test_apply_relinks_dangling_rows(refactor_db):
    conn, _ = refactor_db
    # Simulate rename + reindex: old row gone, new row present, edges dangling.
    conn.execute("DELETE FROM functions WHERE id = 'src/mod.py::old'")
    conn.execute(
        "INSERT INTO functions (id, file, name, signature, line_start, "
        "line_end, semantic_hash) VALUES ('src/mod.py::new', 'src/mod.py', "
        "'new', 'sig', 10, 20, 'sh')"
    )
    conn.execute(
        "UPDATE call_graph SET callee_id = NULL WHERE callee_id = 'src/mod.py::old'"
    )
    conn.execute(
        "UPDATE functions SET taint_source = 'src/mod.py::old' "
        "WHERE id = 'src/mod.py::helper'"
    )
    conn.commit()

    report = apply_refactor(conn, "src/mod.py::old", new_name="new")
    assert report.status == "applied"
    assert report.new_id == "src/mod.py::new"
    assert report.relinked == 2
    remaining = conn.execute(
        "SELECT COUNT(*) FROM call_graph WHERE callee_id IS NULL"
    ).fetchone()[0]
    assert remaining == 0
    fixed = conn.execute(
        "SELECT taint_source FROM functions WHERE id = 'src/mod.py::helper'"
    ).fetchone()[0]
    assert fixed == "src/mod.py::new"


def test_apply_ambiguous_without_new_name(refactor_db):
    conn, _ = refactor_db
    conn.execute("DELETE FROM functions WHERE id = 'src/mod.py::old'")
    conn.execute(
        "UPDATE call_graph SET callee_id = NULL WHERE callee_id = 'src/mod.py::old'"
    )
    conn.commit()
    # src/mod.py still holds 'helper' plus the new function.
    conn.execute(
        "INSERT INTO functions (id, file, name, signature, line_start, "
        "line_end, semantic_hash) VALUES ('src/mod.py::new', 'src/mod.py', "
        "'new', 'sig', 10, 20, 'sh')"
    )
    conn.commit()
    report = apply_refactor(conn, "src/mod.py::old")
    assert report.status == "ambiguous"
    assert "--new-name" in report.message


def test_run_plan_missing_function(refactor_db):
    _, root = refactor_db
    with pytest.raises(ValueError, match="not found in index"):
        run_refactor_plan(root, "src/mod.py::ghost")
