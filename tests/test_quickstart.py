from pathlib import Path
from ctx_engine.commands.quickstart import run_quickstart


def test_run_quickstart(tmp_path, capsys):
    run_quickstart(tmp_path)
    captured = capsys.readouterr()
    out = captured.out
    assert "ctx" in out
    assert "quickstart" in out.lower() or "setup" in out.lower() or "ctx init" in out
    assert len(out) > 50


def test_run_quickstart_no_git_repo(tmp_path, capsys):
    run_quickstart(tmp_path)
    captured = capsys.readouterr()
    assert len(captured.out) > 50


def test_run_quickstart_bare(tmp_path, capsys):
    run_quickstart(tmp_path)
    captured = capsys.readouterr()
    assert captured.err == ""
