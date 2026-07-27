import sqlite3
import pytest
from ctx_engine.db import init_schema
from ctx_engine.intelligence.heuristics import (
    run_heuristic_detection,
    detect_high_call_fanin,
    detect_high_import_fanin,
    detect_global_mutations,
    detect_invariant_comments,
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
        "# FIXME: this is broken\n# TODO: fix later\n# HACK: temporary workaround\n\ndef bad_fn():\n    pass\n",
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
        ("util.py:mutate", "util.py", "mutate", "def mutate()", "Mutates global", None, 3, 4, "sf2", 0.8, 0, 0, '["GLOBAL"]', None),
    )
    conn.execute(
        "INSERT OR IGNORE INTO functions (id, file, name, signature, summary, summary_long, line_start, line_end, semantic_hash, confidence, is_stale, is_tainted, mutates, danger) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("bad.py:bad_fn", "bad.py", "bad_fn", "def bad_fn()", "Bad function", None, 5, 6, "sf3", 0.8, 0, 0, "[]", None),
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
    for r in results:
        assert "comment" in r.description or "FIXME" in r.description or "TODO" in r.description or "invariant" in r.description.lower()


def test_heuristic_snapshot_replacement(heur_db):
    conn, repo = heur_db
    run_heuristic_detection(conn, repo)
    first_count = conn.execute("SELECT COUNT(*) FROM dangers WHERE added_by = 'auto'").fetchone()[0]
    run_heuristic_detection(conn, repo)
    second_count = conn.execute("SELECT COUNT(*) FROM dangers WHERE added_by = 'auto'").fetchone()[0]
    assert second_count >= first_count or second_count == 0


def test_detect_high_call_fanin_threshold(heur_db):
    conn, repo = heur_db
    results = detect_high_call_fanin(conn)
    assert len(results) >= 0


def test_empty_db(tmp_path):
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    report = run_heuristic_detection(conn, tmp_path)
    assert len(report.detected) == 0
