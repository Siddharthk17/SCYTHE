import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from watchdog.observers import Observer

from ctx_engine.daemon.daemon import (
    daemonize,
    is_process_alive,
    read_pid_file,
    read_watch_state,
    register_sigterm_handler,
    remove_pid_file,
    send_stop_signal,
    setup_watch_logging,
    write_pid_file,
    write_watch_state,
)
from ctx_engine.daemon.local_llm import (
    OllamaClient,
    get_available_models,
    is_ollama_available,
    select_model,
)
from ctx_engine.daemon.watcher import CtxFileEventHandler
from ctx_engine.db.connection import connect
from ctx_engine.discovery import (
    assert_inside_git_repo,
    get_parseable_extensions,
)

logger = logging.getLogger("ctx")


def _debounce_seconds() -> float:
    try:
        return float(os.environ.get("CTX_WATCH_DEBOUNCE_MS", "500")) / 1000.0
    except (ValueError, TypeError):
        return 0.5


def run_watch(
    repo_root: Path,
    with_ollama: bool = False,
    daemon_mode: bool = False,
    log_file: str | None = None,
) -> None:
    assert_inside_git_repo(repo_root)

    db_path = repo_root / ".ctx" / "index.db"
    if not db_path.exists():
        print("Error: .ctx/index.db not found. Run 'ctx init' first.", file=sys.stderr)
        sys.exit(1)

    ctx_dir = repo_root / ".ctx"
    pid_path = ctx_dir / "watch.pid"
    log_path = ctx_dir / "watch.log" if log_file is None else Path(log_file)
    state_path = ctx_dir / "watch-state.json"

    # Log to file in both modes; the daemon additionally detaches.
    setup_watch_logging(log_path)

    if daemon_mode:
        # Print before forking so the parent's pipe gets the message and
        # closes. The grandchild's stdout is /dev/null after daemonize().
        print("  Daemon starting...")
        print(f"  Logs: {log_path}")
        print("  Stop: ctx watch stop")
        import sys as _sys

        _sys.stdout.flush()
        _sys.stderr.flush()
        daemonize(pid_path=pid_path)
        # Only the detached grandchild continues past daemonize().

    write_pid_file(pid_path)
    register_sigterm_handler(pid_path)

    model_name = None
    ollama_client = None
    ollama_host = os.environ.get("CTX_OLLAMA_HOST", "http://localhost:11434")

    if with_ollama:
        if is_ollama_available(ollama_host):
            available = get_available_models(ollama_host)
            model_override = os.environ.get("CTX_OLLAMA_MODEL")
            model_name = select_model(available, model_override)
            if model_name:
                conn_factory = lambda: connect(db_path)
                ollama_client = OllamaClient(
                    model_name, ollama_host, conn_factory, repo_root
                )
            else:
                print("Warning: No suitable Ollama model found. "
                      "Continuing without local summarization.", file=sys.stderr)
        else:
            print("Warning: Ollama is not available at %s. "
                  "Continuing without local summarization." % ollama_host,
                  file=sys.stderr)

    parseable_extensions = get_parseable_extensions()

    def conn_factory():
        return connect(db_path)

    event_handler = CtxFileEventHandler(
        conn_factory=conn_factory,
        repo_root=repo_root,
        parseable_extensions=parseable_extensions,
        debounce_seconds=_debounce_seconds(),
        ollama_client=ollama_client,
        state_path=state_path,
    )

    observer = Observer()
    observer.schedule(event_handler, str(repo_root), recursive=True)
    observer.start()

    started_at = datetime.now(timezone.utc)
    # Ephemeral daemon state lives here, never in SQLite.
    existing_state = read_watch_state(state_path)
    write_watch_state(
        state_path,
        {
            **existing_state,
            "started_at": started_at.isoformat(),
            "ollama_model": model_name,
            "ollama_host": ollama_host,
            "events_processed": 0,
            "semantic_changes": 0,
            "formatting_changes": 0,
            "last_event": None,
        },
    )

    if not daemon_mode:
        print()
        print(f"  watching: {repo_root}")
        if model_name:
            print(f"  ollama: enabled (model: {model_name} @ {ollama_host})")
        elif with_ollama:
            print("  ollama: disabled (no model found)")
        else:
            print("  ollama: disabled (run with --with-ollama to enable)")
        print(f"  log: {log_path}")
        print()
        print("  Press Ctrl-C to stop.")
        print()
    else:
        logger.info("ctx watch daemon started (PID: %d)", os.getpid())
        if model_name:
            logger.info("Ollama enabled: %s @ %s", model_name, ollama_host)

    def cleanup():
        observer.stop()
        observer.join()
        if ollama_client:
            ollama_client.stop()
        remove_pid_file(pid_path)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        cleanup()
        print("ctx watch stopped.")
    except SystemExit:
        cleanup()
        raise


