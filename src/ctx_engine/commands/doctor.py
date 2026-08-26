"""`ctx doctor` — comprehensive health check with auto-fixes for common issues.

Runs a fixed checklist of 36 health checks grouped into 7 categories and prints
a color-coded pass/fail report. With --fix, applies safe automated fixes for
issues that have a known remediation. With --json, emits a machine-readable
report for CI pipelines and the MCP server.
"""
import json
import os
import sqlite3
import subprocess
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ctx_engine.db import connect, init_schema
from ctx_engine.commands.export_cmd import run_export
from ctx_engine.commands.install_hooks import (
    POST_COMMIT_HOOK,
    PRE_COMMIT_HOOK,
    run_install_hooks,
)
from ctx_engine.db.schema import PERFORMANCE_INDICES
from ctx_engine.mcp_server.tools.renderers import render_generation_timestamp

# Performance indices we expect to exist. The names match the create-statement
# targets in db/schema.py — used by the G1–G4 doctor checks.
EXPECTED_PERF_INDICES = [
    "idx_functions_file",
    "idx_call_graph_caller",
    "idx_call_graph_callee",
    "idx_taint_queue_priority",
    "idx_functions_stale_tainted",
    "idx_changes_file_time",
    "idx_files_stale",
]

ALL_EXPECTED_TABLES = {
    "files", "functions", "call_graph", "dangers", "changes",
    "taint_queue", "session_log", "decisions", "directories",
}

OLLAMA_PREFERRED_MODELS = (
    "qwen2.5-coder:7b-instruct",
    "qwen2.5-coder:7b",
    "codellama:7b",
    "deepseek-coder:6.7b",
    "llama3.1:8b",
)


def _normalize_ts(value: str) -> datetime:
    """Parse an ISO timestamp and drop sub-second precision.

    Export generation timestamps are second-precision (rendered as
    '%Y-%m-%dT%H:%M:%SZ'), while DB updated_at/created_at values carry
    microseconds. Comparing raw values would flag a perfectly fresh export
    as stale whenever it was generated within the same second the index was
    last written. Normalizing both sides to whole seconds eliminates the
    false-stale artifact while keeping the freshness guarantee intact.
    """
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.replace(microsecond=0)


@dataclass
class CheckResult:
    id: str
    category: str
    name: str
    passed: bool
    detail: str = ""
    auto_fix: bool = False
    fix_label: str = ""


@dataclass
class DoctorReport:
    timestamp: str
    repo: str
    total_checks: int
    passed: int
    failed: int
    auto_fixable: int
    checks: list[dict] = field(default_factory=list)


# ── Check implementations ──────────────────────────────────────────────────────


