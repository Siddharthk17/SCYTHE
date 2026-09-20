import logging
import json
from pathlib import Path
from ctx_engine.db import connect
from ctx_engine.discovery import discover_parseable_files
from ctx_engine.reindex import run_reindex_pipeline
from ctx_engine.commands.summarize import get_summarize_selection
from ctx_engine.intelligence.llm_client import (
    get_anthropic_client,
    get_model_name,
    call_llm_with_retry,
    parse_response,
    apply_summary_batch,
    SYSTEM_INSTRUCTION,
)
from ctx_engine.commands.export_cmd import run_export
from ctx_engine.intelligence.heuristics import run_heuristic_detection

logger = logging.getLogger("ctx")


def run_sync(repo_root: Path, dry_run: bool = False) -> None:
    """Reindex and re-summarize — make everything fresh."""
    db_path = repo_root / ".ctx" / "index.db"
    if not db_path.exists():
        raise FileNotFoundError(
            "Database not found. Please run 'ctx init' first."
        )

    print("ctx sync")
    print()

    conn = connect(db_path)
    parseable = discover_parseable_files(repo_root)

    parse_error_count, parse_error_paths, changed_func_ids = run_reindex_pipeline(conn, repo_root, parseable)

    # Report absolute post-reindex state (not before/after deltas): Phase 1
    # only adds stale/taint flags, but pre-existing flags from a prior
    # skipped/failed summarize must still be visible in the report and in
    # the Phase 2 cost estimate.
    stale_files = conn.execute("SELECT COUNT(*) FROM files WHERE is_stale = 1").fetchone()[0]
    tainted_funcs = conn.execute("SELECT COUNT(*) FROM functions WHERE is_tainted = 1").fetchone()[0]
    stale_funcs = conn.execute("SELECT COUNT(*) FROM functions WHERE is_stale = 1").fetchone()[0]
    total_files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]

    unchanged = total_files - stale_files

    print("  Phase 1: reindex")
    if stale_files > 0 or tainted_funcs > 0 or stale_funcs > 0:
        print(f"    {stale_files} files changed — {stale_funcs} functions stale, {tainted_funcs} tainted")
    else:
        print("    No files changed.")
    print(f"    ({unchanged} files unchanged — hashes matched, metadata preserved)")
    if parse_error_count > 0:
        print(f"    {parse_error_count} file(s) with parse errors:")
        for err_path in parse_error_paths:
            print(f"      - {err_path}")

    print()

    changed_files: set[str] = set()
    for fid in changed_func_ids:
        if "::" in fid:
            file_part = fid.split("::", 1)[0]
            changed_files.add(file_part)

    files_data, total_funcs_needing_summary, batches = get_summarize_selection(
        conn, repo_root, force=False, path_filter=changed_files if changed_files else None
    )

    if not files_data:
        print("  Phase 2: summarize")
        print("    No files need summarization.")
        print()
        total_funcs_updated = 0
        total_in_tokens = 0
        total_out_tokens = 0
    elif dry_run:
        total_chars_in = 0
        total_chars_out = 0
        for batch in batches:
            total_chars_in += len(json.dumps(batch)) + len(SYSTEM_INSTRUCTION)
            total_chars_out += len(batch) * 500

        in_tokens_est = total_chars_in // 4
        out_tokens_est = total_chars_out // 4

        taint_only_count = sum(
            1 for fd in files_data
            if not fd.get("purpose_needs_update", True)
        )
        stale_count = len(files_data) - taint_only_count

        print("  Phase 2: summarize (dry run — no API calls)")
        print(f"    Selection: {len(files_data)} files ({stale_count} stale, {taint_only_count} taint-only)")
        print(f"    Functions needing summary: {total_funcs_needing_summary}")
        print(f"    Estimated batches: {len(batches)}")
        print(f"    Estimated input: ~{in_tokens_est:,} tokens")
        print(f"    Estimated output: ~{out_tokens_est:,} tokens")
        print()
        print("  Run without --dry-run to apply.")

        total_funcs_updated = 0
        total_in_tokens = 0
        total_out_tokens = 0
    else:
        print("  Phase 2: summarize")

        try:
            client = get_anthropic_client()
        except ValueError as e:
            logger.error("Summarization skipped: %s", e)
            print(f"    SKIPPED -- {e}")
            print("    Set ANTHROPIC_API_KEY and re-run to summarize.")
            client = None

        total_files_updated = 0
        total_funcs_updated = 0
        total_in_tokens = 0
        total_out_tokens = 0

        if client is not None:
            model = get_model_name()

            for i, batch in enumerate(batches):
                batch_funcs_count = sum(
                    1 for f in batch for func in f["functions"] if func["needs_summary"]
                )
                user_content = json.dumps(batch)
                try:
                    response_text, in_tok, out_tok = call_llm_with_retry(
                        client, model, SYSTEM_INSTRUCTION, user_content
                    )
                    parsed_results = parse_response(response_text)
                    files_up, funcs_up = apply_summary_batch(conn, parsed_results)
                    total_files_updated += files_up
                    total_funcs_updated += funcs_up
                    total_in_tokens += in_tok
                    total_out_tokens += out_tok

                    print(f"    [{i+1}/{len(batches)}] batch: {len(batch)} files, {batch_funcs_count} functions -> done (in: {in_tok:,} tok, out: {out_tok:,} tok)")
                except Exception as e:
                    logger.error("Failed to process batch %d: %s", i + 1, e)
                    print(f"    [{i+1}/{len(batches)}] batch: {len(batch)} files, {batch_funcs_count} functions -> FAILED (skipped)")

        print()

    # Phase 3: export context files (auto-calls ctx export)
    print("  Phase 3: export")
    if not dry_run:
        try:
            export_report = run_export(conn, repo_root)
            if export_report.written:
                print(f"    {', '.join(export_report.written)} updated")
                for p in export_report.written:
                    print(f"      {p}")
            if export_report.skipped:
                print(f"    ({len(export_report.skipped)} already current — skipped)")
            if not export_report.written and not export_report.skipped:
                print("    CLAUDE.md, .github/copilot-instructions.md, .ctx/opencode.md updated")
        except Exception as e:
            logger.error("Export failed: %s", e)
            print(f"    EXPORT FAILED: {e}")
    else:
        print("    (skipped — dry run)")

    # Phase 4: danger detection (auto-runs heuristics after export)
    print("  Phase 4: danger detection")
    if not dry_run:
        try:
            heuristic_report = run_heuristic_detection(conn, repo_root)
            added = len(heuristic_report.added)
            removed = len(heuristic_report.removed)
            if added or removed:
                print(f"    {added} danger zones added (auto)")
                print(f"    {removed} stale danger zone removed (auto)")
            else:
                print(f"    {len(heuristic_report.detected)} danger zones detected — all current")
        except Exception as e:
            logger.error("Heuristic detection failed: %s", e)
            print(f"    HEURISTIC DETECTION FAILED: {e}")
    else:
        print("    (skipped — dry run)")

    conn.close()

    if not dry_run and files_data:
        conn = connect(db_path)
        remaining_taint_queue = conn.execute("SELECT COUNT(*) FROM taint_queue").fetchone()[0]
        remaining_stale_funcs = conn.execute("SELECT COUNT(*) FROM functions WHERE is_stale = 1").fetchone()[0]
        remaining_stale_files = conn.execute("SELECT COUNT(*) FROM files WHERE is_stale = 1").fetchone()[0]
        conn.close()

        print("  Done.")
        print(f"    {total_funcs_updated} functions updated")
        print(f"    taint_queue: {remaining_taint_queue} entries remaining")
        print(f"    is_stale: {remaining_stale_files} files, {remaining_stale_funcs} functions")
