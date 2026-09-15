"""Tests for ctx impact (Week 9)."""
import json
import sqlite3

import pytest

from ctx_engine.commands.impact_cmd import (
    compute_transitive_impact,
    group_by_severity,
    render_mermaid_impact,
    report_to_json,
    resolve_impact_targets,
    run_impact,
)
from ctx_engine.db import init_schema


@pytest.fixture
def impact_db(tmp_path):
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    def add_file(path, system="misc", used_by=None):
        conn.execute(
            "INSERT INTO files (path, system, semantic_hash, content_hash, "
            "exports, imports, used_by, used_by_count, is_stale) "
            "VALUES (?, ?, 'sh', 'ch', '[]', '[]', ?, 0, 0)",
            (path, system, json.dumps(used_by or [])),
        )

    def add_func(fid, path, name="f"):
        conn.execute(
            "INSERT INTO functions (id, file, name, signature, line_start, "
            "line_end, semantic_hash) VALUES (?, ?, ?, 'sig', 1, 5, 'sh')",
            (fid, path, name),
        )

    def add_edge(caller, callee, name):
        conn.execute(
            "INSERT INTO call_graph (caller_id, callee_id, callee_name) "
            "VALUES (?, ?, ?)",
            (caller, callee, name),
        )

    yield conn, tmp_path, add_file, add_func, add_edge
    conn.close()


def _chain(impact_db):
    conn, root, add_file, add_func, add_edge = impact_db
    add_file("c.py", system="search")
    add_file("b.py", system="search")
    add_file("a.py", system="training")
    add_func("c.py::C", "c.py", "C")
    add_func("b.py::B", "b.py", "B")
    add_func("a.py::A", "a.py", "A")
    add_edge("b.py::B", "c.py::C", "C")
    add_edge("a.py::A", "b.py::B", "B")
    conn.commit()
    return conn, root


def test_single_hop_critical(impact_db):
    conn, _, add_file, add_func, add_edge = impact_db
    add_file("b.py")
    add_file("a.py")
    add_func("b.py::B", "b.py", "B")
    add_func("a.py::A", "a.py", "A")
    add_edge("a.py::A", "b.py::B", "B")
    conn.commit()

    report = compute_transitive_impact(conn, ["b.py::B"])
    assert report.affected_functions["b.py::B"] == 0
    assert report.affected_functions["a.py::A"] == 1
    assert "a.py" in report.affected_files
    assert "b.py" in report.affected_files
    groups = group_by_severity(report)
    assert [f for f, _ in groups["critical"]] == ["a.py::A"]
    assert groups["major"] == [] and groups["minor"] == []


def test_two_hop_major(impact_db):
    conn, _ = _chain(impact_db)
    report = compute_transitive_impact(conn, ["c.py::C"])
    assert report.affected_functions["b.py::B"] == 1
    assert report.affected_functions["a.py::A"] == 2
    groups = group_by_severity(report)
    assert [f for f, _ in groups["critical"]] == ["b.py::B"]
    assert [f for f, _ in groups["major"]] == ["a.py::A"]


def test_diamond_graph(impact_db):
    conn, _, add_file, add_func, add_edge = impact_db
    for p in ("c.py", "a.py", "b.py", "d.py"):
        add_file(p)
    add_func("c.py::C", "c.py", "C")
    add_func("a.py::A", "a.py", "A")
    add_func("b.py::B", "b.py", "B")
    add_func("d.py::D", "d.py", "D")
    add_edge("a.py::A", "c.py::C", "C")
    add_edge("b.py::B", "c.py::C", "C")
    add_edge("d.py::D", "a.py::A", "A")
    add_edge("d.py::D", "b.py::B", "B")
    conn.commit()

    report = compute_transitive_impact(conn, ["c.py::C"])
    groups = group_by_severity(report)
    assert sorted(f for f, _ in groups["critical"]) == ["a.py::A", "b.py::B"]
    assert [f for f, _ in groups["major"]] == ["d.py::D"]


def test_depth_flag_limits_traversal(impact_db):
    conn, _ = _chain(impact_db)
    report = compute_transitive_impact(conn, ["c.py::C"], max_depth=1)
    assert "b.py::B" in report.affected_functions
    assert "a.py::A" not in report.affected_functions
    groups = group_by_severity(report)
    assert groups["major"] == [] and groups["minor"] == []


def test_depth_zero_returns_only_target(impact_db):
    conn, _ = _chain(impact_db)
    report = compute_transitive_impact(conn, ["c.py::C"], max_depth=0)
    assert report.affected_functions == {"c.py::C": 0}
    assert group_by_severity(report) == {
        "critical": [], "major": [], "minor": [],
    }


def test_no_callers_empty_blast_radius(impact_db, capsys):
    conn, root, add_file, add_func, _ = impact_db
    add_file("solo.py")
    add_func("solo.py::S", "solo.py", "S")
    conn.commit()
    run_impact(root, "solo.py::S")
    out = capsys.readouterr().out
    assert "no dependent functions found" in out


def test_file_target_unions_functions(impact_db):
    conn, _, add_file, add_func, add_edge = impact_db
    add_file("lib.py")
    add_file("user.py")
    add_func("lib.py::F1", "lib.py", "F1")
    add_func("lib.py::F2", "lib.py", "F2")
    add_func("user.py::U", "user.py", "U")
    add_edge("user.py::U", "lib.py::F1", "F1")
    conn.commit()

    ids, label = resolve_impact_targets(conn, "lib.py")
    assert sorted(ids) == ["lib.py::F1", "lib.py::F2"]
    report = compute_transitive_impact(conn, ids)
    assert "user.py::U" in report.affected_functions


def test_system_boundary_crossing(impact_db):
    conn, _ = _chain(impact_db)
    report = compute_transitive_impact(conn, ["c.py::C"])
    payload = report_to_json(conn, report)
    assert payload["system_crossings"] == [
        {"from": "search", "to": "training", "via": "a.py::A"}
    ]


def test_mermaid_classes(impact_db):
    conn, _ = _chain(impact_db)
    report = compute_transitive_impact(conn, ["c.py::C"])
    report.target_label = "c.py::C"
    out = render_mermaid_impact(conn, report)
    assert out.startswith("```mermaid")
    assert "graph TD" in out
    assert "c_py__C:::target" in out
    assert "b_py__B:::critical" in out
    assert "a_py__A:::major" in out


def test_json_depths(impact_db):
    conn, _ = _chain(impact_db)
    report = compute_transitive_impact(conn, ["c.py::C"])
    report.target_label = "c.py::C"
    payload = report_to_json(conn, report)
    assert payload["affected_functions"] == {
        "c.py::C": 0, "b.py::B": 1, "a.py::A": 2,
    }
    assert payload["summary"]["critical"] == 1
    assert payload["summary"]["major"] == 1


def test_circular_graph_terminates(impact_db):
    conn, _, add_file, add_func, add_edge = impact_db
    add_file("a.py")
    add_file("b.py")
    add_func("a.py::A", "a.py", "A")
    add_func("b.py::B", "b.py", "B")
    add_edge("a.py::A", "b.py::B", "B")
    add_edge("b.py::B", "a.py::A", "A")
    conn.commit()

    report = compute_transitive_impact(conn, ["a.py::A"])
    assert report.affected_functions == {"a.py::A": 0, "b.py::B": 1}


def test_unknown_target_raises(impact_db):
    conn, _, _, _, _ = impact_db
    with pytest.raises(ValueError, match="Target not found"):
        resolve_impact_targets(conn, "nope.py::Missing")
