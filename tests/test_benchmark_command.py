"""Tests for ctx benchmark (Week 8)."""
import json
import re
import sqlite3

import pytest

import ctx_engine.mcp_server.tools.assembly as assembly_mod
from ctx_engine.commands.benchmark_cmd import (
    collect_metrics,
    compute_score,
    measure_danger_coverage,
    measure_token_efficiency,
    run_benchmark,
)
from ctx_engine.db import init_schema


@pytest.fixture
def bench_db(tmp_path):
    """Small indexed repo: 4 functions, mixed confidence, a small call graph."""
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    def add_file(path, imports="[]"):
        conn.execute(
            "INSERT INTO files (path, semantic_hash, content_hash, exports, "
            "imports, used_by) VALUES (?, 'sh', 'ch', '[]', ?, '[]')",
            (path, imports),
        )

    def add_fn(fn_id, path, confidence, summary="summary"):
        conn.execute(
            "INSERT INTO functions (id, file, name, signature, summary, "
            "line_start, line_end, semantic_hash, confidence) "
            "VALUES (?, ?, ?, 'sig', ?, 1, 10, 'sh', ?)",
            (fn_id, path, fn_id.rsplit("::", 1)[-1], summary, confidence),
        )

    add_file("a.py", '["b.py"]')
    add_file("b.py")
    add_fn("a.py::high1", "a.py", 1.0)
    add_fn("a.py::high2", "a.py", 0.95)
    add_fn("b.py::mid", "b.py", 0.7)
    add_fn("b.py::low", "b.py", 0.3)
    # Call graph: 2 resolved, 1 ambiguous, 1 unresolved (NULL callee).
    conn.execute(
        "INSERT INTO call_graph (caller_id, callee_id, callee_name, is_ambiguous) "
        "VALUES ('a.py::high1', 'b.py::mid', 'mid', 0)"
    )
    conn.execute(
        "INSERT INTO call_graph (caller_id, callee_id, callee_name, is_ambiguous) "
        "VALUES ('a.py::high2', 'b.py::mid', 'mid', 0)"
    )
    conn.execute(
        "INSERT INTO call_graph (caller_id, callee_id, callee_name, is_ambiguous) "
        "VALUES ('a.py::high1', 'b.py::low', 'low', 1)"
    )
    conn.execute(
        "INSERT INTO call_graph (caller_id, callee_id, callee_name, is_ambiguous) "
        "VALUES ('b.py::mid', NULL, 'missing', 0)"
    )
    conn.commit()
    yield conn, tmp_path
    conn.close()


@pytest.fixture
def stub_assembly(monkeypatch):
    """Replace the real assembly algorithm with a deterministic token source."""
    monkeypatch.setattr(
        assembly_mod, "assemble_context", lambda conn, root, path: "x" * 4000
    )


# ── Metric collection ─────────────────────────────────────────────────────


def test_collect_metrics_counts(bench_db):
    conn, root = bench_db
    metrics = collect_metrics(conn, root)
    assert metrics["summary_quality"]["total"] == 4
    assert metrics["summary_quality"]["summarized"] == 4
    assert metrics["summary_quality"]["high_confidence"] == 2
    assert metrics["summary_quality"]["low_confidence"] == 1
    assert metrics["call_graph"]["total"] == 4
    assert metrics["call_graph"]["resolved"] == 2
    assert metrics["call_graph"]["ambiguous"] == 1
    assert metrics["call_graph"]["unresolved"] == 1
    assert metrics["call_graph"]["resolution_rate"] == pytest.approx(0.5)
    assert metrics["import_graph"]["files_with_imports"] == 1
    assert metrics["import_graph"]["total_edges"] == 1


def test_token_efficiency_stubbed(bench_db, stub_assembly):
    conn, root = bench_db
    token_eff = measure_token_efficiency(conn, root)
    assert token_eff["sampled"] == 2  # only 2 files in the fixture
    assert token_eff["avg"] == 1000  # 4000 chars // 4
    assert token_eff["max"] == 1000
    assert token_eff["all_under_budget"] is True


def test_token_efficiency_over_budget(bench_db, monkeypatch):
    monkeypatch.setattr(
        assembly_mod, "assemble_context",
        lambda conn, root, path: "x" * (8001 * 4 if path == "a.py" else 100),
    )
    conn, root = bench_db
    token_eff = measure_token_efficiency(conn, root)
    assert token_eff["all_under_budget"] is False
    assert token_eff["under_budget"] == 1


def test_danger_coverage_empty(bench_db):
    conn, _ = bench_db
    danger_cov = measure_danger_coverage(conn)
    assert danger_cov["high_fanin_count"] == 0  # nothing has >= 10 callers
    assert danger_cov["coverage_rate"] == 1.0  # vacuous coverage


def test_danger_coverage_covered_and_uncovered(bench_db):
    conn, _ = bench_db
    for i in range(10):
        conn.execute(
            "INSERT INTO functions (id, file, name, signature, line_start, "
            "line_end, semantic_hash) VALUES (?, 'a.py', ?, 'sig', 1, 2, 'sh')",
            (f"a.py::caller{i}", f"caller{i}"),
        )
        conn.execute(
            "INSERT INTO call_graph (caller_id, callee_id, callee_name) "
            "VALUES (?, 'b.py::mid', 'mid')",
            (f"a.py::caller{i}",),
        )
    conn.execute(
        "INSERT INTO dangers (id, scope, description, reason, added_by) "
        "VALUES ('d1', 'b.py::mid', 'hot path', 'reason', 'human')"
    )
    conn.commit()
    danger_cov = measure_danger_coverage(conn)
    assert danger_cov["high_fanin_count"] == 1
    assert danger_cov["covered"] == 1
    assert danger_cov["coverage_rate"] == 1.0


