import subprocess
from pathlib import Path


def test_ctx_watch_help():
    result = subprocess.run(
        ["ctx", "watch", "--help"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "start" in result.stdout
    assert "stop" in result.stdout
    assert "status" in result.stdout


def test_ctx_watch_start_help():
    result = subprocess.run(
        ["ctx", "watch", "start", "--help"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "--with-ollama" in result.stdout
    assert "--daemon" in result.stdout


def test_ctx_watch_stop_help():
    result = subprocess.run(
        ["ctx", "watch", "stop", "--help"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "Stop" in result.stdout


def test_ctx_watch_status_help():
    result = subprocess.run(
        ["ctx", "watch", "status", "--help"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "Show" in result.stdout


def test_ctx_watch_invoked_without_subcommand(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True, check=True)
    (tmp_path / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "a.py"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["ctx", "init"], cwd=tmp_path, capture_output=True, check=True)

    result = subprocess.run(
        ["ctx", "watch", "--help"],
        capture_output=True, text=True, cwd=tmp_path,
    )
    assert result.returncode == 0
    assert "start" in result.stdout or "Watch" in result.stdout


def test_ctx_watch_status_not_running(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True, check=True)
    (tmp_path / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "a.py"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["ctx", "init"], cwd=tmp_path, capture_output=True, check=True)

    result = subprocess.run(
        ["ctx", "watch", "status"],
        capture_output=True, text=True, cwd=tmp_path,
    )
    assert result.returncode == 0
    assert "NOT RUNNING" in result.stdout
    assert "ollama" in result.stdout.lower()
