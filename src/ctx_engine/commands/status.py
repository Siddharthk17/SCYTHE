import json
import os
import sqlite3
from pathlib import Path
from ctx_engine.db import connect
from ctx_engine.discovery import EXTENSION_TO_LANGUAGE
from ctx_engine.commands.install_hooks import PRE_COMMIT_HOOK, POST_COMMIT_HOOK
from ctx_engine.daemon.daemon import (
    is_process_alive,
    read_pid_file,
    read_watch_state,
    remove_pid_file,
)
from ctx_engine.daemon.local_llm import is_ollama_available, get_available_models, select_model
from ctx_engine.mcp_server.tools.renderers import render_generation_timestamp


def _hook_status(git_dir: Path, name: str, expected_content: str) -> str:
    hook_path = git_dir / "hooks" / name
    if not hook_path.exists():
        return "NOT INSTALLED"
    try:
        actual = hook_path.read_text()
    except (PermissionError, OSError):
        return "ERROR (cannot read)"
    if actual == expected_content:
        return "INSTALLED"
    return "MODIFIED (not the ctx hook — manual hook present)"


def _ollama_status_text(ollama_host: str) -> str:
    if is_ollama_available(ollama_host):
        available = get_available_models(ollama_host)
        model = select_model(available)
        if model:
            return f"available (model: {model})"
        return "available (no preferred model found)"
    return "not available"


