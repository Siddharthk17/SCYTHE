import subprocess
import sqlite3
import pytest
from ctx_engine.db import init_schema
from ctx_engine.commands.status import run_status


@pytest.fixture
def status_repo(tmp_path):
    """Temp git + ctx with a commit logged and hooks installed."""
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True, check=True)

    (tmp_path / "a.py").write_text("def f():\n    pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.py"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, capture_output=True, check=True)

    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    init_schema(conn)
    conn.execute(
        "INSERT INTO files (path, semantic_hash, content_hash, purpose, summary) VALUES ('a.py', 'h1', 'c1', 'purpose', 'summary')",
    )
    conn.execute(
        "INSERT INTO changes (commit_hash, summary, author, file) VALUES ('deadbee', 'initial', 'human', 'a.py')",
    )
    conn.commit()
    conn.close()

    # Install hooks with the exact ctx hook content
    from ctx_engine.commands.install_hooks import PRE_COMMIT_HOOK, POST_COMMIT_HOOK
    git_hooks = tmp_path / ".git" / "hooks"
    git_hooks.mkdir(exist_ok=True)
    (git_hooks / "pre-commit").write_text(PRE_COMMIT_HOOK)
    (git_hooks / "post-commit").write_text(POST_COMMIT_HOOK)
    (git_hooks / "pre-commit").chmod(0o755)
    (git_hooks / "post-commit").chmod(0o755)

    return tmp_path


def test_status_shows_hooks(status_repo, capsys):
    """Status output shows installed hooks."""
    run_status(status_repo)
    out = capsys.readouterr().out
    assert "pre-commit" in out
    assert "INSTALLED" in out


def test_status_shows_recent_changes(status_repo, capsys):
    """Status output shows recent changes from the changes table."""
    run_status(status_repo)
    out = capsys.readouterr().out
    assert "recent changes" in out.lower()
    assert "deadbee" in out


def test_status_shows_wal_mode(tmp_path, capsys):
    """Status output includes WAL mode annotation."""
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    init_schema(conn)
    conn.execute("PRAGMA journal_mode=wal;")
    conn.execute(
        "INSERT INTO files (path, semantic_hash, content_hash) VALUES ('f.py', 'h', 'c')",
    )
    conn.commit()
    conn.close()

    run_status(tmp_path)
    out = capsys.readouterr().out
    assert "WAL mode" in out


def test_status_no_changes_section(tmp_path, capsys):
    """No records in changes table -> 'none' message."""
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    init_schema(conn)
    conn.close()

    run_status(tmp_path)
    out = capsys.readouterr().out
    assert "recent changes" in out.lower()
    assert "none" in out.lower() or "install-hooks" in out.lower()
