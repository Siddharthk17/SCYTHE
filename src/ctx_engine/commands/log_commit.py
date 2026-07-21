import logging
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from ctx_engine.db import connect

logger = logging.getLogger("ctx")


def get_commit_metadata(repo_root: Path, commit_hash: str) -> tuple[str, list[str]]:
    """Return (subject, changed_files) for a given commit hash."""
    if not commit_hash or not commit_hash.strip():
        raise ValueError("Commit hash must not be empty")

    subject = subprocess.run(
        ["git", "log", "--format=%s", "-1", commit_hash],
        cwd=repo_root, capture_output=True, text=True,
    ).stdout.strip()

    changed = subprocess.run(
        ["git", "diff-tree", "--no-commit-id", "-r", "--name-only", commit_hash],
        cwd=repo_root, capture_output=True, text=True,
    ).stdout.strip().splitlines()

    if not subject and not changed:
        raise ValueError(f"Commit {commit_hash} does not exist.")

    return subject, [p.strip() for p in changed if p.strip()]


def run_log_commit(repo_root: Path, commit_hash: str = "HEAD") -> None:
    """Record commit metadata in the changes table."""
    db_path = repo_root / ".ctx" / "index.db"
    if not db_path.exists():
        raise FileNotFoundError(
            "Run 'ctx init' first — .ctx/index.db does not exist."
        )

    result = subprocess.run(
        ["git", "rev-parse", commit_hash],
        cwd=repo_root, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise ValueError(f"Commit {commit_hash} does not exist.")
    resolved_hash = result.stdout.strip()

    subject, changed_files = get_commit_metadata(repo_root, resolved_hash)
    if not changed_files:
        print(f"ctx log-commit — {resolved_hash[:7]} \"{subject}\"")
        print()
        print("  No files changed in this commit.")
        return

    conn = connect(db_path)
    conn.row_factory = sqlite3.Row

    now = datetime.now(timezone.utc).isoformat()
    inserted = 0

    with conn:
        for file_path in changed_files:
            if not file_path:
                continue
            before = conn.total_changes
            conn.execute(
                """
                INSERT INTO changes (file, commit_hash, summary, author, timestamp)
                VALUES (?, ?, ?, 'human', ?)
                ON CONFLICT(file, commit_hash) DO NOTHING
                """,
                (file_path, resolved_hash, subject, now),
            )
            if conn.total_changes > before:
                inserted += 1

            conn.execute(
                """
                DELETE FROM changes
                WHERE file = ?
                  AND id NOT IN (
                    SELECT id FROM changes
                    WHERE file = ?
                    ORDER BY timestamp DESC
                    LIMIT 20
                  )
                """,
                (file_path, file_path),
            )

    conn.close()

    print(f"ctx log-commit — {resolved_hash[:7]} \"{subject}\"")
    print()
    print(f"  {inserted} files recorded in changes:")
    for p in changed_files:
        print(f"    - {p}")
