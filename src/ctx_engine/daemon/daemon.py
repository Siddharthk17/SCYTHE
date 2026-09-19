import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("ctx")

_cleanup_pid_path: Path | None = None


def _sigterm_handler(signum: int, frame: object) -> None:
    logger.info("Received SIGTERM, shutting down...")
    if _cleanup_pid_path is not None:
        remove_pid_file(_cleanup_pid_path)
    sys.exit(0)


def register_sigterm_handler(pid_path: Path) -> None:
    global _cleanup_pid_path
    _cleanup_pid_path = pid_path
    signal.signal(signal.SIGTERM, _sigterm_handler)
    signal.signal(signal.SIGINT, _sigterm_handler)


def daemonize(pid_path: Path | None = None) -> None:
    if os.name == "nt":
        import subprocess

        DETACHED_PROCESS = 0x00000008
        # Strip --daemon so the child does not re-daemonize forever.
        argv = [a for a in sys.argv if a != "--daemon"]
        subprocess.Popen(
            argv,
            creationflags=DETACHED_PROCESS,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        sys.exit(0)

    if pid_path is not None:
        register_sigterm_handler(pid_path)

    if os.fork() > 0:
        sys.exit(0)
    os.setsid()
    if os.fork() > 0:
        sys.exit(0)
    sys.stdin = open(os.devnull, "r")
    # Detach stdout/stderr so the parent's pipe closes and `ctx watch
    # --daemon | head` returns immediately. Daemon output goes to the log.
    sys.stdout = open(os.devnull, "w")
    sys.stderr = open(os.devnull, "w")


def write_pid_file(pid_path: Path) -> None:
    pid_path.write_text(str(os.getpid()), encoding="utf-8")


def read_pid_file(pid_path: Path) -> int | None:
    if not pid_path.exists():
        return None
    try:
        return int(pid_path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def remove_pid_file(pid_path: Path) -> None:
    if pid_path.exists():
        pid_path.unlink()


def is_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def send_stop_signal(pid: int, timeout_seconds: float = 3.0) -> bool:
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not is_process_alive(pid):
            return True
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    return not is_process_alive(pid)


def write_watch_state(state_path: Path, data: dict) -> None:
    data["_updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.rename(state_path)


def read_watch_state(state_path: Path) -> dict:
    defaults = {
        "events_processed": 0,
        "semantic_changes": 0,
        "formatting_changes": 0,
        "last_event": None,
        "started_at": None,
        "ollama_model": None,
    }
    if not state_path.exists():
        return dict(defaults)
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return dict(defaults)
    for key, value in defaults.items():
        data.setdefault(key, value)
    return data


def setup_watch_logging(log_path: Path, max_bytes: int = 5 * 1024 * 1024) -> None:
    """File logging with single-backup rotation. Idempotent on re-entry.

    Attaches to the ctx logger only. Watchdog's own debug stream stays at
    WARNING so inotify events for the log file itself never feed back
    into the log (which previously grew gigabytes in minutes).
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)

    if log_path.exists() and log_path.stat().st_size > max_bytes:
        backup = Path(str(log_path) + ".1")
        try:
            if backup.exists():
                backup.unlink()
        except OSError:
            pass
        try:
            log_path.rename(backup)
        except OSError:
            pass

    ctx_logger = logging.getLogger("ctx")
    for handler in list(ctx_logger.handlers):
        if isinstance(handler, logging.FileHandler) and getattr(
            handler, "baseFilename", None
        ) == str(log_path):
            break
    else:
        handler = logging.FileHandler(str(log_path), encoding="utf-8")
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%SZ",
            )
        )
        # Use UTC for the asctime so log stamps match the Z suffix.
        handler.formatter.converter = time.gmtime  # type: ignore[attr-defined]
        ctx_logger.addHandler(handler)
    ctx_logger.setLevel(logging.DEBUG)
    ctx_logger.propagate = False
    # Silence watchdog internals; their DEBUG in-event stream caused the
    # 3.6G feedback loop when attached to the root logger.
    for noisy in ("watchdog", "watchdog.observers", "watchdog.observers.inotify"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
