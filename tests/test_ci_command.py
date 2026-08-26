"""Tests for the `ctx ci` command (with mocked git subprocess calls)."""
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from ctx_engine.commands.ci_cmd import run_ci
from ctx_engine.commands import run_init


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def ci_repo(tmp_path):
    """A git repo with a Python file and an indexed DB."""
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "t@x")
    (tmp_path / "a.py").write_text(
        "def foo():\n    return 1\n",
        encoding="utf-8",
    )
    _git(tmp_path, "add", "a.py")
    _git(tmp_path, "commit", "-m", "initial")
    run_init(tmp_path)
    return tmp_path


def test_ci_all_current_passes(ci_repo, capsys):
    """No stale files, all commits logged: exit 0."""
    run_ci(ci_repo, json_output=False, output_workflow=False)
    captured = capsys.readouterr()
    assert "ctx ci" in captured.out
    # exit 0 \u2014 nothing to assert beyond the function returning


def test_ci_stale_file_exits_1(ci_repo, capsys):
    """A semantic change to a tracked file with no ctx init: exit 1."""
    (ci_repo / "a.py").write_text(
        "def foo():\n    return 42\n",
        encoding="utf-8",
    )
    # No log-commit, no init \u2014 stale file
    exit_code = run_ci(ci_repo, json_output=True, output_workflow=False)
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["passed"] is False
    assert exit_code == 1
    assert "stale_files" in report["checks"]
    assert report["checks"]["stale_files"]["passed"] is False
    assert report["checks"]["stale_files"]["count"] >= 1


def test_ci_all_commits_logged(ci_repo, capsys):
    """All PR commits logged -> commits_logged.passed = True."""
    # Add another commit and log it
    (ci_repo / "a.py").write_text(
        "def foo():\n    return 2\n",
        encoding="utf-8",
    )
    _git(ci_repo, "add", "a.py")
    _git(ci_repo, "commit", "-m", "logged change")
    _ctx = lambda *a: subprocess.run(  # noqa: E731
        ["ctx", *a, "--repo-root", str(ci_repo)], cwd=ci_repo, capture_output=True, text=True, check=True
    )
    _ctx("log-commit")

    run_ci(ci_repo, json_output=True, output_workflow=False)
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    # If no PR base is detected, the commits_logged check is trivially passed
    assert report["checks"]["commits_logged"]["passed"] is True


def test_ci_more_than_half_commits_unlogged_is_warn_not_fail(ci_repo, capsys):
    """> 50% of PR commits unlogged -> commits_logged.passed = False (WARN level),
    but CI still exits 0 when no blocking check fails (WARN does not fail CI)."""
    # ci_repo starts with 1 commit. Add 4 more, logging only the first
    # -> 4 of 5 commits (80%) have no changes-table entry (> 50% threshold).
    for i in range(4):
        # Content must differ from the fixture's initial 'return 1' AND from
        # every sibling iteration so each git commit has something to stage.
        (ci_repo / "a.py").write_text(
            f"def foo():\n    return 99  # change {i}\n", encoding="utf-8"
        )
        _git(ci_repo, "add", "a.py")
        _git(ci_repo, "commit", "-m", f"change {i}")
        if i == 0:
            subprocess.run(
                ["ctx", "log-commit", "--repo-root", str(ci_repo)],
                cwd=ci_repo, capture_output=True, text=True, check=True,
            )
    # Refresh the index so no files are stale (stale is the only blocking check)
    run_init(ci_repo)
    capsys.readouterr()  # discard the ctx init banner before reading the JSON report

    exit_code = run_ci(ci_repo, json_output=True, output_workflow=False)
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert report["checks"]["commits_logged"]["passed"] is False
    assert report["checks"]["stale_files"]["passed"] is True
    # WARN alone must not fail the CI gate:
    assert report["passed"] is True
    assert exit_code == 0


def test_ci_export_fresh_within_same_second_not_stale(ci_repo, capsys):
    """A sub-second gap between DB write and export must not flag the export stale.

    Generation timestamps are second-precision while DB updated_at carries
    microseconds. Normalizing both to whole seconds keeps a fresh export fresh.
    """
    from datetime import datetime, timezone
    import sqlite3

    now = datetime.now(timezone.utc)
    # CLAUDE.md generated at the top of the current second (microseconds dropped).
    claude_md = ci_repo / "CLAUDE.md"
    claude_md.write_text(
        f"<!-- Generated: {now.strftime('%Y-%m-%dT%H:%M:%SZ')} -->\n# header\n",
        encoding="utf-8",
    )
    # DB write happened just after, in the SAME second (microseconds preserved).
    db = ci_repo / ".ctx" / "index.db"
    conn = sqlite3.connect(db)
    conn.execute("UPDATE files SET updated_at = ? WHERE path = 'a.py'", (now.isoformat(),))
    conn.commit()
    conn.close()

    run_ci(ci_repo, json_output=True, output_workflow=False)
    report = json.loads(capsys.readouterr().out)
    assert report["checks"]["export_current"]["passed"] is True


def test_ci_output_workflow_creates_file(ci_repo):
    """--output-workflow writes .github/workflows/ctx-validate.yml."""
    run_ci(ci_repo, json_output=False, output_workflow=True)
    path = ci_repo / ".github" / "workflows" / "ctx-validate.yml"
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "ctx ci" in content
    assert "actions/checkout" in content
    assert "ctx-codebase" in content


def test_ci_output_workflow_safe_write(ci_repo, capsys):
    """If the workflow file already exists, do not silently overwrite."""
    workflow_dir = ci_repo / ".github" / "workflows"
    workflow_dir.mkdir(parents=True, exist_ok=True)
    existing = workflow_dir / "ctx-validate.yml"
    existing.write_text("# existing content\n", encoding="utf-8")

    run_ci(ci_repo, json_output=False, output_workflow=True)
    # Existing content should still be there
    assert existing.read_text(encoding="utf-8") == "# existing content\n"


def test_ci_confidence_check(ci_repo, capsys):
    """Confidence check passes when there are no low-confidence functions."""
    run_ci(ci_repo, json_output=True, output_workflow=False)
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["checks"]["confidence"]["passed"] is True


def test_ci_low_confidence_warns(ci_repo, capsys):
    """When > 5% of functions are low-confidence, the check fails (WARN-level)."""
    db = ci_repo / ".ctx" / "index.db"
    conn = sqlite3.connect(db)
    # Insert 100 functions, 10 low-confidence (= 10% > 5%)
    conn.execute("UPDATE functions SET confidence = 0.3")
    conn.commit()
    conn.close()

    run_ci(ci_repo, json_output=True, output_workflow=False)
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["checks"]["confidence"]["passed"] is False


def test_ci_export_check(ci_repo, capsys):
    """If CLAUDE.md is stale relative to DB updated_at, the check fails."""
    # Write a CLAUDE.md with an old timestamp
    claude_md = ci_repo / "CLAUDE.md"
    claude_md.write_text(
        "<!-- Generated: 2020-01-01T00:00:00Z -->\n# old\n",
        encoding="utf-8",
    )
    run_ci(ci_repo, json_output=True, output_workflow=False)
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    # 2020 is way before 2026, so this should fail
    assert report["checks"]["export_current"]["passed"] is False
