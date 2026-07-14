import logging
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
import json
import sqlite3

logger = logging.getLogger("ctx")


def run_sync(repo_root: Path, dry_run: bool = False) -> None:
    db_path = repo_root / ".ctx" / "index.db"
    if not db_path.exists():
        raise FileNotFoundError(
            "Database not found. Please run 'ctx init' first."
        )

    print("ctx sync")
    print()

    # ── Phase 1: Reindex ──────────────────────────────────────────────────
    conn = connect(db_path)
    conn.row_factory = None

    parseable = discover_parseable_files(repo_root)

    # Snapshot stale/taint counts before reindex
    before = conn.execute("SELECT COUNT(*) FROM files WHERE is_stale = 1").fetchone()[0]
    before_taint = conn.execute("SELECT COUNT(*) FROM functions WHERE is_tainted = 1").fetchone()[0]
    before_stale_funcs = conn.execute("SELECT COUNT(*) FROM functions WHERE is_stale = 1").fetchone()[0]

    parse_error_count = run_reindex_pipeline(conn, repo_root, parseable)

    after = conn.execute("SELECT COUNT(*) FROM files WHERE is_stale = 1").fetchone()[0]
    after_taint = conn.execute("SELECT COUNT(*) FROM functions WHERE is_tainted = 1").fetchone()[0]
    after_stale_funcs = conn.execute("SELECT COUNT(*) FROM functions WHERE is_stale = 1").fetchone()[0]
    total_files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]

    stale_files_delta = max(0, after - before)
    stale_funcs_delta = max(0, after_stale_funcs - before_stale_funcs)
    taint_delta = max(0, after_taint - before_taint)

    unchanged = total_files - stale_files_delta

    print("  Phase 1: reindex")
    if stale_files_delta > 0 or taint_delta > 0 or stale_funcs_delta > 0:
        print(f"    {stale_files_delta} files changed — {stale_funcs_delta} functions stale, {taint_delta} tainted")
    else:
        print("    No files changed.")
    print(f"    ({unchanged} files unchanged — hashes matched, metadata preserved)")
    if parse_error_count:
        print(f"    {parse_error_count} files with parse errors")

    conn.row_factory = None
    conn.close()

    # ── Phase 2: Summarize ────────────────────────────────────────────────
    print()

    conn = connect(db_path)
    conn.row_factory = sqlite3.Row

    files_data, total_funcs_needing_summary, batches = get_summarize_selection(
        conn, repo_root, force=False
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

        client = get_anthropic_client()
        model = get_model_name()

        total_files_updated = 0
        total_funcs_updated = 0
        total_in_tokens = 0
        total_out_tokens = 0

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

    conn.row_factory = None
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
