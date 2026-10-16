"""`ctx post-merge` — advisory staleness nudge after a merge.

Content-hash-only comparison: no AST parsing, no LLM, no perceptible
delay. Always exits 0 — never blocks the merge.
"""
import hashlib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ctx_engine.db import connect
from ctx_engine.discovery import get_parseable_extensions


@dataclass
class PostMergeReport:
    needs_sync: list[str] = field(default_factory=list)
    checked: int = 0


def run_post_merge_check(repo_root: Path) -> PostMergeReport:
    result = subprocess.run(
        ["git", "diff", "--name-only", "ORIG_HEAD", "HEAD"],
        cwd=repo_root, capture_output=True, text=True,
    )
    if result.returncode != 0:
        return PostMergeReport(needs_sync=[], checked=0)

    changed_paths = [p.strip() for p in result.stdout.splitlines() if p.strip()]
    parseable_ext = get_parseable_extensions()

    db_path = repo_root / ".ctx" / "index.db"
    if not db_path.exists():
        return PostMergeReport(needs_sync=[], checked=0)

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
        return PostMergeReport(needs_sync=needs_sync, checked=len(changed_paths))
    finally:
        conn.close()


def run_post_merge(repo_root: Path) -> None:
    report = run_post_merge_check(repo_root)
    if report.needs_sync:
        count = len(report.needs_sync)
        print(f"ctx: {count} file{'s' if count != 1 else ''} changed by this merge need re-indexing.")
        print("     Run 'ctx sync' to update the context index.")
