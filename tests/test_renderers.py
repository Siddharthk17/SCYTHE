import sqlite3
import pytest
from pathlib import Path
from ctx_engine.db import init_schema
from ctx_engine.mcp_server.tools.renderers import (
    extract_project_snapshot,
    render_claude_md,
    render_copilot_instructions,
    render_opencode_config,
    render_generation_timestamp,
)


@pytest.fixture
def render_db(tmp_path):
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    (tmp_path / "a.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def sub(a, b): return a - b\n", encoding="utf-8")

    conn.execute(
        "INSERT OR IGNORE INTO files (path, semantic_hash, content_hash, purpose, summary, is_stale, confidence, danger, exports, imports, used_by, used_by_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("a.py", "sh1", "ch1", "Test file A", "File A", 0, 0.9, None, '["add"]', "[]", "[]", 0),
    )
    conn.execute(
        "INSERT OR IGNORE INTO files (path, semantic_hash, content_hash, purpose, summary, is_stale, confidence, danger, exports, imports, used_by, used_by_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("b.py", "sh2", "ch2", "Test file B", "File B", 0, 0.8, None, '["sub"]', '["a"]', "[]", 0),
    )
    conn.execute(
        "INSERT OR IGNORE INTO functions (id, file, name, signature, summary, summary_long, line_start, line_end, semantic_hash, confidence, is_stale, is_tainted, mutates, danger) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("a.py:add", "a.py", "add", "def add(a, b)", "Adds two numbers", None, 1, 1, "sf1", 0.8, 0, 0, "[]", None),
    )
    conn.execute(
        "INSERT OR IGNORE INTO functions (id, file, name, signature, summary, summary_long, line_start, line_end, semantic_hash, confidence, is_stale, is_tainted, mutates, danger) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("b.py:sub", "b.py", "sub", "def sub(a, b)", "Subtracts numbers", None, 1, 1, "sf2", 0.7, 0, 0, "[]", None),
    )
    conn.execute(
        "INSERT OR IGNORE INTO call_graph (caller_id, callee_name, callee_id, is_ambiguous) "
        "VALUES (?, ?, ?, ?)",
        ("b.py:sub", "add", "a.py:add", 0),
    )
    conn.execute(
        "INSERT OR IGNORE INTO dangers (id, scope, description, reason, added_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("d-001", "a.py", "No error handling", "Missing try/except", "human", "2026-07-01T00:00:00Z"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO decisions (id, scope, decision, alternatives, reason, added_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("dec-001", "a.py", "Use plain functions", "Classes", "Simplicity", "human", "2026-07-01T00:00:00Z"),
    )
    conn.commit()
    return conn, tmp_path


def test_extract_project_snapshot(render_db):
    conn, repo = render_db
    snap = extract_project_snapshot(conn, repo)
    assert snap.repo_name == repo.name
    assert len(snap.systems) >= 1
    all_files = [f for s in snap.systems for f in s.files]
    assert len(all_files) == 2
    assert snap.global_dangers is not None
    assert snap.decisions is not None


def test_render_claude_md(render_db):
    conn, repo = render_db
    snap = extract_project_snapshot(conn, repo)
    md = render_claude_md(snap)
    assert isinstance(md, str)
    assert len(md) > 50
    assert repo.name in md
    assert "a.py" in md or "Test file A" in md
    assert "## Architecture" in md
    assert "## Danger Zones" in md
    assert "## Architectural Decisions" in md
    assert "## Index Health" in md
    assert "## ctx Workflow" in md
    assert "## Index Health" in md


def test_render_copilot_instructions(render_db):
    conn, repo = render_db
    snap = extract_project_snapshot(conn, repo)
    md = render_copilot_instructions(snap)
    assert isinstance(md, str)
    assert len(md) > 20


def test_render_opencode_config(render_db):
    conn, repo = render_db
    snap = extract_project_snapshot(conn, repo)
    md = render_opencode_config(snap)
    assert isinstance(md, str)
    assert len(md) > 20
    assert "# OpenCode Context" in md
    assert "workflow:" in md


