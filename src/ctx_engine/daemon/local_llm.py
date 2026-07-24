import hashlib
import json
import logging
import os
import queue
import sqlite3
import threading
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

logger = logging.getLogger("ctx")

FUNCTION_SUMMARY_PROMPT = """You are updating a code index. Summarize this function in JSON.

Function: {function_id}
Signature: {signature}
Source:
{source}

Respond with ONLY this JSON object, no other text:
{{"summary": "<15 words max, starts with verb, what it does>", "summary_long": "<1-2 sentences, more detail>", "danger": "<one concrete invariant or null>"}}"""

FILE_PURPOSE_PROMPT = """Summarize this file in JSON.

File: {file_path}
Exports: {exports}
Source excerpt (first 50 lines):
{excerpt}

Respond with ONLY this JSON:
{{"purpose": "<1 sentence: what file does and why>", "summary": "<10 words max>"}}"""

PREFERRED_MODELS = [
    "qwen2.5-coder:7b-instruct-q4_K_M",
    "qwen2.5-coder:7b-instruct",
    "qwen2.5-coder:3b-instruct",
    "codellama:7b-instruct",
    "llama3.2:3b-instruct",
]


def is_ollama_available(host: str = "http://localhost:11434") -> bool:
    try:
        req = urllib.request.Request(f"{host}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def get_available_models(host: str = "http://localhost:11434") -> list[str]:
    try:
        req = urllib.request.Request(f"{host}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
            return [m["name"] for m in data.get("models", [])]
    except Exception:
        return []


def select_model(
    available: list[str],
    override: str | None = None,
) -> str | None:
    if override:
        return override if override in available else None
    for model in PREFERRED_MODELS:
        if model in available:
            return model
    return None


class OllamaClient:
    def __init__(
        self,
        model: str,
        host: str,
        conn_factory: Callable[[], sqlite3.Connection],
        repo_root: Path,
    ):
        self._model = model
        self._host = host
        self._conn_factory = conn_factory
        self._repo_root = repo_root
        self._queue: queue.Queue[tuple[str, list[str]] | None] = queue.Queue()
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def enqueue(self, file_path: str, function_ids: list[str]) -> None:
        self._queue.put((file_path, function_ids))

    def stop(self) -> None:
        self._queue.put(None)
        self._thread.join(timeout=5)

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                break
            file_path, function_ids = item
            conn = self._conn_factory()
            try:
                self._summarize_functions(conn, file_path, function_ids)
            except Exception as e:
                logger.warning(
                    "Ollama summarization failed for %s: %s", file_path, e
                )
            finally:
                conn.close()
                self._queue.task_done()

    def _summarize_functions(
        self,
        conn: sqlite3.Connection,
        file_path: str,
        function_ids: list[str],
    ) -> None:
        for fn_id in function_ids:
            fn_row = conn.execute(
                "SELECT * FROM functions WHERE id = ?", (fn_id,)
            ).fetchone()
            if fn_row is None:
                continue

            abs_path = self._repo_root / fn_row["file"]
            try:
                source_lines = abs_path.read_text(
                    encoding="utf-8"
                ).splitlines()
                source = "\n".join(
                    source_lines[
                        fn_row["line_start"] - 1 : fn_row["line_end"]
                    ]
                )
            except (IOError, UnicodeDecodeError):
                continue

            prompt = FUNCTION_SUMMARY_PROMPT.format(
                function_id=fn_id,
                signature=fn_row["signature"],
                source=source,
            )

            response_text = self._call_ollama(prompt)
            if response_text is None:
                continue

            parsed = self._parse_response(response_text)
            if parsed is None:
                continue

            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """UPDATE functions
                   SET summary = ?, summary_long = ?, danger = ?,
                       confidence = 0.8, is_stale = 0,
                       is_tainted = 0, taint_source = NULL, updated_at = ?
                   WHERE id = ?""",
                (
                    parsed.get("summary"),
                    parsed.get("summary_long"),
                    parsed.get("danger"),
                    now,
                    fn_id,
                ),
            )
            conn.execute(
                "DELETE FROM taint_queue WHERE function_id = ?", (fn_id,)
            )
            conn.commit()
            logger.info(
                "Ollama summarized (confidence=0.8): %s", fn_id
            )

    def _call_ollama(self, prompt: str) -> str | None:
        body = json.dumps(
            {
                "model": self._model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": 0.1,
                    "num_predict": 200,
                },
            }
        ).encode()
        try:
            req = urllib.request.Request(
                f"{self._host}/api/generate",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                return data.get("response", "").strip()
        except Exception as e:
            logger.warning("Ollama API call failed: %s", e)
            return None

    def _parse_response(self, text: str) -> dict | None:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0]
            cleaned = cleaned.strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            logger.warning(
                "Ollama response is not valid JSON: %.100s", text
            )
            return None
