import sqlite3
import subprocess
from unittest.mock import patch

from ctx_engine.commands.quickstart import run_quickstart
from ctx_engine.commands.status import run_status
from ctx_engine.commands.watch import run_watch_status
from ctx_engine.daemon.daemon import write_pid_file, write_watch_state
from ctx_engine.db import init_schema


def _git_repo(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True, check=True)
    return tmp_path


def _indexed_repo(tmp_path):
    _git_repo(tmp_path)
    (tmp_path / "a.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.py"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)
    from ctx_engine.commands.init import run_init

    run_init(tmp_path)
    return tmp_path


def test_status_languages_line(tmp_path, capsys):
    repo = _indexed_repo(tmp_path)
    run_status(repo)
    out = capsys.readouterr().out
    assert "languages:" in out
    assert "10 registered" in out
    assert "4 tier-1" in out
    assert "6 tier-2" in out


def test_status_watcher_modes(tmp_path, capsys):
    repo = _indexed_repo(tmp_path)
    run_status(repo)
    assert "NOT RUNNING" in capsys.readouterr().out

    from ctx_engine.daemon.daemon import is_process_alive

    with patch(
        "ctx_engine.commands.status.is_process_alive", return_value=True
    ), patch("ctx_engine.commands.status.read_pid_file", return_value=12345):
        run_status(repo)
        out = capsys.readouterr().out
        assert "RUNNING" in out
        assert "mode:" in out.lower()


def test_quickstart_three_modes(tmp_path, capsys):
    repo = _git_repo(tmp_path)
    run_quickstart(repo)
    out = capsys.readouterr().out
    assert "daemon.summarization_mode api" in out
    assert "daemon.summarization_mode ollama" in out
    assert "neither is required" in out


def test_watch_status_three_modes(tmp_path, capsys):
    repo = _indexed_repo(tmp_path)

    def _running_with_mode(mode, extra=None):
        pid_path = repo / ".ctx" / "watch.pid"
        state_path = repo / ".ctx" / "watch-state.json"
        write_pid_file(pid_path)
        import os

        # Overwrite pid with a live pid (our own) so is_process_alive passes.
        pid_path.write_text(str(os.getpid()), encoding="utf-8")
        state = {
            "started_at": "2026-01-01T00:00:00+00:00",
            "summarization_mode": mode,
            "events_processed": 47,
            "semantic_changes": 12,
            "formatting_changes": 35,
            "last_event": None,
            "api_batches_sent": 3,
            "api_batch_window_s": 90,
            "api_max_batch_size": 20,
            "ollama_model": "qwen2.5-coder:7b-instruct" if mode == "ollama" else None,
            "ollama_host": "http://localhost:11434",
        }
        if extra:
            state.update(extra)
        write_watch_state(state_path, state)

    _running_with_mode("off")
    run_watch_status(repo)
    out_off = capsys.readouterr().out
    assert "RUNNING" in out_off
    assert "Summarization mode:" in out_off
    assert "off" in out_off.lower()
    assert "staleness tracked" in out_off.lower()

    _running_with_mode("api")
    run_watch_status(repo)
    out_api = capsys.readouterr().out
    assert "Summarization mode:" in out_api
    assert "api" in out_api.lower()
    assert "batches every" in out_api.lower()
    assert "API-mode batches sent" in out_api

    _running_with_mode("ollama")
    run_watch_status(repo)
    out_ollama = capsys.readouterr().out
    assert "Summarization mode:" in out_ollama
    assert "ollama" in out_ollama.lower()
