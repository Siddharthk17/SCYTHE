import json
import logging
import sqlite3
from pathlib import Path
import click
from ctx_engine.db import connect
from ctx_engine.discovery import EXTENSION_TO_LANGUAGE
from ctx_engine.reindex import run_reindex_pipeline
from ctx_engine.commands.summarize import get_function_source, get_taint_warning
from ctx_engine.intelligence.llm_client import (
    get_anthropic_client,
    get_model_name,
    call_llm_with_retry,
    parse_response,
    apply_summary_batch,
    SYSTEM_INSTRUCTION,
)

logger = logging.getLogger("ctx")


def run_update(repo_root: Path, file_path_str: str) -> None:
    """Reindex and unconditionally summarize a single file, displaying before/after diffs."""
    db_path = repo_root / ".ctx" / "index.db"
    if not db_path.exists():
        raise FileNotFoundError("Database not found. Please run 'ctx init' first.")

    conn = connect(db_path)
    conn.row_factory = sqlite3.Row

    repo_root_resolved = repo_root.resolve()
    candidate = Path(file_path_str)
    if candidate.is_absolute():
        candidate_resolved = candidate.resolve()
        try:
            relative_path = candidate_resolved.relative_to(repo_root_resolved).as_posix()
        except ValueError:
            click.echo(f"Error: {file_path_str} is outside the repository root ({repo_root})", err=True)
            conn.close()
            raise click.Abort()
    else:
        relative_path = candidate.as_posix()

    file_row = conn.execute("SELECT path, purpose, summary FROM files WHERE path = ?", (relative_path,)).fetchone()
    if not file_row:
        click.echo(f"Error: {file_path_str} is not indexed. Run 'ctx init' first.", err=True)
        conn.close()
        raise click.Abort()

    old_purpose = file_row["purpose"]

    old_funcs: dict[str, str | None] = {
        row["id"]: row["summary"] for row in conn.execute(
            "SELECT id, summary FROM functions WHERE file = ?", (relative_path,)
        ).fetchall()
    }

    ext = Path(relative_path).suffix
    language = EXTENSION_TO_LANGUAGE.get(ext)
    if not language:
        click.echo(f"Error: Unsupported file type for {file_path_str}", err=True)
        conn.close()
        raise click.Abort()

    parse_error_count, parse_error_paths, _ = run_reindex_pipeline(conn, repo_root, {relative_path: language})
    if parse_error_count > 0:
        logger.warning("Update for %s encountered %d parse error(s) in file", relative_path, parse_error_count)

    refreshed_file = conn.execute(
        "SELECT path, purpose, summary, danger, exports, imports, used_by_count FROM files WHERE path = ?",
        (relative_path,)
    ).fetchone()
    refreshed_funcs = conn.execute(
        "SELECT id, class_name, name, signature, summary, line_start, line_end, is_tainted, taint_source, mutates FROM functions WHERE file = ?",
        (relative_path,)
    ).fetchall()

    funcs_payload: list[dict] = []
    for func_row in refreshed_funcs:
        func_id = func_row["id"]
        funcs_payload.append({
            "id": func_id,
            "signature": func_row["signature"],
            "mutates": json.loads(func_row["mutates"]) if func_row["mutates"] else [],
            "needs_summary": True,
            "current_summary": func_row["summary"],
            "taint_warning": get_taint_warning(conn, func_row["taint_source"]) if func_row["is_tainted"] == 1 else None,
            "source": get_function_source(repo_root / relative_path, func_row["line_start"], func_row["line_end"])
        })

    file_payload = {
        "path": relative_path,
        "purpose_needs_update": True,
        "exports": json.loads(refreshed_file["exports"]) if refreshed_file["exports"] else [],
        "imports": json.loads(refreshed_file["imports"]) if refreshed_file["imports"] else [],
        "used_by_count": refreshed_file["used_by_count"],
        "current_purpose": refreshed_file["purpose"],
        "functions": funcs_payload
    }

    client = get_anthropic_client()
    model = get_model_name()

    user_content = json.dumps([file_payload])
    response_text, _, _ = call_llm_with_retry(client, model, SYSTEM_INSTRUCTION, user_content)
    parsed_results = parse_response(response_text)

    apply_summary_batch(conn, parsed_results)

    new_file_row = conn.execute("SELECT purpose, summary FROM files WHERE path = ?", (relative_path,)).fetchone()
    new_funcs: dict[str, str | None] = {
        row["id"]: row["summary"] for row in conn.execute(
            "SELECT id, summary FROM functions WHERE file = ?", (relative_path,)
        ).fetchall()
    }

    conn.close()

    print(f"ctx update — {relative_path}")
    print()
    print("  purpose:")
    print(f"    - {old_purpose or '(none)'}")
    print(f"    + {new_file_row['purpose'] or '(none)'}")
    print()

    for func_id, new_sum in new_funcs.items():
        func_name = func_id.split("::", 1)[1]
        old_sum = old_funcs.get(func_id)

        print(f"  {func_name}:")
        if old_sum == new_sum:
            print("    (unchanged)")
        else:
            print(f"    - {old_sum or '(none)'}")
            print(f"    + {new_sum or '(none)'}")
        print()
