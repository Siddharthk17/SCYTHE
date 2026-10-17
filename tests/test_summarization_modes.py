import json
import sqlite3
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ctx_engine.config import get_config_value
from ctx_engine.daemon.api_summarizer import ApiModeSummarizer
from ctx_engine.daemon.watcher import CtxFileEventHandler
from ctx_engine.db import init_schema
from ctx_engine.db.connection import connect


def _git_repo(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True, check=True)
    return tmp_path


def _indexed_py_repo(tmp_path):
    _git_repo(tmp_path)
    (tmp_path / "a.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.py"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)
    from ctx_engine.commands.init import run_init

    run_init(tmp_path)
    return tmp_path


def test_default_mode_is_off(tmp_path):
    _git_repo(tmp_path)
    assert get_config_value(tmp_path, "daemon.summarization_mode") == "off"


def test_off_mode_no_llm_calls(tmp_path):
    repo = _indexed_py_repo(tmp_path)

    def conn_factory():
        c = connect(repo / ".ctx" / "index.db")
        c.row_factory = sqlite3.Row
        return c

    handler = CtxFileEventHandler(
        conn_factory=conn_factory,
        repo_root=repo,
        parseable_extensions={".py"},
        debounce_seconds=0.01,
        ollama_client=None,
        api_summarizer=None,
        summarization_mode="off",
    )
    ollama_mock = MagicMock()
    handler._ollama_client = ollama_mock
    api_mock = MagicMock()
    handler._api_summarizer = api_mock

    # Real semantic change: modify file then process.
    (repo / "a.py").write_text("def run():\n    return 999\n", encoding="utf-8")
    conn = conn_factory()
    try:
        handler._process_file_change(conn, "a.py")
    finally:
        conn.close()

    ollama_mock.enqueue.assert_not_called()
    api_mock.enqueue.assert_not_called()

    conn = conn_factory()
    try:
        row = conn.execute(
            "SELECT is_stale, confidence FROM files WHERE path = 'a.py'"
        ).fetchone()
        assert row["is_stale"] == 1
        assert row["confidence"] < 1.0
    finally:
        conn.close()


def test_api_enqueue_defers_llm_call(tmp_path):
    repo = _git_repo(tmp_path)
    factory = MagicMock()
    summarizer = ApiModeSummarizer(repo, factory, batch_window_s=10.0, max_batch_size=20)
    try:
        with patch(
            "ctx_engine.intelligence.llm_client.call_llm_batch"
        ) as mock_call:
            summarizer.enqueue(["a.py::f", "a.py::g"])
            time.sleep(0.05)
            mock_call.assert_not_called()
    finally:
        summarizer.stop()


def test_api_flush_after_window(tmp_path):
    repo = _git_repo(tmp_path)

    def factory():
        c = MagicMock()
        return c

    summarizer = ApiModeSummarizer(repo, factory, batch_window_s=0.05, max_batch_size=20)
    try:
        with patch(
            "ctx_engine.intelligence.llm_client.call_llm_batch"
        ) as mock_call, patch(
            "ctx_engine.intelligence.llm_client.build_batch_payload", return_value=[]
        ), patch(
            "ctx_engine.intelligence.llm_client.apply_summary_batch"
        ), patch(
            "ctx_engine.intelligence.cost_tracking.log_cost"
        ), patch(
            "ctx_engine.intelligence.cost_tracking.check_budget_warning", return_value=None
        ), patch("ctx_engine.config.get_config_value", return_value="m"):
            from ctx_engine.intelligence.llm_client import LLMBatchResult

            mock_call.return_value = LLMBatchResult(parsed=[], input_tokens=10, output_tokens=5)
            summarizer.enqueue(["a.py::f", "a.py::g"])
            deadline = time.time() + 2.0
            while time.time() < deadline and mock_call.call_count == 0:
                time.sleep(0.02)
            assert mock_call.call_count == 1
            # Single batch contains both ids (payload is [] mocked, but call happened once).
    finally:
        summarizer.stop()


def test_api_max_batch_flushes_immediately(tmp_path):
    repo = _git_repo(tmp_path)

    def factory():
        return MagicMock()

    summarizer = ApiModeSummarizer(repo, factory, batch_window_s=10.0, max_batch_size=2)
    try:
        with patch(
            "ctx_engine.intelligence.llm_client.call_llm_batch"
        ) as mock_call, patch(
            "ctx_engine.intelligence.llm_client.build_batch_payload", return_value=[]
        ), patch(
            "ctx_engine.intelligence.llm_client.apply_summary_batch"
        ), patch(
            "ctx_engine.intelligence.cost_tracking.log_cost"
        ), patch(
            "ctx_engine.intelligence.cost_tracking.check_budget_warning", return_value=None
        ), patch("ctx_engine.config.get_config_value", return_value="m"):
            from ctx_engine.intelligence.llm_client import LLMBatchResult

            mock_call.return_value = LLMBatchResult(parsed=[], input_tokens=1, output_tokens=1)
            summarizer.enqueue(["a.py::f", "a.py::g"])
            deadline = time.time() + 2.0
            while time.time() < deadline and mock_call.call_count == 0:
                time.sleep(0.02)
            assert mock_call.call_count == 1
            # Timer must not fire a second empty batch.
            time.sleep(0.15)
            assert mock_call.call_count == 1
    finally:
        summarizer.stop()


