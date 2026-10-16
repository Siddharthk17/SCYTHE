import os
import subprocess

from ctx_engine.commands.install_hooks import (
    POST_CHECKOUT_HOOK,
    POST_MERGE_HOOK,
    run_install_hooks,
)
from ctx_engine.commands.init import run_init
from ctx_engine.commands.post_checkout_cmd import run_post_checkout_check
from ctx_engine.commands.post_merge_cmd import run_post_merge_check


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True)


def _make_repo(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "t@t.com")
    (tmp_path / "a.py").write_text("def run():\n    return 1\n")
    _git(tmp_path, "add", "a.py")
    _git(tmp_path, "commit", "-m", "init")
    run_init(tmp_path)
    return tmp_path


def _current_branch(repo):
    result = subprocess.run(
        ["git", "branch", "--show-current"], cwd=repo,
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def test_post_merge_reports_changed_file(tmp_path):
    repo = _make_repo(tmp_path)
    base = _current_branch(repo)
    _git(repo, "checkout", "-b", "feature")
    (repo / "a.py").write_text("def run():\n    return 2\n")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-m", "change run")
    _git(repo, "checkout", base)
    _git(repo, "merge", "feature", "--no-edit")
    report = run_post_merge_check(repo)
    assert "a.py" in report.needs_sync


def test_post_merge_silent_when_no_parseable_changes(tmp_path):
    repo = _make_repo(tmp_path)
    base = _current_branch(repo)
    _git(repo, "checkout", "-b", "docs-branch")
    (repo / "notes.txt").write_text("hello\n")
    _git(repo, "add", "notes.txt")
    _git(repo, "commit", "-m", "docs")
    _git(repo, "checkout", base)
    _git(repo, "merge", "docs-branch", "--no-edit")
    report = run_post_merge_check(repo)
    assert report.needs_sync == []


def test_post_merge_no_orig_head_no_crash(tmp_path):
    repo = _make_repo(tmp_path)
    report = run_post_merge_check(repo)
    assert report.needs_sync == []
    assert report.checked == 0


def test_post_checkout_reports_differing_files(tmp_path):
    repo = _make_repo(tmp_path)
    base = _current_branch(repo)
    prev = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    _git(repo, "checkout", "-b", "other")
    (repo / "a.py").write_text("def run():\n    return 99\n")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-m", "other change")
    new = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    # Stay on `other`: the worktree now holds content the index (built on
    # base) has never seen, so the advisory check must flag it.
    report = run_post_checkout_check(repo, prev_head=prev, new_head=new, branch_flag="1")
    assert "a.py" in report.needs_sync


def test_post_checkout_branch_flag_zero_is_noop(tmp_path):
    repo = _make_repo(tmp_path)
    report = run_post_checkout_check(
        repo, prev_head="aaa", new_head="bbb", branch_flag="0"
    )
    assert report.needs_sync == []
    assert report.checked == 0


def test_post_checkout_same_head_is_noop(tmp_path):
    repo = _make_repo(tmp_path)
    report = run_post_checkout_check(
        repo, prev_head="same", new_head="same", branch_flag="1"
    )
    assert report.needs_sync == []


def test_post_merge_checkout_without_db_no_crash(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "t@t.com")
    assert run_post_merge_check(tmp_path).needs_sync == []
    assert run_post_checkout_check(
        tmp_path, prev_head="a", new_head="b", branch_flag="1"
    ).needs_sync == []


def test_post_merge_always_exits_zero(tmp_path, capsys):
    from click.testing import CliRunner

    from ctx_engine.cli import main as _cli_main

    repo = _make_repo(tmp_path)
    runner = CliRunner()
    for args in (["post-merge"], ["post-checkout", "--branch-flag", "0"]):
        result = runner.invoke(_cli_main, [*args, "--repo-root", str(repo)])
        assert result.exit_code == 0, result.output

    # Also verify the installed console script resolves and exits 0 when
    # invoked as a subprocess (PATH is fixed by tests/conftest.py).
    import shutil as _shutil
    import subprocess as _sp

    ctx_bin = _shutil.which("ctx")
    if ctx_bin is not None:
        for args in (["post-merge"], ["post-checkout", "--branch-flag", "0"]):
            proc = _sp.run(
                [ctx_bin, *args, "--repo-root", str(repo)],
                capture_output=True, text=True,
            )
            assert proc.returncode == 0, proc.stderr


def test_install_hooks_writes_merge_and_checkout(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "t@t.com")
    run_install_hooks(tmp_path)
    merge = tmp_path / ".git" / "hooks" / "post-merge"
    checkout = tmp_path / ".git" / "hooks" / "post-checkout"
    assert merge.read_text() == POST_MERGE_HOOK
    assert checkout.read_text() == POST_CHECKOUT_HOOK
    assert os.access(str(merge), os.X_OK)
    assert os.access(str(checkout), os.X_OK)
