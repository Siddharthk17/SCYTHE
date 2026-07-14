# LLM Client integration using the Anthropic API.

import json
import logging
import os
import time
import sqlite3
from datetime import datetime, timezone
import anthropic

logger = logging.getLogger("ctx")

SYSTEM_INSTRUCTION = (
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

def get_anthropic_client() -> anthropic.Anthropic:
    """Return an instantiated Anthropic client, checking for the API key lazily."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY is not set. Required for 'ctx summarize' / 'ctx update'.")
    return anthropic.Anthropic(api_key=api_key)

def get_model_name() -> str:
    """Get the model name from CTX_LLM_MODEL environment variable or default to claude-3-5-haiku-20241022."""
    return os.environ.get("CTX_LLM_MODEL", "claude-3-5-haiku-20241022")

def call_llm_with_retry(
    client: anthropic.Anthropic,
    model: str,
    system_prompt: str,
    user_content: str
) -> tuple[str, int, int]:
    """Call the Anthropic API with retry and exponential backoff on transient errors."""
    attempts = 3
    delay = 2
    for attempt in range(attempts):
        try:
            message = client.messages.create(
                model=model,
                max_tokens=4000,
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
            # Identify transient errors (429, 5xx)
            is_transient = False
            status_code = getattr(e, "status_code", None)
            if status_code in (429, 500, 502, 503, 504) or isinstance(e, (anthropic.RateLimitError, anthropic.InternalServerError)):
                is_transient = True
                
            if is_transient and attempt < attempts - 1:
                logger.warning(
                    "Anthropic API call failed (attempt %d/%d): %s. Retrying in %ds...",
                    attempt + 1, attempts, e, delay
                )
                time.sleep(delay)
                delay *= 2
            else:
                raise e
    raise RuntimeError("Failed to call Anthropic API after max retries")

def parse_response(text: str) -> list[dict]:
    """Defensively clean and parse the JSON array response from the model."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1]
        cleaned = cleaned.rsplit("```", 1)[0]
    if cleaned.startswith("json"):
        if "\n" in cleaned:
            cleaned = cleaned.split("\n", 1)[1]
        else:
            cleaned = cleaned[4:]
    cleaned = cleaned.strip()
    return json.loads(cleaned)

def batch_files(
    files_data: list[dict],
    max_files_per_batch: int = 20,
    max_tokens_per_batch: int = 50000
) -> list[list[dict]]:
    """Group file payloads into batches bounded by file count and estimated input token count."""
    batches = []
    current_batch = []
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
    """Apply the parsed summary results to the database and clean up taint queue."""
    files_updated = 0
    functions_updated = 0
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        for file_obj in parsed_results:
            path = file_obj.get("path")
            purpose_needs_update = file_obj.get("purpose_needs_update", True)
            
            if not path:
                continue
            
            if purpose_needs_update:
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
