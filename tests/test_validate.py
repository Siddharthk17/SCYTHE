import subprocess
import sqlite3
import pytest
from pathlib import Path
from ctx_engine.db import init_schema
from ctx_engine.commands.validate import run_validate


def _commit_and_stage_lockstep(repo: Path, path: str, content: str, db_path: Path):
    """Write content, commit it, then re-stage same content so git diff --cached sees it."""
    (repo / path).write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", path], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "auto"], cwd=repo, capture_output=True, check=True)
    _insert_file_into_db(db_path, path)

def _insert_file_into_db(db_path: Path, path: str, s_hash: str = "dummy", c_hash: str = "dummy"):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR IGNORE INTO files (path, semantic_hash, content_hash, purpose, summary) VALUES (?, ?, ?, ?, ?)",
        (path, s_hash, c_hash, "purpose", "summary"),
    )
    conn.commit()
    conn.close()


def _compute_semantic_hash(source: bytes, ext: str) -> str:
    from ctx_engine.discovery import EXTENSION_TO_LANGUAGE
    from ctx_engine.languages.registry import get_parser
    from ctx_engine.hashing import file_semantic_hash
    lang = EXTENSION_TO_LANGUAGE[ext]
    parser = get_parser(lang)
    tree = parser.parse(source)
    return file_semantic_hash(tree, source, lang)


@pytest.fixture
def validatable_repo(tmp_path):
    """Create a temp git repo with .ctx/index.db."""
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True, check=True)
    (tmp_path / ".gitignore").write_text(".ctx/\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)

    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    init_schema(conn)
    conn.close()
    return tmp_path


def test_validate_happy_path(validatable_repo):
    """Stage, commit, re-stage same content -> semantic hash matches -> exit 0."""
    source = "def add(a, b):\n    return a + b\n".encode()
    s_hash = _compute_semantic_hash(source, ".py")
    db_path = validatable_repo / ".ctx" / "index.db"
    _commit_and_stage_lockstep(validatable_repo, "calc.py", source.decode(), db_path)
    # Overwrite DB hash to what we computed, then re-stage same content
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE files SET semantic_hash = ? WHERE path = 'calc.py'", (s_hash,))
    conn.commit()
    conn.close()
    subprocess.run(["git", "add", "calc.py"], cwd=validatable_repo, capture_output=True, check=True)
    run_validate(validatable_repo)


def test_validate_stale_file(validatable_repo):
    """Change staged content without re-indexing -> stale."""
    db_path = validatable_repo / ".ctx" / "index.db"
    _commit_and_stage_lockstep(validatable_repo, "calc.py",
        "def add(a, b):\n    return a + b\n", db_path)
    # Change and re-stage only (no re-index)
    (validatable_repo / "calc.py").write_text("def add(a, b):\n    return a + b + 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "calc.py"], cwd=validatable_repo, capture_output=True, check=True)
    with pytest.raises(SystemExit):
        run_validate(validatable_repo)


def test_validate_not_indexed(validatable_repo):
    """New Python file staged but not indexed -> NOT INDEXED."""
    (validatable_repo / "new.py").write_text("def f():\n    pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "new.py"], cwd=validatable_repo, capture_output=True, check=True)
    with pytest.raises(SystemExit):
        run_validate(validatable_repo)


def test_validate_deleted_file(validatable_repo):
    """Deleted file is silently skipped."""
    db_path = validatable_repo / ".ctx" / "index.db"
    _commit_and_stage_lockstep(validatable_repo, "calc.py",
        "def add(a, b):\n    return a + b\n", db_path)
    subprocess.run(["git", "rm", "calc.py"], cwd=validatable_repo, capture_output=True, check=True)
    run_validate(validatable_repo)


def test_validate_non_parseable(validatable_repo):
    """Non-parseable file (.json) staged -> silently skipped."""
    (validatable_repo / "data.json").write_text('{"key": "value"}', encoding="utf-8")
    subprocess.run(["git", "add", "data.json"], cwd=validatable_repo, capture_output=True, check=True)
    run_validate(validatable_repo)


def test_validate_formatting_only_change(validatable_repo):
    """Only whitespace changes -> semantic hash matches -> exit 0."""
    db_path = validatable_repo / ".ctx" / "index.db"
    source = "def add(a, b):\n    return a + b\n".encode()
    s_hash = _compute_semantic_hash(source, ".py")
    _commit_and_stage_lockstep(validatable_repo, "calc.py", source.decode(), db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE files SET semantic_hash = ? WHERE path = 'calc.py'", (s_hash,))
    conn.commit()
    conn.close()
    # Re-stage with extra blank lines
    (validatable_repo / "calc.py").write_text("def add(a, b):\n\n    return a + b\n", encoding="utf-8")
    subprocess.run(["git", "add", "calc.py"], cwd=validatable_repo, capture_output=True, check=True)
    run_validate(validatable_repo)


def test_validate_explicit_files(validatable_repo):
    """--files overrides staged-file detection."""
    db_path = validatable_repo / ".ctx" / "index.db"
    source = "def f():\n    return 1\n".encode()
    s_hash = _compute_semantic_hash(source, ".py")
    _commit_and_stage_lockstep(validatable_repo, "explicit.py", source.decode(), db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE files SET semantic_hash = ? WHERE path = 'explicit.py'", (s_hash,))
    conn.commit()
    conn.close()
    subprocess.run(["git", "add", "explicit.py"], cwd=validatable_repo, capture_output=True, check=True)
    run_validate(validatable_repo, files=["explicit.py"])


def test_validate_no_db(tmp_path):
    """No .ctx/index.db -> clear error."""
    with pytest.raises(FileNotFoundError, match="Database not found"):
        run_validate(tmp_path)


def test_validate_not_in_git(tmp_path):
    """Outside a git repo -> clear error."""
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE files (path TEXT PRIMARY KEY, semantic_hash TEXT)")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="Not inside a git repository"):
        run_validate(tmp_path)