def _check_git_repo(repo_root: Path) -> CheckResult:
    try:
        subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=repo_root, capture_output=True, text=True, check=True,
        )
        return CheckResult("A1", "Prerequisites", "git repository", True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return CheckResult("A1", "Prerequisites", "git repository", False, "not a git repository")


def _check_ctx_dir(repo_root: Path) -> CheckResult:
    p = repo_root / ".ctx"
    if p.is_dir():
        return CheckResult("A2", "Prerequisites", ".ctx/ directory", True)
    return CheckResult("A2", "Prerequisites", ".ctx/ directory", False,
                       ".ctx/ does not exist", auto_fix=True, fix_label="create .ctx/")


def _check_db_exists(repo_root: Path) -> CheckResult:
    p = repo_root / ".ctx" / "index.db"
    if p.exists():
        return CheckResult("A3", "Prerequisites", ".ctx/index.db", True)
    return CheckResult("A3", "Prerequisites", ".ctx/index.db", False,
                       "database file missing — run 'ctx init'")


def _check_db_opens(repo_root: Path) -> CheckResult:
    p = repo_root / ".ctx" / "index.db"
    if not p.exists():
        return CheckResult("A4", "Prerequisites", "database readable", False, "no DB file")
    try:
        conn = connect(p)
        conn.execute("SELECT 1").fetchone()
        conn.close()
        return CheckResult("A4", "Prerequisites", "database readable", True)
    except sqlite3.DatabaseError as err:
        return CheckResult("A4", "Prerequisites", "database readable", False, f"corruption: {err}")


def _check_db_integrity(repo_root: Path) -> CheckResult:
    p = repo_root / ".ctx" / "index.db"
    if not p.exists():
        return CheckResult("A5", "Prerequisites", "database integrity", False, "no DB file")
    try:
        conn = connect(p)
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        conn.close()
        if result == "ok":
            return CheckResult("A5", "Prerequisites", "database integrity", True)
        return CheckResult("A5", "Prerequisites", "database integrity", False,
                           f"PRAGMA integrity_check returned: {result}")
    except sqlite3.DatabaseError as err:
        return CheckResult("A5", "Prerequisites", "database integrity", False, f"{err}")


def _check_journal_mode(repo_root: Path) -> CheckResult:
    p = repo_root / ".ctx" / "index.db"
    if not p.exists():
        return CheckResult("A6", "Prerequisites", "WAL mode", False, "no DB file")
    try:
        conn = connect(p)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        if mode.lower() == "wal":
            return CheckResult("A6", "Prerequisites", "WAL mode", True)
        return CheckResult("A6", "Prerequisites", "WAL mode", False,
                           f"mode is {mode}, expected wal", auto_fix=True,
                           fix_label="PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError as err:
        return CheckResult("A6", "Prerequisites", "WAL mode", False, f"{err}")


def _check_foreign_keys(repo_root: Path) -> CheckResult:
    p = repo_root / ".ctx" / "index.db"
    if not p.exists():
        return CheckResult("A7", "Prerequisites", "foreign keys", False, "no DB file")
    try:
        conn = connect(p)
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        conn.close()
        if fk == 1:
            return CheckResult("A7", "Prerequisites", "foreign keys", True)
        return CheckResult("A7", "Prerequisites", "foreign keys", False,
                           f"foreign_keys is {fk}, expected 1", auto_fix=True,
                           fix_label="PRAGMA foreign_keys=ON")
    except sqlite3.DatabaseError as err:
        return CheckResult("A7", "Prerequisites", "foreign keys", False, f"{err}")


def _check_tables_present(repo_root: Path) -> CheckResult:
    p = repo_root / ".ctx" / "index.db"
    if not p.exists():
        return CheckResult("B1", "Schema", "all 9 tables", False, "no DB file")
    try:
        conn = connect(p)
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        present = {r[0] for r in rows}
        conn.close()
        # FTS5 virtual tables live in sqlite_master as well; the 'files'/'functions'
        # names are the base tables.
        missing = ALL_EXPECTED_TABLES - present
        if not missing:
            return CheckResult("B1", "Schema", "all 9 tables", True)
        return CheckResult("B1", "Schema", "all 9 tables", False,
                           f"missing: {sorted(missing)}", auto_fix=True,
                           fix_label="recreate schema (destructive)")
    except sqlite3.DatabaseError as err:
        return CheckResult("B1", "Schema", "all 9 tables", False, f"{err}")


def _check_mtime_columns(repo_root: Path) -> CheckResult:
    p = repo_root / ".ctx" / "index.db"
    if not p.exists():
        return CheckResult("B2", "Schema", "mtime/file_size columns", False, "no DB file")
    try:
        conn = connect(p)
        rows = conn.execute("PRAGMA table_info(files)").fetchall()
        cols = {r[1] for r in rows}
        conn.close()
        missing = {"mtime", "file_size"} - cols
        if not missing:
            return CheckResult("B2", "Schema", "mtime/file_size columns", True)
        return CheckResult("B2", "Schema", "mtime/file_size columns", False,
                           f"missing: {sorted(missing)}", auto_fix=True,
                           fix_label="run apply_migrations()")
    except sqlite3.DatabaseError as err:
        return CheckResult("B2", "Schema", "mtime/file_size columns", False, f"{err}")


def _check_callgraph_autoincrement(repo_root: Path) -> CheckResult:
    p = repo_root / ".ctx" / "index.db"
    if not p.exists():
        return CheckResult("B3", "Schema", "call_graph autoincrement PK", False, "no DB file")
    try:
        conn = connect(p)
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'call_graph'"
        ).fetchone()
        conn.close()
        if not row:
            return CheckResult("B3", "Schema", "call_graph autoincrement PK", False, "table missing")
        sql = row[0] or ""
        if "AUTOINCREMENT" in sql.upper():
            return CheckResult("B3", "Schema", "call_graph autoincrement PK", True)
        return CheckResult("B3", "Schema", "call_graph autoincrement PK", False,
                           "call_graph.id is not AUTOINCREMENT (data migration required)")
    except sqlite3.DatabaseError as err:
        return CheckResult("B3", "Schema", "call_graph autoincrement PK", False, f"{err}")


def _check_perf_indices(repo_root: Path) -> CheckResult:
    p = repo_root / ".ctx" / "index.db"
    if not p.exists():
        return CheckResult("B4", "Schema", "SQL performance indices", False, "no DB file")
    try:
        conn = connect(p)
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
        ).fetchall()
        present = {r[0] for r in rows}
        conn.close()
        missing = set(EXPECTED_PERF_INDICES) - present
        if not missing:
            return CheckResult("B4", "Schema", "SQL performance indices", True)
        return CheckResult("B4", "Schema", "SQL performance indices", False,
                           f"{len(missing)} of {len(EXPECTED_PERF_INDICES)} missing",
                           auto_fix=True, fix_label="create missing indices")
    except sqlite3.DatabaseError as err:
        return CheckResult("B4", "Schema", "SQL performance indices", False, f"{err}")


