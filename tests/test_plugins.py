import subprocess

from ctx_engine.commands.danger_cmd import danger_detect
from ctx_engine.commands.init import run_init
from ctx_engine.commands.plugin_cmd import run_plugin_list, run_plugin_validate
from ctx_engine.db import connect
from ctx_engine.intelligence.plugins import (
    discover_plugins,
    load_plugin,
    run_plugin_safely,
    PluginInfo,
)

VALID_PLUGIN = '''import hashlib

PLUGIN_NAME = "fixed_marker"


def detect(conn, repo_root):
    return [{
        "id": hashlib.sha256(b"plugin-fixed-record").hexdigest()[:12],
        "scope": "*",
        "description": "Fixed marker from test plugin.",
        "reason": "Flagged by plugin 'fixed_marker'.",
    }]
'''

RAISING_PLUGIN = '''def detect(conn, repo_root):
    raise RuntimeError("boom")
'''

NON_LIST_PLUGIN = '''def detect(conn, repo_root):
    return {"not": "a list"}
'''


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


def _write_plugin(repo, name, content):
    plugin_dir = repo / ".ctx" / "plugins"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    path = plugin_dir / name
    path.write_text(content, encoding="utf-8")
    return path


def test_discover_finds_py_skips_underscore_and_other(tmp_path):
    repo = _git_repo(tmp_path)
    _write_plugin(repo, "good.py", VALID_PLUGIN)
    _write_plugin(repo, "_private.py", VALID_PLUGIN)
    _write_plugin(repo, "notes.txt", "not python")
    found = {info.name for info in discover_plugins(repo)}
    assert found == {"good"}


def test_discover_absent_dir_returns_empty(tmp_path):
    assert discover_plugins(tmp_path) == []


def test_load_plugin_well_formed(tmp_path):
    repo = _git_repo(tmp_path)
    path = _write_plugin(repo, "good.py", VALID_PLUGIN)
    assert callable(load_plugin(PluginInfo(path=path, name="good")))


def test_load_plugin_syntax_error_returns_none(tmp_path):
    repo = _git_repo(tmp_path)
    path = _write_plugin(repo, "broken.py", "def detect(:\n  broken syntax {{{\n")
    assert load_plugin(PluginInfo(path=path, name="broken")) is None


def test_load_plugin_missing_detect_returns_none(tmp_path):
    repo = _git_repo(tmp_path)
    path = _write_plugin(repo, "nodetect.py", "X = 1\n")
    assert load_plugin(PluginInfo(path=path, name="nodetect")) is None


def test_run_plugin_safely_raising_returns_empty(tmp_path):
    repo = _indexed_repo(tmp_path)
    path = _write_plugin(repo, "raising.py", RAISING_PLUGIN)
    info = PluginInfo(path=path, name="raising")
    conn = connect(repo / ".ctx" / "index.db")
    try:
        assert run_plugin_safely(info, load_plugin(info), conn, repo) == []
    finally:
        conn.close()


def test_run_plugin_safely_non_list_returns_empty(tmp_path):
    repo = _indexed_repo(tmp_path)
    path = _write_plugin(repo, "nonlist.py", NON_LIST_PLUGIN)
    info = PluginInfo(path=path, name="nonlist")
    conn = connect(repo / ".ctx" / "index.db")
    try:
        assert run_plugin_safely(info, load_plugin(info), conn, repo) == []
    finally:
        conn.close()


def test_end_to_end_plugin_records_added_as_auto(tmp_path):
    repo = _indexed_repo(tmp_path)
    _write_plugin(repo, "fixed_marker.py", VALID_PLUGIN)
    conn = connect(repo / ".ctx" / "index.db")
    try:
        result = danger_detect(conn, repo)
        scopes = [d.scope for d in result["detected"]]
        assert "*" in scopes
        row = conn.execute(
            "SELECT added_by FROM dangers WHERE description = ?",
            ("Fixed marker from test plugin.",),
        ).fetchone()
        assert row is not None
        assert row["added_by"] == "auto"
    finally:
        conn.close()


def test_plugin_records_removed_when_plugin_gone(tmp_path):
    repo = _indexed_repo(tmp_path)
    path = _write_plugin(repo, "fixed_marker.py", VALID_PLUGIN)
    conn = connect(repo / ".ctx" / "index.db")
    try:
        danger_detect(conn, repo)
        path.unlink()
        danger_detect(conn, repo)
        row = conn.execute(
            "SELECT 1 FROM dangers WHERE description = ?",
            ("Fixed marker from test plugin.",),
        ).fetchone()
        assert row is None
    finally:
        conn.close()


def test_plugin_list_includes_security_warning(tmp_path, capsys):
    repo = _git_repo(tmp_path)
    run_plugin_list(repo)
    out = capsys.readouterr().out
    assert "arbitrary Python" in out


def test_plugin_validate_valid_reports_all_checks(tmp_path, capsys):
    repo = _indexed_repo(tmp_path)
    path = _write_plugin(repo, "fixed_marker.py", VALID_PLUGIN)
    run_plugin_validate(repo, str(path))
    out = capsys.readouterr().out
    assert "Plugin is valid." in out


def test_plugin_validate_raising_graceful(tmp_path, capsys):
    repo = _indexed_repo(tmp_path)
    path = _write_plugin(repo, "raising.py", RAISING_PLUGIN)
    try:
        run_plugin_validate(repo, str(path))
    except SystemExit:
        pass
    out = capsys.readouterr().out
    assert "RuntimeError" in out or "INVALID" in out
