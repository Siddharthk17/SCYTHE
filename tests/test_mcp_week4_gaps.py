"""Week 4 gap regression tests — production-grade hardening.

Covers the critical gaps found in the Week 4 strict audit:
- log_change per-file cap must not delete other files' history (P0 data loss)
- folding root target must list only root files
- pruning steps 1+2 forced over budget
- get_callers positive path, get_tainted non-empty with Priority
- server per-call connections, dual create_server signature, ctx_ aliases
- 2-arg handler compatibility (acceptance script form)
"""
import sqlite3
from pathlib import Path

from ctx_engine.db import init_schema
from ctx_engine.mcp_server.tools.assembly import assemble_context, assemble_zone0
from ctx_engine.mcp_server.tools.folding import format_folded_directory_tree
from ctx_engine.mcp_server.tools.read_tools import (
    handle_get_callers,
    handle_get_tainted,
    handle_get_function,
)
from ctx_engine.mcp_server.tools.write_tools import (
    handle_log_change,
    handle_log_session,
    handle_update_function,
    handle_add_danger,
)


def _mem_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    return conn


def test_log_change_cap_preserves_other_files(tmp_path):
    conn = _mem_conn()
    for i in range(5):
        conn.execute(
            "INSERT INTO changes (file, commit_hash, summary, author, timestamp) "
            "VALUES (?,?,?,?,?)",
            ("a.py", f"ha{i}", f"sa{i}", "human", f"2026-01-0{i + 1}T00:00:00Z"),
        )
    for i in range(5):
        conn.execute(
            "INSERT INTO changes (file, commit_hash, summary, author, timestamp) "
            "VALUES (?,?,?,?,?)",
            ("b.py", f"hb{i}", f"sb{i}", "human", f"2026-02-0{i + 1}T00:00:00Z"),
        )
    conn.commit()
    handle_log_change(conn, tmp_path, {"file": "a.py", "summary": "new"})
    a = conn.execute("SELECT COUNT(*) FROM changes WHERE file='a.py'").fetchone()[0]
    b = conn.execute("SELECT COUNT(*) FROM changes WHERE file='b.py'").fetchone()[0]
    assert a == 6
    assert b == 5, f"other file history wiped: b.py={b}"
    conn.close()


def test_log_change_rolling_cap_still_enforced(tmp_path):
    conn = _mem_conn()
    for i in range(25):
        handle_log_change(conn, tmp_path, {"file": "a.py", "summary": f"C{i}"})
    rows = conn.execute(
        "SELECT * FROM changes WHERE file='a.py' ORDER BY timestamp"
    ).fetchall()
    assert len(rows) == 20
    conn.close()


def test_folding_root_target_lists_only_root(tmp_path):
    conn = _mem_conn()
    conn.execute(
        "INSERT INTO files (path, semantic_hash, purpose, content_hash) VALUES (?,?,?,?)",
        ("main.py", "s1", "root", "h1"),
    )
    conn.execute(
        "INSERT INTO files (path, semantic_hash, purpose, content_hash) VALUES (?,?,?,?)",
        ("src/a.py", "s2", "a", "h2"),
    )
    conn.execute(
        "INSERT INTO directories (path, file_count, summary) VALUES (?,?,?)",
        ("src", 1, "src summary"),
    )
    out = format_folded_directory_tree(conn, tmp_path, "main.py")
    assert "main.py (TARGET)" in out
    # nested file must NOT leak into the root active listing
    assert "a.py" not in out.split("src/")[0], out
    assert "src/  [1 files" in out
    conn.close()


def test_pruning_step1_long_to_short(tmp_path):
    conn = _mem_conn()
    conn.execute(
        "INSERT INTO files (path, semantic_hash, purpose, content_hash) VALUES (?,?,?,?)",
        ("big.py", "s", "big", "h"),
    )
    long_text = "L" * 2000
    for i in range(40):
        conn.execute(
            "INSERT INTO functions (id, file, name, signature, summary, summary_long,"
            " line_start, line_end, semantic_hash, confidence, is_stale, is_tainted, mutates)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"big.py:f{i}", "big.py", f"f{i}", f"def f{i}()",
             f"short {i}", long_text, i * 10 + 1, i * 10 + 5,
             f"s{i}", 1.0, 0, 0, "[]"),
        )
    conn.commit()
    full = assemble_zone0(conn, "big.py", None, long_summaries=True)
    short = assemble_zone0(conn, "big.py", None, long_summaries=False)
    assert len(short) < len(full)
    result = assemble_context(conn, tmp_path, "big.py", budget=8000)
    assert len(result) // 4 <= 8000
    conn.close()


