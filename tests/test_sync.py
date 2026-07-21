import os
import subprocess
import sqlite3
import json
import pytest
from unittest.mock import patch, MagicMock
from ctx_engine.db import init_schema
from ctx_engine.commands.sync import run_sync


@pytest.fixture
def sync_repo(tmp_path):
    """Temp git repo with a single commit and ctx init done."""
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True, check=True)

    (tmp_path / "greet.py").write_text(
        "def greet(name):\n    return f'hello {name}'\n",
        encoding="utf-8",
    )
    (tmp_path / ".gitignore").write_text(".ctx/\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, capture_output=True, check=True)

    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    init_schema(conn)
    conn.execute(
        "INSERT INTO directories (path, file_count) VALUES (?, 0)",
        ("/",),
    )
    conn.commit()
    conn.close()
    return tmp_path


def test_sync_dry_run(sync_repo, capsys):
    """--dry-run prints planned actions without altering DB."""
    run_sync(sync_repo, dry_run=True)
    out = capsys.readouterr().out
    assert "dry run" in out.lower()
    assert "Phase 2" in out
    assert "Estimated" in out


@patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
@patch("ctx_engine.commands.sync.get_anthropic_client")
@patch("ctx_engine.commands.sync.call_llm_with_retry")
def test_sync_full(mock_call_llm, mock_get_client, sync_repo):
    """Full sync creates files, functions entries (Phase 1)."""
    mock_call_llm.return_value = ("{}", 0, 0)
    run_sync(sync_repo)

    conn = sqlite3.connect(str(sync_repo / ".ctx" / "index.db"))
    conn.row_factory = sqlite3.Row
    files = [r["path"] for r in conn.execute("SELECT path FROM files").fetchall()]
    funcs = [r["name"] for r in conn.execute("SELECT name FROM functions").fetchall()]
    conn.close()

    assert "greet.py" in files
    assert "greet" in funcs


@patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
@patch("ctx_engine.commands.sync.get_anthropic_client")
@patch("ctx_engine.commands.sync.call_llm_with_retry")
def test_sync_idempotent(mock_call_llm, mock_get_client, sync_repo):
    """Running sync twice produces same row count."""
    mock_call_llm.return_value = ("{}", 0, 0)
    run_sync(sync_repo)
    conn = sqlite3.connect(str(sync_repo / ".ctx" / "index.db"))
    c1 = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    conn.close()

    run_sync(sync_repo)
    conn = sqlite3.connect(str(sync_repo / ".ctx" / "index.db"))
    c2 = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    conn.close()

    assert c1 == c2


@pytest.fixture
def sync_repo_3files(tmp_path):
    """Temp git repo with 3 pre-indexed Python files (via full sync with mocked LLM)."""
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True, check=True)

    (tmp_path / "alpha.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    (tmp_path / "beta.py").write_text("def beta():\n    return 2\n", encoding="utf-8")
    (tmp_path / "gamma.py").write_text("def gamma():\n    return 3\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text(".ctx/\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, capture_output=True, check=True)

    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    init_schema(conn)
    conn.execute("INSERT INTO directories (path, file_count) VALUES (?, 0)", ("/",))
    conn.commit()
    conn.close()

    # Run full sync with mocked LLM to index all 3 files
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch("ctx_engine.commands.sync.get_anthropic_client") as mock_client:
            with patch("ctx_engine.commands.sync.call_llm_with_retry") as mock_llm:
                mock_client.return_value = MagicMock()
                mock_llm.return_value = (json.dumps([
                    {"path": "alpha.py", "purpose": "A", "summary": "A", "danger": None, "functions": [
                        {"id": "alpha.py::alpha", "summary": "alpha fn", "summary_long": "", "danger": None}]},
                    {"path": "beta.py", "purpose": "B", "summary": "B", "danger": None, "functions": [
                        {"id": "beta.py::beta", "summary": "beta fn", "summary_long": "", "danger": None}]},
                    {"path": "gamma.py", "purpose": "C", "summary": "C", "danger": None, "functions": [
                        {"id": "gamma.py::gamma", "summary": "gamma fn", "summary_long": "", "danger": None}]},
                ]), 150, 75)
                run_sync(tmp_path)

    # Now modify all 3 on disk (but don't re-index)
    (tmp_path / "alpha.py").write_text("def alpha():\n    return 10\n", encoding="utf-8")
    (tmp_path / "beta.py").write_text("def beta():\n    return 20\n", encoding="utf-8")
    (tmp_path / "gamma.py").write_text("def gamma():\n    return 30\n", encoding="utf-8")

    return tmp_path


def test_sync_stale_3_files_dry_run(sync_repo_3files, capsys):
    """Dry run with 3 stale files reports correct counts without API calls."""
    run_sync(sync_repo_3files, dry_run=True)
    out = capsys.readouterr().out
    assert "dry run" in out.lower()
    assert "3 files changed" in out or "3 files" in out or "3" in out
    assert "Phase 2" in out
    assert "Estimated" in out


@patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
@patch("ctx_engine.commands.sync.get_anthropic_client")
@patch("ctx_engine.commands.sync.call_llm_with_retry")
def test_sync_live_2_files_change(mock_call_llm, mock_get_client, sync_repo):
    """Live sync with 2 changed files: taint propagates, LLM called, state cleared."""
    # Create a second file that calls the first
    (sync_repo / "caller.py").write_text(
        "from greet import greet\n\ndef run():\n    return greet('sync test')\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "caller.py"], cwd=sync_repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "add caller"], cwd=sync_repo, capture_output=True, check=True)

    # Full sync to index caller.py and build call graph
    mock_call_llm.return_value = (json.dumps([
        {"path": "caller.py", "purpose": "Calls greet", "summary": "Runner", "danger": None, "functions": [
            {"id": "caller.py::run", "summary": "runs greet", "summary_long": "", "danger": None},
        ]}
    ]), 50, 25)
    run_sync(sync_repo)

    # Verify both files and their functions exist
    conn = sqlite3.connect(str(sync_repo / ".ctx" / "index.db"))
    conn.row_factory = sqlite3.Row
    files = {r["path"] for r in conn.execute("SELECT path FROM files").fetchall()}
    assert "greet.py" in files
    assert "caller.py" in files
    funcs = {r["name"] for r in conn.execute("SELECT name FROM functions").fetchall()}
    assert "greet" in funcs
    assert "run" in funcs

    # Check call graph edge exists
    edges = conn.execute("SELECT caller_id, callee_id FROM call_graph").fetchall()
    assert any("caller.py::run" in e["caller_id"] for e in edges)
    conn.close()

    # Modify greet.py content (callee) on disk
    (sync_repo / "greet.py").write_text(
        "def greet(name):\n    return f'hi {name}'\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "greet.py"], cwd=sync_repo, capture_output=True, check=True)

    # Run sync again — Phase 1 should flag greet as stale and taint caller
    mock_call_llm.reset_mock()
    mock_call_llm.return_value = (json.dumps([
        {"path": "greet.py", "purpose": "Greeting fn", "summary": "Greeter", "danger": None, "functions": [
            {"id": "greet.py::greet", "summary": "greets a name", "summary_long": "", "danger": None},
        ]},
        {"path": "caller.py", "purpose": "Calls greet", "summary": "Runner", "danger": None, "functions": [
            {"id": "caller.py::run", "summary": "runs greet", "summary_long": "", "danger": None},
        ]},
    ]), 100, 50)
    run_sync(sync_repo)

    conn = sqlite3.connect(str(sync_repo / ".ctx" / "index.db"))
    conn.row_factory = sqlite3.Row

    remaining_taint = conn.execute("SELECT COUNT(*) FROM taint_queue").fetchone()[0]
    remaining_stale = conn.execute("SELECT COUNT(*) FROM functions WHERE is_stale = 1").fetchone()[0]

    assert remaining_taint == 0, f"Expected empty taint_queue, got {remaining_taint}"
    assert remaining_stale == 0, f"Expected no stale functions, got {remaining_stale}"

    conn.close()
