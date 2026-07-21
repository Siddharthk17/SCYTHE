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
    """Extract the raw lines of code for a function from the local file.

    Returns the source code string, or an empty string on any I/O error.
    """
    try:
        lines = file_abspath.read_text(encoding="utf-8").splitlines()
        return "\n".join(lines[line_start-1:line_end])
    except (FileNotFoundError, PermissionError, OSError) as err:
        logger.warning("Cannot read %s for source extraction: %s", file_abspath, err)
        return ""


def get_taint_warning(conn: sqlite3.Connection, taint_source_id: str | None) -> str:
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


def get_summarize_selection(
    conn: sqlite3.Connection,
    repo_root: Path,
    force: bool = False,
    batch_size: int = 20,
    path_filter: set[str] | None = None,
) -> tuple[list[dict], int, list[list[dict]]]:
    """Build file payloads for summarization selection.

    When path_filter is provided (from Phase 1's changed-file set),
    use it instead of re-querying for stale/null-purpose files.

    Returns (files_data, total_funcs_needing_summary, batches).
    """
    if force:
        files_rows = conn.execute("SELECT path, purpose, summary, danger, exports, imports, used_by_count, is_stale FROM files;").fetchall()
    elif path_filter is not None:
        placeholders = ",".join("?" for _ in path_filter)
        files_rows = conn.execute(
            f"""
            SELECT DISTINCT f.path, f.purpose, f.summary, f.danger, f.exports, f.imports, f.used_by_count, f.is_stale
            FROM files f
            WHERE f.path IN ({placeholders})
               OR EXISTS (
                   SELECT 1 FROM functions fn
                   WHERE fn.file = f.path
                     AND fn.is_tainted = 1
               )
            """,
            list(path_filter)
        ).fetchall()
    else:
        files_rows = conn.execute(
            """
            SELECT DISTINCT f.path, f.purpose, f.summary, f.danger, f.exports, f.imports, f.used_by_count, f.is_stale
            FROM files f
            WHERE f.purpose IS NULL
               OR f.is_stale = 1
               OR EXISTS (
                   SELECT 1 FROM functions fn
                   WHERE fn.file = f.path
                     AND fn.is_tainted = 1
               )
            """
        ).fetchall()

    files_data: list[dict] = []
    total_funcs_needing_summary = 0

    for f_row in files_rows:
        path = f_row["path"]
        is_taint_only = f_row["purpose"] is not None and f_row["is_stale"] == 0

        funcs_rows = conn.execute(
            "SELECT id, class_name, name, signature, summary, summary_long, danger, line_start, line_end, is_stale, is_tainted, taint_source, mutates FROM functions WHERE file = ?;",
            (path,)
        ).fetchall()

        funcs_payload: list[dict] = []
        for func_row in funcs_rows:
            needs_sum = force or func_row["is_stale"] == 1 or func_row["summary"] is None or (is_taint_only and func_row["is_tainted"] == 1)

            func_data = {
                "id": func_row["id"],
                "signature": func_row["signature"],
                "mutates": json.loads(func_row["mutates"]) if func_row["mutates"] else [],
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
            "purpose_needs_update": not is_taint_only,
            "exports": json.loads(f_row["exports"]) if f_row["exports"] else [],
            "imports": json.loads(f_row["imports"]) if f_row["imports"] else [],
            "used_by_count": f_row["used_by_count"],
            "current_purpose": f_row["purpose"],
            "functions": funcs_payload
        })

    batches = batch_files(files_data, max_files_per_batch=batch_size)
    return files_data, total_funcs_needing_summary, batches


def run_summarize(
    repo_root: Path,
    batch_size: int = 20,
    force: bool = False,
    dry_run: bool = False
) -> None:
    """Perform bulk LLM summarization of files/functions needing updates."""
    if batch_size < 1:
        raise ValueError(f"batch_size must be >= 1, got {batch_size}")
    db_path = repo_root / ".ctx" / "index.db"
    if not db_path.exists():
        raise FileNotFoundError("Database not found. Please run 'ctx init' first.")

    conn = connect(db_path)
    conn.row_factory = sqlite3.Row

    files_data, total_funcs_needing_summary, batches = get_summarize_selection(conn, repo_root, force, batch_size)

    if not files_data:
        print("No files need summarization.")
        conn.close()
        return

    if dry_run:
        total_chars_in = 0
        total_chars_out = 0
        for batch in batches:
            total_chars_in += len(json.dumps(batch)) + len(SYSTEM_INSTRUCTION)
            total_chars_out += len(batch) * 500

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

    client = get_anthropic_client()
    model = get_model_name()

    skipped_files: list[str] = []
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