def _check_fts5_tables(repo_root: Path) -> CheckResult:
    p = repo_root / ".ctx" / "index.db"
    if not p.exists():
        return CheckResult("B5", "Schema", "FTS5 tables", False, "no DB file")
    try:
        conn = connect(p)
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('files_fts','functions_fts')"
        ).fetchall()
        present = {r[0] for r in rows}
        conn.close()
        missing = {"files_fts", "functions_fts"} - present
        if not missing:
            return CheckResult("B5", "Schema", "FTS5 tables", True)
        return CheckResult("B5", "Schema", "FTS5 tables", False,
                           f"missing: {sorted(missing)}", auto_fix=True,
                           fix_label="recreate FTS5 tables and triggers")
    except sqlite3.DatabaseError as err:
        return CheckResult("B5", "Schema", "FTS5 tables", False, f"{err}")


# ── Category C — Index health ─────────────────────────────────────────────────


def _check_files_indexed(repo_root: Path) -> CheckResult:
    p = repo_root / ".ctx" / "index.db"
    if not p.exists():
        return CheckResult("C1", "Index Health", "files indexed", False, "no DB file")
    try:
        conn = connect(p)
        n = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        conn.close()
        if n > 0:
            return CheckResult("C1", "Index Health", "files indexed", True, detail=f"{n} files")
        return CheckResult("C1", "Index Health", "files indexed", False,
                           "0 files in index — run 'ctx init'")
    except sqlite3.DatabaseError as err:
        return CheckResult("C1", "Index Health", "files indexed", False, f"{err}")


def _check_null_purpose(repo_root: Path) -> CheckResult:
    p = repo_root / ".ctx" / "index.db"
    if not p.exists():
        return CheckResult("C2", "Index Health", "all files have purpose", False, "no DB file")
    try:
        conn = connect(p)
        n = conn.execute("SELECT COUNT(*) FROM files WHERE purpose IS NULL").fetchone()[0]
        conn.close()
        if n == 0:
            return CheckResult("C2", "Index Health", "all files have purpose", True)
        return CheckResult("C2", "Index Health", "all files have purpose", False,
                           f"{n} file(s) with NULL purpose — run 'ctx summarize' or 'ctx sync'")
    except sqlite3.DatabaseError as err:
        return CheckResult("C2", "Index Health", "all files have purpose", False, f"{err}")


def _check_is_stale(repo_root: Path) -> CheckResult:
    p = repo_root / ".ctx" / "index.db"
    if not p.exists():
        return CheckResult("C3", "Index Health", "no is_stale files", False, "no DB file")
    try:
        conn = connect(p)
        n = conn.execute("SELECT COUNT(*) FROM files WHERE is_stale = 1").fetchone()[0]
        conn.close()
        if n == 0:
            return CheckResult("C3", "Index Health", "no is_stale files", True)
        return CheckResult("C3", "Index Health", "no is_stale files", False,
                           f"{n} stale file(s) — run 'ctx sync'")
    except sqlite3.DatabaseError as err:
        return CheckResult("C3", "Index Health", "no is_stale files", False, f"{err}")


def _check_is_tainted(repo_root: Path) -> CheckResult:
    p = repo_root / ".ctx" / "index.db"
    if not p.exists():
        return CheckResult("C4", "Index Health", "no tainted functions", False, "no DB file")
    try:
        conn = connect(p)
        n = conn.execute("SELECT COUNT(*) FROM functions WHERE is_tainted = 1").fetchone()[0]
        conn.close()
        if n == 0:
            return CheckResult("C4", "Index Health", "no tainted functions", True)
        return CheckResult("C4", "Index Health", "no tainted functions", False,
                           f"{n} tainted function(s) — run 'ctx sync'")
    except sqlite3.DatabaseError as err:
        return CheckResult("C4", "Index Health", "no tainted functions", False, f"{err}")


def _check_taint_queue_empty(repo_root: Path) -> CheckResult:
    p = repo_root / ".ctx" / "index.db"
    if not p.exists():
        return CheckResult("C5", "Index Health", "taint_queue empty", False, "no DB file")
    try:
        conn = connect(p)
        n = conn.execute("SELECT COUNT(*) FROM taint_queue").fetchone()[0]
        conn.close()
        if n == 0:
            return CheckResult("C5", "Index Health", "taint_queue empty", True)
        return CheckResult("C5", "Index Health", "taint_queue empty", False,
                           f"{n} queued — run 'ctx sync' to drain")
    except sqlite3.DatabaseError as err:
        return CheckResult("C5", "Index Health", "taint_queue empty", False, f"{err}")


