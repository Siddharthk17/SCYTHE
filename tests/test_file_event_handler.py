import json
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from watchdog.events import FileModifiedEvent, FileCreatedEvent

from ctx_engine.daemon.watcher import CtxFileEventHandler
from ctx_engine.db import init_schema, connect


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


@pytest.fixture
def real_db_and_repo(tmp_path):
    (tmp_path / ".git").mkdir()
    py_file = tmp_path / "test.py"
    py_file.write_text("def foo():\n    return 1\n")
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "add", "test.py"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)

    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir()
    conn = connect(db_path)
    init_schema(conn)

    stat = py_file.stat()
    conn.execute(
        "INSERT INTO files (path, content_hash, semantic_hash, confidence, is_stale, mtime, file_size) "
        "VALUES (?, ?, ?, 1.0, 0, ?, ?)",
        ("test.py", "old_hash", "old_sem_hash", stat.st_mtime, stat.st_size),
    )
    conn.commit()
    conn.close()

    return tmp_path, db_path


def test_handle_change_with_real_db_and_reindex(real_db_and_repo):
    repo_root, db_path = real_db_and_repo

    def conn_factory():
        c = connect(db_path)
        c.row_factory = sqlite3.Row
        return c

    handler = CtxFileEventHandler(
        conn_factory=conn_factory,
        repo_root=repo_root,
        parseable_extensions={".py"},
        debounce_seconds=0.1,
        ollama_client=None,
        state_path=repo_root / ".ctx" / "watch-state.json",
    )

    (repo_root / "test.py").write_text("def bar():\n    return 2\n")
    handler._pending = {"test.py": 0.0}
    handler._flush()

    conn = conn_factory()
    row = conn.execute("SELECT content_hash, is_stale FROM files WHERE path = ?", ("test.py",)).fetchone()
    assert row is not None
    assert row["content_hash"] != "old_hash"
    assert row["is_stale"] == 1

    state_path = repo_root / ".ctx" / "watch-state.json"
    assert state_path.exists()
    state = json.loads(state_path.read_text())
    assert state["events_processed"] >= 1
    assert state["semantic_changes"] >= 1
    assert state["last_event"] == "test.py"
    conn.close()
