# init command implementation for ctx.

import json
import logging
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from ctx_engine.db import connect, init_schema
from ctx_engine.discovery import (
    assert_inside_git_repo,
    discover_all_tracked_paths,
    discover_parseable_files,
)
from ctx_engine.directories import build_directory_counts
from ctx_engine.reindex import run_reindex_pipeline

logger = logging.getLogger("ctx")

def current_timestamp() -> str:
    """Return the current timezone-aware UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()

def run_init(repo_root: Path) -> None:
    """Run the complete initialization and indexing flow for the repository."""
    # 1. Assert inside a Git repo
    assert_inside_git_repo(repo_root)

    # 2. Database path and directory creation
    ctx_dir = repo_root / ".ctx"
    ctx_dir.mkdir(exist_ok=True)
    db_path = ctx_dir / "index.db"

    # 3. Connection and schema initialization
    conn = connect(db_path)
    init_schema(conn)

    # 4. Discover files
    tracked = discover_all_tracked_paths(repo_root)
    parseable = discover_parseable_files(repo_root)

    # 5. Run reindexing pipeline
    parse_error_count, parse_error_paths = run_reindex_pipeline(conn, repo_root, parseable)

    # 6. Directories pass
    now = current_timestamp()
    dir_counts = build_directory_counts(tracked)
    with conn:
        for dir_path, count in dir_counts.items():
            conn.execute(
                """
                INSERT OR REPLACE INTO directories (
                    path, system, summary, file_count, updated_at
                ) VALUES (?, NULL, NULL, ?, ?)
                """,
                (dir_path, count, now)
            )

    # 7. Collect database counts for report
    function_count = conn.execute("SELECT count(*) FROM functions").fetchone()[0]
    
    import_edges_count = 0
    for row in conn.execute("SELECT imports FROM files WHERE imports IS NOT NULL").fetchall():
        try:
            import_edges_count += len(json.loads(row[0]))
        except Exception:
            pass

    call_edges_count = conn.execute("SELECT count(*) FROM call_graph").fetchone()[0]
    ambiguous_calls = conn.execute("SELECT count(*) FROM call_graph WHERE is_ambiguous = 1").fetchone()[0]
    unresolved_calls = conn.execute("SELECT count(*) FROM call_graph WHERE callee_id IS NULL").fetchone()[0]

    conn.close()

    # 8. Print summary report
    repo_name = repo_root.name
    language_counts = Counter(parseable.values())
    parsed_lang_str = ", ".join(f"{lang}: {count}" for lang, count in sorted(language_counts.items()))
    skipped_count = len(tracked) - len(parseable)

    print(f"ctx init — {repo_name}")
    print()
    print(f"  {len(tracked)} files tracked by git")
    print(f"  {len(parseable)} files parsed ({parsed_lang_str})")
    print(f"  {skipped_count} files skipped (unsupported extension)")
    print(f"  {parse_error_count} files with parse errors")
    if parse_error_paths:
        for err_path in parse_error_paths:
            print(f"      - {err_path}")
    print(f"  {function_count} functions extracted")
    print(f"  {import_edges_count} import edges resolved")
    print(f"  {call_edges_count} call graph edges ({ambiguous_calls} ambiguous, {unresolved_calls} unresolved)")
    print(f"  {len(dir_counts)} directories indexed")
    print()
    print("  .ctx/index.db ready (WAL mode)")