def _check_mtime_coverage(repo_root: Path) -> CheckResult:
    p = repo_root / ".ctx" / "index.db"
    if not p.exists():
        return CheckResult("C6", "Index Health", "mtime cache coverage", False, "no DB file")
    try:
        conn = connect(p)
        total = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        if total == 0:
            conn.close()
            return CheckResult("C6", "Index Health", "mtime cache coverage", False,
                               "no files indexed")
        cached = conn.execute(
            "SELECT COUNT(*) FROM files WHERE mtime IS NOT NULL"
        ).fetchone()[0]
        conn.close()
        pct = (cached / total) * 100
        if pct >= 90.0:
            return CheckResult("C6", "Index Health", "mtime cache coverage", True,
                               detail=f"{pct:.0f}%")
        return CheckResult("C6", "Index Health", "mtime cache coverage", False,
                           f"{pct:.0f}% — run 'ctx init' to populate")
    except sqlite3.DatabaseError as err:
        return CheckResult("C6", "Index Health", "mtime cache coverage", False, f"{err}")


def _check_callgraph_consistency(repo_root: Path) -> CheckResult:
    p = repo_root / ".ctx" / "index.db"
    if not p.exists():
        return CheckResult("C7", "Index Health", "call graph self-consistent", False, "no DB file")
    try:
        conn = connect(p)
        n = conn.execute(
            "SELECT COUNT(*) FROM call_graph WHERE callee_id IS NOT NULL "
            "AND callee_id NOT IN (SELECT id FROM functions)"
        ).fetchone()[0]
        conn.close()
        if n == 0:
            return CheckResult("C7", "Index Health", "call graph self-consistent", True)
        return CheckResult("C7", "Index Health", "call graph self-consistent", False,
                           f"{n} dangling call_graph row(s)", auto_fix=True,
                           fix_label="DELETE dangling call_graph rows")
    except sqlite3.DatabaseError as err:
        return CheckResult("C7", "Index Health", "call graph self-consistent", False, f"{err}")


# ── Category D — API and models ───────────────────────────────────────────────


def _check_anthropic_key() -> CheckResult:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return CheckResult("D1", "API and Models", "ANTHROPIC_API_KEY set", True)
    return CheckResult("D1", "API and Models", "ANTHROPIC_API_KEY set", False,
                       "ANTHROPIC_API_KEY is not set in the environment")


def _http_head_ok(url: str, timeout: float = 3.0) -> bool:
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError):
        return False


def _http_get_json(url: str, timeout: float = 3.0) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError, ValueError):
        return None


def _check_anthropic_api() -> CheckResult:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return CheckResult("D2", "API and Models", "Anthropic API reachable", False,
                           "cannot test without API key (D1 failed)")
    if _http_head_ok("https://api.anthropic.com"):
        return CheckResult("D2", "API and Models", "Anthropic API reachable", True)
    return CheckResult("D2", "API and Models", "Anthropic API reachable", False,
                       "HEAD to https://api.anthropic.com failed")


def _check_ollama() -> CheckResult:
    host = os.environ.get("CTX_OLLAMA_HOST", "http://localhost:11434")
    if _http_head_ok(f"{host}/api/tags"):
        return CheckResult("D3", "API and Models", "Ollama reachable", True,
                           detail=f"host: {host}")
    return CheckResult("D3", "API and Models", "Ollama reachable", False,
                       f"cannot reach {host} — install from https://ollama.ai if you want background auto-summarization")


def _check_ollama_model() -> CheckResult:
    host = os.environ.get("CTX_OLLAMA_HOST", "http://localhost:11434")
    data = _http_get_json(f"{host}/api/tags")
    if data is None or "models" not in data:
        return CheckResult("D4", "API and Models", "Preferred Ollama model available", False,
                           "Ollama not reachable or returned unexpected response")
    model_names = []
    for m in data.get("models", []):
        if isinstance(m, dict):
            n = m.get("name") or m.get("model")
            if n:
                model_names.append(n)
    for preferred in OLLAMA_PREFERRED_MODELS:
        if any(preferred in n for n in model_names):
            return CheckResult("D4", "API and Models", "Preferred Ollama model available", True,
                               detail=f"found {preferred}")
    return CheckResult("D4", "API and Models", "Preferred Ollama model available", False,
                       f"none of {list(OLLAMA_PREFERRED_MODELS)} present — run 'ollama pull qwen2.5-coder:7b-instruct'")


# ── Category E — Git hooks ────────────────────────────────────────────────────


