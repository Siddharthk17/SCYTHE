"""Append-only API spend ledger (``.ctx/costs.jsonl``).

One JSON object per line, one line per LLM API call. Costs are estimates
from ``BUILTIN_PRICING`` (or a ``[costs.pricing.<model>]`` override), never
bills — label every display "estimated".
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from ctx_engine.config import BUILTIN_PRICING, get_config_value, load_config

logger = logging.getLogger("ctx")


def get_pricing(repo_root: Path, model: str) -> dict:
    """config.toml [costs.pricing.<model>] > BUILTIN_PRICING > $0 for local models."""
    try:
        config = load_config(repo_root)
    except ValueError:
        config = {}
    custom = config.get("costs", {}).get("pricing", {}) if isinstance(config, dict) else {}
    if isinstance(custom, dict) and model in custom:
        entry = custom[model]
        try:
            return {
                "input_per_million": float(entry.get("input_per_million", 0.0)),
                "output_per_million": float(entry.get("output_per_million", 0.0)),
            }
        except (TypeError, ValueError, AttributeError):
            pass
    if model in BUILTIN_PRICING:
        return dict(BUILTIN_PRICING[model])
    return {"input_per_million": 0.0, "output_per_million": 0.0}


def log_cost(repo_root, command: str, model: str, input_tokens: int, output_tokens: int) -> float:
    """Append one ledger entry. Returns estimated cost in USD. Never raises."""
    try:
        pricing = get_pricing(repo_root, model)
        cost = (input_tokens / 1_000_000) * pricing["input_per_million"] \
            + (output_tokens / 1_000_000) * pricing["output_per_million"]
    except Exception:
        return 0.0
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "command": command,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_cost_usd": round(cost, 6),
    }
    try:
        path = Path(repo_root) / ".ctx" / "costs.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
    except OSError as err:
        logger.warning("Failed to write cost log: %s", err)
    except Exception as err:  # never break the caller for telemetry
        logger.warning("Failed to write cost log: %s", err)
    return cost


def _iter_entries(repo_root: Path):
    path = Path(repo_root) / ".ctx" / "costs.jsonl"
    if not path.exists():
        return
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
    except OSError:
        return


def _start_of_current_month() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _start_of_today() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _parse_ts(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _sum_costs_since(repo_root: Path, since: datetime) -> float:
    total = 0.0
    for entry in _iter_entries(repo_root):
        parsed = _parse_ts(entry.get("timestamp", ""))
        if parsed is None:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if parsed >= since:
            try:
                total += float(entry.get("estimated_cost_usd", 0.0))
            except (TypeError, ValueError):
                continue
    return total


def summarize_costs(repo_root: Path, since: datetime | None = None) -> dict:
    """Aggregate the ledger by command and model. Skips malformed lines."""
    by_command: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    total = 0.0
    calls = 0
    for entry in _iter_entries(repo_root):
        if since is not None:
            parsed = _parse_ts(entry.get("timestamp", ""))
            if parsed is None:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            if parsed < since:
                continue
        try:
            cost = float(entry.get("estimated_cost_usd", 0.0))
            in_tok = int(entry.get("input_tokens", 0))
            out_tok = int(entry.get("output_tokens", 0))
        except (TypeError, ValueError):
            continue
        command = str(entry.get("command", "unknown"))
        model = str(entry.get("model", "unknown"))
        total += cost
        calls += 1
        cmd = by_command.setdefault(command, {"cost": 0.0, "calls": 0, "in": 0, "out": 0})
        cmd["cost"] += cost
        cmd["calls"] += 1
        cmd["in"] += in_tok
        cmd["out"] += out_tok
        mod = by_model.setdefault(model, {"cost": 0.0, "calls": 0})
        mod["cost"] += cost
        mod["calls"] += 1
    return {"total": total, "calls": calls, "by_command": by_command, "by_model": by_model}


def check_budget_warning(repo_root: Path) -> str | None:
    """One-line warning when this month's spend meets the configured budget."""
    try:
        budget = get_config_value(repo_root, "costs.monthly_budget_usd")
    except ValueError:
        return None
    try:
        budget_value = float(budget)
    except (TypeError, ValueError):
        return None
    if not budget_value or budget_value <= 0:
        return None
    month_total = _sum_costs_since(repo_root, _start_of_current_month())
    if month_total >= budget_value:
        return f"Monthly budget exceeded: ${month_total:.4f} of ${budget_value:.2f} spent (estimated)."
    return None


def _filtered_entries(repo_root: Path, since) -> object:
    """Yield ledger entries with timestamp >= since. Skips malformed lines."""
    for entry in _iter_entries(repo_root):
        parsed = _parse_ts(entry.get("timestamp", ""))
        if parsed is None:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if parsed >= since:
            yield entry
