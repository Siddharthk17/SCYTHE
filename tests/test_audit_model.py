"""Tests for ctx audit-model (Week 8)."""
import json
import sqlite3
import subprocess

import pytest

from ctx_engine.commands.audit_model_cmd import (
    AuditIssue,
    check_model_change_coverage,
    check_metadata_freshness,
    check_taint_clearance,
    check_description_accuracy,
    check_session_log,
    run_audit_model,
    get_commits_since,
    get_files_changed_in_commit,
)
from ctx_engine.db import init_schema


@pytest.fixture
def git_repo(tmp_path):
    """A real git repo with an index database, for the git-backed checks."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Tester", "-c", "user.email=t@t.io",
         "commit", "--allow-empty", "-q", "-m", "init"],
        cwd=tmp_path, check=True,
    )
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    yield conn, tmp_path
    conn.close()


def _write_and_commit(tmp_path, filename, content, message,
                      name="Tester", email="t@t.io"):
    (tmp_path / filename).write_text(content)
    subprocess.run(["git", "add", filename], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", f"user.name={name}", "-c", f"user.email={email}",
         "commit", "-q", "-m", message],
        cwd=tmp_path, check=True,
    )
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True
    )
    return result.stdout.strip()


MODEL_NAME = "Claude"
MODEL_EMAIL = "claude@anthropic.local"


def _write_and_commit_as_model(tmp_path, filename, content, message):
    """A commit whose git author identifies it as model-authored."""
    return _write_and_commit(tmp_path, filename, content, message,
                             name=MODEL_NAME, email=MODEL_EMAIL)


def _insert_changes_row(conn, file_path, commit_hash, author="model", summary="did things"):
    conn.execute(
        "INSERT INTO changes (file, commit_hash, summary, author, timestamp) "
        "VALUES (?, ?, ?, ?, ?)",
        (file_path, commit_hash, summary, author, "2026-06-14T10:00:00Z"),
    )
    conn.commit()


def _insert_function(conn, fn_id, file_path, line_start, line_end, **kw):
    conn.execute(
        "INSERT INTO functions (id, file, name, signature, line_start, line_end, "
        "semantic_hash, is_stale, is_tainted, confidence) "
        "VALUES (?, ?, ?, ?, ?, ?, 'sh', ?, ?, 1.0)",
        (fn_id, file_path, fn_id.rsplit("::", 1)[-1], f"def x()", line_start, line_end,
         kw.get("is_stale", 0), kw.get("is_tainted", 0)),
    )


# ── Check 1 — coverage ────────────────────────────────────────────────────────


def test_coverage_missing_log_flagged(git_repo):
    conn, repo_root = git_repo
    commit = _write_and_commit_as_model(repo_root, "b.py", "x = 1\n", "model edit")

    issues = check_model_change_coverage(conn, None, repo_root)
    matching = [i for i in issues if i.file == "b.py" and i.commit == commit[:7]]
    assert matching, "file changed in a model commit with no changes row must be flagged"
    assert matching[0].type == "missing_log"
    assert matching[0].severity == "warning"


def test_coverage_human_only_history_clean(git_repo):
    """Pure-human history carries no model signal — coverage passes clean."""
    conn, repo_root = git_repo
    _write_and_commit(repo_root, "b.py", "x = 1\n", "human edit")
    _write_and_commit(repo_root, "c.py", "y = 2\n", "another human edit")

    assert check_model_change_coverage(conn, None, repo_root) == []


def test_coverage_logged_commit_clean(git_repo):
    conn, repo_root = git_repo
    commit = _write_and_commit(repo_root, "a.py", "x = 1\n", "logged edit")
    _insert_changes_row(conn, "a.py", commit, author="model")

    issues = check_model_change_coverage(conn, None, repo_root)
    assert not [i for i in issues if i.file == "a.py"]


def test_coverage_since_range_limits_scope(git_repo):
    conn, repo_root = git_repo
    _write_and_commit_as_model(repo_root, "a.py", "a = 1\n", "first")
    first = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_root,
                           capture_output=True, text=True).stdout.strip()
    _write_and_commit_as_model(repo_root, "b.py", "b = 1\n", "second")

    issues = check_model_change_coverage(conn, first, repo_root)
    assert [i for i in issues if i.file == "a.py"] == []
    assert [i for i in issues if i.file == "b.py"]


# ── Check 2 — freshness ───────────────────────────────────────────────────────


def test_freshness_stale_file_flagged(git_repo):
    conn, repo_root = git_repo
    _write_and_commit(repo_root, "a.py", "a = 1\n", "placeholder")
    conn.execute(
        "INSERT INTO files (path, semantic_hash, content_hash, exports, imports, used_by, is_stale) "
        "VALUES ('a.py', 'sh', 'ch', '[]', '[]', '[]', 1)"
    )
    conn.execute(
        "INSERT INTO changes (file, commit_hash, summary, author, timestamp) "
        "VALUES ('a.py', 'deadbeef', 'edit', 'model', '2026-06-14T10:00:00Z')"
    )
    conn.commit()

    issues = check_metadata_freshness(conn, None, repo_root)
    assert any(i.type == "stale_file" and i.file == "a.py" for i in issues)


def test_freshness_stale_function_flagged(git_repo):
    conn, repo_root = git_repo
    _write_and_commit(repo_root, "a.py", "a = 1\n", "placeholder")
    conn.execute(
        "INSERT INTO changes (file, commit_hash, summary, author, timestamp) "
        "VALUES ('a.py', 'deadbeef', 'edit', 'model', '2026-06-14T10:00:00Z')"
    )
    conn.execute(
        "INSERT INTO files (path, semantic_hash, content_hash, exports, imports, used_by) "
        "VALUES ('a.py', 'sh', 'ch', '[]', '[]', '[]')"
    )
    _insert_function(conn, "a.py::foo", "a.py", 1, 10, is_stale=1)
    conn.commit()

    issues = check_metadata_freshness(conn, None, repo_root)
    assert any(i.type == "stale_functions" and i.file == "a.py" for i in issues)


def test_freshness_current_file_clean(git_repo):
    conn, repo_root = git_repo
    _write_and_commit(repo_root, "a.py", "a = 1\n", "placeholder")
    conn.execute(
        "INSERT INTO changes (file, commit_hash, summary, author, timestamp) "
        "VALUES ('a.py', 'deadbeef', 'edit', 'model', '2026-06-14T10:00:00Z')"
    )
    conn.execute(
        "INSERT INTO files (path, semantic_hash, content_hash, exports, imports, used_by, is_stale) "
        "VALUES ('a.py', 'sh', 'ch', '[]', '[]', '[]', 0)"
    )
    conn.commit()

    assert check_metadata_freshness(conn, None, repo_root) == []


# ── Check 3 — taint clearance ─────────────────────────────────────────────────


def test_taint_clearance_model_sourced_taint_flagged(git_repo):
    conn, repo_root = git_repo
    conn.execute(
        "INSERT INTO files (path, semantic_hash, content_hash, exports, imports, used_by) "
        "VALUES ('model_touched.py', 'sh', 'ch', '[]', '[]', '[]')"
    )
    _insert_function(conn, "model_touched.py::ZeroNet.forward", "model_touched.py", 1, 5)
    _insert_function(conn, "caller.py::dispatch", "caller.py", 1, 5)
    conn.execute(
        "INSERT INTO changes (file, commit_hash, summary, author, timestamp) "
        "VALUES ('model_touched.py', 'deadbeef', 'edit', 'model', '2026-06-14T10:00:00Z')"
    )
    conn.execute(
        "INSERT INTO taint_queue (function_id, taint_source, queued_at, priority) "
        "VALUES ('caller.py::dispatch', 'model_touched.py::ZeroNet.forward', "
        "'2026-06-14T10:00:00Z', 0)"
    )
    conn.commit()

    issues = check_taint_clearance(conn, None, repo_root)
    assert len(issues) == 1
    assert issues[0].file == "caller.py::dispatch"
    assert "model_touched.py::ZeroNet.forward" in issues[0].description


def test_taint_clearance_human_sourced_taint_clean(git_repo):
    conn, repo_root = git_repo
    _insert_function(conn, "human.py::fn", "human.py", 1, 5)
    _insert_function(conn, "caller.py::dispatch", "caller.py", 1, 5)
    conn.execute(
        "INSERT INTO changes (file, commit_hash, summary, author, timestamp) "
        "VALUES ('human.py', 'deadbeef', 'edit', 'human', '2026-06-14T10:00:00Z')"
    )
    conn.execute(
        "INSERT INTO taint_queue (function_id, taint_source, queued_at, priority) "
        "VALUES ('caller.py::dispatch', 'human.py::fn', '2026-06-14T10:00:00Z', 0)"
    )
    conn.commit()

    assert check_taint_clearance(conn, None, repo_root) == []


# ── Check 4 — description accuracy ────────────────────────────────────────────


def test_description_accuracy_matching_summary_clean(git_repo):
    conn, repo_root = git_repo
    (repo_root / "a.py").write_text("def foo():\n    return 1\n")
    commit = _write_and_commit(repo_root, "a.py", "def foo():\n    return 2\n", "tweak")
    _insert_function(conn, "a.py::foo", "a.py", 1, 2)
    _insert_changes_row(conn, "a.py", commit, author="model", summary="updated foo")

    assert check_description_accuracy(conn, None, repo_root) == []


def test_description_accuracy_mismatch_flagged(git_repo):
    conn, repo_root = git_repo
    (repo_root / "a.py").write_text("def foo():\n    return 1\n")
    commit = _write_and_commit(repo_root, "a.py", "def foo():\n    return 2\n", "tweak")
    _insert_function(conn, "a.py::foo", "a.py", 1, 2)
    _insert_changes_row(conn, "a.py", commit, author="model", summary="updated training loop")

    issues = check_description_accuracy(conn, None, repo_root)
    assert len(issues) == 1
    assert issues[0].type == "description_mismatch"


# ── Check 5 — session log ─────────────────────────────────────────────────────


def test_session_log_missing_flagged(git_repo):
    from datetime import datetime, timezone
    conn, repo_root = git_repo
    commit = _write_and_commit_as_model(repo_root, "a.py", "a = 1\n", "model work")
    _insert_changes_row(conn, "a.py", commit, author="model")

    issues = check_session_log(conn, None, repo_root)
    assert len(issues) == 1
    assert issues[0].type == "missing_session_log"


def test_session_log_present_clean(git_repo):
    from datetime import datetime, timezone
    conn, repo_root = git_repo
    commit = _write_and_commit_as_model(repo_root, "a.py", "a = 1\n", "model work")
    _insert_changes_row(conn, "a.py", commit, author="model")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO session_log (entry, files_touched, timestamp) "
        "VALUES ('worked on a.py', '[\"a.py\"]', ?)",
        (now,),
    )
    conn.commit()

    assert check_session_log(conn, None, repo_root) == []


def test_session_log_human_only_history_clean(git_repo):
    conn, repo_root = git_repo
    _write_and_commit(repo_root, "a.py", "a = 1\n", "human work")

    assert check_session_log(conn, None, repo_root) == []


# ── run_audit_model — JSON and exit codes ─────────────────────────────────────


def test_json_output_valid_schema(git_repo, capsys):
    conn, repo_root = git_repo
    conn.execute(
        "INSERT INTO files (path, semantic_hash, content_hash, exports, imports, used_by, is_stale) "
        "VALUES ('a.py', 'sh', 'ch', '[]', '[]', '[]', 1)"
    )
    conn.execute(
        "INSERT INTO changes (file, commit_hash, summary, author, timestamp) "
        "VALUES ('a.py', 'deadbeef', 'edit', 'model', '2026-06-14T10:00:00Z')"
    )
    conn.commit()

    exit_code = run_audit_model(repo_root, json_output=True)
    doc = json.loads(capsys.readouterr().out)
    assert set(doc) == {
        "timestamp", "repo", "range", "model_commits", "model_file_changes", "checks",
    }
    assert set(doc["checks"]) == {
        "coverage", "freshness", "taint_clearance", "description_accuracy", "session_log",
    }
    assert doc["checks"]["freshness"]["passed"] is False
    assert doc["checks"]["freshness"]["issues"][0]["type"] == "stale_file"
    # Documented per-check keys (spec schema) alongside the issue lists.
    assert doc["checks"]["freshness"]["stale_files"] == ["a.py"]
    assert doc["checks"]["taint_clearance"]["uncleared_taints"] == 0
    assert doc["checks"]["description_accuracy"]["mismatches"] == []
    assert "missing_entries" in doc["checks"]["session_log"]
    assert exit_code == 1


def test_human_only_repo_all_checks_pass(git_repo, capsys):
    """A repo with human commits and no model signal: all checks pass, exit 0."""
    conn, repo_root = git_repo
    _write_and_commit(repo_root, "a.py", "a = 1\n", "human work")
    _write_and_commit(repo_root, "b.py", "b = 2\n", "more human work")

    exit_code = run_audit_model(repo_root, json_output=True)
    doc = json.loads(capsys.readouterr().out)
    assert all(c["passed"] for c in doc["checks"].values())
    assert doc["model_commits"] == 0
    assert exit_code == 0


def test_clean_repo_all_checks_pass(git_repo, capsys):
    conn, repo_root = git_repo
    exit_code = run_audit_model(repo_root, json_output=True)
    doc = json.loads(capsys.readouterr().out)
    assert all(c["passed"] for c in doc["checks"].values())
    assert doc["model_commits"] == 0
    assert exit_code == 0


def test_text_report_footer(git_repo, capsys):
    conn, repo_root = git_repo
    commit = _write_and_commit_as_model(repo_root, "a.py", "a = 1\n", "model work")
    _insert_changes_row(conn, "a.py", commit, author="model")

    run_audit_model(repo_root, json_output=False)
    out = capsys.readouterr().out
    assert "ctx audit-model" in out
    assert "Session log" in out
    assert "ctx sync" in out  # remediation hint