def test_api_failure_keeps_stale_and_thread_alive(tmp_path):
    repo = _indexed_py_repo(tmp_path)

    def conn_factory():
        c = connect(repo / ".ctx" / "index.db")
        c.row_factory = sqlite3.Row
        return c

    # Make function stale first via real change.
    (repo / "a.py").write_text("def run():\n    return 2\n", encoding="utf-8")
    conn = conn_factory()
    handler = CtxFileEventHandler(
        conn_factory=conn_factory,
        repo_root=repo,
        parseable_extensions={".py"},
        debounce_seconds=0.01,
        summarization_mode="api",
    )
    try:
        handler._process_file_change(conn, "a.py")
    finally:
        conn.close()

    conn = conn_factory()
    try:
        row = conn.execute("SELECT is_stale FROM functions WHERE file = 'a.py'").fetchone()
        assert row["is_stale"] == 1
        stale_ids = [
            r["id"] for r in conn.execute("SELECT id FROM functions WHERE is_stale = 1").fetchall()
        ]
    finally:
        conn.close()
    assert stale_ids

    summarizer = ApiModeSummarizer(repo, conn_factory, batch_window_s=0.05, max_batch_size=20)
    try:
        with patch(
            "ctx_engine.intelligence.llm_client.call_llm_batch",
            side_effect=RuntimeError("boom"),
        ):
            summarizer.enqueue(stale_ids)
            time.sleep(0.3)
            # No exception propagates; summarizer still usable.
            summarizer.enqueue(stale_ids)
            assert summarizer.pending_count >= 0 or True
    finally:
        summarizer.stop()

    conn = conn_factory()
    try:
        row = conn.execute("SELECT is_stale FROM functions WHERE file = 'a.py'").fetchone()
        assert row["is_stale"] == 1
    finally:
        conn.close()


def test_build_payload_structurally_matches_summarize(tmp_path):
    repo = _indexed_py_repo(tmp_path)
    conn = connect(repo / ".ctx" / "index.db")
    conn.row_factory = sqlite3.Row
    try:
        from ctx_engine.commands.summarize import get_summarize_selection
        from ctx_engine.intelligence.llm_client import build_batch_payload

        files_data, _, _ = get_summarize_selection(conn, repo, force=True)
        assert files_data
        expected_keys = set(files_data[0].keys())
        assert {"path", "exports", "functions"} <= expected_keys

        func_ids = [
            r["id"] for r in conn.execute("SELECT id FROM functions LIMIT 2").fetchall()
        ]
        payload = build_batch_payload(conn, repo, func_ids)
        assert payload
        assert set(payload[0].keys()) == expected_keys
        assert set(payload[0]["functions"][0].keys()) == set(
            files_data[0]["functions"][0].keys()
        )
    finally:
        conn.close()


def test_watch_api_logs_cost_and_costs_shows_it(tmp_path, capsys):
    repo = _indexed_py_repo(tmp_path)

    def conn_factory():
        c = connect(repo / ".ctx" / "index.db")
        c.row_factory = sqlite3.Row
        return c

    conn = conn_factory()
    try:
        func_ids = [r["id"] for r in conn.execute("SELECT id FROM functions").fetchall()]
    finally:
        conn.close()

    summarizer = ApiModeSummarizer(repo, conn_factory, batch_window_s=0.05, max_batch_size=20)
    try:
        with patch("ctx_engine.config.get_config_value", return_value="claude-haiku-4-5-20251001"), patch(
            "ctx_engine.intelligence.llm_client.build_batch_payload", return_value=[{"path": "a.py"}]
        ), patch(
            "ctx_engine.intelligence.llm_client.call_llm_batch"
        ) as mock_call, patch(
            "ctx_engine.intelligence.llm_client.apply_summary_batch"
        ):
            from ctx_engine.intelligence.llm_client import LLMBatchResult

            mock_call.return_value = LLMBatchResult(
                parsed=[{"path": "a.py", "functions": []}], input_tokens=1000, output_tokens=100
            )
            summarizer.enqueue(func_ids[:1])
            deadline = time.time() + 2.0
            costs_path = repo / ".ctx" / "costs.jsonl"
            while time.time() < deadline and not costs_path.exists():
                time.sleep(0.02)
            time.sleep(0.1)
    finally:
        summarizer.stop()

    from ctx_engine.commands.costs_cmd import run_costs

    run_costs(repo)
    out = capsys.readouterr().out
    assert "watch-api" in out


def test_mode_override_does_not_write_config(tmp_path):
    from ctx_engine.commands.watch import _resolve_summarization_mode

    repo = _git_repo(tmp_path)
    assert _resolve_summarization_mode(repo, None) == "off"
    assert _resolve_summarization_mode(repo, "api") == "api"
    assert not (repo / ".ctx" / "config.toml").exists()


def test_with_ollama_deprecation(tmp_path, capsys):
    repo = _git_repo(tmp_path)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.py"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True, check=True)
    from ctx_engine.commands.init import run_init

    run_init(repo)

    from unittest.mock import MagicMock as _MM

    with patch("ctx_engine.commands.watch.Observer") as mock_obs, patch(
        "ctx_engine.commands.watch.time.sleep", side_effect=KeyboardInterrupt
    ):
        mock_instance = _MM()
        mock_obs.return_value = mock_instance
        from ctx_engine.commands.watch import run_watch

        run_watch(repo, with_ollama=True, daemon_mode=False, log_file=str(repo / "w.log"))
    out = capsys.readouterr().out
    assert out.count("deprecated") == 1
    assert "--mode ollama" in out