def _check_hook(repo_root: Path, name: str, expected: str, check_id: str) -> CheckResult:
    hook_path = repo_root / ".git" / "hooks" / name
    if not hook_path.exists():
        return CheckResult(check_id, "Git Hooks", f"{name} hook", False,
                           f"{hook_path} not installed", auto_fix=True,
                           fix_label="run ctx install-hooks")
    try:
        actual = hook_path.read_text()
    except (PermissionError, OSError) as err:
        return CheckResult(check_id, "Git Hooks", f"{name} hook", False, f"cannot read: {err}")
    if expected not in actual:
        return CheckResult(check_id, "Git Hooks", f"{name} hook", False,
                           f"{name} is present but not a ctx hook")
    if not os.access(str(hook_path), os.X_OK):
        return CheckResult(check_id, "Git Hooks", f"{name} hook", False,
                           f"{name} not executable", auto_fix=True,
                           fix_label="chmod +x")
    return CheckResult(check_id, "Git Hooks", f"{name} hook", True)


def _check_pre_commit(repo_root: Path) -> CheckResult:
    return _check_hook(repo_root, "pre-commit", PRE_COMMIT_HOOK.strip(), "E1")


def _check_post_commit(repo_root: Path) -> CheckResult:
    return _check_hook(repo_root, "post-commit", POST_COMMIT_HOOK.strip(), "E2")


def _check_hooks_executable(repo_root: Path) -> CheckResult:
    hooks = [
        repo_root / ".git" / "hooks" / "pre-commit",
        repo_root / ".git" / "hooks" / "post-commit",
    ]
    missing = [h.name for h in hooks if not h.exists()]
    if missing:
        return CheckResult("E3", "Git Hooks", "hooks executable", False,
                           f"missing: {missing}", auto_fix=True,
                           fix_label="run ctx install-hooks")
    not_exec = [h.name for h in hooks if not os.access(str(h), os.X_OK)]
    if not_exec:
        return CheckResult("E3", "Git Hooks", "hooks executable", False,
                           f"not executable: {not_exec}", auto_fix=True,
                           fix_label="chmod +x")
    return CheckResult("E3", "Git Hooks", "hooks executable", True)


# ── Category F — Output files ─────────────────────────────────────────────────


def _check_export_file_exists(repo_root: Path, rel_path: str, check_id: str, name: str) -> CheckResult:
    p = repo_root / rel_path
    if p.exists():
        return CheckResult(check_id, "Output Files", name, True)
    return CheckResult(check_id, "Output Files", name, False,
                       f"{rel_path} not generated", auto_fix=True,
                       fix_label="run ctx export")


def _check_claude_md_exists(repo_root: Path) -> CheckResult:
    return _check_export_file_exists(repo_root, "CLAUDE.md", "F1", "CLAUDE.md exists")


def _check_copilot_md_exists(repo_root: Path) -> CheckResult:
    return _check_export_file_exists(repo_root, ".github/copilot-instructions.md", "F3", "copilot-instructions.md exists")


def _check_opencode_md_exists(repo_root: Path) -> CheckResult:
    return _check_export_file_exists(repo_root, ".ctx/opencode.md", "F4", ".ctx/opencode.md exists")


def _check_claude_md_fresh(repo_root: Path) -> CheckResult:
    p = repo_root / "CLAUDE.md"
    db = repo_root / ".ctx" / "index.db"
    if not p.exists():
        return CheckResult("F2", "Output Files", "CLAUDE.md is current", False,
                           "CLAUDE.md not generated", auto_fix=True,
                           fix_label="run ctx export")
    if not db.exists():
        return CheckResult("F2", "Output Files", "CLAUDE.md is current", True,
                           detail="(no DB to compare)")
    gen_ts = render_generation_timestamp(p)
    if gen_ts is None:
        return CheckResult("F2", "Output Files", "CLAUDE.md is current", False,
                           "no generation timestamp in CLAUDE.md", auto_fix=True,
                           fix_label="run ctx export")
    try:
        conn = connect(db)
        latest = conn.execute(
            "SELECT MAX(updated_at) FROM ("
            "SELECT MAX(updated_at) AS updated_at FROM files "
            "UNION ALL SELECT MAX(updated_at) AS updated_at FROM functions "
            "UNION ALL SELECT MAX(created_at) AS updated_at FROM dangers "
            "UNION ALL SELECT MAX(created_at) AS updated_at FROM decisions)"
        ).fetchone()[0]
        conn.close()
    except sqlite3.DatabaseError:
        return CheckResult("F2", "Output Files", "CLAUDE.md is current", True,
                           detail="(cannot read DB)")
    if not latest:
        return CheckResult("F2", "Output Files", "CLAUDE.md is current", True)
    try:
        gen_dt = _normalize_ts(gen_ts)
        db_dt = _normalize_ts(latest)
    except (ValueError, TypeError) as err:
        return CheckResult("F2", "Output Files", "CLAUDE.md is current", False, f"timestamp parse: {err}")
    if gen_dt >= db_dt:
        return CheckResult("F2", "Output Files", "CLAUDE.md is current", True)
    return CheckResult("F2", "Output Files", "CLAUDE.md is current", False,
                       f"generated {gen_ts}, DB updated {latest}", auto_fix=True,
                       fix_label="run ctx export")


