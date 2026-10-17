import json
import logging
import os
import time
import sqlite3
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    import anthropic

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger("ctx")

SYSTEM_INSTRUCTION: str = (
    "You are a technical codebase context generator. For each file in the input JSON, produce a response adhering strictly to the schema rules.\n\n"
    "File-level rules:\n"
    "- purpose: one sentence, what the file does and why it exists.\n"
    "- summary: 10 words or fewer, explaining the file's purpose.\n"
    "- danger: one sentence detailing the most critical invariant in the file, or null.\n\n"
    "Function-level rules (only for functions where needs_summary is true):\n"
    "- summary: 15 words or fewer, starting with an active verb, explaining the action and result.\n"
    "- summary_long: one to two sentences, providing more detail.\n"
    "- danger: one concrete invariant specific to this function, or null.\n\n"
    "Context rules:\n"
    "- If a function has a taint_warning, take the warning into account because the dependency it references has changed; its summary should reflect its current behavior in light of that.\n\n"
    "- If purpose_needs_update is false for a file, do not return purpose, summary, or danger at the file level — return only the functions array for that file.\n\n"
    "Format requirements:\n"
    "Respond with a JSON array only. No markdown code fences, no preamble, no trailing commentary — the response must be valid JSON starting with `[` and ending with `]`."
)

