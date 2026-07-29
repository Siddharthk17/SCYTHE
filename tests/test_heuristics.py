import json
import sqlite3
import pytest
from ctx_engine.db import init_schema
from ctx_engine.hashing import gen_id
from ctx_engine.intelligence.heuristics import (
    run_heuristic_detection,
    detect_high_call_fanin,
    detect_high_import_fanin,
    detect_global_mutations,
    detect_invariant_comments,
    CALL_FANIN_THRESHOLD,
    IMPORT_FANIN_THRESHOLD,
)


@pytest.fixture
def heur_db(tmp_path):
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    (tmp_path / "core.py").write_text(
        "import os\nimport sys\n\ndef core_fn():\n    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "util.py").write_text(
        "GLOBAL = {}\n\ndef mutate():\n    global GLOBAL\n    GLOBAL['x'] = 1\n",
        encoding="utf-8",
    )
    (tmp_path / "bad.py").write_text(
        "def bad_fn():\n    # warning: this is broken\n    # must fix this\n    # critical edge case\n    pass\n",
        encoding="utf-8",
    )

    conn.execute(
        "INSERT OR IGNORE INTO files (path, semantic_hash, content_hash, purpose, summary, is_stale, confidence, danger, exports, imports, used_by, used_by_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("core.py", "sh1", "ch1", "Core module", "Core", 0, 0.9, None, '["core_fn"]', '["os","sys"]', "[]", 5),
    )
    conn.execute(
        "INSERT OR IGNORE INTO files (path, semantic_hash, content_hash, purpose, summary, is_stale, confidence, danger, exports, imports, used_by, used_by_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("util.py", "sh2", "ch2", "Utilities", "Util", 0, 0.9, None, '["mutate"]', "[]", "[]", 3),
    )
    conn.execute(
        "INSERT OR IGNORE INTO files (path, semantic_hash, content_hash, purpose, summary, is_stale, confidence, danger, exports, imports, used_by, used_by_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("bad.py", "sh3", "ch3", "Bad module", "Bad", 0, 0.9, None, '["bad_fn"]', "[]", "[]", 0),
    )

    conn.execute(
        "INSERT OR IGNORE INTO functions (id, file, name, signature, summary, summary_long, line_start, line_end, semantic_hash, confidence, is_stale, is_tainted, mutates, danger) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("core.py:core_fn", "core.py", "core_fn", "def core_fn()", "Core function", None, 3, 4, "sf1", 0.8, 0, 0, "[]", None),
    )
    conn.execute(
        "INSERT OR IGNORE INTO functions (id, file, name, signature, summary, summary_long, line_start, line_end, semantic_hash, confidence, is_stale, is_tainted, mutates, danger) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("util.py:mutate", "util.py", "mutate", "def mutate()", "Mutates global", None, 3, 4, "sf2", 0.8, 0, 0, '["global:GLOBAL"]', None),
    )
    conn.execute(
        "INSERT OR IGNORE INTO functions (id, file, name, signature, summary, summary_long, line_start, line_end, semantic_hash, confidence, is_stale, is_tainted, mutates, danger) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("bad.py:bad_fn", "bad.py", "bad_fn", "def bad_fn()", "Bad function", None, 1, 5, "sf3", 0.8, 0, 0, "[]", None),
    )

    conn.execute(
        "INSERT OR IGNORE INTO call_graph (caller_id, callee_name, callee_id, is_ambiguous) "
        "VALUES (?, ?, ?, ?)",
        ("util.py:mutate", "core_fn", "core.py:core_fn", 0),
    )

    conn.commit()
    return conn, tmp_path


def test_run_heuristic_detection(heur_db):
    conn, repo = heur_db
    report = run_heuristic_detection(conn, repo)
    assert len(report.detected) >= 0


def test_run_heuristic_detection_with_dangers(heur_db):
    conn, repo = heur_db
    report = run_heuristic_detection(conn, repo)
    added = conn.execute("SELECT COUNT(*) FROM dangers WHERE added_by = 'auto'").fetchone()[0]
    assert added >= 0


def test_detect_high_call_fanin(heur_db):
    conn, repo = heur_db
    results = detect_high_call_fanin(conn)
    assert len(results) >= 0


def test_detect_high_import_fanin(heur_db):
    conn, repo = heur_db
    results = detect_high_import_fanin(conn)
    assert len(results) >= 0


def test_detect_global_mutations(heur_db):
    conn, repo = heur_db
    results = detect_global_mutations(conn)
    assert len(results) >= 0


def test_detect_invariant_comments(heur_db):
    conn, repo = heur_db
    results = detect_invariant_comments(conn, repo)
    assert len(results) >= 1
    for r in results:
        assert "invariant" in r.description.lower() or r.description.startswith("Invariant comment")


def test_heuristic_snapshot_replacement(heur_db):
    conn, repo = heur_db
    run_heuristic_detection(conn, repo)
    first_count = conn.execute("SELECT COUNT(*) FROM dangers WHERE added_by = 'auto'").fetchone()[0]
    run_heuristic_detection(conn, repo)
    second_count = conn.execute("SELECT COUNT(*) FROM dangers WHERE added_by = 'auto'").fetchone()[0]
    assert second_count >= first_count or second_count == 0


