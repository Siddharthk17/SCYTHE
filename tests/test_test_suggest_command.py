"""Tests for ctx test-suggest (Week 9)."""
import json
import sqlite3
import subprocess

import pytest

from ctx_engine.commands.test_suggest_cmd import (
    run_test_suggest,
    suggest_for_function,
    suggestions_to_json,
)
from ctx_engine.db import init_schema


@pytest.fixture
def suggest_db(tmp_path):
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    def add_file(path):
        conn.execute(
            "INSERT INTO files (path, semantic_hash, content_hash, "
            "exports, imports, used_by, used_by_count, is_stale) "
            "VALUES (?, 'sh', 'ch', '[]', '[]', '[]', 0, 0)",
            (path,),
        )

    def add_func(fid, path, name, mutates="[]", danger=None,
                 confidence=1.0, is_stale=0):
        conn.execute(
            "INSERT INTO functions (id, file, name, signature, mutates, danger, "
            "line_start, line_end, semantic_hash, confidence, is_stale) "
            "VALUES (?, ?, ?, 'sig', ?, ?, 1, 10, 'sh', ?, ?)",
            (fid, path, name, mutates, danger, confidence, is_stale),
        )

    def add_edge(caller, callee, name):
        conn.execute(
            "INSERT INTO call_graph (caller_id, callee_id, callee_name) "
            "VALUES (?, ?, ?)",
            (caller, callee, name),
        )

    add_file("src/mcts.py")
    add_file("src/self_play.py")
    add_file("tests/test_mcts.py")
    target = "src/mcts.py::MCTS._expand_batch"
    add_func(
        target, "src/mcts.py", "_expand_batch",
        mutates=json.dumps(["node.children", "node.is_expanded", "node.total_value"]),
        danger="undo_virtual_loss() must run even on exception",
    )
    add_func("src/self_play.py::WorkerPool._dispatch", "src/self_play.py", "_dispatch")
    add_func("tests/test_mcts.py::test_expand", "tests/test_mcts.py", "test_expand")
    add_edge("src/self_play.py::WorkerPool._dispatch", target, "_expand_batch")
    add_edge("tests/test_mcts.py::test_expand", target, "_expand_batch")
    conn.commit()
    yield conn, tmp_path, target
    conn.close()


def test_danger_suggestion_references_description(suggest_db):
    conn, _, target = suggest_db
    items = suggest_for_function(conn, target)
    dz = [s for s in items if s.category == "danger"]
    assert len(dz) == 1
    assert "undo_virtual_loss() must run even on exception" in dz[0].detail
    assert dz[0].priority == "CRITICAL"


def test_mutation_one_per_item(suggest_db):
    conn, _, target = suggest_db
    items = suggest_for_function(conn, target)
    muts = [s for s in items if s.category == "mutation"]
    assert len(muts) == 3
    assert "node.children" in muts[0].detail


def test_caller_gap_and_covered(suggest_db):
    conn, _, target = suggest_db
    items = suggest_for_function(conn, target)
    callers = [s for s in items if s.category == "caller"]
    assert len(callers) == 2
    gap = next(s for s in callers if "WorkerPool._dispatch" in s.title)
    assert gap.priority == "HIGH"
    assert "no test exercises this path" in gap.detail
    covered = next(s for s in callers if "test_expand" in s.title)
    assert covered.priority == "LOW"


def test_empty_mutations_no_crash(suggest_db):
    conn, _, _ = suggest_db
    conn.execute(
        "INSERT INTO functions (id, file, name, signature, line_start, "
        "line_end, semantic_hash) "
        "VALUES ('src/mcts.py::bare', 'src/mcts.py', 'bare', 'sig', 1, 2, 'sh')"
    )
    conn.commit()
    items = suggest_for_function(conn, "src/mcts.py::bare")
    assert [s for s in items if s.category == "mutation"] == []


def test_zero_callers_no_crash(suggest_db):
    conn, _, _ = suggest_db
    conn.execute(
        "INSERT INTO functions (id, file, name, signature, line_start, "
        "line_end, semantic_hash) "
        "VALUES ('src/mcts.py::leaf', 'src/mcts.py', 'leaf', 'sig', 1, 2, 'sh')"
    )
    conn.commit()
    items = suggest_for_function(conn, "src/mcts.py::leaf")
    assert [s for s in items if s.category == "caller"] == []
    assert [s for s in items if s.category == "depth"] == []


def test_json_format_has_all_categories(suggest_db, capsys):
    _, root, target = suggest_db
    run_test_suggest(root, function_id=target, format="json")
    payload = json.loads(capsys.readouterr().out)
    assert payload["targets"] == [target]
    categories = {s["category"] for s in payload["suggestions"][target]}
    assert {"danger", "mutation", "caller", "depth"} <= categories
    assert all(
        {"category", "priority", "title", "detail"} <= set(s)
        for s in payload["suggestions"][target]
    )


def test_priority_ordering(suggest_db):
    conn, _, target = suggest_db
    items = suggest_for_function(conn, target)
    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    ranks = [order[s.priority] for s in items]
    assert ranks == sorted(ranks)
    assert items[0].priority == "CRITICAL"


def test_stale_function_warns(suggest_db):
    conn, _, _ = suggest_db
    conn.execute(
        "INSERT INTO functions (id, file, name, signature, line_start, "
        "line_end, semantic_hash, confidence, is_stale) "
        "VALUES ('src/mcts.py::old', 'src/mcts.py', 'old', 'sig', 1, 2, 'sh', 0.1, 1)"
    )
    conn.commit()
    items = suggest_for_function(conn, "src/mcts.py::old")
    stale = [s for s in items if s.category == "staleness"]
    assert len(stale) == 1
    assert stale[0].priority == "CRITICAL"


def test_staged_mode_detects_changed_function(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "m.py").write_text("def f():\n    return 1\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True,
                   capture_output=True)

    ctx_dir = root / ".ctx"
    ctx_dir.mkdir(exist_ok=True)
    conn = sqlite3.connect(ctx_dir / "index.db")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    conn.execute(
        "INSERT INTO files (path, semantic_hash, content_hash) "
        "VALUES ('m.py', 'sh', 'ch')"
    )
    conn.execute(
        "INSERT INTO functions (id, file, name, signature, mutates, "
        "line_start, line_end, semantic_hash) "
        "VALUES ('m.py::f', 'm.py', 'f', 'sig', '[\"x\"]', 1, 2, 'sh')"
    )
    conn.commit()
    conn.close()

    (root / "m.py").write_text("def f():\n    return 2\n")
    subprocess.run(["git", "add", "m.py"], cwd=root, check=True)

    from io import StringIO
    import contextlib

    buf = StringIO()
    with contextlib.redirect_stdout(buf):
        run_test_suggest(root, staged=True)
    out = buf.getvalue()
    assert "m.py::f" in out
    assert "MUT-1" in out


def test_missing_function_raises(suggest_db):
    conn, _, _ = suggest_db
    with pytest.raises(ValueError, match="not found in index"):
        suggest_for_function(conn, "ghost.py::nope")
