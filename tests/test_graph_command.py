"""Tests for ctx graph (Week 8)."""
import json
import sqlite3

import pytest

from ctx_engine.commands.graph_cmd import (
    render_dot,
    render_mermaid,
    collect_call_edges,
    run_graph,
)
from ctx_engine.db import init_schema


@pytest.fixture
def graph_db(tmp_path):
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    def add_file(path, system, imports, used_by_count=0, is_stale=0, tainted=0):
        conn.execute(
            "INSERT INTO files (path, system, semantic_hash, content_hash, "
            "exports, imports, used_by, used_by_count, is_stale) "
            "VALUES (?, ?, 'sh', 'ch', '[]', ?, '[]', ?, ?)",
            (path, system, json.dumps(imports), used_by_count, is_stale),
        )
        if tainted:
            conn.execute(
                "INSERT INTO functions (id, file, name, signature, line_start, "
                "line_end, semantic_hash, is_tainted) "
                "VALUES (?, ?, ?, 'sig', 1, 2, 'sh', 1)",
                (f"{path}::fn", path, "fn"),
            )

    add_file("src/ctx_engine/mcts.py", "search", ["src/ctx_engine/targets.py"])
    add_file("src/ctx_engine/self_play.py", "training", ["src/ctx_engine/mcts.py"])
    add_file("src/ctx_engine/targets.py", "training", [], used_by_count=20)
    add_file("src/ctx_engine/stale.py", "misc", [], is_stale=1)
    add_file("src/ctx_engine/tainted.py", "misc", [], tainted=1)
    conn.commit()
    yield conn, tmp_path
    conn.close()


# ── DOT format ────────────────────────────────────────────────────────────────


def test_dot_valid_syntax_and_content(graph_db):
    conn, _ = graph_db
    graph = {
        "src/ctx_engine/mcts.py": {
            "system": "search", "used_by_count": 0, "is_stale": False,
            "tainted": False, "imports": ["src/ctx_engine/targets.py"], "used_by": [],
        },
        "src/ctx_engine/targets.py": {
            "system": "training", "used_by_count": 0, "is_stale": False,
            "tainted": False, "imports": [], "used_by": [],
        },
    }
    dot = render_dot(graph, None, False)
    assert dot.startswith("digraph ctx_import_graph {")
    assert dot.rstrip().endswith("}")
    assert '"src/ctx_engine/mcts.py"' in dot
    assert '"src/ctx_engine/mcts.py" -> "src/ctx_engine/targets.py";' in dot


def test_dot_high_fanin_color(graph_db):
    conn, _ = graph_db
    graph = {
        "src/ctx_engine/targets.py": {
            "system": "training", "used_by_count": 20, "is_stale": False,
            "tainted": False, "imports": [], "used_by": [],
        },
    }
    dot = render_dot(graph, None, False)
    assert "#F1948A" in dot
    assert "imported by 20" in dot


def test_dot_stale_color(graph_db):
    conn, _ = graph_db
    graph = {
        "src/ctx_engine/stale.py": {
            "system": "misc", "used_by_count": 0, "is_stale": True,
            "tainted": False, "imports": [], "used_by": [],
        },
    }
    assert "#F9E79F" in render_dot(graph, None, False)


def test_dot_tainted_color(graph_db):
    conn, _ = graph_db
    graph = {
        "src/ctx_engine/tainted.py": {
            "system": "misc", "used_by_count": 0, "is_stale": False,
            "tainted": True, "imports": [], "used_by": [],
        },
    }
    assert "#FAD7A0" in render_dot(graph, None, False)


# ── Mermaid format ────────────────────────────────────────────────────────────


def test_mermaid_valid_syntax_and_content(graph_db):
    conn, _ = graph_db
    graph = {
        "src/ctx_engine/mcts.py": {
            "system": "search", "used_by_count": 0, "is_stale": False,
            "tainted": False, "imports": ["src/ctx_engine/targets.py"], "used_by": [],
        },
        "src/ctx_engine/targets.py": {
            "system": "training", "used_by_count": 20, "is_stale": False,
            "tainted": False, "imports": [], "used_by": [],
        },
    }
    out = render_mermaid(graph, "src/ctx_engine/mcts.py", False)
    assert out.startswith("```mermaid")
    assert out.rstrip().endswith("```")
    assert "graph LR" in out
    assert "mcts_py[\"mcts.py (search)\"]" in out
    assert "src_ctx_engine_mcts_py --> src_ctx_engine_targets_py" in out
    # Focus file styled darker blue; high fan-in styled red.
    assert "style src_ctx_engine_mcts_py fill:#85C1E9" in out
    assert "style src_ctx_engine_targets_py fill:#F1948A" in out


