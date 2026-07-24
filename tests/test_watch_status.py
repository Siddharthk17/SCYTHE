import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from ctx_engine.daemon.daemon import (
    write_pid_file,
    read_pid_file,
    remove_pid_file,
    is_process_alive,
    send_stop_signal,
    write_watch_state,
    read_watch_state,
    daemonize,
)


class TestPidManagement:
    def test_write_and_read_pid(self, tmp_path):
        pid_path = tmp_path / "watch.pid"
        write_pid_file(pid_path)
        assert read_pid_file(pid_path) is not None

    def test_read_pid_nonexistent(self, tmp_path):
        assert read_pid_file(tmp_path / "nope.pid") is None

    def test_remove_pid_file(self, tmp_path):
        pid_path = tmp_path / "watch.pid"
        write_pid_file(pid_path)
        remove_pid_file(pid_path)
        assert not pid_path.exists()


class TestProcessManagement:
    def test_is_process_alive_nonexistent(self):
        assert is_process_alive(999999999) is False

    @patch("os.kill", side_effect=OSError)
    def test_send_stop_signal_failure(self, mock_kill):
        assert send_stop_signal(12345) is False


class TestWatchState:
    def test_write_and_read_state(self, tmp_path):
        state_path = tmp_path / "watch-state.json"
        write_watch_state(state_path, {"events_processed": 5, "semantic_changes": 2})
        state = read_watch_state(state_path)
        assert state["events_processed"] == 5
        assert state["semantic_changes"] == 2
        assert "_updated_at" in state

    def test_read_state_nonexistent(self, tmp_path):
        state = read_watch_state(tmp_path / "nope.json")
        assert state["events_processed"] == 0
        assert state["last_event"] is None
