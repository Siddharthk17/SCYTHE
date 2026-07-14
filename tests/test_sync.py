import os
import subprocess
import sqlite3
import pytest
from unittest.mock import patch
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
