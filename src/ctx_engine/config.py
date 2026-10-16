"""`ctx config` — single-file configuration with env-var overrides.

One file (``.ctx/config.toml``) consolidates every tunable previously
scattered as a module-level constant or ad-hoc ``os.environ`` lookup.
Precedence, highest to lowest::

    1. Environment variable (where one exists — backward compatibility)
    2. .ctx/config.toml
    3. Built-in default

``config.toml`` is team-shareable (committable); secrets are never stored
there — see ``FORBIDDEN_KEY_PATTERNS``.
"""
import copy
import logging
import os
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 fallback
    import tomli as tomllib

logger = logging.getLogger("ctx")

DEFAULT_CONFIG = {
    "llm": {
        "model": "claude-haiku-4-5-20251001",
        "explain_model": "claude-sonnet-4-6",
        "review_model": "claude-sonnet-4-6",
    },
    "ollama": {
        "host": "http://localhost:11434",
        "model": "",
    },
    "watch": {
        "debounce_ms": 500,
    },
    "context": {
        "token_budget": 8000,
    },
    "heuristics": {
        "call_fanin_threshold": 10,
        "import_fanin_threshold": 15,
    },
    "confidence": {
        "decay_factor": 0.85,
        "low_confidence_threshold": 0.5,
        "likely_stale_threshold": 0.2,
    },
    "summarize": {
        "batch_size": 20,
        "batch_token_limit": 50000,
    },
    "history": {
        "changes_per_file_cap": 20,
        "session_log_cap": 10,
    },
    "costs": {
        "monthly_budget_usd": 0.0,
    },
}

BUILTIN_PRICING = {
    "claude-haiku-4-5-20251001": {"input_per_million": 1.00, "output_per_million": 5.00},
    "claude-sonnet-4-6": {"input_per_million": 3.00, "output_per_million": 15.00},
}

FORBIDDEN_KEY_PATTERNS = ("api_key", "secret", "token", "password", "credential")