def test_pruning_step2_zone1_collapse(tmp_path):
    from ctx_engine.mcp_server.tools.assembly import assemble_zone1
    from ctx_engine.mcp_server.tools.centrality import compute_centrality
    conn = _mem_conn()
    conn.execute(
        "INSERT INTO files (path, semantic_hash, purpose, summary, content_hash,"
        " imports, used_by, used_by_count, exports) VALUES (?,?,?,?,?,?,?,?,?)",
        ("t.py", "s", "t", "target", "h", '["d1.py", "d2.py"]', "[]", 0, "[]"),
    )
    for d in ("d1.py", "d2.py"):
        conn.execute(
            "INSERT INTO files (path, semantic_hash, purpose, summary, content_hash,"
            " imports, used_by, used_by_count, exports) VALUES (?,?,?,?,?,?,?,?,?)",
            (d, "s", f"purpose {d}", f"summary {d}", "h",
             "[]", "[]", 0, '["e1","e2","e3"]'),
        )
    conn.commit()
    centrality = compute_centrality(conn, "t.py")
    long_z1 = assemble_zone1(conn, "t.py", centrality, long_mode=True)
    short_z1 = assemble_zone1(conn, "t.py", centrality, long_mode=False)
    assert len(short_z1) <= len(long_z1)
    assert "FILE:" in short_z1
    conn.close()


def test_get_callers_positive_path(tmp_path):
    conn = _mem_conn()
    conn.execute(
        "INSERT INTO files (path, semantic_hash, purpose, content_hash) VALUES (?,?,?,?)",
        ("a.py", "s", "p", "h"),
    )
    conn.execute(
        "INSERT INTO functions (id, file, name, signature, summary, line_start,"
        " line_end, semantic_hash, confidence, is_stale, is_tainted, mutates)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("a.py:caller", "a.py", "caller", "def caller()",
         "calls callee", 1, 5, "s1", 1.0, 0, 0, "[]"),
    )
    conn.execute(
        "INSERT INTO functions (id, file, name, signature, summary, line_start,"
        " line_end, semantic_hash, confidence, is_stale, is_tainted, mutates)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("a.py:callee", "a.py", "callee", "def callee()",
         "does work", 10, 15, "s2", 1.0, 0, 0, "[]"),
    )
    conn.execute(
        "INSERT INTO call_graph (caller_id, callee_id, callee_name) VALUES (?,?,?)",
        ("a.py:caller", "a.py:callee", "callee"),
    )
    conn.commit()
    out = handle_get_callers(conn, tmp_path, {"function_id": "a.py:callee"})
    assert "a.py:caller" in out
    assert "def caller()" in out
    conn.close()


def test_get_tainted_nonempty_with_priority(tmp_path):
    conn = _mem_conn()
    conn.execute(
        "INSERT INTO files (path, semantic_hash, purpose, content_hash) VALUES (?,?,?,?)",
        ("a.py", "s", "p", "h"),
    )
    conn.execute(
        "INSERT INTO functions (id, file, name, signature, summary, line_start,"
        " line_end, semantic_hash, confidence, is_stale, is_tainted,"
        " taint_source, mutates) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("a.py:f", "a.py", "f", "def f()", "does f",
         1, 5, "s1", 0.9, 0, 1, "a.py:g", "[]"),
    )
    conn.execute(
        "INSERT INTO taint_queue (function_id, taint_source, queued_at, priority)"
        " VALUES (?,?,?,?)",
        ("a.py:f", "a.py:g", "2026-06-14T10:23:45Z", 4),
    )
    conn.commit()
    out = handle_get_tainted(conn, tmp_path, {})
    assert "a.py:f" in out
    assert "a.py:g" in out
    assert "Priority: 4" in out
    out_file = handle_get_tainted(conn, tmp_path, {"file": "a.py"})
    assert "a.py:f" in out_file
    out_other = handle_get_tainted(conn, tmp_path, {"file": "other.py"})
    assert "(none)" in out_other
    conn.close()


