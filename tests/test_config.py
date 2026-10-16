import pytest

from ctx_engine.config import (
    DEFAULT_CONFIG,
    get_config_value,
    load_config,
    valid_keys,
    write_toml,
)
from ctx_engine.commands.config_cmd import (
    run_config_get,
    run_config_list,
    run_config_reset,
    run_config_set,
)
from ctx_engine.intelligence.confidence import decay


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (
        "CTX_LLM_MODEL", "CTX_EXPLAIN_MODEL", "CTX_REVIEW_MODEL",
        "CTX_OLLAMA_HOST", "CTX_OLLAMA_MODEL", "CTX_WATCH_DEBOUNCE_MS",
    ):
        monkeypatch.delenv(var, raising=False)


def test_load_config_without_file_returns_defaults(tmp_path):
    assert load_config(tmp_path) == DEFAULT_CONFIG


def test_partial_config_deep_merges(tmp_path):
    ctx_dir = tmp_path / ".ctx"
    ctx_dir.mkdir()
    (ctx_dir / "config.toml").write_text(
        "[watch]\ndebounce_ms = 750\n", encoding="utf-8"
    )
    config = load_config(tmp_path)
    assert config["watch"]["debounce_ms"] == 750
    assert config["llm"] == DEFAULT_CONFIG["llm"]
    assert config["heuristics"] == DEFAULT_CONFIG["heuristics"]


def test_env_var_wins_over_file_and_default(tmp_path, monkeypatch):
    ctx_dir = tmp_path / ".ctx"
    ctx_dir.mkdir()
    (ctx_dir / "config.toml").write_text(
        "[watch]\ndebounce_ms = 750\n", encoding="utf-8"
    )
    monkeypatch.setenv("CTX_WATCH_DEBOUNCE_MS", "900")
    assert get_config_value(tmp_path, "watch.debounce_ms") == 900


def test_file_wins_when_env_unset(tmp_path):
    ctx_dir = tmp_path / ".ctx"
    ctx_dir.mkdir()
    (ctx_dir / "config.toml").write_text(
        "[watch]\ndebounce_ms = 750\n", encoding="utf-8"
    )
    assert get_config_value(tmp_path, "watch.debounce_ms") == 750


def test_default_when_neither_set(tmp_path):
    assert get_config_value(tmp_path, "watch.debounce_ms") == 500
    assert get_config_value(tmp_path, "llm.model") == "claude-haiku-4-5-20251001"


def test_write_toml_round_trip_types(tmp_path):
    ctx_dir = tmp_path / ".ctx"
    ctx_dir.mkdir()
    target = ctx_dir / "config.toml"
    config = {
        "llm": {"model": "custom-model", "explain_model": "exp"},
        "watch": {"debounce_ms": 750},
        "costs": {"monthly_budget_usd": 5.5},
    }
    write_toml(config, target)
    loaded = load_config(tmp_path)
    assert loaded["watch"]["debounce_ms"] == 750
    assert isinstance(loaded["watch"]["debounce_ms"], int)
    assert loaded["costs"]["monthly_budget_usd"] == 5.5
    assert isinstance(loaded["costs"]["monthly_budget_usd"], float)


def test_config_set_rejects_unknown_key(tmp_path, capsys):
    with pytest.raises(SystemExit):
        run_config_set(tmp_path, "nope.bogus", "1")
    out = capsys.readouterr().out
    assert "unknown config key" in out
    assert "llm.model" in out


def test_config_set_rejects_forbidden_pattern(tmp_path, capsys):
    with pytest.raises(SystemExit):
        run_config_set(tmp_path, "llm.api_key", "sk-test-should-fail")
    out = capsys.readouterr().out
    assert "cannot be stored" in out
    assert not (tmp_path / ".ctx" / "config.toml").exists()


def test_config_set_get_reset_cycle(tmp_path, capsys):
    run_config_set(tmp_path, "heuristics.call_fanin_threshold", "25")
    capsys.readouterr()
    run_config_get(tmp_path, "heuristics.call_fanin_threshold")
    assert capsys.readouterr().out.strip() == "25"
    assert get_config_value(tmp_path, "heuristics.call_fanin_threshold") == 25
    assert isinstance(get_config_value(tmp_path, "heuristics.call_fanin_threshold"), int)
    run_config_reset(tmp_path, "heuristics.call_fanin_threshold")
    assert get_config_value(tmp_path, "heuristics.call_fanin_threshold") == 10


def test_config_reset_unknown_key_rejected(tmp_path):
    with pytest.raises(SystemExit):
        run_config_reset(tmp_path, "bogus.key")


def test_config_list_shows_sources(tmp_path, capsys):
    run_config_set(tmp_path, "watch.debounce_ms", "750")
    capsys.readouterr()
    run_config_list(tmp_path)
    out = capsys.readouterr().out
    assert "[F]" in out
    assert "[D]" in out
    assert "watch.debounce_ms" in out


def test_valid_keys_cover_all_defaults():
    keys = valid_keys()
    assert "llm.model" in keys
    assert "costs.monthly_budget_usd" in keys
    assert len(keys) == sum(len(v) for v in DEFAULT_CONFIG.values())


def test_decay_backward_compatible_single_arg():
    assert decay(0.9) == pytest.approx(0.9 * 0.85)