def get_anthropic_client() -> "anthropic.Anthropic":
    """Return an instantiated Anthropic client, checking for the API key lazily.

    The anthropic SDK is imported lazily so that offline commands
    (ctx init, ctx status, ctx summarize --dry-run) work without the
    dependency installed and without an API key configured.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY is not set. Required for 'ctx summarize' / 'ctx update'.")
    try:
        import anthropic as _anthropic
    except ImportError as err:
        raise ImportError(
            "The 'anthropic' package is required for 'ctx summarize' / 'ctx update'. "
            "Install it with: pip install anthropic"
        ) from err
    return _anthropic.Anthropic(api_key=api_key)

def get_model_name(repo_root=None) -> str:
    """Get the summarization model: env CTX_LLM_MODEL > config llm.model > default."""
    direct = os.environ.get("CTX_LLM_MODEL")
    if direct:
        return direct
    if repo_root is not None:
        try:
            from ctx_engine.config import get_config_value as _get_config_value

            return _get_config_value(repo_root, "llm.model")
        except (ValueError, TypeError):
            pass
    return os.environ.get("CTX_LLM_MODEL", "claude-haiku-4-5-20251001")

def call_llm_with_retry(
    client: "Any",
    model: str,
    system_prompt: str,
    user_content: str,
    max_tokens: int = 4000,
) -> tuple[str, int, int]:
    """Call the Anthropic API with retry and exponential backoff on transient errors.

    Retries 3 attempts with base delay 2s on 429/5xx. Logs a warning on
    each retry. Raises the last error if all attempts fail — callers must
    catch per-batch so one failed batch never aborts the whole run.

    Returns (response_text, input_tokens, output_tokens).
    Raises the last error if all retry attempts fail.
    """
    attempts = 3
    delay = 2
    # Resolve transient-error types lazily so this module imports without anthropic.
    try:
        import anthropic as _anthropic

        _transient_types: tuple[type[BaseException], ...] = (
            _anthropic.RateLimitError,
            _anthropic.InternalServerError,
            _anthropic.APIStatusError,
        )
    except ImportError:
        _transient_types = ()
    for attempt in range(attempts):
        try:
            message = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=[
                    {"role": "user", "content": user_content}
                ]
            )
            input_tokens = message.usage.input_tokens
            output_tokens = message.usage.output_tokens

            response_text = ""
            for block in message.content:
                if block.type == "text":
                    response_text += block.text
            return response_text, input_tokens, output_tokens
        except Exception as e:
            status_code = getattr(e, "status_code", None)
            is_transient = status_code in (429, 500, 502, 503, 504) or (
                bool(_transient_types) and isinstance(e, _transient_types)
            )

            if is_transient and attempt < attempts - 1:
                logger.warning(
                    "Anthropic API call failed (attempt %d/%d): %s. Retrying in %ds...",
                    attempt + 1, attempts, e, delay
                )
                time.sleep(delay)
                delay *= 2
            else:
                raise
    raise RuntimeError("Failed to call Anthropic API after max retries")

def parse_response(text: str) -> list[dict]:
    """Defensively clean and parse the JSON array response from the model.

    Strips ``` / ```json fences (models wrap raw JSON anyway) before
    json.loads. On failure raises with a truncated raw preview so callers
    can log the offending payload together with the batch file paths.

    Raises ValueError if the response is empty, contains only fence markers,
    or is not a JSON array. Raises json.JSONDecodeError on malformed JSON.
    """
    if not text or not text.strip():
        raise ValueError("Empty response from LLM — no content received")
    cleaned = text.strip()
    if cleaned.startswith("```"):
        parts = cleaned.split("\n", 1)
        cleaned = parts[1] if len(parts) > 1 else ""
        if "```" in cleaned:
            cleaned = cleaned.rsplit("```", 1)[0]
    if cleaned.lstrip().startswith("json"):
        stripped = cleaned.lstrip()
        if "\n" in stripped:
            cleaned = stripped.split("\n", 1)[1]
        else:
            cleaned = stripped[4:]
    cleaned = cleaned.strip()
    if not cleaned:
        raise ValueError("Response contained only fence markers — no JSON content")
    # Let json.JSONDecodeError (a ValueError subclass) propagate — callers
    # catch per-batch, log raw response + file paths, and continue.
    result = json.loads(cleaned)
    if not isinstance(result, list):
        raise ValueError(f"Expected JSON array from LLM, got {type(result).__name__}")
    return result


def batch_files(
    files_data: list[dict],
    max_files_per_batch: int = 20,
    max_tokens_per_batch: int = 50000
) -> list[list[dict]]:
    """Group file payloads into batches bounded by file count and estimated input size.

    A batch closes when either 20 files or ~50,000 estimated input tokens
    (len(json) // 4 char-based estimate) is reached, whichever comes first.
    A single file whose payload alone exceeds the token budget forms its
    own batch. ctx update <file> is always a batch of one.
    """
    if max_files_per_batch < 1:
        raise ValueError(f"max_files_per_batch must be >= 1, got {max_files_per_batch}")
    batches = []
    current_batch: list[dict] = []
    current_tokens = 0
    for f in files_data:
        f_text = json.dumps(f)
        f_tokens = len(f_text) // 4
        if len(current_batch) >= max_files_per_batch or (current_batch and current_tokens + f_tokens > max_tokens_per_batch):
            batches.append(current_batch)
            current_batch = [f]
            current_tokens = f_tokens
        else:
            current_batch.append(f)
            current_tokens += f_tokens
    if current_batch:
        batches.append(current_batch)
    return batches


def apply_summary_batch(conn: sqlite3.Connection, parsed_results: list[dict]) -> tuple[int, int]:
    """Apply the parsed summary results to the database and clean up taint queue.

    File-level update (purpose/summary/danger, confidence=1.0, is_stale=0)
    runs when the request asked for it (purpose_needs_update, default True
    per Week 2 spec) OR when the model returned file-level fields anyway.
    Taint-only files (purpose_needs_update=False) intentionally skip the
    file row — they are already fresh (is_stale=0, confidence=1.0) and only
    their tainted functions need refresh. Function updates always reset
    confidence=1.0, is_stale=0, is_tainted=0, taint_source=NULL and delete
    the corresponding taint_queue row.

    Returns (files_updated, functions_updated).
    """
    files_updated = 0
    functions_updated = 0
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        for file_obj in parsed_results:
            path = file_obj.get("path")
            purpose_needs_update = file_obj.get("purpose_needs_update", True)

            if not path:
                continue

            # Spec-default is unconditional file update (flag defaults True).
            # Taint-only optimization (flag False) skips the already-fresh
            # file row unless the model returned file fields anyway.
            wants_file_update = purpose_needs_update or any(
                k in file_obj for k in ("purpose", "summary", "danger")
            )

            if wants_file_update:
                purpose = file_obj.get("purpose")
                summary = file_obj.get("summary")
                danger = file_obj.get("danger")

                conn.execute(
                    """
                    UPDATE files
                    SET purpose = ?, summary = ?, danger = ?, confidence = 1.0, is_stale = 0, updated_at = ?
                    WHERE path = ?
                    """,
                    (purpose, summary, danger, now, path)
                )
                files_updated += 1

            for func_obj in file_obj.get("functions", []):
                func_id = func_obj.get("id")
                f_summary = func_obj.get("summary")
                f_summary_long = func_obj.get("summary_long")
                f_danger = func_obj.get("danger")

                if not func_id:
                    continue

                conn.execute(
                    """
                    UPDATE functions
                    SET summary = ?, summary_long = ?, danger = ?,
                        confidence = 1.0, is_stale = 0, is_tainted = 0, taint_source = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (f_summary, f_summary_long, f_danger, now, func_id)
                )
                conn.execute("DELETE FROM taint_queue WHERE function_id = ?", (func_id,))
                functions_updated += 1
    return files_updated, functions_updated


# ── Week 11: shared batch machinery (ctx summarize + watch-api) ─────────────

from dataclasses import dataclass, field
from pathlib import Path as _Path


@dataclass
class LLMBatchResult:
    parsed: list[dict] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