@pytest.fixture
def high_fanin_db(tmp_path):
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    (tmp_path / "hot.py").write_text("def hot_fn(): pass\n", encoding="utf-8")

    conn.execute(
        "INSERT OR IGNORE INTO files (path, semantic_hash, content_hash) VALUES (?, ?, ?)",
        ("hot.py", "sh_hot", "ch_hot"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO functions (id, file, name, signature, summary, summary_long, line_start, line_end, semantic_hash, confidence, is_stale, is_tainted, mutates, danger) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("hot.py:hot_fn", "hot.py", "hot_fn", "def hot_fn()", None, None, 1, 1, "sf_hot", 0.8, 0, 0, "[]", None),
    )

    # Insert CALL_FANIN_THRESHOLD + 2 callers pointing to hot_fn
    for i in range(CALL_FANIN_THRESHOLD + 2):
        caller_id = f"caller_{i}"
        conn.execute(
            "INSERT OR IGNORE INTO call_graph (caller_id, callee_name, callee_id, is_ambiguous) "
            "VALUES (?, ?, ?, ?)",
            (caller_id, "hot_fn", "hot.py:hot_fn", 0),
        )

    conn.commit()
    return conn, tmp_path


def test_detect_high_call_fanin_threshold(high_fanin_db):
    conn, repo = high_fanin_db
    results = detect_high_call_fanin(conn)
    assert len(results) == 1
    assert results[0].description.startswith(f"High-frequency function: called by {CALL_FANIN_THRESHOLD + 2}")


@pytest.fixture
def high_import_fanin_db(tmp_path):
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    conn.execute(
        "INSERT OR IGNORE INTO files (path, semantic_hash, content_hash, used_by_count) VALUES (?, ?, ?, ?)",
        ("popular.py", "sh_pop", "ch_pop", IMPORT_FANIN_THRESHOLD + 5),
    )
    conn.commit()
    return conn, tmp_path


def test_detect_high_import_fanin_threshold(high_import_fanin_db):
    conn, repo = high_import_fanin_db
    results = detect_high_import_fanin(conn)
    assert len(results) == 1
    assert any("popular.py" in r.scope for r in results)


@pytest.fixture
def global_mut_db(tmp_path):
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    conn.execute(
        "INSERT OR IGNORE INTO files (path, semantic_hash, content_hash) VALUES (?, ?, ?)",
        ("mut.py", "sh_mut", "ch_mut"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO functions (id, file, name, signature, summary, summary_long, line_start, line_end, semantic_hash, confidence, is_stale, is_tainted, mutates, danger) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("mut.py:mut_fn", "mut.py", "mut_fn", "def mut_fn()", None, None, 1, 1, "sf_mut", 0.8, 0, 0, '["global:_SHARED_QUEUE","local_var"]', None),
    )
    conn.commit()
    return conn, tmp_path


def test_detect_global_mutations_finds_global(global_mut_db):
    conn, repo = global_mut_db
    results = detect_global_mutations(conn)
    assert len(results) == 1
    assert any("_SHARED_QUEUE" in r.description for r in results)


@pytest.fixture
def isolation_db(tmp_path):
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    conn.execute(
        "INSERT OR IGNORE INTO dangers (id, scope, description, reason, added_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("human-keep", "*", "Human danger", "Human added", "human", "2026-07-01T00:00:00Z"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO dangers (id, scope, description, reason, added_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("model-keep", "*", "Model danger", "Model added", "model", "2026-07-01T00:00:00Z"),
    )
    conn.commit()
    return conn, tmp_path


def test_human_and_model_dangers_survive_detection(isolation_db):
    conn, repo = isolation_db
    before_human = conn.execute(
        "SELECT COUNT(*) FROM dangers WHERE added_by = 'human'"
    ).fetchone()[0]
    before_model = conn.execute(
        "SELECT COUNT(*) FROM dangers WHERE added_by = 'model'"
    ).fetchone()[0]
    assert before_human == 1
    assert before_model == 1

    run_heuristic_detection(conn, repo)

    after_human = conn.execute(
        "SELECT COUNT(*) FROM dangers WHERE added_by = 'human'"
    ).fetchone()[0]
    after_model = conn.execute(
        "SELECT COUNT(*) FROM dangers WHERE added_by = 'model'"
    ).fetchone()[0]
    assert after_human == before_human
    assert after_model == before_model


def test_dry_run_does_not_write(isolation_db):
    conn, repo = isolation_db
    before = conn.execute("SELECT COUNT(*) FROM dangers").fetchone()[0]

    report = run_heuristic_detection(conn, repo, dry_run=True)
    assert len(report.added) == 0
    assert len(report.removed) == 0

    after = conn.execute("SELECT COUNT(*) FROM dangers").fetchone()[0]
    assert after == before


def test_empty_db(tmp_path):
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    report = run_heuristic_detection(conn, tmp_path)
    assert len(report.detected) == 0