def _check_gitattributes(repo_root: Path) -> CheckResult:
    p = repo_root / ".gitattributes"
    expected = {
        "CLAUDE.md linguist-generated=true",
        ".github/copilot-instructions.md linguist-generated=true",
        ".ctx/opencode.md linguist-generated=true",
    }
    if p.exists():
        existing = set()
        for line in p.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                existing.add(stripped)
        missing = expected - existing
        if not missing:
            return CheckResult("F5", "Output Files", ".gitattributes marks generated", True)
        return CheckResult("F5", "Output Files", ".gitattributes marks generated", False,
                           f"{len(missing)} missing entries", auto_fix=True,
                           fix_label="append to .gitattributes")
    return CheckResult("F5", "Output Files", ".gitattributes marks generated", False,
                       ".gitattributes does not exist", auto_fix=True,
                       fix_label="create .gitattributes")


def _check_mcp_json(repo_root: Path) -> CheckResult:
    p = repo_root / ".mcp.json"
    if not p.exists():
        return CheckResult("F6", "Output Files", ".mcp.json configured", False,
                           ".mcp.json missing", auto_fix=True,
                           fix_label="run ctx generate-mcp-config")
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as err:
        return CheckResult("F6", "Output Files", ".mcp.json configured", False,
                           f"invalid JSON: {err}")
    if "ctx" in cfg.get("mcpServers", {}):
        return CheckResult("F6", "Output Files", ".mcp.json configured", True)
    return CheckResult("F6", "Output Files", ".mcp.json configured", False,
                       "'ctx' not in mcpServers", auto_fix=True,
                       fix_label="run ctx generate-mcp-config")


# ── Category G — Performance ──────────────────────────────────────────────────


def _check_index_exists(repo_root: Path, expected_name: str) -> bool:
    p = repo_root / ".ctx" / "index.db"
    if not p.exists():
        return False
    try:
        conn = connect(p)
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name = ?",
            (expected_name,),
        ).fetchone()
        conn.close()
        return row is not None
    except sqlite3.DatabaseError:
        return False


def _check_files_system_index(repo_root: Path) -> CheckResult:
    if _check_index_exists(repo_root, "idx_files_stale"):
        return CheckResult("G1", "Performance", "files.is_stale index", True)
    return CheckResult("G1", "Performance", "files.is_stale index", False,
                       "index missing", auto_fix=True, fix_label="create idx_files_stale")


def _check_functions_file_index(repo_root: Path) -> CheckResult:
    if _check_index_exists(repo_root, "idx_functions_file"):
        return CheckResult("G2", "Performance", "functions.file index", True)
    return CheckResult("G2", "Performance", "functions.file index", False,
                       "index missing", auto_fix=True, fix_label="create idx_functions_file")


def _check_callgraph_indices(repo_root: Path) -> CheckResult:
    caller = _check_index_exists(repo_root, "idx_call_graph_caller")
    callee = _check_index_exists(repo_root, "idx_call_graph_callee")
    if caller and callee:
        return CheckResult("G3", "Performance", "call_graph caller/callee indices", True)
    missing = []
    if not caller:
        missing.append("caller")
    if not callee:
        missing.append("callee")
    return CheckResult("G3", "Performance", "call_graph caller/callee indices", False,
                       f"missing: {missing}", auto_fix=True, fix_label="create call_graph indices")


def _check_changes_index(repo_root: Path) -> CheckResult:
    if _check_index_exists(repo_root, "idx_changes_file_time"):
        return CheckResult("G4", "Performance", "changes.file/timestamp index", True)
    return CheckResult("G4", "Performance", "changes.file/timestamp index", False,
                       "index missing", auto_fix=True, fix_label="create idx_changes_file_time")


# ── Orchestration ─────────────────────────────────────────────────────────────


CATEGORY_NAMES = {
    "A": "Prerequisites",
    "B": "Schema",
    "C": "Index Health",
    "D": "API and Models",
    "E": "Git Hooks",
    "F": "Output Files",
    "G": "Performance",
}


