import json
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from ctx_engine.commands.doctor import _collect_checks, run_doctor
from ctx_engine.config import reset_config_value, set_config_value
from ctx_engine.db import init_schema


def _git_repo(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True, check=True)
    return tmp_path


def _healthy_repo(tmp_path):
    _git_repo(tmp_path)
    (tmp_path / "a.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.py"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)
    from ctx_engine.commands.init import run_init

    run_init(tmp_path)
    return tmp_path


def _ids_by_prefix(repo):
    return {c.id: c for c in _collect_checks(repo)}


def test_category_d_conditional_api(tmp_path):
    repo = _healthy_repo(tmp_path)
    set_config_value(repo, "daemon.summarization_mode", "api")
    ids = _ids_by_prefix(repo)
    assert "D3" not in ids
    assert "D4" not in ids
    assert "D5" in ids


def test_category_d_conditional_ollama(tmp_path):
    repo = _healthy_repo(tmp_path)
    set_config_value(repo, "daemon.summarization_mode", "ollama")
    ids = _ids_by_prefix(repo)
    assert "D3" in ids
    assert "D4" in ids
    assert "D5" not in ids


def test_category_i_timeout_fix(tmp_path):
    repo = _healthy_repo(tmp_path)
    set_config_value(repo, "hooks.validate_timeout_ms", "0")
    ids = _ids_by_prefix(repo)
    assert ids["I1"].passed is False
    run_doctor(repo, apply_fix=True)
    ids2 = _ids_by_prefix(repo)
    assert ids2["I1"].passed is True


def test_category_i_degraded_rate(tmp_path):
    repo = _healthy_repo(tmp_path)
    path = repo / ".ctx" / "hook-timings.jsonl"
    with open(path, "w", encoding="utf-8") as handle:
        for _ in range(15):
            handle.write(json.dumps({"degraded": False}) + "\n")
        for _ in range(5):
            handle.write(json.dumps({"degraded": True}) + "\n")
    ids = _ids_by_prefix(repo)
    # 5/20 = 25% -> not < 25%, should fail.
    assert ids["I3"].passed is False
    with open(path, "w", encoding="utf-8") as handle:
        for _ in range(18):
            handle.write(json.dumps({"degraded": False}) + "\n")
        for _ in range(2):
            handle.write(json.dumps({"degraded": True}) + "\n")
    ids2 = _ids_by_prefix(repo)
    assert ids2["I3"].passed is True


def test_category_j_all_pass_healthy(tmp_path):
    repo = _healthy_repo(tmp_path)
    ids = _ids_by_prefix(repo)
    for check_id in ("J1", "J2", "J3", "J4"):
        assert check_id in ids
        assert ids[check_id].passed is True


def test_category_j_fails_on_broken_adapter(tmp_path):
    repo = _healthy_repo(tmp_path)
    with patch(
        "ctx_engine.languages.python_adapter.PythonAdapter.extract",
        side_effect=RuntimeError("boom"),
    ):
        ids = _ids_by_prefix(repo)
        assert ids["J1"].passed is False


def test_doctor_fix_idempotent(tmp_path, capsys):
    repo = _healthy_repo(tmp_path)
    set_config_value(repo, "hooks.validate_timeout_ms", "-5")
    set_config_value(repo, "hooks.max_file_size_bytes", "-1")
    set_config_value(repo, "daemon.summarization_mode", "api")
    set_config_value(repo, "daemon.api_mode_batch_window_seconds", "-1")
    run_doctor(repo, apply_fix=True)
    capsys.readouterr()
    run_doctor(repo, apply_fix=True)
    ids = _ids_by_prefix(repo)
    assert ids["I1"].passed is True
    assert ids["I2"].passed is True
