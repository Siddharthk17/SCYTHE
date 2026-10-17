import logging
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger("ctx")


class ApiModeSummarizer:
    """Batch watcher-detected changes through the summarize pipeline.

    Runs on a debounce timer (tens of seconds, not per-save) because every
    flush costs real money, unlike the free local-Ollama path.
    """

    def __init__(
        self,
        repo_root: Path,
        conn_factory: Callable[[], sqlite3.Connection],
        batch_window_s: float,
        max_batch_size: int,
    ):
        self._repo_root = repo_root
        self._conn_factory = conn_factory
        self._batch_window_s = batch_window_s
        self._max_batch_size = max_batch_size
        self._pending: set[str] = set()
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._batches_sent = 0

    def enqueue(self, function_ids: list[str]) -> None:
        """Non-blocking. Returns immediately."""
        with self._lock:
            self._pending.update(function_ids)
            if len(self._pending) >= self._max_batch_size:
                self._flush_locked()
                return
            if self._timer is None:
                self._timer = threading.Timer(
                    self._batch_window_s, self._flush_from_timer
                )
                self._timer.daemon = True
                self._timer.start()

    def _flush_from_timer(self) -> None:
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        """Caller must hold self._lock."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        ids = list(self._pending)
        self._pending.clear()
        if not ids:
            return
        threading.Thread(target=self._send_batch, args=(ids,), daemon=True).start()

    def _send_batch(self, function_ids: list[str]) -> None:
        conn = self._conn_factory()
        try:
            from ctx_engine.config import get_config_value
            from ctx_engine.intelligence.llm_client import (
                apply_summary_batch,
                build_batch_payload,
                call_llm_batch,
            )

            model = get_config_value(self._repo_root, "llm.model")
            payload = build_batch_payload(conn, self._repo_root, function_ids)
            result = call_llm_batch(self._repo_root, payload, command_name="watch-api")
            apply_summary_batch(conn, result.parsed)
            try:
                from ctx_engine.intelligence.cost_tracking import (
                    check_budget_warning,
                    log_cost,
                )

                log_cost(
                    self._repo_root,
                    "watch-api",
                    model,
                    result.input_tokens,
                    result.output_tokens,
                )
                warning = check_budget_warning(self._repo_root)
            except Exception:
                warning = None
            if warning:
                logger.warning(warning)
            logger.info(
                "watch-api: summarized %d function(s) in one batch", len(function_ids)
            )
            self._batches_sent += 1
            try:
                from ctx_engine.daemon.daemon import read_watch_state, write_watch_state

                state_path = self._repo_root / ".ctx" / "watch-state.json"
                state = read_watch_state(state_path)
                state["api_batches_sent"] = state.get("api_batches_sent", 0) + 1
                write_watch_state(state_path, state)
            except Exception:
                pass
        except Exception as err:
            logger.warning(
                "watch-api batch failed, functions remain flagged stale: %s", err
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass

    @property
    def batches_sent(self) -> int:
        return self._batches_sent

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def stop(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._flush_locked()
