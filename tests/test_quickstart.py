from pathlib import Path
from ctx_engine.commands.quickstart import run_quickstart


def test_run_quickstart(tmp_path, capsys):
    run_quickstart(tmp_path)
    captured = capsys.readouterr()
    out = captured.out
    assert "ctx" in out
    assert "quickstart" in out.lower() or "setup" in out.lower() or "ctx init" in out
    assert len(out) > 50
    assert "Step 1:" in out
    assert "Step 2:" in out
    assert "Step 3:" in out
    assert "Step 4:" in out
    assert "Step 5:" in out
    assert "Step 6:" in out
    assert "Step 7:" in out
    assert "ctx init" in out
    assert "ctx sync" in out
    assert "ctx export" in out


def test_run_quickstart_no_git_repo(tmp_path, capsys):
    run_quickstart(tmp_path)
    captured = capsys.readouterr()
    out = captured.out
    assert len(out) > 50
    assert "git init" in out


def test_run_quickstart_bare(tmp_path, capsys):
    run_quickstart(tmp_path)
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "Step 1:" in captured.out