# ── run_graph behaviors (real DB) ─────────────────────────────────────────────


def test_run_graph_dot_stdout(graph_db, capsys):
    conn, repo_root = graph_db
    run_graph(repo_root, format="dot")
    out = capsys.readouterr().out
    assert "digraph ctx_import_graph {" in out
    for path in ("src/ctx_engine/mcts.py", "src/ctx_engine/targets.py"):
        assert path in out


def test_run_graph_focus_depth1(graph_db, capsys):
    """Depth 1: only the focus file and direct imports/dependents."""
    conn, repo_root = graph_db
    run_graph(repo_root, focus="src/ctx_engine/self_play.py", depth=1)
    out = capsys.readouterr().out
    assert "self_play.py" in out
    assert "mcts.py" in out          # direct import
    assert "targets.py" not in out   # import-of-import — depth 2 territory


def test_run_graph_focus_depth2(graph_db, capsys):
    """Depth 2: imports-of-imports included."""
    conn, repo_root = graph_db
    run_graph(repo_root, focus="src/ctx_engine/self_play.py", depth=2)
    out = capsys.readouterr().out
    assert "self_play.py" in out
    assert "mcts.py" in out
    assert "targets.py" in out       # second hop


def test_run_graph_output_file_dot(graph_db, tmp_path):
    conn, repo_root = graph_db
    out_path = tmp_path / "out" / "graph.dot"
    run_graph(repo_root, format="mermaid", output="out/graph.dot")
    content = out_path.read_text()
    # .dot extension overrides --format.
    assert content.startswith("digraph")
    assert not content.startswith("```")


def test_run_graph_output_file_mermaid(graph_db, tmp_path):
    conn, repo_root = graph_db
    run_graph(repo_root, format="dot", output="graph.md")
    content = (repo_root / "graph.md").read_text()
    assert content.startswith("```mermaid")


def test_run_graph_empty_imports_no_edges(graph_db, capsys):
    """Files without imports → nodes only, no crash, no edges."""
    conn, repo_root = graph_db
    conn.execute("DELETE FROM files WHERE imports != '[]'")
    conn.commit()
    run_graph(repo_root, format="dot")
    out = capsys.readouterr().out
    assert "digraph" in out
    assert "->" not in out


def test_run_graph_large_graph_warning(graph_db, monkeypatch, capsys):
    conn, repo_root = graph_db
    # Inflate the node count past the warning threshold.
    for i in range(105):
        conn.execute(
            "INSERT INTO files (path, system, semantic_hash, content_hash, "
            "exports, imports, used_by, used_by_count, is_stale) "
            f"VALUES ('bulk/file{i}.py', 'bulk', 'sh', 'ch', '[]', '[]', '[]', 0, 0)"
        )
    conn.commit()
    run_graph(repo_root, format="dot")
    out = capsys.readouterr().out
    assert "Large graph" in out
    assert "digraph" in out  # still generated


def test_run_graph_focus_not_indexed_raises(graph_db):
    conn, repo_root = graph_db
    with pytest.raises(ValueError):
        run_graph(repo_root, focus="nope.py")


def test_run_graph_with_calls_dashed_edges(graph_db, capsys):
    conn, repo_root = graph_db
    conn.execute(
        "INSERT INTO functions (id, file, name, signature, line_start, line_end, "
        "semantic_hash) VALUES ('src/ctx_engine/mcts.py::a', "
        "'src/ctx_engine/mcts.py', 'a', 'sig', 1, 2, 'sh')"
    )
    conn.execute(
        "INSERT INTO functions (id, file, name, signature, line_start, line_end, "
        "semantic_hash) VALUES ('src/ctx_engine/targets.py::b', "
        "'src/ctx_engine/targets.py', 'b', 'sig', 1, 2, 'sh')"
    )
    conn.execute(
        "INSERT INTO call_graph (caller_id, callee_id, callee_name, is_ambiguous) "
        "VALUES ('src/ctx_engine/mcts.py::a', 'src/ctx_engine/targets.py::b', 'b', 0)"
    )
    conn.commit()
    run_graph(repo_root, format="dot", with_calls=True)
    out = capsys.readouterr().out
    assert "[style=dashed];" in out