def _export_freshness(repo_root: Path, conn) -> dict[str, str]:
    export_files = {
        "CLAUDE.md": "claude",
        ".github/copilot-instructions.md": "copilot",
        ".ctx/opencode.md": "opencode",
    }
    latest_db_update = conn.execute(
        "SELECT MAX(updated_at) FROM ("
        "SELECT MAX(updated_at) as updated_at FROM files "
        "UNION ALL SELECT MAX(updated_at) FROM functions "
        "UNION ALL SELECT MAX(created_at) FROM dangers "
        "UNION ALL SELECT MAX(created_at) FROM decisions"
        ")"
    ).fetchone()[0]

    statuses = {}
    for file_path, _ in export_files.items():
        abs_path = repo_root / file_path
        if not abs_path.exists():
            statuses[file_path] = "NOT GENERATED"
            continue
        gen_ts = render_generation_timestamp(abs_path)
        if gen_ts is None:
            statuses[file_path] = "NO TIMESTAMP"
            continue
        try:
            gen_dt = datetime.fromisoformat(gen_ts.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            statuses[file_path] = "INVALID TIMESTAMP"
            continue
        if latest_db_update:
            try:
                db_dt = datetime.fromisoformat(latest_db_update.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                statuses[file_path] = "?"
                continue
            if gen_dt >= db_dt:
                statuses[file_path] = "CURRENT"
            else:
                statuses[file_path] = "OUTDATED"
        else:
            statuses[file_path] = "CURRENT"
    return statuses


def run_status(repo_root: Path, full: bool = False) -> None:
    """Read the SQLite index database and display a summary of current repository state."""
    db_path = repo_root / ".ctx" / "index.db"
    if not db_path.exists():
        raise FileNotFoundError("Database not found. Please run 'ctx init' first.")

    conn = connect(db_path)

    tables = [
        "files", "functions", "call_graph", "dangers", "changes",
        "taint_queue", "session_log", "decisions", "directories"
    ]
    counts: dict[str, int] = {}
    for table in tables:
        counts[table] = conn.execute(f"SELECT count(*) FROM {table};").fetchone()[0]

    lang_counts: dict[str, int] = {}
    rows = conn.execute("SELECT path FROM files;").fetchall()
    for row in rows:
        ext = Path(row["path"]).suffix
        lang = EXTENSION_TO_LANGUAGE.get(ext, "unknown")
        lang_counts[lang] = lang_counts.get(lang, 0) + 1

    unresolved_count = conn.execute("SELECT count(*) FROM call_graph WHERE callee_id IS NULL;").fetchone()[0]
    ambiguous_count = conn.execute("SELECT count(*) FROM call_graph WHERE is_ambiguous = 1;").fetchone()[0]

    fts_available = True
    try:
        conn.execute("SELECT count(*) FROM files_fts;")
    except sqlite3.OperationalError:
        fts_available = False

    fresh = conn.execute("SELECT count(*) FROM functions WHERE confidence >= 1.0;").fetchone()[0]
    decayed_once = conn.execute("SELECT count(*) FROM functions WHERE confidence >= 0.5 AND confidence < 1.0;").fetchone()[0]
    low_confidence = conn.execute("SELECT count(*) FROM functions WHERE confidence >= 0.2 AND confidence < 0.5;").fetchone()[0]
    likely_stale = conn.execute("SELECT count(*) FROM functions WHERE confidence < 0.2;").fetchone()[0]

    stale_functions_count = conn.execute("SELECT count(*) FROM functions WHERE is_stale = 1;").fetchone()[0]
    stale_files_count = conn.execute("SELECT count(*) FROM files WHERE is_stale = 1;").fetchone()[0]
    tainted_functions_count = conn.execute("SELECT count(*) FROM functions WHERE is_tainted = 1;").fetchone()[0]
    taint_queue_count = conn.execute("SELECT count(*) FROM taint_queue;").fetchone()[0]

    git_dir = repo_root / ".git"
    pre_status = _hook_status(git_dir, "pre-commit", PRE_COMMIT_HOOK)
    post_status = _hook_status(git_dir, "post-commit", POST_COMMIT_HOOK)

    recent_changes = conn.execute(
        "SELECT commit_hash, summary, timestamp FROM changes ORDER BY timestamp DESC LIMIT 5"
    ).fetchall()

    mcp_json_path = repo_root / ".mcp.json"
    if mcp_json_path.exists():
        try:
            mcp_config = json.loads(mcp_json_path.read_text(encoding="utf-8"))
            ctx_configured = "ctx" in mcp_config.get("mcpServers", {})
            if ctx_configured:
                mcp_config_status = f"PRESENT (ctx serve configured)"
            else:
                mcp_config_status = f"PRESENT (ctx not configured)"
        except (json.JSONDecodeError, OSError):
            mcp_config_status = "ERROR (invalid .mcp.json)"
    else:
        mcp_config_status = "NOT CONFIGURED (run 'ctx generate-mcp-config')"

    pid_path = repo_root / ".ctx" / "watch.pid"
    state_path = repo_root / ".ctx" / "watch-state.json"
    pid = read_pid_file(pid_path)
    ollama_host = os.environ.get("CTX_OLLAMA_HOST", "http://localhost:11434")
    if pid is not None and is_process_alive(pid):
        state = read_watch_state(state_path)
        watcher_status = f"RUNNING (PID: {pid})"
        watcher_events = (
            f"{state.get('events_processed', 0)} processed "
            f"({state.get('semantic_changes', 0)} semantic, "
            f"{state.get('formatting_changes', 0)} formatting-only)"
        )
        watcher_ollama = _ollama_status_text(ollama_host)
    else:
        if pid is not None:
            remove_pid_file(pid_path)
        watcher_status = "NOT RUNNING"
        watcher_events = ""
        watcher_ollama = _ollama_status_text(ollama_host)

    total_files = counts.get("files", 0)
    if total_files > 0:
        cached_files = conn.execute(
            "SELECT COUNT(*) FROM files WHERE mtime IS NOT NULL"
        ).fetchone()[0]
        mtime_coverage = (cached_files / total_files * 100) if total_files > 0 else 0
        uncached = total_files - cached_files
    else:
        cached_files = 0
        mtime_coverage = 0.0
        uncached = 0

    export_statuses = _export_freshness(repo_root, conn)

    # Pre-fetch all data needed for display
    fresh_count = conn.execute("SELECT COUNT(*) FROM functions WHERE confidence >= 1.0").fetchone()[0]
    total_funcs = counts.get("functions", 0)
    all_fresh = fresh_count == total_funcs if total_funcs > 0 else True

    danger_counts = conn.execute(
        "SELECT added_by, COUNT(*) as cnt FROM dangers GROUP BY added_by"
    ).fetchall()
    danger_summary = ", ".join(f"{r['cnt']} {r['added_by']}" for r in danger_counts) if danger_counts else "0"
    total_dangers = conn.execute("SELECT COUNT(*) FROM dangers").fetchone()[0]

    decision_count = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    decision_human_count = conn.execute(
        "SELECT COUNT(*) FROM decisions WHERE added_by = 'human'"
    ).fetchone()[0]

    last_sync = conn.execute(
        "SELECT MAX(updated_at) FROM files"
    ).fetchone()[0] or "never"

    conn.close()

    repo_name = repo_root.name

    if not full:
        # ── Concise view ──
        health_parts = []
        if stale_files_count == 0 and stale_functions_count == 0 and tainted_functions_count == 0 and all_fresh:
            health_parts.append("✓ current")
        else:
            health_parts.append("✗ ")
            if stale_files_count > 0 or stale_functions_count > 0:
                health_parts.append(f"{stale_files_count} stale files, {stale_functions_count} stale functions")
            if tainted_functions_count > 0:
                health_parts.append(f"{tainted_functions_count} tainted functions")

        export_parts = []
        for fname, status in sorted(export_statuses.items()):
            fshort = fname.split("/")[-1] if "/" in fname else fname
            if status == "CURRENT":
                export_parts.append(f"{fshort} ✓")
            elif status == "OUTDATED":
                export_parts.append(f"{fshort} ✗ (outdated)")
            elif status == "NOT GENERATED":
                export_parts.append(f"{fshort} NOT GENERATED")
            else:
                export_parts.append(f"{fshort} {status}")

        last_export = export_statuses.get("CLAUDE.md", "")
        if last_export == "CURRENT":
            last_export_ts = render_generation_timestamp(repo_root / "CLAUDE.md") or "?"
        else:
            last_export_ts = "?"

        print(f"ctx status — {repo_name}")
        print()
        print(f"  index:    {counts.get('files', 0)} files, {counts.get('functions', 0)} functions")
        if len(health_parts) > 1:
            print(f"  health:   {' '.join(health_parts)}")
            needs_sync = stale_files_count > 0 or stale_functions_count > 0 or tainted_functions_count > 0
            if needs_sync:
                print(f"            → run 'ctx sync' to fix")
        else:
            print(f"  health:   {health_parts[0]}")
        print(f"  hooks:    pre-commit {pre_status}, post-commit {post_status}")
        if "NOT INSTALLED" in pre_status:
            print(f"            → run 'ctx install-hooks' to enable commit-time validation")
        print(f"  mcp:      {mcp_config_status}")
        print(f"  watcher:  {watcher_status}")
        print(f"  ollama:   {watcher_ollama}")
        print(f"  export:   {' | '.join(export_parts)}")
        if any("OUTDATED" in s for s in export_statuses.values()):
            print(f"            → run 'ctx export' to refresh")
        print(f"  dangers:  {total_dangers} zones ({danger_summary})")
        print(f"  decisions: {decision_count} recorded ({decision_human_count} human)")
        print()
        print(f"  Last sync: {last_sync[:19] if last_sync != 'never' else 'never'}  |  Last export: {last_export_ts}")

    else:
        # ── Full view ──
        print(f"ctx status — {repo_name} (full)")
        print()
        print("  database: .ctx/index.db (WAL mode)")
        print(f"  fts5: {'available' if fts_available else 'unavailable'}")
        print()
        print("  tables:")
        for table, count in counts.items():
            print(f"    {table:13}: {count}")
        print()
        print(f"  call graph:")
        print(f"    unresolved    : {unresolved_count}")
        print(f"    ambiguous     : {ambiguous_count}")
        print()
        print("  files by language:")
        if lang_counts:
            for lang, count in sorted(lang_counts.items()):
                print(f"    {lang:13}: {count}")
        else:
            print("    None")
        print()
        print("  confidence distribution (functions):")
        print(f"    1.0        : {fresh:<6}  (fresh)")
        print(f"    0.5–1.0    : {decayed_once:<6}  (decayed)")
        print(f"    0.2–0.5    : {low_confidence:<6}  [LOW CONFIDENCE]")
        print(f"    0.0–0.2    : {likely_stale:<6}  [LIKELY STALE]")
        print()
        print("  file watcher:")
        print(f"    status: {watcher_status}")
        if watcher_status.startswith("RUNNING"):
            print(f"    events: {watcher_events}")
        print(f"    ollama: {watcher_ollama}")
        print()
        print("  mtime cache:")
        if total_files > 0:
            print(f"    files with mtime cached: {cached_files} of {total_files} "
                  f"({uncached} uncached — will parse on next init)")
            print(f"    cache coverage: {mtime_coverage:.1f}%")
        else:
            print("    (no files indexed — run 'ctx init')")
        print()
        print("  staleness:")
        print(f"    is_stale   : {stale_functions_count} functions, {stale_files_count} files")
        print(f"    is_tainted : {tainted_functions_count} functions")
        print(f"    taint_queue: {taint_queue_count} entries")
        print()
        print("  git hooks:")
        print(f"    pre-commit  : {pre_status}")
        print(f"    post-commit : {post_status}")
        print()
        print("  export status:")
        for fname, status in sorted(export_statuses.items()):
            print(f"    {fname:45}: {status}")
        print()
        print("  recent changes (last 5):")
        if recent_changes:
            for row in recent_changes:
                h = row["commit_hash"][:7] if row["commit_hash"] else "?"
                s = row["summary"] or ""
                t = row["timestamp"] or ""
                print(f"    {h}  \"{s}\"  {t}")
        else:
            print("    (none — run 'ctx install-hooks' and make a commit)")
        print()
        print("  mcp server:")
        print(f"    config: {mcp_config_status}")
        print("    last connection: (no connection log yet — connects on demand)")
