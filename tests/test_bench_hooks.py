import sqlite3
import subprocess
from pathlib import Path

import pytest

from ctx_engine.commands.bench_hooks_cmd import run_bench_hooks, select_benchmark_sample
from ctx_engine.db import init_schema


def _indexed_repo(tmp_path, n_files=30):
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True, check=True)
    (tmp_path / ".gitignore").write_text(".ctx/\n", encoding="utf-8")
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    init_schema(conn)
    for i in range(n_files):
        rel = f"mod_{i:02d}.py"
        content = f"def func_{i}():\n    return {i}\n"
        (tmp_path / rel).write_text(content, encoding="utf-8")
        conn.execute(
            "INSERT INTO files (path, semantic_hash, content_hash, purpose, summary) "
            "VALUES (?, ?, ?, ?, ?)",
            (rel, f"h{i}", f"c{i}", "purpose", "summary"),
        )
    conn.commit()
    file_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    func_count = conn.execute("SELECT COUNT(*) FROM functions").fetchone()[0]
    conn.close()
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)
    return tmp_path


def test_select_sample_exact_count(tmp_path):
    repo = _indexed_repo(tmp_path, n_files=30)
    conn = sqlite3.connect(repo / ".ctx" / "index.db")
    conn.row_factory = sqlite3.Row
    sample = select_benchmark_sample(conn, repo, n=20)
    conn.close()
    assert len(sample) == 20
    assert len(set(sample)) == 20
    db_paths = {
        r[0]
        for r in sqlite3.connect(repo / ".ctx" / "index.db").execute(
            "SELECT path FROM files"
        ).fetchall()
    }
    assert set(sample) <= db_paths


def test_select_sample_small_repo(tmp_path):
    repo = _indexed_repo(tmp_path, n_files=5)
    conn = sqlite3.connect(repo / ".ctx" / "index.db")
    conn.row_factory = sqlite3.Row
    sample = select_benchmark_sample(conn, repo, n=20)
    conn.close()
    assert len(sample) == 5


def test_bench_makes_zero_db_writes(tmp_path):
    repo = _indexed_repo(tmp_path, n_files=10)
    db_path = repo / ".ctx" / "index.db"
    conn = sqlite3.connect(db_path)
    before_files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    before_funcs = conn.execute("SELECT COUNT(*) FROM functions").fetchone()[0]
    conn.close()
    run_bench_hooks(repo, samples=5)
    conn = sqlite3.connect(db_path)
    after_files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    after_funcs = conn.execute("SELECT COUNT(*) FROM functions").fetchone()[0]
    conn.close()
    assert before_files == after_files
    assert before_funcs == after_funcs


def test_bench_makes_zero_git_mutation(tmp_path, capsys):
    repo = _indexed_repo(tmp_path, n_files=10)
    before = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    run_bench_hooks(repo, samples=5)
    capsys.readouterr()
    after = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    assert before == after


def test_bench_output_has_all_rows(tmp_path, capsys):
    repo = _indexed_repo(tmp_path, n_files=10)
    run_bench_hooks(repo, samples=5)
    out = capsys.readouterr().out
    for label in ("1 file:", "5 files:", "20 files:", "50 files:"):
        assert label in out, f"missing row {label}"
    assert "✓" in out or "⚠" in out


def test_bench_samples_override(tmp_path):
    repo = _indexed_repo(tmp_path, n_files=30)
    conn = sqlite3.connect(repo / ".ctx" / "index.db")
    conn.row_factory = sqlite3.Row
    sample = select_benchmark_sample(conn, repo, n=5)
    conn.close()
    assert len(sample) == 5
