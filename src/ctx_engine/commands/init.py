import concurrent.futures
import json
import logging
import os
import sqlite3
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
    run_reindex_pipeline_from_results,
)
from ctx_engine.commands.export_cmd import run_export

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
    skipped_count = len(tracked) - len(parseable)

    to_skip: list[str] = []
    to_parse: list[tuple[str, str, str]] = []

    for rel_path, language in sorted(parseable.items()):
        abs_path = repo_root / rel_path
        if can_skip_file(conn, abs_path, rel_path):
            logger.debug("Skipped (mtime cache hit): %s", rel_path)
            to_skip.append(rel_path)
        else:
            to_parse.append((rel_path, language, str(repo_root)))

    worker_count = max(1, os.cpu_count() or 1)
    parse_results = []

    t_start = time_module.time()
    parse_time = 0.0

    if to_parse:
        t_parse_start = time_module.time()
        with ProcessPoolExecutor(max_workers=worker_count) as pool:
            futures = {pool.submit(parse_one_file, args): args[0] for args in to_parse}
            completed = 0
            total = len(to_parse)
            for future in concurrent.futures.as_completed(futures):
                parse_results.append(future.result())
                completed += 1
                if total > 50 and completed % 10 == 0:
                    print(f"  Parsing: {completed}/{total} files...")
        parse_time = time_module.time() - t_parse_start

    for result in parse_results:
        if result.error:
            logger.warning("Failed to parse %s: %s", result.rel_path, result.error)

    files_to_reindex: dict[str, str] = {}
    for result in parse_results:
        if result.error is None:
            files_to_reindex[result.rel_path] = result.language
        else:
            # Counted as a parse error below; never inserted as empty.
            files_to_reindex[result.rel_path] = result.language
    # Drop errored results from the write path; they are reported only.
    error_paths = {r.rel_path for r in parse_results if r.error}
    writable_results = [r for r in parse_results if not r.error]
    writable_map = {k: v for k, v in files_to_reindex.items() if k not in error_paths}

    parse_error_count, parse_error_paths, _ = run_reindex_pipeline_from_results(
        conn, repo_root, writable_results, writable_map
    )
    # Surface worker-level failures that never reached the DB writer.
    for result in parse_results:
        if result.error and result.rel_path not in parse_error_paths:
            parse_error_paths.append(result.rel_path)
    parse_error_count = len(parse_error_paths)

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

    # Auto-export after indexing — only if there's actually content
    any_purpose = False
    for row in conn.execute("SELECT purpose FROM files WHERE purpose IS NOT NULL LIMIT 3"):
        if row[0]:
            any_purpose = True
            break

    if any_purpose:
        try:
            export_report = run_export(conn, repo_root)
        except Exception as e:
            logger.warning("Auto-export failed: %s", e)
            export_report = None
    else:
        export_report = None

    # Week 7: update SQLite query planner statistics on every init.
    # Takes < 100ms and makes subsequent queries noticeably faster.
    try:
        conn.execute("PRAGMA optimize;")
    except sqlite3.OperationalError:
        pass

    conn.close()

    from ctx_engine.commands.snapshot_cmd import ensure_snapshots_gitignored
    ensure_snapshots_gitignored(repo_root)

    total_time = time_module.time() - t_start

    parsed_counts = Counter(r.language for r in parse_results if not r.error)
    parsed_lang_str = ", ".join(
        f"{lang}: {count}" for lang, count in sorted(parsed_counts.items())
    )

    print(f"ctx init — {repo_name}")
    print()
    print(f"  {len(tracked)} files tracked by git")
    print(f"  {len(to_skip)} files skipped (mtime cache hit, no changes since last index)")
    if to_parse:
        print(f"  {len(to_parse)} files parsed in parallel ({worker_count} workers)")
        if parsed_lang_str:
            print(f"    {parsed_lang_str}")
    else:
        print(f"  0 files parsed in parallel ({worker_count} workers)")
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

    if export_report:
        print(f"  export: {len(export_report.written)} file(s) written, {len(export_report.skipped)} already current")

    if to_skip:
        # Estimate from pure parse time, not total (which includes
        # import/call graph + export). Falls back to 5ms/file when
        # nothing was parsed this run.
        if to_parse and parse_time > 0:
            avg_parse_time = parse_time / max(1, len(to_parse))
        else:
            avg_parse_time = 0.05
        estimated_without_cache = avg_parse_time * len(to_skip) + total_time
        print(f"  Total time: {total_time:.1f}s (vs. ~{estimated_without_cache:.0f}s without mtime cache)")
    else:
        print(f"  Total time: {total_time:.1f}s (first run — no mtime cache yet)")
