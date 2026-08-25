"""Tests for `ctx doctor` health-check command."""
import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from ctx_engine.commands.doctor import (
    _collect_checks,
    run_doctor,
    CheckResult,
    EXPECTED_PERF_INDICES,
)
from ctx_engine.commands.install_hooks import POST_COMMIT_HOOK, PRE_COMMIT_HOOK
from ctx_engine.db import init_schema


@pytest.fixture
def git_repo(tmp_path):
    """Create a temp directory initialized as a git repo with user config."""
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True, check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "t@x"], cwd=tmp_path, capture_output=True, check=True
    )
    return tmp_path


def _make_indexed_repo(git_repo: Path) -> None:
    """Create a populated .ctx/index.db on the test repo."""
    (git_repo / ".ctx").mkdir(exist_ok=True)
    db_path = git_repo / ".ctx" / "index.db"
    conn = sqlite3.connect(db_path)
    init_schema(conn)
    conn.execute(
        "INSERT INTO files (path, semantic_hash, content_hash, purpose, summary, exports, mtime, file_size) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("a.py", "h", "c", "test", "t", "[]", 1.0, 100),
    )
    conn.execute(
        "INSERT INTO functions (id, file, name, signature, line_start, line_end, semantic_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("a.py::run", "a.py", "run", "def run()", 1, 2, "sh"),
    )
    conn.commit()
    conn.close()


def test_doctor_collect_runs_all_categories(git_repo):
    """All checks should be produced; no crashes on an empty (but valid) repo."""
    _make_indexed_repo(git_repo)
    checks = _collect_checks(git_repo)
    # 7 A + 5 B + 7 C + 4 D + 3 E + 6 F + 4 G = 36
    assert len(checks) == 36
    categories = {c.category for c in checks}
    assert "Prerequisites" in categories
    assert "Schema" in categories
    assert "Index Health" in categories
    assert "API and Models" in categories
    assert "Git Hooks" in categories
    assert "Output Files" in categories
    assert "Performance" in categories


def test_doctor_a3_fails_when_db_missing(git_repo):
    """Check A3 (DB exists) fails when .ctx/index.db is removed."""
    _make_indexed_repo(git_repo)
    (git_repo / ".ctx" / "index.db").unlink()
    checks = _collect_checks(git_repo)
    a3 = next(c for c in checks if c.id == "A3")
    assert a3.passed is False
    assert a3.auto_fix is False  # must run ctx init


def test_doctor_b4_fails_when_indices_missing(git_repo):
    """Check B4 fails when performance indices are missing."""
    _make_indexed_repo(git_repo)
    # Drop all performance indices
    db = git_repo / ".ctx" / "index.db"
    conn = sqlite3.connect(db)
    for name in EXPECTED_PERF_INDICES:
        conn.execute(f"DROP INDEX IF EXISTS {name}")
    conn.commit()
    conn.close()

    checks = _collect_checks(git_repo)
    b4 = next(c for c in checks if c.id == "B4")
    assert b4.passed is False
    assert b4.auto_fix is True


def test_doctor_b4_fix_creates_indices(git_repo):
    """--fix recreates missing performance indices."""
    _make_indexed_repo(git_repo)
    db = git_repo / ".ctx" / "index.db"
    conn = sqlite3.connect(db)
    for name in EXPECTED_PERF_INDICES:
        conn.execute(f"DROP INDEX IF EXISTS {name}")
    conn.commit()
    conn.close()

    run_doctor(git_repo, apply_fix=True, json_output=True)
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
    ).fetchall()
    present = {r[0] for r in rows}
    conn.close()
    assert set(EXPECTED_PERF_INDICES).issubset(present)


def test_doctor_c2_fails_when_purpose_null(git_repo):
    """Check C2 (no null purposes) fails when a file has purpose = NULL."""
    _make_indexed_repo(git_repo)
    db = git_repo / ".ctx" / "index.db"
    conn = sqlite3.connect(db)
    conn.execute("UPDATE files SET purpose = NULL WHERE path = 'a.py'")
    conn.commit()
    conn.close()

    checks = _collect_checks(git_repo)
    c2 = next(c for c in checks if c.id == "C2")
    assert c2.passed is False
    assert c2.auto_fix is False  # needs LLM, manual


