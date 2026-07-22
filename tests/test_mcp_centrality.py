import json
import sqlite3
import pytest
from ctx_engine.db import init_schema
from ctx_engine.mcp_server.tools.centrality import compute_centrality


@pytest.fixture
def graph_db(tmp_path):
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    conn.execute(
        "INSERT INTO files (path, semantic_hash, imports, content_hash) VALUES (?, ?, ?, ?)",
        ("a.py", "sh1", json.dumps(["b.py", "c.py"]), "h1"),
    )
    conn.execute(
        "INSERT INTO files (path, semantic_hash, imports, content_hash) VALUES (?, ?, ?, ?)",
        ("b.py", "sh2", json.dumps(["c.py"]), "h2"),
    )
    conn.execute(
        "INSERT INTO files (path, semantic_hash, imports, content_hash) VALUES (?, ?, ?, ?)",
        ("c.py", "sh3", json.dumps([]), "h3"),
    )
    conn.commit()
    return conn


@pytest.fixture
def star_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    conn.execute(
        "INSERT INTO files (path, semantic_hash, imports, content_hash) VALUES (?, ?, ?, ?)",
        ("center.py", "sh1", json.dumps([]), "h1"),
    )
    for i in range(5):
        conn.execute(
            "INSERT INTO files (path, semantic_hash, imports, content_hash) VALUES (?, ?, ?, ?)",
            (f"leaf{i}.py", "sh2", json.dumps(["center.py"]), "h2"),
        )
    conn.commit()
    return conn


def test_centrality_returns_scores(graph_db):
    scores = compute_centrality(graph_db, "a.py")
    assert "a.py" in scores
    assert "b.py" in scores
    assert "c.py" in scores


def test_centrality_target_boosted(graph_db):
    scores = compute_centrality(graph_db, "a.py")
    assert scores["a.py"] > 0


def test_centrality_three_file_chain(graph_db):
    scores = compute_centrality(graph_db, "a.py")
    assert scores["a.py"] > 0
    assert scores["b.py"] > 0
    assert scores["c.py"] > 0


def test_centrality_star_graph(star_db):
    scores = compute_centrality(star_db, "leaf0.py")
    assert "center.py" in scores
    assert scores["center.py"] > 0
    assert scores["center.py"] > scores["leaf1.py"]


def test_centrality_empty_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    scores = compute_centrality(conn, "nonexistent.py")
    assert scores == {}


def test_centrality_no_imports():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    conn.execute(
        "INSERT INTO files (path, semantic_hash, imports, content_hash) VALUES (?, ?, ?, ?)",
        ("standalone.py", "sh1", json.dumps([]), "h1"),
    )
    conn.commit()
    scores = compute_centrality(conn, "standalone.py")
    assert "standalone.py" in scores
    assert scores["standalone.py"] == pytest.approx(0.15, abs=0.01)
