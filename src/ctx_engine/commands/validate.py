import logging
import sqlite3
import subprocess
from pathlib import Path
from ctx_engine.db import connect
from ctx_engine.discovery import EXTENSION_TO_LANGUAGE
from ctx_engine.languages.registry import get_parser
from ctx_engine.hashing import file_semantic_hash, file_content_hash

logger = logging.getLogger("ctx")


def get_staged_blob(repo_root: Path, path: str) -> bytes | None:
    """Return the git staged blob for path, or None if not staged."""
    result = subprocess.run(
        ["git", "show", f":0:{path}"],
        cwd=repo_root,
        capture_output=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def get_staged_paths(repo_root: Path) -> list[str]:
    """Return staged file paths for added, copied, or modified files."""
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    return [p.strip() for p in result.stdout.splitlines() if p.strip()]


def run_validate(repo_root: Path, files: list[str] | None = None) -> None:
    """Validate staged file content against the index.

    When files is provided, validates those paths instead of auto-detecting staged files.
    Raises SystemExit(1) if any stale or unindexed files are found.
    """
    db_path = repo_root / ".ctx" / "index.db"
    if not db_path.exists():
        raise FileNotFoundError(
            "Database not found. Please run 'ctx init' first."
        )

    try:
        subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=repo_root, capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as err:
        raise ValueError(
            f"Not inside a git repository: {repo_root}"
        ) from err

    if files is not None:
        staged_paths = files
    else:
        staged_paths = get_staged_paths(repo_root)

    if not staged_paths:
        print("ctx validate: all staged files match the index.")
        print("  Checked: 0 files")
        return

    conn = connect(db_path)
    conn.row_factory = sqlite3.Row

    stale_paths: list[str] = []
    not_indexed_paths: list[str] = []
    skipped_count = 0
    checked_count = 0

    for staged_path in staged_paths:
        ext = Path(staged_path).suffix
        language = EXTENSION_TO_LANGUAGE.get(ext)
        if not language:
            skipped_count += 1
            continue

        file_row = conn.execute(
            "SELECT semantic_hash, content_hash FROM files WHERE path = ?", (staged_path,)
        ).fetchone()

        blob = get_staged_blob(repo_root, staged_path)
        if blob is None:
            skipped_count += 1
            continue

        try:
            parser = get_parser(language)
            tree = parser.parse(blob)

            if tree.root_node.has_error:
                logger.warning(
                    "WARNING: parse errors in staged %s — validating against best-effort hash",
                    staged_path,
                )

            staged_hash = file_semantic_hash(tree, blob, language)
        except Exception:
            logger.warning(
                "WARNING: could not parse staged %s — skipping validation",
                staged_path,
            )
            skipped_count += 1
            continue

        checked_count += 1

        if file_row is None:
            not_indexed_paths.append(staged_path)
        elif file_row["semantic_hash"] != staged_hash:
            stale_paths.append(staged_path)

    conn.close()

    if not stale_paths and not not_indexed_paths:
        print("ctx validate: all staged files match the index.")
        if checked_count > 0 or skipped_count > 0:
            print(f"  Checked: {checked_count} files{(' (' + str(skipped_count) + ' non-parseable skipped)') if skipped_count else ''}")
        return

    print("ctx validate: index is out of date for staged files.")
    print()

    if stale_paths:
        print("  STALE (metadata exists but doesn't match staged content):")
        for p in stale_paths:
            print(f"    - {p}")
        print()

    if not_indexed_paths:
        print("  NOT INDEXED (never run 'ctx init' or 'ctx update' on these):")
        for p in not_indexed_paths:
            print(f"    - {p}")
        print()

    print("  To fix:")
    print("    ctx sync           # re-index and re-summarize everything")
    print("    ctx update <file>  # re-index and re-summarize a single file")
    print()

    import sys
    print("  Commit blocked.", file=sys.stderr)
    raise SystemExit(1)