def test_doctor_e1_fails_when_hook_missing(git_repo):
    """Check E1 fails when the pre-commit hook doesn't exist."""
    _make_indexed_repo(git_repo)
    # Make sure hooks dir is clean
    hooks = git_repo / ".git" / "hooks"
    if hooks.exists():
        for f in hooks.iterdir():
            if f.is_file():
                f.unlink()

    checks = _collect_checks(git_repo)
    e1 = next(c for c in checks if c.id == "E1")
    assert e1.passed is False
    assert e1.auto_fix is True


def test_doctor_e1_fix_installs_hook(git_repo):
    """--fix runs ctx install-hooks and resolves E1/E2/E3."""
    _make_indexed_repo(git_repo)
    hooks = git_repo / ".git" / "hooks"
    if hooks.exists():
        for f in hooks.iterdir():
            if f.is_file():
                f.unlink()

    run_doctor(git_repo, apply_fix=True, json_output=True)
    pre = (git_repo / ".git" / "hooks" / "pre-commit").read_text()
    assert "ctx validate" in pre
    post = (git_repo / ".git" / "hooks" / "post-commit").read_text()
    assert "ctx log-commit" in post


def test_doctor_f2_fix_runs_export(git_repo):
    """--fix runs ctx export when CLAUDE.md is missing/stale."""
    _make_indexed_repo(git_repo)
    run_doctor(git_repo, apply_fix=True, json_output=True)
    assert (git_repo / "CLAUDE.md").exists()
    assert (git_repo / ".github" / "copilot-instructions.md").exists()
    assert (git_repo / ".ctx" / "opencode.md").exists()


def test_doctor_json_output_is_valid(git_repo):
    """--json output is valid JSON matching the documented schema."""
    _make_indexed_repo(git_repo)
    import io
    import sys

    captured = io.StringIO()
    saved_stdout = sys.stdout
    sys.stdout = captured
    try:
        run_doctor(git_repo, apply_fix=False, json_output=True)
    finally:
        sys.stdout = saved_stdout
    payload = json.loads(captured.getvalue())
    for key in ("timestamp", "repo", "total_checks", "passed", "failed", "auto_fixable", "checks"):
        assert key in payload
    for c in payload["checks"]:
        assert "id" in c
        assert "category" in c
        assert "name" in c
        assert "passed" in c
        assert "auto_fix" in c


def test_doctor_idempotent_fix(git_repo):
    """Running --fix twice produces no changes on the second run."""
    _make_indexed_repo(git_repo)
    # First run: apply everything
    run_doctor(git_repo, apply_fix=True, json_output=True)
    # Second run: should be a no-op
    run_doctor(git_repo, apply_fix=True, json_output=True)
    # Nothing should have crashed
    assert (git_repo / "CLAUDE.md").exists()


def test_doctor_anthropic_key_set(monkeypatch, git_repo):
    """D1 passes when ANTHROPIC_API_KEY is set."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    _make_indexed_repo(git_repo)
    checks = _collect_checks(git_repo)
    d1 = next(c for c in checks if c.id == "D1")
    assert d1.passed is True


def test_doctor_anthropic_key_unset(monkeypatch, git_repo):
    """D1 fails when ANTHROPIC_API_KEY is not set."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _make_indexed_repo(git_repo)
    checks = _collect_checks(git_repo)
    d1 = next(c for c in checks if c.id == "D1")
    assert d1.passed is False


def test_doctor_human_readable_summary(git_repo, capsys):
    """Human-readable output mentions key summary sections."""
    _make_indexed_repo(git_repo)
    run_doctor(git_repo, apply_fix=False, json_output=False)
    out = capsys.readouterr().out
    assert "ctx doctor" in out
    assert "Prerequisites" in out
    assert "Schema" in out
    assert "Index Health" in out


def test_doctor_call_graph_consistency_check(git_repo):
    """C7 detects and reports dangling call_graph rows."""
    _make_indexed_repo(git_repo)
    db = git_repo / ".ctx" / "index.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO call_graph (caller_id, callee_id, callee_name, callee_file) "
        "VALUES (?, ?, ?, ?)",
        ("a.py::run", "nonexistent::func", "func", "nonexistent.py"),
    )
    conn.commit()
    conn.close()

    checks = _collect_checks(git_repo)
    c7 = next(c for c in checks if c.id == "C7")
    assert c7.passed is False
    assert c7.auto_fix is True
