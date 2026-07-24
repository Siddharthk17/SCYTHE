import concurrent.futures
import json
import logging
import os
import time as time_module
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from ctx_engine.db import connect, init_schema
from ctx_engine.discovery import (
    assert_inside_git_repo,
    discover_all_tracked_paths,
    discover_parseable_files,
)
from ctx_engine.directories import build_directory_counts
from ctx_engine.reindex import (
    can_skip_file,
    parse_one_file,
    run_reindex_pipeline,
)

logger = logging.getLogger("ctx")


def current_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_init(repo_root: Path) -> None:
    assert_inside_git_repo(repo_root)

    ctx_dir = repo_root / ".ctx"
    ctx_dir.mkdir(exist_ok=True)
    db_path = ctx_dir / "index.db"

    conn = connect(db_path)
    init_schema(conn)

    tracked = discover_all_tracked_paths(repo_root)
    parseable = discover_parseable_files(repo_root)

    repo_name = repo_root.name
    language_counts = Counter(parseable.values())
    parsed_lang_str = ", ".join(f"{lang}: {count}" for lang, count in sorted(language_counts.items()))
    skipped_count = len(tracked) - len(parseable)

    to_skip: list[str] = []
    to_parse: list[tuple[str, str, str]] = []

    for rel_path, language in sorted(parseable.items()):
        abs_path = repo_root / rel_path
        if can_skip_file(conn, abs_path, rel_path):
            to_skip.append(rel_path)
        else:
            to_parse.append((rel_path, language, str(repo_root)))

    worker_count = max(1, os.cpu_count() or 1)
    parse_results = []

    t_start = time_module.time()

    if to_parse:
        with ProcessPoolExecutor(max_workers=worker_count) as pool:
            futures = {pool.submit(parse_one_file, args): args[0] for args in to_parse}
            completed = 0
            total = len(to_parse)
            for future in concurrent.futures.as_completed(futures):
                parse_results.append(future.result())
                completed += 1
                if total > 50 and completed % 10 == 0:
                    print(f"  Parsing: {completed}/{total} files...")

    for result in parse_results:
        if result.error:
            logger.warning("Failed to parse %s: %s", result.rel_path, result.error)

    files_to_reindex = {}
    for result in parse_results:
        if result.error is None:
            conn.execute(
                "UPDATE files SET mtime = ?, file_size = ? WHERE path = ?",
                (result.mtime, result.file_size, result.rel_path),
            )
        files_to_reindex[result.rel_path] = result.language
    conn.commit()

    parse_error_count, parse_error_paths, _ = run_reindex_pipeline(
        conn, repo_root, files_to_reindex
    )

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

    total_time = time_module.time() - t_start

    print(f"ctx init — {repo_name}")
    print()
    print(f"  {len(tracked)} files tracked by git")
    print(f"  {len(to_skip)} files skipped (mtime cache hit, no changes since last index)")
    if to_parse:
        parsed_count = len(to_parse)
        print(f"  {parsed_count} files parsed in parallel ({worker_count} workers)")
        print(f"    {parsed_lang_str}")
    else:
        print(f"  {len(tracked) - skipped_count} files parsed ({parsed_lang_str})")
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

    if to_skip and total_time > 0.5:
        avg_parse_time = total_time / max(1, len(to_parse)) if to_parse else 0.05
        estimated_without_cache = avg_parse_time * len(to_skip) + total_time
        print(f"  Total time: {total_time:.1f}s (vs. ~{estimated_without_cache:.0f}s without mtime cache)")
    elif to_skip:
        print(f"  Total time: {total_time:.1f}s")
    else:
        print(f"  Total time: {total_time:.1f}s (first run — no mtime cache yet)")
