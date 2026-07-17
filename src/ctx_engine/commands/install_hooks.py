import logging
import os
import shutil
import stat
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("ctx")

PRE_COMMIT_HOOK = """#!/bin/sh
ctx validate --repo-root "$(git rev-parse --show-toplevel)"
"""

POST_COMMIT_HOOK = """#!/bin/sh
ctx log-commit --repo-root "$(git rev-parse --show-toplevel)"
"""


def _write_hook_safe(hook_path: Path, content: str, hook_label: str, git_dir: Path) -> bool:
    if hook_path.exists():
        existing = hook_path.read_text()
        if existing == content:
            print(f"  {hook_label} → {hook_path}  (already installed)")
            return False

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        backup_path = git_dir / "hooks" / f"{hook_path.name}.ctx-backup.{timestamp}"
        shutil.copy2(str(hook_path), str(backup_path))
        print(
            f"  WARNING: existing {hook_label} hook backed up to:"
        )
        print(f"    {backup_path}")
        print("  Your original hook is preserved — ctx has replaced it.")
        print(f"  If you need both to run, edit .git/hooks/{hook_path.name} to call both.")
        print()

    hook_path.write_text(content)
    hook_path.chmod(hook_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"  {hook_label} → {hook_path}  (installed, chmod +x)")
    return True


def run_install_hooks(repo_root: Path) -> None:
    git_dir = repo_root / ".git"

    if not git_dir.is_dir():
        raise ValueError(f"Not a git repository: {repo_root}")

    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(exist_ok=True)

    pre_commit_path = hooks_dir / "pre-commit"
    post_commit_path = hooks_dir / "post-commit"

    print("ctx install-hooks")
    print()

    _write_hook_safe(pre_commit_path, PRE_COMMIT_HOOK, "pre-commit", git_dir)
    _write_hook_safe(post_commit_path, POST_COMMIT_HOOK, "post-commit", git_dir)

    for p in (pre_commit_path, post_commit_path):
        if p.exists() and not os.access(str(p), os.X_OK):
            p.chmod(0o755)

    print()
    print("  Done. Every commit will now be validated against .ctx/index.db.")
    print("  Run 'ctx sync' to bring the index fully up to date before your next commit.")
