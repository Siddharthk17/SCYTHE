import subprocess
import pytest
from ctx_engine.commands import run_init


def test_init_speed_output_first_run(tmp_path, capsys):
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True, check=True)
    (tmp_path / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "a.py"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)

    run_init(tmp_path)
    out = capsys.readouterr().out

    assert "ctx init" in out
    assert "files tracked by git" in out
    assert "mtime cache" in out or "first run" in out
    assert "Total time:" in out


def test_init_speed_output_second_run_shows_cache_hits(tmp_path, capsys):
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True, check=True)
    (tmp_path / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "a.py"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)

    run_init(tmp_path)
    capsys.readouterr()

    run_init(tmp_path)
    out = capsys.readouterr().out

    assert "files tracked by git" in out
    assert "skipped (mtime cache hit" in out


def test_init_speed_output_parse_error_counts(tmp_path, capsys):
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True, check=True)
    (tmp_path / "bad.py").write_text("def broken(:\n")
    subprocess.run(["git", "add", "bad.py"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)

    run_init(tmp_path)
    out = capsys.readouterr().out

    assert "parse error" in out.lower()
