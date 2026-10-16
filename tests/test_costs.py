import json
from datetime import datetime, timedelta, timezone

import pytest

from ctx_engine.commands.costs_cmd import run_costs
from ctx_engine.config import set_config_value
from ctx_engine.intelligence.cost_tracking import (
    check_budget_warning,
    get_pricing,
    log_cost,
    summarize_costs,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (
        "CTX_LLM_MODEL", "CTX_EXPLAIN_MODEL", "CTX_REVIEW_MODEL",
        "CTX_OLLAMA_HOST", "CTX_OLLAMA_MODEL", "CTX_WATCH_DEBOUNCE_MS",
    ):
        monkeypatch.delenv(var, raising=False)


def test_log_cost_appends_shaped_line(tmp_path):
    cost = log_cost(tmp_path, "explain", "claude-sonnet-4-6", 1000, 500)
    assert cost > 0
    lines = (tmp_path / ".ctx" / "costs.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["command"] == "explain"
    assert entry["model"] == "claude-sonnet-4-6"
    assert entry["input_tokens"] == 1000
    assert entry["output_tokens"] == 500
    assert "timestamp" in entry
    assert entry["estimated_cost_usd"] == round(cost, 6)


def test_unknown_model_costs_zero_but_still_logged(tmp_path):
    cost = log_cost(tmp_path, "watch-ollama", "qwen2.5-coder:7b-instruct", 0, 0)
    assert cost == 0.0
    assert get_pricing(tmp_path, "qwen2.5-coder:7b-instruct") == {
        "input_per_million": 0.0, "output_per_million": 0.0,
    }
    assert len((tmp_path / ".ctx" / "costs.jsonl").read_text().splitlines()) == 1


def test_log_cost_never_raises_when_unwritable(tmp_path, monkeypatch):
    ctx_dir = tmp_path / ".ctx"
    ctx_dir.mkdir()
    costs = ctx_dir / "costs.jsonl"
    costs.mkdir()  # a directory at the ledger path makes appends fail
    assert log_cost(tmp_path, "summarize", "claude-haiku-4-5-20251001", 10, 10) >= 0


def test_summarize_costs_aggregates(tmp_path):
    log_cost(tmp_path, "summarize", "claude-haiku-4-5-20251001", 1_000_000, 0)
    log_cost(tmp_path, "summarize", "claude-haiku-4-5-20251001", 1_000_000, 0)
    log_cost(tmp_path, "explain", "claude-sonnet-4-6", 0, 1_000_000)
    summary = summarize_costs(tmp_path)
    assert summary["calls"] == 3
    assert summary["by_command"]["summarize"]["calls"] == 2
    assert summary["by_command"]["summarize"]["in"] == 2_000_000
    assert summary["by_model"]["claude-sonnet-4-6"]["calls"] == 1
    assert summary["total"] == pytest.approx(
        summary["by_command"]["summarize"]["cost"]
        + summary["by_command"]["explain"]["cost"]
    )


def test_malformed_lines_skipped(tmp_path):
    ledger = tmp_path / ".ctx" / "costs.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text('{"command": "x", "broken"\nnot json at all\n', encoding="utf-8")
    log_cost(tmp_path, "explain", "claude-sonnet-4-6", 100, 100)
    summary = summarize_costs(tmp_path)
    assert summary["calls"] == 1


def test_since_filter(tmp_path):
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    ledger = tmp_path / ".ctx" / "costs.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps({"timestamp": old, "command": "old", "model": "m",
                    "input_tokens": 5, "output_tokens": 5,
                    "estimated_cost_usd": 9.0}) + "\n",
        encoding="utf-8",
    )
    log_cost(tmp_path, "explain", "claude-sonnet-4-6", 100, 100)
    recent = datetime.now(timezone.utc) - timedelta(days=1)
    summary = summarize_costs(tmp_path, since=recent)
    assert summary["calls"] == 1
    assert "explain" in summary["by_command"]


def test_budget_warning_disabled_by_default(tmp_path):
    log_cost(tmp_path, "explain", "claude-sonnet-4-6", 1_000_000, 1_000_000)
    assert check_budget_warning(tmp_path) is None


def test_budget_warning_when_over(tmp_path):
    set_config_value(tmp_path, "costs.monthly_budget_usd", "0.000001")
    log_cost(tmp_path, "explain", "claude-sonnet-4-6", 1000, 1000)
    warning = check_budget_warning(tmp_path)
    assert warning is not None
    assert "budget" in warning.lower()


def test_costs_json_output_valid(tmp_path, capsys):
    log_cost(tmp_path, "summarize", "claude-haiku-4-5-20251001", 1000, 500)
    run_costs(tmp_path, json_output=True)
    payload = json.loads(capsys.readouterr().out)
    assert payload["calls"] == 1
    assert "by_command" in payload
    assert "by_model" in payload


def test_costs_empty_ledger_no_crash(tmp_path, capsys):
    run_costs(tmp_path)
    out = capsys.readouterr().out
    assert "$0.00" in out
