"""`ctx post-checkout` — advisory staleness nudge after a branch switch.

Only runs on real branch switches (``branch_flag == "1"``). Diff logic
matches ``post-merge``: content-hash comparison only. Always exits 0.
"""
import hashlib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ctx_engine.db import connect
from ctx_engine.discovery import get_parseable_extensions


@dataclass
class PostCheckoutReport:
    needs_sync: list[str] = field(default_factory=list)
    checked: int = 0


def _content_hashes_at(ref: str, repo_root: Path) -> dict[str, str] | None:
    result = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", ref],
        cwd=repo_root, capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return {p.strip(): "" for p in result.stdout.splitlines() if p.strip()}


def run_post_checkout_check(
    repo_root: Path,
    prev_head: str | None = None,
    new_head: str | None = None,
    branch_flag: str = "1",
) -> PostCheckoutReport:
    if branch_flag != "1":
        return PostCheckoutReport(needs_sync=[], checked=0)
    if not prev_head or not new_head or prev_head == new_head:
        return PostCheckoutReport(needs_sync=[], checked=0)

    db_path = repo_root / ".ctx" / "index.db"
    if not db_path.exists():
        return PostCheckoutReport(needs_sync=[], checked=0)

    result = subprocess.run(
        ["git", "diff", "--name-only", prev_head, new_head],
        cwd=repo_root, capture_output=True, text=True,
    )
    if result.returncode != 0:
        return PostCheckoutReport(needs_sync=[], checked=0)

    changed_paths = [p.strip() for p in result.stdout.splitlines() if p.strip()]
    parseable_ext = get_parseable_extensions()

    conn = connect(db_path)
    try:
        needs_sync = []
        for rel_path in changed_paths:
            if Path(rel_path).suffix.lower() not in parseable_ext:
                continue
            abs_path = repo_root / rel_path
            if not abs_path.exists():
                continue
            try:
                new_hash = hashlib.sha256(abs_path.read_bytes()).hexdigest()
            except OSError:
                continue
            row = conn.execute(
                "SELECT content_hash FROM files WHERE path = ?", (rel_path,)
            ).fetchone()
            if row is None or row["content_hash"] != new_hash:
                needs_sync.append(rel_path)
        return PostCheckoutReport(needs_sync=needs_sync, checked=len(changed_paths))
    finally:
        conn.close()


def run_post_checkout(
    repo_root: Path,
    prev_head: str | None = None,
    new_head: str | None = None,
    branch_flag: str = "1",
) -> None:
    report = run_post_checkout_check(repo_root, prev_head, new_head, branch_flag)
    if report.needs_sync:
        count = len(report.needs_sync)
        print(f"ctx: {count} file{'s' if count != 1 else ''} differ from the previous branch and need re-indexing.")
        print("     Run 'ctx sync' to update the context index.")
