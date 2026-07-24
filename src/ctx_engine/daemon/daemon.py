import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("ctx")


def daemonize() -> None:
    if os.name == "nt":
        return
    if os.fork() > 0:
        sys.exit(0)
    os.setsid()
    if os.fork() > 0:
        sys.exit(0)
    sys.stdin = open(os.devnull, "r")


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
    if not state_path.exists():
        return {
            "events_processed": 0,
            "semantic_changes": 0,
            "formatting_changes": 0,
            "last_event": None,
        }
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {
            "events_processed": 0,
            "semantic_changes": 0,
            "formatting_changes": 0,
            "last_event": None,
        }


def setup_watch_logging(log_path: Path, max_bytes: int = 5 * 1024 * 1024) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)

    if log_path.exists() and log_path.stat().st_size > max_bytes:
        backup = log_path.with_suffix(".log.1")
        log_path.rename(backup)

    handler = logging.FileHandler(str(log_path), encoding="utf-8")
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%SZ",
        )
    )
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.DEBUG)