def run_watch_stop(repo_root: Path) -> None:
    pid_path = repo_root / ".ctx" / "watch.pid"
    pid = read_pid_file(pid_path)

    if pid is None:
        print("ctx watch is not running (no PID file found)")
        return

    if not is_process_alive(pid):
        remove_pid_file(pid_path)
        print(f"Cleaned up stale PID file (process was not running)")
        return

    if send_stop_signal(pid):
        remove_pid_file(pid_path)
        print(f"ctx watch stopped.")
    else:
        print(f"Failed to stop ctx watch (PID: {pid})", file=sys.stderr)
        sys.exit(1)


def _ollama_status_text(ollama_host: str) -> str:
    # Lazy import for the same cycle as status.py.
    from ctx_engine.daemon.local_llm import (
        is_ollama_available,
        get_available_models,
        select_model,
    )

    if is_ollama_available(ollama_host):
        available = get_available_models(ollama_host)
        model = select_model(available)
        if model:
            return f"available (model: {model})"
        return "available (no preferred model found)"
    return "not available"


def run_watch_status(repo_root: Path) -> None:
    pid_path = repo_root / ".ctx" / "watch.pid"
    state_path = repo_root / ".ctx" / "watch-state.json"

    pid = read_pid_file(pid_path)

    if pid is None or not is_process_alive(pid):
        if pid is not None:
            remove_pid_file(pid_path)
        ollama_host = os.environ.get("CTX_OLLAMA_HOST", "http://localhost:11434")
        print("  file watcher:")
        print("    status: NOT RUNNING")
        print(f"    ollama: {_ollama_status_text(ollama_host)}")
        print("    (run 'ctx watch' or 'ctx watch --daemon' to start)")
        return

    state = read_watch_state(state_path)

    started_at = state.get("started_at") or state.get("_updated_at", "?")
    events_processed = state.get("events_processed", 0)
    semantic_changes = state.get("semantic_changes", 0)
    formatting_changes = state.get("formatting_changes", 0)
    last_event = state.get("last_event")
    session_model = state.get("ollama_model")
    ollama_host = state.get("ollama_host") or os.environ.get(
        "CTX_OLLAMA_HOST", "http://localhost:11434"
    )
    if session_model:
        ollama_line = f"enabled (model: {session_model} @ {ollama_host})"
    else:
        ollama_line = _ollama_status_text(ollama_host)

    print("  file watcher:")
    print(f"    status: RUNNING (PID: {pid}, started {started_at})")
    print(f"    events (since start): {events_processed} processed "
          f"({semantic_changes} semantic, {formatting_changes} formatting-only)")
    print(f"    ollama: {ollama_line}")
    if last_event:
        print(f"    last event: {last_event}")
    print()

    db_path = repo_root / ".ctx" / "index.db"
    if db_path.exists():
        conn = connect(db_path)
        total_files = conn.execute(
            "SELECT COUNT(*) FROM files"
        ).fetchone()[0]
        cached_files = conn.execute(
            "SELECT COUNT(*) FROM files WHERE mtime IS NOT NULL"
        ).fetchone()[0]
        conn.close()

        coverage = (cached_files / total_files * 100) if total_files > 0 else 0
        uncached = total_files - cached_files
        print("  mtime cache:")
        print(f"    files with mtime cached: {cached_files} of {total_files} "
              f"({uncached} uncached — will parse on next init)")
        print(f"    cache coverage: {coverage:.1f}%")
    else:
        print("  mtime cache:")
        print("    (no database — run 'ctx init' first)")
