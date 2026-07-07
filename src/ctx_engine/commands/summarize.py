# summarize command implementation for ctx.

import json
import logging
import sqlite3
from pathlib import Path
from ctx_engine.db import connect
from ctx_engine.intelligence.llm_client import (
    get_anthropic_client,
    get_model_name,
    call_llm_with_retry,
    parse_response,
    batch_files,
    apply_summary_batch,
    SYSTEM_INSTRUCTION,
)

logger = logging.getLogger("ctx")

def get_function_source(file_abspath: Path, line_start: int, line_end: int) -> str:
    """Extract the raw lines of code for a function from the local file."""
    try:
        lines = file_abspath.read_text(encoding="utf-8").splitlines()
        # line_start and line_end are 1-indexed, inclusive
        return "\n".join(lines[line_start-1:line_end])
    except Exception:
        return ""

def get_taint_warning(conn: sqlite3.Connection, taint_source_id: str) -> str:
    """Resolve the taint source function ID to a human-readable warning note."""
    if not taint_source_id:
        return "depends on a dependency that changed"
    row = conn.execute("SELECT file, name, class_name FROM functions WHERE id = ?", (taint_source_id,)).fetchone()
    if row:
        if isinstance(row, sqlite3.Row):
            func_name = f"{row['class_name']}.{row['name']}" if row['class_name'] else row['name']
            file_name = row['file']
        else:
            file_name, name, class_name = row
            func_name = f"{class_name}.{name}" if class_name else name
        return f"depends on {func_name}() in {file_name}, which changed"
    return f"depends on {taint_source_id}, which changed"

def run_summarize(
    repo_root: Path,
    batch_size: int = 20,
    force: bool = False,
    dry_run: bool = False
) -> None:
    """Perform bulk LLM summarization of files/functions needing updates."""
    db_path = repo_root / ".ctx" / "index.db"
    if not db_path.exists():
        raise FileNotFoundError("Database not found. Please run 'ctx init' first.")

    conn = connect(db_path)
    conn.row_factory = sqlite3.Row

    # 1. Determine files to summarize
    if force:
        files_rows = conn.execute("SELECT path, purpose, summary, danger, exports, imports FROM files;").fetchall()
    else:
        files_rows = conn.execute(
            "SELECT path, purpose, summary, danger, exports, imports FROM files WHERE purpose IS NULL OR is_stale = 1;"
        ).fetchall()

    if not files_rows:
        print("No files need summarization.")
        conn.close()
        return

    # 2. Build payloads for each file
    files_data = []
    total_funcs_needing_summary = 0

    for f_row in files_rows:
        path = f_row["path"]
        
        # Get functions in this file
        funcs_rows = conn.execute(
            "SELECT id, class_name, name, signature, summary, summary_long, danger, line_start, line_end, is_stale, is_tainted, taint_source FROM functions WHERE file = ?;",
            (path,)
        ).fetchall()

        funcs_payload = []
        for func_row in funcs_rows:
            # needs_summary is true if functions is stale, doesn't have summary, or force is active
            needs_sum = force or func_row["is_stale"] == 1 or func_row["summary"] is None
            
            func_data = {
                "id": func_row["id"],
                "signature": func_row["signature"],
                "needs_summary": needs_sum,
                "current_summary": func_row["summary"],
                "taint_warning": get_taint_warning(conn, func_row["taint_source"]) if func_row["is_tainted"] == 1 else None,
            }
            if needs_sum:
                func_data["source"] = get_function_source(repo_root / path, func_row["line_start"], func_row["line_end"])
                total_funcs_needing_summary += 1
            funcs_payload.append(func_data)

        files_data.append({
            "path": path,
            "exports": json.loads(f_row["exports"]) if f_row["exports"] else [],
            "imports": json.loads(f_row["imports"]) if f_row["imports"] else [],
            "used_by_count": conn.execute("SELECT count(*) FROM files WHERE imports LIKE ?", (f'%"{path}"%',)).fetchone()[0],
            "current_purpose": f_row["purpose"],
            "functions": funcs_payload
        })

    # Group into batches
    batches = batch_files(files_data, max_files_per_batch=batch_size)

    # 3. Handle Dry Run
    if dry_run:
        # Simple character estimate
        total_chars_in = 0
        total_chars_out = 0
        for batch in batches:
            total_chars_in += len(json.dumps(batch)) + len(SYSTEM_INSTRUCTION)
            total_chars_out += len(batch) * 500  # Estimate 500 characters output per file

        in_tokens_est = total_chars_in // 4
        out_tokens_est = total_chars_out // 4

        repo_name = repo_root.name
        print(f"ctx summarize — {repo_name} (dry run)")
        print()
        print(f"  {len(files_data)} files selected for summarization")
        print(f"  {total_funcs_needing_summary} functions needing summary")
        print(f"  {len(batches)} batches planned")
        print(f"  Estimated tokens: {in_tokens_est:,} input, {out_tokens_est:,} output (estimate only)")
        conn.close()
        return

    # 4. Actual execution
    client = get_anthropic_client()
    model = get_model_name()
    
    skipped_files = []
    total_files_updated = 0
    total_funcs_updated = 0
    total_in_tokens = 0
    total_out_tokens = 0

    for i, batch in enumerate(batches):
        batch_funcs_count = sum(1 for f in batch for func in f["functions"] if func["needs_summary"])
        user_content = json.dumps(batch)
        try:
            response_text, in_tok, out_tok = call_llm_with_retry(
                client, model, SYSTEM_INSTRUCTION, user_content
            )
            parsed_results = parse_response(response_text)
            
            # Apply database changes
            files_up, funcs_up = apply_summary_batch(conn, parsed_results)
            total_files_updated += files_up
            total_funcs_updated += funcs_up
            total_in_tokens += in_tok
            total_out_tokens += out_tok
            
            print(f"[{i+1}/{len(batches)}] batch: {len(batch)} files, {batch_funcs_count} functions -> done (in: {in_tok:,} tok, out: {out_tok:,} tok)")
        except Exception as e:
            logger.error("Failed to process batch %d: %s", i + 1, e)
            for f in batch:
                skipped_files.append(f["path"])
            print(f"[{i+1}/{len(batches)}] batch: {len(batch)} files, {batch_funcs_count} functions -> FAILED (skipped)")

    conn.close()

    # 5. Final Report
    repo_name = repo_root.name
    skipped_count = len(skipped_files)

    print()
    print(f"ctx summarize — {repo_name}")
    print()
    print(f"  {total_files_updated} files processed across {len(batches)} batches")
    print(f"  {total_funcs_updated} functions summarized")
    print(f"  {total_in_tokens:,} total input tokens, {total_out_tokens:,} total output tokens")
    print(f"  {skipped_count} files skipped due to batch errors")
    if skipped_count > 0:
        for skip_path in sorted(skipped_files):
            print(f"      - {skip_path}")
