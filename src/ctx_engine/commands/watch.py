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


def _debounce_seconds(repo_root: Path | None = None) -> float:
    try:
        import os as _os

        direct = _os.environ.get("CTX_WATCH_DEBOUNCE_MS")
        if direct:
            return float(direct) / 1000.0
        if repo_root is not None:
            try:
                from ctx_engine.config import get_config_value as _get_config_value

                return float(_get_config_value(repo_root, "watch.debounce_ms")) / 1000.0
            except (ValueError, TypeError):
                pass
        return float(os.environ.get("CTX_WATCH_DEBOUNCE_MS", "500")) / 1000.0
    except (ValueError, TypeError):
        return 0.5


def _config_ollama_host(repo_root: Path) -> str:
    direct = os.environ.get("CTX_OLLAMA_HOST")
    if direct:
        return direct
    try:
        from ctx_engine.config import get_config_value as _get_config_value

        return _get_config_value(repo_root, "ollama.host")
    except (ValueError, TypeError):
        return "http://localhost:11434"


def _config_ollama_model(repo_root: Path) -> str | None:
    direct = os.environ.get("CTX_OLLAMA_MODEL")
    if direct:
        return direct
    try:
        from ctx_engine.config import get_config_value as _get_config_value

        value = _get_config_value(repo_root, "ollama.model")
        return value or None
    except (ValueError, TypeError):
        return None


def _resolve_summarization_mode(repo_root: Path, mode_override: str | None) -> str:
    if mode_override is not None:
        return mode_override
    try:
        from ctx_engine.config import get_config_value as _get_config_value

        mode = _get_config_value(repo_root, "daemon.summarization_mode")
        return str(mode) if mode else "off"
    except (ValueError, TypeError):
        return "off"


def _api_batch_config(repo_root: Path) -> tuple[float, int]:
    try:
        from ctx_engine.config import get_config_value as _get

        window = float(_get(repo_root, "daemon.api_mode_batch_window_seconds"))
    except (ValueError, TypeError):
        window = 90.0
    try:
        from ctx_engine.config import get_config_value as _get2

        max_size = int(_get2(repo_root, "daemon.api_mode_max_batch_size"))
    except (ValueError, TypeError):
        max_size = 20
    return window, max_size


def run_watch(
    repo_root: Path,
    with_ollama: bool = False,
    daemon_mode: bool = False,
    log_file: str | None = None,
    mode: str | None = None,
) -> None:
    assert_inside_git_repo(repo_root)

    # Backward compat: --with-ollama == --mode ollama.
    if with_ollama and mode is None:
        print(
            "Note: --with-ollama is deprecated, use --mode ollama instead.",
        )
        mode = "ollama"

    effective_mode = _resolve_summarization_mode(repo_root, mode)
    if effective_mode not in ("off", "ollama", "api"):
        print(
            f"Warning: unknown summarization mode '{effective_mode}', falling back to 'off'.",
            file=sys.stderr,
        )
        effective_mode = "off"

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
    api_summarizer = None
    ollama_host = _config_ollama_host(repo_root)

    if effective_mode == "ollama":
        if is_ollama_available(ollama_host):
            available = get_available_models(ollama_host)
            model_override = _config_ollama_model(repo_root)
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
    elif effective_mode == "api":
        from ctx_engine.daemon.api_summarizer import ApiModeSummarizer

        window_s, max_batch = _api_batch_config(repo_root)

        def conn_factory():
            return connect(db_path)

        api_summarizer = ApiModeSummarizer(
            repo_root, conn_factory, window_s, max_batch
        )

    parseable_extensions = get_parseable_extensions()

    def conn_factory():
        return connect(db_path)

    event_handler = CtxFileEventHandler(
        conn_factory=conn_factory,
        repo_root=repo_root,
        parseable_extensions=parseable_extensions,
        debounce_seconds=_debounce_seconds(repo_root),
        ollama_client=ollama_client,
        state_path=state_path,
        api_summarizer=api_summarizer,
        summarization_mode=effective_mode,
    )

    observer = Observer()
    observer.schedule(event_handler, str(repo_root), recursive=True)
    observer.start()

    started_at = datetime.now(timezone.utc)
    # Ephemeral daemon state lives here, never in SQLite.
    existing_state = read_watch_state(state_path)
    window_s, max_batch = _api_batch_config(repo_root)
    write_watch_state(
        state_path,
        {
            **existing_state,
            "started_at": started_at.isoformat(),
            "summarization_mode": effective_mode,
            "ollama_model": model_name,
            "ollama_host": ollama_host,
            "api_batch_window_s": window_s,
            "api_max_batch_size": max_batch,
            "api_batches_sent": 0,
            "events_processed": 0,
            "semantic_changes": 0,
            "formatting_changes": 0,
            "last_event": None,
        },
    )

    if effective_mode == "ollama":
        logger.info("ctx watch summarization mode: ollama (model: %s)", model_name)
    elif effective_mode == "api":
        logger.info(
            "ctx watch summarization mode: api (batches every %gs or %d functions)",
            window_s,
            max_batch,
        )
    else:
        logger.info("ctx watch summarization mode: off (staleness only)")

    if not daemon_mode:
        print()
        print(f"  watching: {repo_root}")
        print(f"  summarization mode: {effective_mode}")
        if effective_mode == "ollama":
            if model_name:
                print(f"  ollama: enabled (model: {model_name} @ {ollama_host})")
            else:
                print("  ollama: disabled (no model found)")
        elif effective_mode == "api":
            print(f"  api: batches every {window_s:g}s or {max_batch} functions")
        else:
            print("  ollama: disabled (summarization off — staleness only)")
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
        if api_summarizer:
            try:
                api_summarizer.stop()
            except Exception:
                pass
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


