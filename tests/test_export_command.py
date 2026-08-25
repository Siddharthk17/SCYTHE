import sqlite3
import time
import pytest
from datetime import datetime, timezone
from pathlib import Path
from ctx_engine.db import init_schema
from ctx_engine.commands.export_cmd import run_export, _ensure_gitattributes
from ctx_engine.mcp_server.tools.renderers import render_generation_timestamp


@pytest.fixture
def export_db(tmp_path):
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    (tmp_path / "a.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")

    conn.execute(
        "INSERT OR IGNORE INTO files (path, semantic_hash, content_hash, purpose, summary, is_stale, confidence, danger, exports, imports, used_by, used_by_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("a.py", "sh1", "ch1", "Test file A", "File A", 0, 0.9, None, '["add"]', "[]", "[]", 0),
    )
    conn.execute(
        "INSERT OR IGNORE INTO functions (id, file, name, signature, summary, summary_long, line_start, line_end, semantic_hash, confidence, is_stale, is_tainted, mutates, danger) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("a.py:add", "a.py", "add", "def add(a, b)", "Adds two numbers", None, 1, 1, "sf1", 0.8, 0, 0, "[]", None),
    )
    conn.commit()
    return conn, tmp_path


def test_run_export_writes_all(export_db):
    conn, repo = export_db
    report = run_export(conn, repo)
    assert (repo / "CLAUDE.md").exists()
    assert (repo / ".github" / "copilot-instructions.md").exists()
    assert (repo / ".ctx" / "opencode.md").exists()
    assert len(report.written) >= 2
    for path in report.written:
        full = repo / path
        assert full.exists()
        assert full.read_text(encoding="utf-8").strip()


def test_run_export_targeted(export_db):
    conn, repo = export_db
    report = run_export(conn, repo, targets={"claude"})
    assert (repo / "CLAUDE.md").exists()
    assert not (repo / ".github" / "copilot-instructions.md").exists()
    assert not (repo / ".ctx" / "opencode.md").exists()
    assert report.written == ["CLAUDE.md"]


def test_run_export_skips_unchanged(export_db):
    conn, repo = export_db
    r1 = run_export(conn, repo)
    assert len(r1.written) > 0
    r2 = run_export(conn, repo)
    assert len(r2.skipped) >= len(r2.written) or len(r2.written) == 0


def test_run_export_copilot_only(export_db):
    conn, repo = export_db
    report = run_export(conn, repo, targets={"copilot"})
    assert (repo / ".github" / "copilot-instructions.md").exists()
    assert report.written == [".github/copilot-instructions.md"]


def test_run_export_opencode_only(export_db):
    conn, repo = export_db
    report = run_export(conn, repo, targets={"opencode"})
    assert (repo / ".ctx" / "opencode.md").exists()
    assert report.written == [".ctx/opencode.md"]


def test_ensure_gitattributes(tmp_path):
    _ensure_gitattributes(tmp_path)
    ga = tmp_path / ".gitattributes"
    assert ga.exists()
    content = ga.read_text(encoding="utf-8")
    assert "CLAUDE.md" in content
    assert "copilot-instructions.md" in content
    assert "opencode.md" in content


def test_ensure_gitattributes_idempotent(tmp_path):
    _ensure_gitattributes(tmp_path)
    _ensure_gitattributes(tmp_path)
    ga = tmp_path / ".gitattributes"
    lines = [l.strip() for l in ga.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
    claude_lines = [l for l in lines if "CLAUDE.md" in l]
    assert len(claude_lines) == 1


def test_run_export_all_targets(export_db):
    conn, repo = export_db
    report = run_export(conn, repo, targets=None)
    assert (repo / "CLAUDE.md").exists()
    assert (repo / ".github" / "copilot-instructions.md").exists()
    assert (repo / ".ctx" / "opencode.md").exists()
    assert len(report.written) == 3


def test_run_export_empty_db(tmp_path):
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    report = run_export(conn, tmp_path)
    assert (tmp_path / "CLAUDE.md").exists()


def test_export_rewrites_after_db_change(export_db):
    conn, repo = export_db
    r1 = run_export(conn, repo)
    assert len(r1.written) == 3

    time.sleep(1)

    conn.execute(
        "UPDATE files SET updated_at = ? WHERE path = 'a.py'",
        (datetime.now(timezone.utc).isoformat(),),
    )
    conn.commit()

    r2 = run_export(conn, repo)
    assert "CLAUDE.md" in r2.written
    assert "CLAUDE.md" not in r2.skipped


def test_run_export_idempotent_across_seconds(export_db):
    """Re-running export after >1s with no DB change must not rewrite files."""
    conn, repo = export_db
    r1 = run_export(conn, repo)
    assert len(r1.written) == 3

    mtime_before = {p: (repo / p).stat().st_mtime_ns for p in r1.written}

    time.sleep(1.1)

    r2 = run_export(conn, repo)
    assert r2.written == []
    assert len(r2.skipped) == 3

    for p, mtime in mtime_before.items():
        assert (repo / p).stat().st_mtime_ns == mtime, f"{p} was rewritten"


def test_run_export_overwrites_manual_edit(export_db):
    """Manually edited output artifacts are regenerated on the next export."""
    conn, repo = export_db
    run_export(conn, repo)

    claude = repo / "CLAUDE.md"
    claude.write_text(
        claude.read_text(encoding="utf-8") + "\nMANUAL EDIT\n",
        encoding="utf-8",
    )

    time.sleep(1.1)
    report = run_export(conn, repo)
    assert "CLAUDE.md" in report.written
    assert "MANUAL EDIT" not in claude.read_text(encoding="utf-8")


def test_run_export_writes_parseable_timestamps(export_db):
    """Every rendered file carries a freshness-detectable generation timestamp."""
    conn, repo = export_db
    run_export(conn, repo)
    for rel in ("CLAUDE.md", ".github/copilot-instructions.md", ".ctx/opencode.md"):
        ts = render_generation_timestamp(repo / rel)
        assert ts is not None, f"no parseable timestamp in {rel}"
        datetime.fromisoformat(ts.replace("Z", "+00:00"))
