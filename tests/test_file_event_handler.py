import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from watchdog.events import FileModifiedEvent, FileCreatedEvent

from ctx_engine.daemon.watcher import CtxFileEventHandler


@pytest.fixture
def handler():
    conn_factory = MagicMock()
    repo_root = Path("/tmp/test_repo")
    parseable_extensions = {".py", ".js"}
    return CtxFileEventHandler(
        conn_factory=conn_factory,
        repo_root=repo_root,
        parseable_extensions=parseable_extensions,
        debounce_seconds=0.1,
        ollama_client=None,
    )


def test_filter_by_extension(handler):
    event = FileModifiedEvent("/tmp/test_repo/main.rs")
    handler.on_modified(event)
    assert len(handler._pending) == 0


def test_accepts_parseable_extension(handler):
    event = FileModifiedEvent("/tmp/test_repo/main.py")
    handler.on_modified(event)
    assert "main.py" in handler._pending


def test_on_created_schedules(handler):
    event = FileCreatedEvent("/tmp/test_repo/main.js")
    handler.on_created(event)
    assert "main.js" in handler._pending


def test_ignores_directory_events(handler):
    event = FileModifiedEvent("/tmp/test_repo/src")
    event.is_directory = True
    handler.on_modified(event)
    assert len(handler._pending) == 0


def test_event_outside_repo_ignored(handler):
    event = FileModifiedEvent("/other/path/file.py")
    handler.on_modified(event)
    assert len(handler._pending) == 0


def test_debounce_replaces_previous_pending(handler):
    handler.on_modified(FileModifiedEvent("/tmp/test_repo/file.py"))
    handler.on_modified(FileModifiedEvent("/tmp/test_repo/file.py"))
    assert "file.py" in handler._pending


def test_schedule_with_timer(handler):
    with patch.object(handler, "_timer") as mock_timer:
        handler._schedule("/tmp/test_repo/file.py")
        assert handler._timer is not None


def test_flush_calls_handle_change(handler):
    handler._pending = {"file.py": 0.0}
    with patch.object(handler, "_handle_change") as mock_handle:
        handler._flush()
        mock_handle.assert_called_once_with("file.py")


def test_flush_clears_pending(handler):
    handler._pending = {"file.py": 0.0}
    handler._flush()
    assert "file.py" not in handler._pending
