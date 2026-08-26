"""Tests for the `ctx diff` command (both modes)."""
import subprocess
from pathlib import Path

import pytest

from ctx_engine.commands.diff_cmd import (
    diff_current_vs_indexed,
    diff_between_commits,
    run_diff,
)
from ctx_engine.db import connect
from ctx_engine.commands import run_init


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _ctx(cwd: Path, *args: str) -> None:
    """Run a ctx subcommand with --repo-root set."""
    result = subprocess.run(
        ["ctx", *args, "--repo-root", str(cwd)],
        cwd=cwd, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ctx {args} failed: {result.stderr}")


@pytest.fixture
def git_repo(tmp_path):
    """A git repo with one Python file and an indexed DB."""
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "t@x")
    (tmp_path / "a.py").write_text(
        "def foo():\n    return 1\n\ndef bar():\n    return 2\n",
        encoding="utf-8",
    )
    (tmp_path / "b.py").write_text(
        "def baz():\n    return 3\n",
        encoding="utf-8",
    )
    _git(tmp_path, "add", "a.py", "b.py")
    _git(tmp_path, "commit", "-m", "initial")
    run_init(tmp_path)
    return tmp_path


# ── Mode 1: current vs indexed ────────────────────────────────────────────────


def test_diff_mode1_reports_semantic_change(git_repo):
    (git_repo / "a.py").write_text(
        "def foo():\n    return 42\n\ndef bar():\n    return 2\n",
        encoding="utf-8",
    )
    conn = connect(git_repo / ".ctx" / "index.db")
    try:
        report = diff_current_vs_indexed(conn, git_repo)
    finally:
        conn.close()

    semantic_files = [d for d in report.changed if d.change_type == "semantic"]
    assert any(d.path == "a.py" for d in semantic_files)
    a_diff = next(d for d in semantic_files if d.path == "a.py")
    assert "a.py::foo" in a_diff.changed_functions
    assert "a.py::bar" not in a_diff.changed_functions


def test_diff_mode1_reports_formatting_only(git_repo):
    (git_repo / "a.py").write_text(
        "def foo():\n    return 1\n\n\ndef bar():\n    return 2\n",
        encoding="utf-8",
    )
    conn = connect(git_repo / ".ctx" / "index.db")
    try:
        report = diff_current_vs_indexed(conn, git_repo)
    finally:
        conn.close()
    formatting_files = [d for d in report.changed if d.change_type == "formatting"]
    assert any(d.path == "a.py" for d in formatting_files)


def test_diff_mode1_reports_new_file(git_repo):
    (git_repo / "c.py").write_text("def new_func():\n    pass\n", encoding="utf-8")
    _git(git_repo, "add", "c.py")
    conn = connect(git_repo / ".ctx" / "index.db")
    try:
        report = diff_current_vs_indexed(conn, git_repo)
    finally:
        conn.close()
    assert "c.py" in report.new


def test_diff_mode1_reports_deleted_file(git_repo):
    (git_repo / "b.py").unlink()
    conn = connect(git_repo / ".ctx" / "index.db")
    try:
        report = diff_current_vs_indexed(conn, git_repo)
    finally:
        conn.close()
    assert "b.py" in report.deleted


def test_diff_mode1_no_changes(git_repo):
    conn = connect(git_repo / ".ctx" / "index.db")
    try:
        report = diff_current_vs_indexed(conn, git_repo)
    finally:
        conn.close()
    assert report.changed == []
    assert report.new == []
    assert report.deleted == []


def test_run_diff_mode1_prints_human_readable(git_repo, capsys):
    run_diff(git_repo, None, None)
    out = capsys.readouterr().out
    assert "ctx diff" in out


# ── Mode 2: commit vs commit ─────────────────────────────────────────────────


def test_diff_mode2_author_breakdown(git_repo):
    from datetime import datetime, timezone
    # First commit: human change with log-commit
    (git_repo / "a.py").write_text(
        "def foo():\n    return 100\n\ndef bar():\n    return 2\n",
        encoding="utf-8",
    )
    _git(git_repo, "add", "a.py")
    _git(git_repo, "commit", "-m", "human change")
    _ctx(git_repo, "log-commit")

    # Second commit: human change with log-commit
    (git_repo / "b.py").write_text("def baz():\n    return 99\n", encoding="utf-8")
    _git(git_repo, "add", "b.py")
    _git(git_repo, "commit", "-m", "another human change")
    _ctx(git_repo, "log-commit")

    # Inject a model-authored entry directly into the changes table for HEAD
    conn = connect(git_repo / ".ctx" / "index.db")
    now = datetime.now(timezone.utc).isoformat()
    head = _git(git_repo, "rev-parse", "HEAD")
    conn.execute(
        "INSERT INTO changes (file, commit_hash, summary, author, timestamp) VALUES (?, ?, ?, ?, ?)",
        ("a.py", head, "model change", "model", now),
    )
    conn.commit()
    conn.close()

    conn = connect(git_repo / ".ctx" / "index.db")
    try:
        report = diff_between_commits(conn, git_repo, "HEAD~2", "HEAD")
    finally:
        conn.close()
    assert "human" in report.by_author or "model" in report.by_author


def test_diff_mode2_lists_unrecorded_commits(git_repo):
    # First commit: log it
    (git_repo / "a.py").write_text(
        "def foo():\n    return 11\n\ndef bar():\n    return 2\n",
        encoding="utf-8",
    )
    _git(git_repo, "add", "a.py")
    _git(git_repo, "commit", "-m", "first")
    _ctx(git_repo, "log-commit")

    # Second commit: do NOT log
    (git_repo / "a.py").write_text(
        "def foo():\n    return 22\n\ndef bar():\n    return 2\n",
        encoding="utf-8",
    )
    _git(git_repo, "add", "a.py")
    _git(git_repo, "commit", "-m", "second (unlogged)")

    conn = connect(git_repo / ".ctx" / "index.db")
    try:
        report = diff_between_commits(conn, git_repo, "HEAD~1", "HEAD")
    finally:
        conn.close()
    assert len(report.not_recorded) >= 1


def test_run_diff_invalid_args(git_repo):
    """One commit argument is a usage error."""
    with pytest.raises(ValueError):
        run_diff(git_repo, "only-one", None)
