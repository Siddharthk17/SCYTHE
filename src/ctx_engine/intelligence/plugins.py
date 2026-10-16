"""Minimal plugin contract for custom danger-detection heuristics.

A plugin is a single ``.ctx/plugins/*.py`` file defining a top-level
callable ``detect(conn, repo_root) -> list[dict]`` returning danger
records ``[{id, scope, description, reason}]``. Plugins run with full
privileges — same trust boundary as ``.git/hooks/``.
"""
import importlib.util
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("ctx")


@dataclass
class PluginInfo:
    path: Path
    name: str


def discover_plugins(repo_root: Path) -> list[PluginInfo]:
    plugin_dir = Path(repo_root) / ".ctx" / "plugins"
    if not plugin_dir.is_dir():
        return []
    found = []
    for candidate in sorted(plugin_dir.glob("*.py")):
        if candidate.name.startswith("_"):
            continue
        found.append(PluginInfo(path=candidate, name=candidate.stem))
    return found


def load_plugin(info: PluginInfo) -> Callable | None:
    spec = importlib.util.spec_from_file_location(info.name, info.path)
    if spec is None or spec.loader is None:
        logger.error("Plugin '%s': could not create module spec.", info.name)
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as err:
        logger.error("Plugin '%s' failed to load: %s", info.name, type(err).__name__ + f": {err}")
        return None
    detect_fn = getattr(module, "detect", None)
    if detect_fn is None or not callable(detect_fn):
        logger.error("Plugin '%s' has no callable detect().", info.name)
        return None
    return detect_fn


def run_plugin_safely(info, detect_fn, conn, repo_root) -> list[dict]:
    try:
        result = detect_fn(conn, repo_root)
    except Exception as err:
        logger.error("Plugin '%s' raised: %s", info.name, err)
        return []
    if not isinstance(result, list):
        logger.error(
            "Plugin '%s': detect() returned %s, expected list.", info.name, type(result).__name__
        )
        return []
    return result


def validate_plugin_file(path: Path, repo_root: Path, conn=None) -> dict:
    """Load and test-run one plugin file. Returns a four-check report."""
    from ctx_engine.db import connect as _connect

    report: dict = {
        "loads": False,
        "has_detect": False,
        "returns_list": False,
        "records_valid": False,
        "record_count": 0,
        "error": None,
    }
    info = PluginInfo(path=Path(path), name=Path(path).stem)
    owned_conn = None
    try:
        detect_fn = load_plugin(info)
        if detect_fn is None:
            report["error"] = "load failed (see log)"
            return report
        report["loads"] = True
        report["has_detect"] = True
        if conn is None:
            db_path = Path(repo_root) / ".ctx" / "index.db"
            if not db_path.exists():
                report["error"] = "no index.db — cannot test-run detect()"
                return report
            owned_conn = _connect(db_path)
            conn = owned_conn
        try:
            result = detect_fn(conn, Path(repo_root))
        except Exception as err:
            report["error"] = f"{type(err).__name__}: {err}"
            return report
        if not isinstance(result, list):
            report["error"] = f"detect() returned {type(result).__name__}, expected list"
            return report
        report["returns_list"] = True
        report["record_count"] = len(result)
        required = {"id", "scope", "description", "reason"}
        report["records_valid"] = all(
            isinstance(record, dict) and required <= set(record) for record in result
        )
        if not report["records_valid"]:
            report["error"] = "some records missing required keys: id, scope, description, reason"
        return report
    finally:
        if owned_conn is not None:
            try:
                owned_conn.close()
            except Exception:
                pass