def _mode_description(repo_root: Path, mode: str, state: dict) -> str:
    if mode == "api":
        window = state.get("api_batch_window_s")
        max_batch = state.get("api_max_batch_size")
        if window is None or max_batch is None:
            window, max_batch = _api_batch_config(repo_root)
        return f"api  (batches every {float(window):g}s or {int(max_batch)} functions)"
    if mode == "ollama":
        model = state.get("ollama_model")
        host = state.get("ollama_host") or _config_ollama_host(repo_root)
        if model:
            return f"ollama  (model: {model} @ {host})"
        return "ollama  (no model — staleness only until Ollama is available)"
    return (
        "off  (staleness tracked automatically; no automatic\n"
        "                              re-summarization — run 'ctx sync' manually, or\n"
        "                              enable one: 'ctx config set daemon.summarization_mode api')"
    )


def run_watch_status(repo_root: Path) -> None:
    pid_path = repo_root / ".ctx" / "watch.pid"
    state_path = repo_root / ".ctx" / "watch-state.json"

    pid = read_pid_file(pid_path)

    if pid is None or not is_process_alive(pid):
        if pid is not None:
            remove_pid_file(pid_path)
        ollama_host = _config_ollama_host(repo_root)
        print("  file watcher:")
        print("    status: NOT RUNNING")
        print(f"    ollama: {_ollama_status_text(ollama_host)}")
        try:
            from ctx_engine.config import get_config_value as _get

            mode = str(_get(repo_root, "daemon.summarization_mode"))
        except (ValueError, TypeError):
            mode = "off"
        print(f"    summarization mode: {mode} (configured, not running)")
        print("    (run 'ctx watch' or 'ctx watch --daemon' to start)")
        return

    state = read_watch_state(state_path)

    started_at = state.get("started_at") or state.get("_updated_at", "?")
    events_processed = state.get("events_processed", 0)
    semantic_changes = state.get("semantic_changes", 0)
    formatting_changes = state.get("formatting_changes", 0)
    last_event = state.get("last_event")
    mode = state.get("summarization_mode") or "off"

    print("  file watcher:")
    print(f"    Status: RUNNING (PID: {pid}, started {started_at})")
    print(f"    Summarization mode: {_mode_description(repo_root, mode, state)}")
    print(f"    Events processed: {events_processed} "
          f"({semantic_changes} semantic, {formatting_changes} formatting-only)")
    if mode == "api":
        batches = state.get("api_batches_sent", 0)
        spend = _session_api_spend(repo_root, started_at)
        print(f"    API-mode batches sent: {batches} "
              f"(total spend this session: ${spend:.4f} — see 'ctx costs')")
    session_model = state.get("ollama_model")
    ollama_host = state.get("ollama_host") or os.environ.get(
        "CTX_OLLAMA_HOST", "http://localhost:11434"
    )
    if session_model:
        ollama_line = f"enabled (model: {session_model} @ {ollama_host})"
    else:
        ollama_line = _ollama_status_text(ollama_host)
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


def _session_api_spend(repo_root: Path, started_at: str | None) -> float:
    try:
        from datetime import datetime as _dt
        from ctx_engine.intelligence.cost_tracking import _iter_entries

        if not started_at or started_at == "?":
            return 0.0
        try:
            start = _dt.fromisoformat(str(started_at).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return 0.0
        total = 0.0
        for entry in _iter_entries(repo_root):
            if entry.get("command") != "watch-api":
                continue
            try:
                from ctx_engine.intelligence.cost_tracking import _parse_ts

                ts = _parse_ts(entry.get("timestamp", ""))
                if ts is None:
                    continue
                if ts >= start:
                    total += float(entry.get("estimated_cost_usd", 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
        return total
    except Exception:
        return 0.0