ENV_VAR_MAP = {
    "llm.model": "CTX_LLM_MODEL",
    "llm.explain_model": "CTX_EXPLAIN_MODEL",
    "llm.review_model": "CTX_REVIEW_MODEL",
    "ollama.host": "CTX_OLLAMA_HOST",
    "ollama.model": "CTX_OLLAMA_MODEL",
    "watch.debounce_ms": "CTX_WATCH_DEBOUNCE_MS",
}


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base in place. Returns base."""
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def load_config(repo_root: Path) -> dict:
    """Load effective file config: defaults deep-merged with .ctx/config.toml."""
    config = copy.deepcopy(DEFAULT_CONFIG)
    config_path = Path(repo_root) / ".ctx" / "config.toml"
    if config_path.exists():
        try:
            with open(config_path, "rb") as handle:
                user_config = tomllib.load(handle)
        except Exception as err:
            logger.warning("Failed to parse %s: %s", config_path, err)
            raise ValueError(f"Invalid TOML in {config_path}: {err}") from err
        if isinstance(user_config, dict):
            deep_merge(config, user_config)
    return config


def _get_default(dotted_key: str):
    section, _, key = dotted_key.partition(".")
    try:
        return DEFAULT_CONFIG[section][key]
    except KeyError:
        raise ValueError(f"Unknown config key: '{dotted_key}'.") from None


def _get_nested(config: dict, dotted_key: str):
    section, _, key = dotted_key.partition(".")
    try:
        return config[section][key]
    except KeyError:
        raise ValueError(f"Unknown config key: '{dotted_key}'.") from None


def _coerce_to_default_type(raw: str, default):
    if isinstance(default, bool):
        lowered = raw.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"Cannot coerce '{raw}' to bool.")
    if isinstance(default, int) and not isinstance(default, bool):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return raw


def get_config_value(repo_root: Path, dotted_key: str):
    """Resolve one key: env var > config.toml > built-in default. Type-coerced."""
    _get_default(dotted_key)  # validates the key eagerly
    env_var = ENV_VAR_MAP.get(dotted_key)
    if env_var and os.environ.get(env_var) not in (None, ""):
        return _coerce_to_default_type(os.environ[env_var], _get_default(dotted_key))
    return _get_nested(load_config(Path(repo_root)), dotted_key)


def get_config_source(repo_root: Path, dotted_key: str) -> str:
    """Return 'E', 'F', or 'D' for the winning source of a key."""
    env_var = ENV_VAR_MAP.get(dotted_key)
    if env_var and os.environ.get(env_var) not in (None, ""):
        return "E"
    config_path = Path(repo_root) / ".ctx" / "config.toml"
    if config_path.exists():
        try:
            with open(config_path, "rb") as handle:
                user_config = tomllib.load(handle)
        except Exception:
            return "D"
        section, _, key = dotted_key.partition(".")
        if isinstance(user_config.get(section), dict) and key in user_config[section]:
            return "F"
    return "D"


def valid_keys() -> list[str]:
    """All settable dotted keys, sorted. Includes costs.pricing.* dynamically? No — static."""
    keys = []
    for section, values in sorted(DEFAULT_CONFIG.items()):
        for key in sorted(values):
            keys.append(f"{section}.{key}")
    return keys


def is_forbidden_key(dotted_key: str) -> bool:
    lowered = dotted_key.lower()
    return any(pattern in lowered for pattern in FORBIDDEN_KEY_PATTERNS)


def _format_toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def write_toml(config: dict, path: Path) -> None:
    """Write a two-level config dict as TOML. No external dependency.

    Nested tables beyond two levels (e.g. ``[costs.pricing.<model>]``)
    are preserved as ``[section.subsection]`` blocks so custom pricing
    is never silently dropped by ``ctx config set/reset``.
    """
    lines: list[str] = []
    for section, values in sorted(config.items()):
        if not isinstance(values, dict):
            continue
        scalar_items = {k: v for k, v in values.items() if not isinstance(v, dict)}
        nested_items = {k: v for k, v in values.items() if isinstance(v, dict)}
        if scalar_items:
            lines.append(f"[{section}]")
            for key, value in sorted(scalar_items.items()):
                lines.append(f"{key} = {_format_toml_value(value)}")
            lines.append("")
        for sub, subvals in sorted(nested_items.items()):
            if not isinstance(subvals, dict) or not subvals:
                continue
            # Only one extra level is supported (e.g. costs.pricing.<model>
            # stores per-model dicts of scalars).
            if all(isinstance(v, dict) for v in subvals.values()):
                for model, pricing in sorted(subvals.items()):
                    if not isinstance(pricing, dict):
                        continue
                    lines.append(f"[{section}.{sub}.{model}]")
                    for key, value in sorted(pricing.items()):
                        if isinstance(value, dict):
                            continue
                        lines.append(f"{key} = {_format_toml_value(value)}")
                    lines.append("")
            else:
                lines.append(f"[{section}.{sub}]")
                for key, value in sorted(subvals.items()):
                    if isinstance(value, dict):
                        continue
                    lines.append(f"{key} = {_format_toml_value(value)}")
                lines.append("")
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def _load_raw_file_config(repo_root: Path) -> dict:
    config_path = Path(repo_root) / ".ctx" / "config.toml"
    if not config_path.exists():
        return {}
    with open(config_path, "rb") as handle:
        data = tomllib.load(handle)
    return data if isinstance(data, dict) else {}


def set_config_value(repo_root: Path, dotted_key: str, raw_value: str):
    """Validate, coerce, and persist one key into .ctx/config.toml."""
    if is_forbidden_key(dotted_key):
        raise ValueError(
            f"ERROR: '{dotted_key}' cannot be stored in config.toml.\n"
            "API keys and secrets must be set via environment variable: ANTHROPIC_API_KEY"
        )
    default = _get_default(dotted_key)
    try:
        coerced = _coerce_to_default_type(raw_value, default)
    except (ValueError, TypeError) as err:
        raise ValueError(f"Invalid value for '{dotted_key}': {err}") from err
    section, _, key = dotted_key.partition(".")
    raw = _load_raw_file_config(repo_root)
    if section not in raw or not isinstance(raw[section], dict):
        raw[section] = {}
    raw[section][key] = coerced
    config_path = Path(repo_root) / ".ctx" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    write_toml(raw, config_path)
    return coerced


def reset_config_value(repo_root: Path, dotted_key: str) -> bool:
    """Remove one key from config.toml. Returns True if a key was removed."""
    _get_default(dotted_key)
    section, _, key = dotted_key.partition(".")
    raw = _load_raw_file_config(repo_root)
    if section in raw and isinstance(raw[section], dict) and key in raw[section]:
        del raw[section][key]
        if not raw[section]:
            del raw[section]
        config_path = Path(repo_root) / ".ctx" / "config.toml"
        if raw:
            write_toml(raw, config_path)
        else:
            try:
                config_path.unlink()
            except FileNotFoundError:
                pass
        return True
    return False
