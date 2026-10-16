"""`ctx config` — inspect and modify .ctx/config.toml."""
import os
import subprocess
from pathlib import Path

from ctx_engine.config import (
    ENV_VAR_MAP,
    get_config_source,
    get_config_value,
    is_forbidden_key,
    load_config,
    reset_config_value,
    set_config_value,
    valid_keys,
    write_toml,
    DEFAULT_CONFIG,
)
import copy


def _ensure_gitignore_exception(repo_root: Path) -> None:
    # Git cannot re-include a file inside an excluded directory. A blanket
    # ".ctx/" entry makes "!.ctx/config.toml" a no-op (verified via
    # git check-ignore). ".ctx/*" ignores contents but keeps the directory
    # itself unignored, so negations work. Migrate old entries in place.
    gitignore = repo_root / ".gitignore"
    wanted = ["!.ctx/config.toml", "!.ctx/shared-metadata.json"]
    if gitignore.exists():
        lines = gitignore.read_text(encoding="utf-8").splitlines()
    else:
        lines = []
    stripped = [l.strip() for l in lines]
    changed = False
    for i, line in enumerate(lines):
        if line.strip() == ".ctx/":
            lines[i] = ".ctx/*"
            changed = True
    if ".ctx/*" not in [l.strip() for l in lines]:
        # No .ctx exclusion at all — add contents-ignore so new exceptions work.
        lines.append(".ctx/*")
        changed = True
    for entry in wanted:
        if entry not in [l.strip() for l in lines]:
            # Negations must come after the exclusion they override.
            try:
                idx = [l.strip() for l in lines].index(".ctx/*")
                lines.insert(idx + 1 + wanted.index(entry), entry)
            except ValueError:
                lines.append(entry)
            changed = True
    if changed or not gitignore.exists():
        gitignore.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_config_init(repo_root: Path) -> None:
    config_path = repo_root / ".ctx" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    write_toml(copy.deepcopy(DEFAULT_CONFIG), config_path)
    _ensure_gitignore_exception(repo_root)
    print("ctx config init")
    print()
    print(f"  Wrote {config_path}")
    print("  (config.toml is team-shareable — commit it to keep ctx behavior consistent)")


def run_config_get(repo_root: Path, dotted_key: str) -> None:
    if dotted_key not in valid_keys():
        print(f"Error: unknown config key '{dotted_key}'.")
        print()
        print("Valid keys:")
        for key in valid_keys():
            print(f"  {key}")
        raise SystemExit(1)
    print(get_config_value(repo_root, dotted_key))


def run_config_set(repo_root: Path, dotted_key: str, raw_value: str) -> None:
    if is_forbidden_key(dotted_key):
        print(f"ERROR: '{dotted_key}' cannot be stored in config.toml.")
        print("API keys and secrets must be set via environment variable: ANTHROPIC_API_KEY")
        raise SystemExit(1)
    if dotted_key not in valid_keys():
        print(f"Error: unknown config key '{dotted_key}'.")
        print()
        print("Valid keys:")
        for key in valid_keys():
            print(f"  {key}")
        raise SystemExit(1)
    try:
        coerced = set_config_value(repo_root, dotted_key, raw_value)
    except ValueError as err:
        print(f"Error: {err}")
        raise SystemExit(1)
    _ensure_gitignore_exception(repo_root)
    print(f"  {dotted_key} = {coerced}")


def run_config_list(repo_root: Path) -> None:
    config = load_config(repo_root)
    print("  EFFECTIVE CONFIGURATION")
    print("  (source: D=default, F=.ctx/config.toml, E=environment variable)")
    print()
    for dotted_key in valid_keys():
        section, _, key = dotted_key.partition(".")
        value = config[section][key]
        source = get_config_source(repo_root, dotted_key)
        extra = ""
        if source == "E":
            extra = f" {ENV_VAR_MAP.get(dotted_key, '')}"
        print(f"  {dotted_key:<32} {value!s:<38} [{source}]{extra}")


def run_config_reset(repo_root: Path, dotted_key: str) -> None:
    if dotted_key not in valid_keys():
        print(f"Error: unknown config key '{dotted_key}'.")
        print()
        print("Valid keys:")
        for key in valid_keys():
            print(f"  {key}")
        raise SystemExit(1)
    removed = reset_config_value(repo_root, dotted_key)
    if removed:
        print(f"  {dotted_key} reset to default")
    else:
        print(f"  {dotted_key} already at default")


def run_config_edit(repo_root: Path) -> None:
    config_path = repo_root / ".ctx" / "config.toml"
    if not config_path.exists():
        run_config_init(repo_root)
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    try:
        subprocess.run([editor, str(config_path)], check=False)
    except FileNotFoundError:
        print(f"Error: editor '{editor}' not found. Set $EDITOR to your preferred editor.")
        raise SystemExit(1)
