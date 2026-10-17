import json
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from ctx_engine.commands.validate import (
    ValidateReport,
    _trim_hook_timings_if_needed,
    run_validate,
    validate_exit_code,
)
from ctx_engine.db import init_schema


def _semantic_hash(source: bytes, ext: str) -> str:
    from ctx_engine.discovery import EXTENSION_TO_LANGUAGE
    from ctx_engine.hashing import file_semantic_hash
    from ctx_engine.languages.registry import get_parser

    lang = EXTENSION_TO_LANGUAGE[ext]
    tree = get_parser(lang).parse(source)
    return file_semantic_hash(tree, source, lang)


@pytest.fixture
def hook_repo(tmp_path):
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


def _stage_and_index(repo: Path, rel: str, content: str):
    (repo / rel).write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", rel], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "auto"], cwd=repo, capture_output=True, check=True)
    source = content.encode()
    s_hash = _semantic_hash(source, Path(rel).suffix)
    db_path = repo / ".ctx" / "index.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR REPLACE INTO files (path, semantic_hash, content_hash, purpose, summary) "
        "VALUES (?, ?, ?, ?, ?)",
        (rel, s_hash, "c", "purpose", "summary"),
    )
    conn.commit()
    conn.close()
    subprocess.run(["git", "add", rel], cwd=repo, capture_output=True, check=True)


def test_unlimited_timeout_healthy(hook_repo):
    _stage_and_index(hook_repo, "calc.py", "def add(a, b):\n    return a + b\n")
    report = run_validate(hook_repo, files=["calc.py"], timeout_ms=999_999)
    assert report.degraded is False
    assert report.timed_out_before_checking == []
    assert "calc.py" in report.checked
    assert validate_exit_code(report) == 0


def test_zero_timeout_never_blocks(hook_repo):
    _stage_and_index(hook_repo, "calc.py", "def add(a, b):\n    return a + b\n")
    report = run_validate(hook_repo, files=["calc.py"], timeout_ms=0)
    assert report.degraded is True
    assert report.checked == []
    assert "calc.py" in report.timed_out_before_checking
    assert validate_exit_code(report) == 0


def test_stale_before_deadline_still_blocks(hook_repo):
    _stage_and_index(hook_repo, "a.py", "def f():\n    return 1\n")
    _stage_and_index(hook_repo, "b.py", "def g():\n    return 2\n")
    # Make a.py stale and stage it first.
    (hook_repo / "a.py").write_text("def f():\n    return 999\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.py", "b.py"], cwd=hook_repo, capture_output=True, check=True)
    # Simulate a mid-run timeout: first file checked, rest timed out.
    # Patch monotonic so the deadline hits after the first file.
    import time as _time

    real_monotonic = _time.monotonic
    start = real_monotonic()
    calls = {"n": 0}

    def _fake_monotonic():
        calls["n"] += 1
        # 1st call: start (run_validate start). 2nd: first file check (in budget).
        # 3rd+: past deadline.
        if calls["n"] <= 2:
            return start + 0.0001
        return start + 1000.0

    with patch("ctx_engine.commands.validate.time") as mock_time:
        mock_time.monotonic.side_effect = _fake_monotonic
        try:
            report = run_validate(hook_repo, files=["a.py", "b.py"], timeout_ms=100)
        except SystemExit as exc:
            assert exc.code == 1
            report = exc.report
    assert "a.py" in report.stale
    assert validate_exit_code(report) == 1
    assert report.degraded is True


def test_size_circuit_breaker(hook_repo):
    from ctx_engine.config import set_config_value

    _stage_and_index(hook_repo, "big.py", "def f():\n    return 1\n")
    set_config_value(hook_repo, "hooks.max_file_size_bytes", "10")
    try:
        report = run_validate(hook_repo, files=["big.py"], timeout_ms=999_999)
    finally:
        from ctx_engine.config import reset_config_value

        reset_config_value(hook_repo, "hooks.max_file_size_bytes")
    assert "big.py" in report.skipped_large
    assert "big.py" not in report.stale
    assert "big.py" not in report.not_indexed
    assert validate_exit_code(report) == 0


def test_hook_timings_appends_one_line(hook_repo):
    _stage_and_index(hook_repo, "calc.py", "def add(a, b):\n    return a + b\n")
    path = hook_repo / ".ctx" / "hook-timings.jsonl"
    before = len(path.read_text(encoding="utf-8").splitlines()) if path.exists() else 0
    run_validate(hook_repo, timeout_ms=999_999)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == before + 1
    entry = json.loads(lines[-1])
    for key in ("timestamp", "elapsed_ms", "checked_count", "degraded", "stale_count"):
        assert key in entry


def test_trim_keeps_most_recent_200(hook_repo):
    path = hook_repo / ".ctx" / "hook-timings.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for i in range(250):
            handle.write(json.dumps({"timestamp": f"2026-01-01T00:00:{i:02d}", "n": i}) + "\n")
    _trim_hook_timings_if_needed(path, _force=True)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 200
    last = json.loads(lines[-1])
    assert last["n"] == 249


def test_validate_exit_code_matrix():
    def _report(stale, not_indexed, degraded):
        return ValidateReport(
            checked=[], stale=stale, not_indexed=not_indexed,
            skipped_large=[], timed_out_before_checking=[],
            degraded=degraded, elapsed_ms=1.0,
        )

    assert validate_exit_code(_report([], [], False)) == 0
    assert validate_exit_code(_report([], [], True)) == 0
    assert validate_exit_code(_report(["a.py"], [], False)) == 1
    assert validate_exit_code(_report(["a.py"], [], True)) == 1
    assert validate_exit_code(_report([], ["b.py"], False)) == 1
    assert validate_exit_code(_report([], ["b.py"], True)) == 1
    assert validate_exit_code(_report(["a.py"], ["b.py"], True)) == 1