def _collect_checks(repo_root: Path) -> list[CheckResult]:
    """Run all health checks and return the list of results."""
    checks: list[CheckResult] = []
    # A — Prerequisites
    checks.append(_check_git_repo(repo_root))
    checks.append(_check_ctx_dir(repo_root))
    checks.append(_check_db_exists(repo_root))
    checks.append(_check_db_opens(repo_root))
    checks.append(_check_db_integrity(repo_root))
    checks.append(_check_journal_mode(repo_root))
    checks.append(_check_foreign_keys(repo_root))
    # B — Schema
    checks.append(_check_tables_present(repo_root))
    checks.append(_check_mtime_columns(repo_root))
    checks.append(_check_callgraph_autoincrement(repo_root))
    checks.append(_check_perf_indices(repo_root))
    checks.append(_check_fts5_tables(repo_root))
    # C — Index health
    checks.append(_check_files_indexed(repo_root))
    checks.append(_check_null_purpose(repo_root))
    checks.append(_check_is_stale(repo_root))
    checks.append(_check_is_tainted(repo_root))
    checks.append(_check_taint_queue_empty(repo_root))
    checks.append(_check_mtime_coverage(repo_root))
    checks.append(_check_callgraph_consistency(repo_root))
    # D — API and models
    checks.append(_check_anthropic_key())
    checks.append(_check_anthropic_api())
    checks.append(_check_ollama())
    checks.append(_check_ollama_model())
    # E — Git hooks
    checks.append(_check_pre_commit(repo_root))
    checks.append(_check_post_commit(repo_root))
    checks.append(_check_hooks_executable(repo_root))
    # F — Output files
    checks.append(_check_claude_md_exists(repo_root))
    checks.append(_check_claude_md_fresh(repo_root))
    checks.append(_check_copilot_md_exists(repo_root))
    checks.append(_check_opencode_md_exists(repo_root))
    checks.append(_check_gitattributes(repo_root))
    checks.append(_check_mcp_json(repo_root))
    # G — Performance
    checks.append(_check_files_system_index(repo_root))
    checks.append(_check_functions_file_index(repo_root))
    checks.append(_check_callgraph_indices(repo_root))
    checks.append(_check_changes_index(repo_root))
    return checks


# ── Auto-fix application ──────────────────────────────────────────────────────


def _apply_fix(check: CheckResult, repo_root: Path) -> bool:
    """Apply the auto-fix for a single failing check. Returns True if applied."""
    try:
        if check.id == "A2":  # create .ctx/
            (repo_root / ".ctx").mkdir(exist_ok=True)
            return True
        if check.id == "A6":  # WAL mode
            p = repo_root / ".ctx" / "index.db"
            if p.exists():
                conn = connect(p)
                conn.execute("PRAGMA journal_mode = WAL;")
                conn.close()
                return True
        if check.id == "A7":  # foreign keys
            p = repo_root / ".ctx" / "index.db"
            if p.exists():
                conn = connect(p)
                conn.execute("PRAGMA foreign_keys = ON;")
                conn.close()
                return True
        if check.id == "B1":  # re-create schema (destructive but reversible via re-init)
            p = repo_root / ".ctx" / "index.db"
            if p.exists():
                conn = connect(p)
                init_schema(conn)
                conn.close()
                return True
        if check.id in ("B2", "B4"):  # migrations / performance indices
            p = repo_root / ".ctx" / "index.db"
            if p.exists():
                conn = connect(p)
                init_schema(conn)  # idempotent — applies migrations and indices
                conn.close()
                return True
        if check.id == "B5":  # FTS5 tables
            p = repo_root / ".ctx" / "index.db"
            if p.exists():
                conn = connect(p)
                init_schema(conn)  # idempotent
                conn.close()
                return True
        if check.id == "C7":  # delete dangling call_graph rows
            p = repo_root / ".ctx" / "index.db"
            if p.exists():
                conn = connect(p)
                conn.execute(
                    "DELETE FROM call_graph WHERE callee_id IS NOT NULL "
                    "AND callee_id NOT IN (SELECT id FROM functions)"
                )
                conn.commit()
                conn.close()
                return True
        if check.id == "E1" or check.id == "E2" or check.id == "E3":
            # All hook fixes route through ctx install-hooks (idempotent)
            run_install_hooks(repo_root)
            return True
        if check.id in ("F1", "F2", "F3", "F4"):  # run ctx export
            p = repo_root / ".ctx" / "index.db"
            if p.exists():
                conn = connect(p)
                run_export(conn, repo_root)
                conn.close()
                return True
        if check.id == "F5":  # .gitattributes
            from ctx_engine.commands.export_cmd import _ensure_gitattributes
            _ensure_gitattributes(repo_root)
            return True
        if check.id == "F6":  # .mcp.json
            from ctx_engine.commands.generate_mcp_config import run_generate_mcp_config
            run_generate_mcp_config(repo_root)
            return True
        if check.id in ("G1", "G2", "G3", "G4"):
            p = repo_root / ".ctx" / "index.db"
            if p.exists():
                conn = connect(p)
                for ddl in PERFORMANCE_INDICES:
                    try:
                        conn.execute(ddl)
                    except sqlite3.OperationalError:
                        pass
                conn.commit()
                conn.close()
                return True
    except Exception as err:
        print(f"  Fix {check.id} failed: {err}", flush=True)
        return False
    return False


