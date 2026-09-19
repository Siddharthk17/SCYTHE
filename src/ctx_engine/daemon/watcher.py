import hashlib
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable

from watchdog.events import (
    FileCreatedEvent,
    FileModifiedEvent,
    FileSystemEventHandler,
)

from ctx_engine.daemon.daemon import write_watch_state, read_watch_state
from ctx_engine.db.connection import connect
from ctx_engine.hashing import file_semantic_hash
from ctx_engine.languages.registry import parse_file
from ctx_engine.reindex import (
    extension_to_language,
    reindex_single_file,
)
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ctx_engine.daemon.local_llm import OllamaClient

logger = logging.getLogger("ctx")


class CtxFileEventHandler(FileSystemEventHandler):
    def __init__(
        self,
        conn_factory: Callable[[], sqlite3.Connection],
        repo_root: Path,
        parseable_extensions: set[str],
        debounce_seconds: float = 0.5,
        ollama_client: "OllamaClient | None" = None,
        state_path: Path | None = None,
    ):
        self._conn_factory = conn_factory
        self._repo_root = repo_root
        self._parseable_extensions = {e.lower() for e in parseable_extensions}
        self._debounce_seconds = debounce_seconds
        self._ollama_client = ollama_client
        self._state_path = state_path
        self._pending: dict[str, float] = {}
        self._pending_lock = threading.Lock()
        self._timer: threading.Timer | None = None

    def on_modified(self, event: FileModifiedEvent) -> None:
        if not event.is_directory:
            self._schedule(event.src_path)

    def on_created(self, event: FileCreatedEvent) -> None:
        if not event.is_directory:
            self._schedule(event.src_path)

    def _schedule(self, abs_path_str: str) -> None:
        abs_path = Path(abs_path_str)
        ext = abs_path.suffix.lower()
        if ext not in self._parseable_extensions:
            return

        try:
            rel_path = str(abs_path.relative_to(self._repo_root))
        except ValueError:
            return

        # Never index the index itself. Watching .ctx/watch.log writes
        # previously re-triggered the observer.
        if rel_path == ".ctx" or rel_path.startswith(".ctx/"):
            return

        with self._pending_lock:
            self._pending[rel_path] = (
                time.monotonic() + self._debounce_seconds
            )
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(
                self._debounce_seconds, self._flush
            )
            self._timer.daemon = True
            self._timer.start()

    def _flush(self) -> None:
        now = time.monotonic()
        with self._pending_lock:
            due = [
                path
                for path, t in self._pending.items()
                if t <= now
            ]
            for path in due:
                del self._pending[path]

        for rel_path in due:
            self._handle_change(rel_path)

    def _handle_change(self, rel_path: str) -> None:
        conn = self._conn_factory()
        try:
            self._process_file_change(conn, rel_path)
        finally:
            conn.close()

    def _update_watch_state(self, change_type: str, rel_path: str) -> None:
        if self._state_path is None:
            return
        state = read_watch_state(self._state_path)
        state["events_processed"] = state.get("events_processed", 0) + 1
        if change_type == "semantic":
            state["semantic_changes"] = state.get("semantic_changes", 0) + 1
        elif change_type == "formatting":
            state["formatting_changes"] = state.get("formatting_changes", 0) + 1
        state["last_event"] = rel_path
        write_watch_state(self._state_path, state)

    def _process_file_change(
        self,
        conn: sqlite3.Connection,
        rel_path: str,
    ) -> None:
        abs_path = self._repo_root / rel_path

        if not abs_path.exists():
            conn.execute(
                "UPDATE files SET is_stale = 1 WHERE path = ?",
                (rel_path,),
            )
            conn.commit()
            logger.info("Deleted (marked stale): %s", rel_path)
            self._update_watch_state("deleted", rel_path)
            return

        try:
            raw_bytes = abs_path.read_bytes()
        except (FileNotFoundError, PermissionError, OSError) as exc:
            # Raced with delete or unreadable — flag stale, never crash.
            logger.warning("Cannot read %s: %s — marking stale", rel_path, exc)
            try:
                conn.execute(
                    "UPDATE files SET is_stale = 1 WHERE path = ?",
                    (rel_path,),
                )
                conn.commit()
            except Exception:
                pass
            return
        new_content_hash = hashlib.sha256(raw_bytes).hexdigest()

        existing = conn.execute(
            "SELECT content_hash, semantic_hash FROM files WHERE path = ?",
            (rel_path,),
        ).fetchone()

        if existing is None:
            logger.info(
                "New unindexed file (will be indexed on next ctx init): %s",
                rel_path,
            )
            return

        if existing["content_hash"] == new_content_hash:
            logger.debug("Content hash unchanged (no-op): %s", rel_path)
            return

        ext = abs_path.suffix.lower()
        language = extension_to_language(ext)
        if language is None:
            return

        tree, source = parse_file(abs_path, language)
        new_semantic_hash = file_semantic_hash(tree, source, language)

        if existing["semantic_hash"] == new_semantic_hash:
            stat = abs_path.stat()
            conn.execute(
                "UPDATE files SET content_hash = ?, mtime = ?, file_size = ? WHERE path = ?",
                (new_content_hash, stat.st_mtime, stat.st_size, rel_path),
            )
            conn.commit()
            logger.info(
                "Formatting change (metadata preserved): %s", rel_path
            )
            self._update_watch_state("formatting", rel_path)
            return

        logger.info(
            "Semantic change detected: %s — reindexing", rel_path
        )

        changed_function_ids = reindex_single_file(
            conn, self._repo_root, rel_path, language
        )

        conn.commit()

        logger.info(
            "Reindexed %s: %d functions changed, taint propagated",
            rel_path,
            len(changed_function_ids),
        )

        if self._ollama_client is not None and changed_function_ids:
            self._ollama_client.enqueue(rel_path, changed_function_ids)

        self._update_watch_state("semantic", rel_path)