def _build_payload_for_files(
    conn: sqlite3.Connection,
    repo_root: _Path,
    files_touched: set[str],
    force_function_ids: set[str] | None = None,
) -> list[dict]:
    """Build summarize-shaped file payloads for an explicit file set.

    Same per-file/per-function keys as ``ctx summarize`` (path,
    purpose_needs_update, exports, imports, used_by_count, current_purpose,
    functions[] with id/signature/mutates/needs_summary/current_summary/
    taint_warning/source). Functions in ``force_function_ids`` are always
    marked needs_summary with source included.
    """
    from ctx_engine.commands.summarize import (
        get_function_source as _get_source,
        get_taint_warning as _get_taint,
    )

    forced = set(force_function_ids or set())
    files_data: list[dict] = []
    for path in sorted(files_touched):
        f_row = conn.execute(
            "SELECT path, purpose, exports, imports, used_by_count, is_stale "
            "FROM files WHERE path = ?",
            (path,),
        ).fetchone()
        if f_row is None:
            continue
        if isinstance(f_row, sqlite3.Row):
            purpose = f_row["purpose"]
            exports_raw = f_row["exports"]
            imports_raw = f_row["imports"]
            used_by_count = f_row["used_by_count"]
            is_stale = f_row["is_stale"]
        else:
            purpose, exports_raw, imports_raw, used_by_count, is_stale = (
                f_row[1], f_row[2], f_row[3], f_row[4], f_row[5],
            )
        is_taint_only = purpose is not None and is_stale == 0

        try:
            import json as _json

            exports = _json.loads(exports_raw) if exports_raw else []
            imports = _json.loads(imports_raw) if imports_raw else []
        except (ValueError, TypeError):
            exports, imports = [], []

        funcs_rows = conn.execute(
            "SELECT id, class_name, name, signature, summary, summary_long, "
            "danger, line_start, line_end, is_stale, is_tainted, taint_source, "
            "mutates FROM functions WHERE file = ?;",
            (path,),
        ).fetchall()

        funcs_payload: list[dict] = []
        for func_row in funcs_rows:
            if isinstance(func_row, sqlite3.Row):
                fid = func_row["id"]
                needs = (
                    func_row["is_stale"] == 1
                    or func_row["summary"] is None
                    or func_row["is_tainted"] == 1
                    or fid in forced
                )
                import json as _json2

                muts = _json2.loads(func_row["mutates"]) if func_row["mutates"] else []
                entry: dict = {
                    "id": fid,
                    "signature": func_row["signature"],
                    "mutates": muts,
                    "needs_summary": needs,
                    "current_summary": func_row["summary"],
                    "taint_warning": _get_taint(conn, func_row["taint_source"])
                    if func_row["is_tainted"] == 1
                    else None,
                }
                if needs:
                    entry["source"] = _get_source(
                        repo_root / path, func_row["line_start"], func_row["line_end"]
                    )
            else:
                fid = func_row[0]
                needs = fid in forced
                entry = {
                    "id": fid,
                    "signature": func_row[3],
                    "mutates": [],
                    "needs_summary": needs,
                    "current_summary": None,
                    "taint_warning": None,
                }
            funcs_payload.append(entry)

        files_data.append(
            {
                "path": path,
                "purpose_needs_update": not is_taint_only,
                "exports": exports,
                "imports": imports,
                "used_by_count": used_by_count,
                "current_purpose": purpose,
                "functions": funcs_payload,
            }
        )
    return files_data


def build_batch_payload(
    conn: sqlite3.Connection,
    repo_root: _Path,
    function_ids: list[str],
) -> list[dict]:
    """Build a summarize-shaped payload scoped to explicit function ids."""
    if not function_ids:
        return []
    placeholders = ",".join("?" * len(function_ids))
    rows = conn.execute(
        f"SELECT DISTINCT file FROM functions WHERE id IN ({placeholders})",
        function_ids,
    ).fetchall()
    files_touched = set()
    for r in rows:
        if isinstance(r, sqlite3.Row):
            files_touched.add(r["file"])
        else:
            files_touched.add(r[0])
    return _build_payload_for_files(
        conn, repo_root, files_touched, force_function_ids=set(function_ids)
    )


def call_llm_batch(
    repo_root: _Path,
    payload: list[dict],
    command_name: str,
    _client: Any | None = None,
    _model: str | None = None,
) -> LLMBatchResult:
    """Run one LLM batch: call, parse, return usage. Shared by summarize/api-watch.

    ``_client``/``_model`` overrides exist so ``ctx summarize`` (whose tests
    mock its own module-level client factory) can reuse this path without
    changing its observable mock points. Production callers omit them.
    """
    client = _client if _client is not None else get_anthropic_client()
    model = _model if _model is not None else get_model_name(repo_root)
    user_content = json.dumps(payload)
    response_text, in_tok, out_tok = call_llm_with_retry(
        client, model, SYSTEM_INSTRUCTION, user_content
    )
    parsed = parse_response(response_text)
    return LLMBatchResult(parsed=parsed, input_tokens=in_tok, output_tokens=out_tok)