def test_get_function_stale_warning_format(tmp_path):
    conn = _mem_conn()
    (tmp_path / "a.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")
    conn.execute(
        "INSERT INTO files (path, semantic_hash, purpose, summary, content_hash,"
        " confidence, is_stale) VALUES (?,?,?,?,?,?,?)",
        ("a.py", "s", "p", "summ", "deadbeef", 1.0, 0),
    )
    conn.execute(
        "INSERT INTO functions (id, file, name, signature, summary, line_start,"
        " line_end, semantic_hash, confidence, is_stale, is_tainted, mutates)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("a.py:add", "a.py", "add", "def add(a, b)", "adds",
         1, 1, "s1", 1.0, 0, 0, "[]"),
    )
    conn.commit()
    out = handle_get_function(conn, tmp_path, {"id": "a.py:add"})
    assert "SOURCE MAY BE STALE" in out
    assert "ctx update a.py" in out
    conn.close()


def test_tainted_warning_uses_spec_glyph(tmp_path):
    conn = _mem_conn()
    conn.execute(
        "INSERT INTO files (path, semantic_hash, purpose, content_hash) VALUES (?,?,?,?)",
        ("a.py", "s", "p", "h"),
    )
    conn.execute(
        "INSERT INTO functions (id, file, name, signature, summary, line_start,"
        " line_end, semantic_hash, confidence, is_stale, is_tainted,"
        " taint_source, mutates) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("a.py:f", "a.py", "f", "def f()", "does f",
         1, 5, "s1", 1.0, 0, 1, "a.py:g", "[]"),
    )
    conn.commit()
    out = assemble_zone0(conn, "a.py", None)
    assert "⚠ TAINTED: a.py:f" in out
    conn.close()


def test_server_signatures_and_aliases(tmp_path):
    from ctx_engine.mcp_server.server import (
        create_server, dispatch_tool, HANDLERS, TOOLS,
        _CTX_ALIASES, READ_TOOL_NAMES, WRITE_TOOL_NAMES,
    )
    # single-arg (legacy)
    app1 = create_server(tmp_path)
    assert getattr(app1, "_ctx_repo_root") == Path(tmp_path)
    assert str(getattr(app1, "_ctx_db_path")).endswith("index.db")
    # spec order (db_path, repo_root)
    db_path = tmp_path / ".ctx" / "index.db"
    app2 = create_server(db_path, tmp_path)
    assert getattr(app2, "_ctx_repo_root") == Path(tmp_path)
    assert getattr(app2, "_ctx_db_path") == Path(db_path)
    # aliases registered
    for alias, canonical in _CTX_ALIASES.items():
        assert alias in HANDLERS
        assert HANDLERS[alias] is HANDLERS[canonical]
    names = {t.name for t in TOOLS}
    assert "get_context" in names and "ctx_get_context" in names
    assert "ctx_get_context" in READ_TOOL_NAMES
    assert "ctx_update_file" in WRITE_TOOL_NAMES
    # dispatch works for both forms
    conn = _mem_conn()
    conn.execute(
        "INSERT INTO files (path, semantic_hash, purpose, content_hash) VALUES (?,?,?,?)",
        ("a.py", "s", "p", "h"),
    )
    conn.commit()
    assert "FILE: a.py" in dispatch_tool("get_context", {"file": "a.py"}, conn, tmp_path)
    assert "FILE: a.py" in dispatch_tool("ctx_get_context", {"file": "a.py"}, conn, tmp_path)
    conn.close()


def test_two_arg_handler_compat(tmp_path):
    # Acceptance-script form: handle_x(conn, {...})
    conn = _mem_conn()
    conn.execute(
        "INSERT INTO files (path, semantic_hash, purpose, summary, content_hash,"
        " confidence, is_stale) VALUES (?,?,?,?,?,?,?)",
        ("a.py", "s", "p", "summ", "h", 0.5, 1),
    )
    conn.execute(
        "INSERT INTO functions (id, file, name, signature, summary, line_start,"
        " line_end, semantic_hash, confidence, is_stale, is_tainted, mutates)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("a.py:f", "a.py", "f", "def f()", "old",
         1, 5, "s1", 0.5, 1, 0, "[]"),
    )
    conn.commit()
    r = handle_update_function(conn, {"id": "a.py:f", "summary": "new via 2-arg"})
    assert "Updated function" in r
    row = conn.execute("SELECT summary, confidence FROM functions WHERE id='a.py:f'").fetchone()
    assert row["summary"] == "new via 2-arg"
    assert row["confidence"] == 1.0
    r2 = handle_log_session(conn, {"entry": "2-arg session"})
    assert "Session log" in r2
    r3 = handle_add_danger(conn, {"scope": "*", "description": "d", "reason": "r"})
    assert "Danger zone added" in r3
    conn.close()
