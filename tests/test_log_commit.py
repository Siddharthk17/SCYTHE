import subprocess
import sqlite3
import pytest
from ctx_engine.db import init_schema
from ctx_engine.commands.log_commit import run_log_commit


@pytest.fixture
def committable_repo(tmp_path):
    """Temp git repo with ctx init already run."""
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True, check=True)

    (tmp_path / "a.py").write_text("def f():\n    pass\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def g():\n    pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.py", "b.py"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, capture_output=True, check=True)

    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    init_schema(conn)
    conn.execute("INSERT INTO files (path, semantic_hash, content_hash) VALUES ('a.py', 'h1', 'c1')")
    conn.execute("INSERT INTO files (path, semantic_hash, content_hash) VALUES ('b.py', 'h2', 'c2')")
    conn.commit()
    conn.close()
    return tmp_path


def test_log_commit_basic(committable_repo):
    """Make a real commit, then log it -> changes table has the right rows."""
    (committable_repo / "a.py").write_text("def f():\n    return 42\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.py"], cwd=committable_repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "feat: return 42"], cwd=committable_repo, capture_output=True, check=True)

    head_hash = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=committable_repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    run_log_commit(committable_repo)

    conn = sqlite3.connect(str(committable_repo / ".ctx" / "index.db"))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM changes").fetchall()
    conn.close()

    assert len(rows) == 1
    assert rows[0]["commit_hash"] == head_hash
    assert rows[0]["summary"] == "feat: return 42"
    assert rows[0]["file"] == "a.py"


def test_log_commit_idempotent(committable_repo):
    """Calling log-commit twice for the same commit -> no duplicate rows."""
    (committable_repo / "a.py").write_text("def f():\n    return 99\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.py"], cwd=committable_repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "second"], cwd=committable_repo, capture_output=True, check=True)

    head_hash = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=committable_repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    run_log_commit(committable_repo)
    run_log_commit(committable_repo)

    conn = sqlite3.connect(str(committable_repo / ".ctx" / "index.db"))
    count = conn.execute("SELECT COUNT(*) FROM changes WHERE commit_hash = ?", (head_hash,)).fetchone()[0]
    conn.close()
    assert count == 1


def test_log_commit_rolling_cap(committable_repo):
    """25 commits to the same file -> only 20 remain."""
    for i in range(25):
        (committable_repo / "a.py").write_text(f"def f():\n    return {i}\n", encoding="utf-8")
        subprocess.run(["git", "add", "a.py"], cwd=committable_repo, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", f"commit {i}"], cwd=committable_repo, capture_output=True, check=True)
        run_log_commit(committable_repo)

    conn = sqlite3.connect(str(committable_repo / ".ctx" / "index.db"))
    count = conn.execute("SELECT COUNT(*) FROM changes WHERE file = 'a.py'").fetchone()[0]
    conn.close()
    assert count <= 20


def test_log_commit_nonexistent_hash(committable_repo):
    """Passing a hash that doesn't exist raises an error."""
    with pytest.raises(ValueError, match="does not exist"):
        run_log_commit(committable_repo, commit_hash="0000000000000000000000000000000000000000")


def test_log_commit_no_db(tmp_path):
    """Missing .ctx/index.db -> clear error."""
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    with pytest.raises(FileNotFoundError, match=".ctx/index.db does not exist"):
        run_log_commit(tmp_path)
