import subprocess

from ctx_engine.commands.doctor import _collect_checks, run_doctor
from ctx_engine.commands.init import run_init
from ctx_engine.commands.install_hooks import run_install_hooks
from ctx_engine.commands.status import run_status
from ctx_engine.config import set_config_value
from ctx_engine.intelligence.cost_tracking import log_cost


def _git_repo(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True, check=True)
    return tmp_path


def _indexed_repo(tmp_path):
    _git_repo(tmp_path)
    (tmp_path / "a.py").write_text("def run():\n    return 1\n")
    subprocess.run(["git", "add", "a.py"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)
    run_init(tmp_path)
    return tmp_path


def _h_checks(repo):
    return {c.id: c for c in _collect_checks(repo)}


def test_status_config_not_configured(tmp_path, capsys):
    repo = _indexed_repo(tmp_path)
    run_status(repo)
    assert "NOT CONFIGURED" in capsys.readouterr().out


def test_status_config_counts(tmp_path, capsys):
    repo = _indexed_repo(tmp_path)
    set_config_value(repo, "watch.debounce_ms", "750")
    set_config_value(repo, "heuristics.call_fanin_threshold", "25")
    run_status(repo)
    out = capsys.readouterr().out
    assert "2 keys customized" in out


def test_status_shows_cost_figures(tmp_path, capsys):
    repo = _indexed_repo(tmp_path)
    log_cost(repo, "summarize", "claude-haiku-4-5-20251001", 1_000_000, 0)
    run_status(repo)
    out = capsys.readouterr().out
    assert "all-time" in out
    assert "last 7 days" in out


def test_status_shows_all_four_hooks(tmp_path, capsys):
    repo = _indexed_repo(tmp_path)
    run_install_hooks(repo)
    run_status(repo)
    out = capsys.readouterr().out
    for hook in ("pre-commit", "post-commit", "post-merge", "post-checkout"):
        assert hook in out
    assert out.count("INSTALLED") >= 4


def test_doctor_h1_fails_on_malformed_toml(tmp_path):
    repo = _git_repo(tmp_path)
    ctx_dir = repo / ".ctx"
    ctx_dir.mkdir()
    (ctx_dir / "config.toml").write_text("[watch\ndebounce_ms = [\n", encoding="utf-8")
    assert _h_checks(repo)["H1"].passed is False


def test_doctor_h1_passes_valid_or_absent(tmp_path):
    repo = _git_repo(tmp_path)
    assert _h_checks(repo)["H1"].passed is True
    ctx_dir = repo / ".ctx"
    ctx_dir.mkdir()
    set_config_value(repo, "watch.debounce_ms", "750")
    assert _h_checks(repo)["H1"].passed is True


def test_doctor_h2_fails_and_fix_removes(tmp_path, capsys):
    repo = _git_repo(tmp_path)
    ctx_dir = repo / ".ctx"
    ctx_dir.mkdir()
    (ctx_dir / "config.toml").write_text(
        '[llm]\nmodel = "x"\napi_key = "sk-leaked-test"\n', encoding="utf-8"
    )
    assert _h_checks(repo)["H2"].passed is False
    run_doctor(repo, apply_fix=True, json_output=True)
    content = (ctx_dir / "config.toml").read_text(encoding="utf-8")
    assert "api_key" not in content
    assert _h_checks(repo)["H2"].passed is True


def test_doctor_h5_fails_then_fix_installs(tmp_path):
    repo = _git_repo(tmp_path)
    assert _h_checks(repo)["H5"].passed is False
    run_doctor(repo, apply_fix=True, json_output=True)
    assert _h_checks(repo)["H5"].passed is True
    for hook in ("pre-commit", "post-commit", "post-merge", "post-checkout"):
        assert (repo / ".git" / "hooks" / hook).exists()


def test_doctor_fix_twice_idempotent(tmp_path):
    repo = _git_repo(tmp_path)
    run_doctor(repo, apply_fix=True, json_output=True)
    first = {c.id: (c.passed, c.detail) for c in _collect_checks(repo)}
    run_doctor(repo, apply_fix=True, json_output=True)
    second = {c.id: (c.passed, c.detail) for c in _collect_checks(repo)}
    for check_id in ("H1", "H2", "H3", "H4", "H5"):
        assert first[check_id] == second[check_id]