def _fix_order_key(check: CheckResult) -> tuple[int, str]:
    """Apply fixes in dependency order: schema before data, data before outputs."""
    category = check.id[0]
    if category == "A":
        return (0, check.id)
    if category == "B":
        return (1, check.id)
    if category == "C":
        return (2, check.id)
    if category == "E":
        return (3, check.id)
    if category == "F":
        return (4, check.id)
    if category == "G":
        return (5, check.id)
    return (6, check.id)


# ── Reporting ─────────────────────────────────────────────────────────────────


def _print_report(repo_root: Path, checks: list[CheckResult]) -> int:
    """Print a human-readable report. Returns the number of failed checks."""
    repo_name = repo_root.name
    print(f"ctx doctor \u2014 {repo_name}")
    print()

    # Group by category
    grouped: dict[str, list[CheckResult]] = {}
    for c in checks:
        grouped.setdefault(c.category, []).append(c)

    for cat_letter, cat_name in CATEGORY_NAMES.items():
        cat_checks = grouped.get(cat_name, [])
        if not cat_checks:
            continue
        print(f"  {cat_letter} \u2014 {cat_name}")
        for c in cat_checks:
            mark = "\u2713" if c.passed else "\u2717"
            print(f"    {mark}  {c.id:3} {c.name}")
            if not c.passed and c.detail:
                print(f"           \u2192 {c.detail}")
            if c.passed and c.detail and not c.detail.startswith("("):
                print(f"           ({c.detail})")
        print()

    failed = [c for c in checks if not c.passed]
    auto_fixable = [c for c in failed if c.auto_fix]
    manual = [c for c in failed if not c.auto_fix]
    print("  " + "\u2500" * 60)
    if not failed:
        print(f"  All {len(checks)} checks passed.")
    else:
        print(
            f"  Summary: {len(failed)} issue(s) found "
            f"({len(auto_fixable)} auto-fixable, {len(manual)} manual)"
        )
        print()
        if auto_fixable:
            print("  Run 'ctx doctor --fix' to apply all auto-fixes.")
        if manual:
            print("  Manual actions required:")
            for c in manual:
                print(f"    {c.id}: {c.detail}")
    return len(failed)


def _emit_json(repo_root: Path, checks: list[CheckResult]) -> DoctorReport:
    failed = [c for c in checks if not c.passed]
    auto_fixable = [c for c in checks if (not c.passed) and c.auto_fix]
    report = DoctorReport(
        timestamp=datetime.now(timezone.utc).isoformat(),
        repo=repo_root.name,
        total_checks=len(checks),
        passed=len(checks) - len(failed),
        failed=len(failed),
        auto_fixable=len(auto_fixable),
        checks=[
            {
                "id": c.id,
                "category": c.category,
                "name": c.name,
                "passed": c.passed,
                "detail": c.detail,
                "auto_fix": c.auto_fix,
            }
            for c in checks
        ],
    )
    return report


# ── Public entry point ────────────────────────────────────────────────────────


def run_doctor(repo_root: Path, apply_fix: bool = False, json_output: bool = False) -> int:
    """Run the doctor checklist. Returns the exit code (0 healthy, 1 issues, 0 fixed+manual)."""
    if not json_output:
        print("Running ctx doctor\u2026")
        print()

    checks = _collect_checks(repo_root)

    if apply_fix:
        fixable = [c for c in checks if (not c.passed) and c.auto_fix]
        fixable.sort(key=_fix_order_key)
        for c in fixable:
            print(f"  Fixing {c.id}: {c.fix_label}...", flush=True)
            ok = _apply_fix(c, repo_root)
            print("    done" if ok else "    FAILED")

        # Re-run after fixes to compute the new state
        checks = _collect_checks(repo_root)
        if not json_output:
            print()
            print("  Re-running checks...")
            print()

    if json_output:
        report = _emit_json(repo_root, checks)
        print(json.dumps(asdict(report), indent=2))
        return 0 if report.failed == 0 else 1

    failed = _print_report(repo_root, checks)
    return 1 if failed else 0
