import os
import subprocess
import pytest
from ctx_engine.commands.install_hooks import run_install_hooks, PRE_COMMIT_HOOK, POST_COMMIT_HOOK


@pytest.fixture
def repo_with_git(tmp_path):
    """Temp git repo without hooks."""
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True, check=True)
    return tmp_path


def test_install_hooks_fresh(repo_with_git):
    """No existing hooks -> both installed and executable."""
    run_install_hooks(repo_with_git)

    pre = repo_with_git / ".git" / "hooks" / "pre-commit"
    post = repo_with_git / ".git" / "hooks" / "post-commit"

    assert pre.exists()
    assert post.exists()
    assert pre.read_text() == PRE_COMMIT_HOOK
    assert post.read_text() == POST_COMMIT_HOOK
    assert os.access(str(pre), os.X_OK)
    assert os.access(str(post), os.X_OK)


def test_install_hooks_idempotent(repo_with_git):
    """Run twice -> no-op second time, mtime unchanged."""
    run_install_hooks(repo_with_git)

    pre = repo_with_git / ".git" / "hooks" / "pre-commit"
    pre_mtime1 = pre.stat().st_mtime_ns

    run_install_hooks(repo_with_git)

    pre_mtime2 = pre.stat().st_mtime_ns
    assert pre_mtime1 == pre_mtime2

    assert pre.read_text() == PRE_COMMIT_HOOK


def test_install_hooks_backup_existing(repo_with_git):
    """Existing hook with different content gets backed up."""
    pre = repo_with_git / ".git" / "hooks" / "pre-commit"
    pre.parent.mkdir(exist_ok=True)
    pre.write_text("#!/bin/sh\necho 'my custom hook'\n")

    run_install_hooks(repo_with_git)

    # Original hook no longer at pre
    assert pre.read_text() == PRE_COMMIT_HOOK

    # Backup exists somewhere
    backups = list(pre.parent.glob("pre-commit.ctx-backup.*"))
    assert len(backups) >= 1
    assert backups[0].read_text() == "#!/bin/sh\necho 'my custom hook'\n"


def test_install_hooks_executable(repo_with_git):
    """Resulting hook files are executable."""
    run_install_hooks(repo_with_git)

    pre = repo_with_git / ".git" / "hooks" / "pre-commit"
    post = repo_with_git / ".git" / "hooks" / "post-commit"

    assert os.access(str(pre), os.X_OK)
    assert os.access(str(post), os.X_OK)
