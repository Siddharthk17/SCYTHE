"""`ctx plugin` — list and validate `.ctx/plugins/` heuristics."""
import logging
from pathlib import Path

from ctx_engine.intelligence.plugins import (
    discover_plugins,
    load_plugin,
    run_plugin_safely,
    validate_plugin_file,
)

logger = logging.getLogger("ctx")

SECURITY_WARNING = (
    "  ⚠ Plugins execute arbitrary Python with full filesystem and database access.\n"
    "    Only enable plugins you wrote or have personally reviewed."
)


def run_plugin_list(repo_root: Path) -> None:
    print(SECURITY_WARNING)
    print()
    print("  DISCOVERED PLUGINS (.ctx/plugins/)")
    plugins = discover_plugins(repo_root)
    if not plugins:
        print("  (none — add .py files to .ctx/plugins/ to extend danger detection)")
        return
    for info in plugins:
        detect_fn = load_plugin(info)
        if detect_fn is not None:
            print(f"  ✓ {info.path.name}     valid")
        else:
            print(f"  ✗ {info.path.name}     INVALID — failed to load (see log)")


def run_plugin_validate(repo_root: Path, target: str) -> None:
    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = repo_root / target
    if not candidate.exists():
        print(f"Error: plugin file not found: {target}")
        raise SystemExit(1)
    report = validate_plugin_file(candidate, repo_root)
    checks = [
        ("File loads without error", report["loads"]),
        ("detect() function found", report["has_detect"]),
        (
            f"detect() returned a list ({report['record_count']} danger records)",
            report["returns_list"],
        ),
        (
            "All records have required keys: id, scope, description, reason",
            report["records_valid"],
        ),
    ]
    for label, passed in checks:
        mark = "✓" if passed else "✗"
        print(f"  {mark} {label}")
    if report["error"] and not (report["loads"] and report["returns_list"] and report["records_valid"]):
        print()
        print(f"  Error: {report['error']}")
    print()
    if all(passed for _, passed in checks):
        print("  Plugin is valid.")
    else:
        print("  Plugin is INVALID.")
        raise SystemExit(1)