def test_render_generation_timestamp(render_db):
    conn, repo = render_db
    snap = extract_project_snapshot(conn, repo)
    md = render_claude_md(snap)
    out_path = repo / "CLAUDE.md"
    out_path.write_text(md, encoding="utf-8")
    ts = render_generation_timestamp(out_path)
    assert ts is not None
    assert "T" in ts


def test_render_generation_timestamp_comment_prefix(tmp_path):
    """opencode.md stores its timestamp behind a '# ' prefix — still parseable."""
    f = tmp_path / "opencode.md"
    f.write_text(
        "# <!-- Generated: 2026-06-14T14:05:14Z -->\n",
        encoding="utf-8",
    )
    assert render_generation_timestamp(f) == "2026-06-14T14:05:14Z"


def test_renderers_byte_identical_with_fixed_timestamp(render_db):
    """With a fixed generation timestamp, output is byte-identical every call."""
    conn, repo = render_db
    snap = extract_project_snapshot(conn, repo)
    fixed = "2026-06-14T14:05:14Z"
    assert render_claude_md(snap, gen_ts=fixed) == render_claude_md(snap, gen_ts=fixed)
    assert render_copilot_instructions(snap, gen_ts=fixed) == render_copilot_instructions(snap, gen_ts=fixed)
    assert render_opencode_config(snap, gen_ts=fixed) == render_opencode_config(snap, gen_ts=fixed)


def test_render_generation_timestamp_missing(tmp_path):
    ts = render_generation_timestamp(tmp_path / "nonexistent.md")
    assert ts is None


def test_render_generation_timestamp_no_marker(tmp_path):
    f = tmp_path / "no_ts.md"
    f.write_text("hello world", encoding="utf-8")
    ts = render_generation_timestamp(f)
    assert ts is None


def test_extract_snapshot_empty_db(tmp_path):
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    snap = extract_project_snapshot(conn, tmp_path)
    assert snap.systems == []
    assert list(snap.global_dangers) == []
    assert list(snap.decisions) == []
    render_claude_md(snap)
    render_copilot_instructions(snap)
    render_opencode_config(snap)


def test_renderers_deterministic(render_db):
    conn, repo = render_db
    snap = extract_project_snapshot(conn, repo)
    a = render_claude_md(snap)
    b = render_claude_md(snap)
    assert a == b
    c = render_copilot_instructions(snap)
    d = render_copilot_instructions(snap)
    assert c == d
    e = render_opencode_config(snap)
    f = render_opencode_config(snap)
    assert e == f


@pytest.fixture
def stale_render_db(tmp_path):
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    conn.execute(
        "INSERT OR IGNORE INTO files (path, semantic_hash, content_hash, purpose, summary, is_stale, confidence, danger, exports, imports, used_by, used_by_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("stale.py", "sh1", "ch1", "Stale file", "Stale", 1, 0.9, None, '["f"]', "[]", "[]", 0),
    )
    conn.execute(
        "INSERT OR IGNORE INTO functions (id, file, name, signature, summary, summary_long, line_start, line_end, semantic_hash, confidence, is_stale, is_tainted, mutates, danger) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("stale.py:f", "stale.py", "f", "def f()", "A function", None, 1, 1, "sf1", 0.8, 1, 1, "[]", None),
    )
    conn.commit()
    return conn, tmp_path


def test_render_claude_md_stale_warning(stale_render_db):
    conn, repo = stale_render_db
    snap = extract_project_snapshot(conn, repo)
    md = render_claude_md(snap)
    assert "⚠" in md
    assert "run `ctx sync`" in md


def test_render_claude_md_all_current(render_db):
    conn, repo = render_db
    snap = extract_project_snapshot(conn, repo)
    md = render_claude_md(snap)
    assert "✓ Index is current" in md


def test_render_copilot_shorter_than_claude(render_db):
    conn, repo = render_db
    snap = extract_project_snapshot(conn, repo)
    claude = render_claude_md(snap)
    copilot = render_copilot_instructions(snap)
    assert len(copilot) < len(claude)
