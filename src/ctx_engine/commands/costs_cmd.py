"""`ctx costs` — cumulative API spend from .ctx/costs.jsonl."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ctx_engine.config import get_config_value
from ctx_engine.intelligence.cost_tracking import (
    _filtered_entries,
    _parse_ts,
    summarize_costs,
)


def _parse_since(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"Cannot parse --since '{raw}'. Use YYYY-MM-DD or full ISO timestamp.")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def run_costs(repo_root: Path, since: str | None = None, json_output: bool = False) -> None:
    since_dt = _parse_since(since)
    summary = summarize_costs(repo_root, since_dt)

    if json_output:
        try:
            budget = get_config_value(repo_root, "costs.monthly_budget_usd")
        except ValueError:
            budget = 0.0
        print(json.dumps({
            "total_estimated_cost_usd": round(summary["total"], 6),
            "calls": summary["calls"],
            "by_command": {
                name: {
                    "estimated_cost_usd": round(data["cost"], 6),
                    "calls": data["calls"],
                    "input_tokens": data["in"],
                    "output_tokens": data["out"],
                }
                for name, data in summary["by_command"].items()
            },
            "by_model": {
                name: {
                    "estimated_cost_usd": round(data["cost"], 6),
                    "calls": data["calls"],
                }
                for name, data in summary["by_model"].items()
            },
            "monthly_budget_usd": budget,
        }, indent=2))
        return

    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    day_ago = now - timedelta(days=1)
    last7 = sum(
        float(entry.get("estimated_cost_usd", 0.0) or 0.0)
        for entry in _filtered_entries(repo_root, week_ago)
    )
    today = sum(
        float(entry.get("estimated_cost_usd", 0.0) or 0.0)
        for entry in _filtered_entries(repo_root, day_ago)
    )

    repo_name = repo_root.name
    print(f"ctx costs — {repo_name}")
    print()
    print(f"  TOTAL SPEND (all time, estimated): ${summary['total']:.4f}")
    print()
    if not summary["by_command"]:
        print("  No API calls recorded yet.")
    else:
        print("  BY COMMAND")
        for name in sorted(summary["by_command"]):
            data = summary["by_command"][name]
            if data["cost"] == 0 and name == "watch-ollama":
                print(f"    {name:<16} $0.0000  ({data['calls']} calls — local model, no cost)")
            else:
                print(
                    f"    {name:<16} ${data['cost']:.4f}  "
                    f"({data['calls']} calls, {data['in']:,} in / {data['out']:,} out tokens)"
                )
        print()
        print("  BY MODEL")
        for name in sorted(summary["by_model"]):
            data = summary["by_model"][name]
            local = "  (local)" if data["cost"] == 0 else ""
            print(f"    {name:<28} ${data['cost']:.4f}{local}")
    print()
    print(f"  LAST 7 DAYS (estimated): ${last7:.4f}")
    print(f"  TODAY (estimated):       ${today:.4f}")
    print()
    try:
        budget = get_config_value(repo_root, "costs.monthly_budget_usd")
    except ValueError:
        budget = 0.0
    try:
        budget_value = float(budget)
    except (TypeError, ValueError):
        budget_value = 0.0
    if budget_value and budget_value > 0:
        print(f"  Monthly budget: ${budget_value:.2f} (estimated spend)")
    else:
        print("  Monthly budget: not set (run 'ctx config set costs.monthly_budget_usd 5.00' to enable warnings)")