# ── Scoring ───────────────────────────────────────────────────────────────


def test_score_formula_sums_to_overall(bench_db, stub_assembly):
    conn, root = bench_db
    metrics = collect_metrics(conn, root)
    token_eff = measure_token_efficiency(conn, root)
    danger_cov = measure_danger_coverage(conn)
    score, components = compute_score(metrics, token_eff, danger_cov)
    assert 0 <= score <= 100
    assert abs(sum(components.values()) - score) <= 1  # ±1 rounding
    assert set(components) == {
        "summary_coverage", "confidence", "call_resolution",
        "token_efficiency", "danger_coverage",
    }


def test_perfect_db_scores_100(tmp_path, stub_assembly):
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    conn.execute(
        "INSERT INTO files (path, semantic_hash, content_hash, exports, "
        "imports, used_by) VALUES ('a.py', 'sh', 'ch', '[]', '[]', '[]')"
    )
    conn.execute(
        "INSERT INTO functions (id, file, name, signature, summary, line_start, "
        "line_end, semantic_hash, confidence) "
        "VALUES ('a.py::f', 'a.py', 'f', 'sig', 's', 1, 5, 'sh', 1.0)"
    )
    conn.execute(
        "INSERT INTO functions (id, file, name, signature, summary, line_start, "
        "line_end, semantic_hash, confidence) "
        "VALUES ('a.py::g', 'a.py', 'g', 'sig', 's', 6, 9, 'sh', 1.0)"
    )
    conn.execute(
        "INSERT INTO call_graph (caller_id, callee_id, callee_name) "
        "VALUES ('a.py::g', 'a.py::f', 'f')"
    )
    conn.commit()
    metrics = collect_metrics(conn, tmp_path)
    token_eff = measure_token_efficiency(conn, tmp_path)
    danger_cov = measure_danger_coverage(conn)
    score, _ = compute_score(metrics, token_eff, danger_cov)
    assert score == 100
    conn.close()


# ── Report rendering ──────────────────────────────────────────────────────


def test_all_five_sections_in_output(bench_db, stub_assembly, capsys):
    conn, root = bench_db
    run_benchmark(root)
    out = capsys.readouterr().out
    for section in [
        "SUMMARY QUALITY", "CALL GRAPH RESOLUTION", "IMPORT GRAPH COVERAGE",
        "TOKEN EFFICIENCY", "DANGER COVERAGE", "OVERALL SCORE",
    ]:
        assert section in out
    score = int(re.search(r"OVERALL SCORE:\s*(\d+)/100", out).group(1))
    assert 0 <= score <= 100


def test_full_coverage_shows_checkmark(bench_db, stub_assembly, capsys):
    _, root = bench_db
    run_benchmark(root)
    out = capsys.readouterr().out
    assert "✓" in out  # e.g. 100% summarized / all files under budget


def test_zero_high_confidence_flags_warning(tmp_path, stub_assembly, capsys):
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    conn.execute(
        "INSERT INTO files (path, semantic_hash, content_hash, exports, "
        "imports, used_by) VALUES ('a.py', 'sh', 'ch', '[]', '[]', '[]')"
    )
    conn.execute(
        "INSERT INTO functions (id, file, name, signature, summary, line_start, "
        "line_end, semantic_hash, confidence) "
        "VALUES ('a.py::f', 'a.py', 'f', 'sig', 's', 1, 5, 'sh', 0.1)"
    )
    conn.commit()
    run_benchmark(tmp_path)
    out = capsys.readouterr().out
    assert "⚠" in out  # 0% high-confidence must not show ✓
    conn.close()


def test_json_output_has_all_keys(bench_db, stub_assembly, capsys):
    _, root = bench_db
    run_benchmark(root, json_output=True)
    doc = json.loads(capsys.readouterr().out)
    assert set(doc) >= {
        "timestamp", "repo", "overall_score", "summary_quality",
        "call_graph", "import_graph", "token_efficiency", "danger_coverage",
    }
    assert set(doc["summary_quality"]) >= {
        "total", "summarized", "high_confidence", "low_confidence", "likely_stale",
    }
    assert set(doc["call_graph"]) >= {
        "total", "resolved", "ambiguous", "unresolved", "resolution_rate",
    }
    assert 0 <= doc["overall_score"] <= 100


def test_empty_db_no_division_by_zero(tmp_path, stub_assembly, capsys):
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    conn.commit()
    run_benchmark(tmp_path)  # must not raise
    out = capsys.readouterr().out
    assert "OVERALL SCORE" in out
    run_benchmark(tmp_path, json_output=True)
    doc = json.loads(capsys.readouterr().out)
    # Empty DB: every ratio is 0/0 → 0, except danger coverage which is
    # vacuously 1.0 (no high fan-in functions to cover) → +10.
    assert doc["overall_score"] == 10
    conn.close()
