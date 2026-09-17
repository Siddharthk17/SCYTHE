"""Week 2 TASK 2 prompt-construction contract.

Verifies per-function needs_summary/source/taint_warning fields built by
get_summarize_selection: stale-or-NULL -> needs_summary True + source present;
fresh-with-summary -> False + no source; tainted -> non-null warning.
"""
import sqlite3

from ctx_engine.commands.summarize import get_summarize_selection
from ctx_engine.db import init_schema


def _seed(tmp_path):
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    # Stale file with one stale fn (no summary) and one fresh fn (has summary)
    conn.execute(
        "INSERT INTO files (path, purpose, summary, semantic_hash, content_hash, "
        "exports, imports, used_by_count, is_stale) "
        "VALUES ('mod.py', NULL, NULL, 'h1', 'c1', '[]', '[]', 0, 1)"
    )
    conn.execute(
        "INSERT INTO functions (id, file, name, signature, line_start, line_end, "
        "semantic_hash, summary, is_stale, is_tainted, taint_source, mutates) "
        "VALUES ('mod.py::stale_fn', 'mod.py', 'stale_fn', 'def stale_fn()', 1, 2, "
        "'hs', NULL, 1, 0, NULL, '[]')"
    )
    conn.execute(
        "INSERT INTO functions (id, file, name, signature, line_start, line_end, "
        "semantic_hash, summary, is_stale, is_tainted, taint_source, mutates) "
        "VALUES ('mod.py::fresh_fn', 'mod.py', 'fresh_fn', 'def fresh_fn()', 3, 4, "
        "'hf', 'does fresh thing', 0, 0, NULL, '[]')"
    )
    # Taint-only file: fresh file row but one tainted fn
    conn.execute(
        "INSERT INTO files (path, purpose, summary, semantic_hash, content_hash, "
        "exports, imports, used_by_count, is_stale) "
        "VALUES ('tainted.py', 'has purpose', 's', 'h2', 'c2', '[]', '[]', 0, 0)"
    )
    conn.execute(
        "INSERT INTO functions (id, file, name, signature, line_start, line_end, "
        "semantic_hash, summary, is_stale, is_tainted, taint_source, mutates) "
        "VALUES ('tainted.py::dep', 'tainted.py', 'dep', 'def dep()', 1, 2, "
        "'hd', 'does dep', 0, 1, 'mod.py::stale_fn', '[]')"
    )
    conn.commit()
    # real source files so get_function_source returns non-empty
    (tmp_path / "mod.py").write_text("def stale_fn():\n    return 1\ndef fresh_fn():\n    return 2\n")
    (tmp_path / "tainted.py").write_text("def dep():\n    return 3\n")
    return db_path


def test_stale_gets_source_fresh_does_not(tmp_path):
    db_path = _seed(tmp_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    files_data, total, _ = get_summarize_selection(conn, tmp_path)
    conn.close()
    mod = next(f for f in files_data if f["path"] == "mod.py")
    by_id = {fn["id"]: fn for fn in mod["functions"]}
    stale = by_id["mod.py::stale_fn"]
    fresh = by_id["mod.py::fresh_fn"]
    assert stale["needs_summary"] is True
    assert "source" in stale and stale["source"] != ""
    assert stale["taint_warning"] is None
    assert fresh["needs_summary"] is False
    assert "source" not in fresh
    assert fresh["current_summary"] == "does fresh thing"
    assert total >= 1


def test_tainted_gets_warning_and_source_in_taint_only_file(tmp_path):
    db_path = _seed(tmp_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    files_data, _, _ = get_summarize_selection(conn, tmp_path)
    conn.close()
    tainted_file = next(f for f in files_data if f["path"] == "tainted.py")
    # taint-only files must not trigger file-level rewrite
    assert tainted_file["purpose_needs_update"] is False
    dep = next(fn for fn in tainted_file["functions"] if fn["id"] == "tainted.py::dep")
    assert dep["needs_summary"] is True
    assert dep["taint_warning"] is not None
    assert "stale_fn" in dep["taint_warning"]
    assert "mod.py" in dep["taint_warning"]
    assert "source" in dep and dep["source"] != ""


def test_force_marks_everything_needs_summary(tmp_path):
    db_path = _seed(tmp_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    files_data, _, _ = get_summarize_selection(conn, tmp_path, force=True)
    conn.close()
    for f in files_data:
        for fn in f["functions"]:
            assert fn["needs_summary"] is True
            assert "source" in fn
