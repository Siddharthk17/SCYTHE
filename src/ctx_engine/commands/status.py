# status command implementation for ctx.

import sqlite3
from pathlib import Path
from ctx_engine.db import connect
from ctx_engine.discovery import EXTENSION_TO_LANGUAGE
from ctx_engine.commands.install_hooks import PRE_COMMIT_HOOK, POST_COMMIT_HOOK


def _hook_status(git_dir: Path, name: str, expected_content: str) -> str:
    hook_path = git_dir / "hooks" / name
    if not hook_path.exists():
        return "NOT INSTALLED"
    actual = hook_path.read_text()
    if actual == expected_content:
        return "INSTALLED"
    return "MODIFIED (not the ctx hook — manual hook present)"


def run_status(repo_root: Path) -> None:
    """Read the SQLite index database and display a summary of current repository state."""
    db_path = repo_root / ".ctx" / "index.db"
    if not db_path.exists():
        raise FileNotFoundError("Database not found. Please run 'ctx init' first.")

    conn = connect(db_path)

    # 1. Get journal mode
    conn.execute("PRAGMA journal_mode;").fetchone()[0]

    # 2. Get table row counts
    tables = [
        "files", "functions", "call_graph", "dangers", "changes",
        "taint_queue", "session_log", "decisions", "directories"
    ]
    counts = {}
    for table in tables:
        counts[table] = conn.execute(f"SELECT count(*) FROM {table};").fetchone()[0]

    # 3. files breakdown by inferred language
    lang_counts = {}
    rows = conn.execute("SELECT path FROM files;").fetchall()
    for row in rows:
        ext = Path(row["path"]).suffix
        lang = EXTENSION_TO_LANGUAGE.get(ext, "unknown")
        lang_counts[lang] = lang_counts.get(lang, 0) + 1

    # 4. List of files where is_stale = 1
    conn.execute("SELECT path FROM files WHERE is_stale = 1;").fetchall()

    # 5. call_graph unresolved and ambiguous count
    conn.execute("SELECT count(*) FROM call_graph WHERE callee_id IS NULL;").fetchone()[0]
    conn.execute("SELECT count(*) FROM call_graph WHERE is_ambiguous = 1;").fetchone()[0]

    # 6. FTS5 availability
    fts_available = True
    try:
        conn.execute("SELECT count(*) FROM files_fts;")
    except sqlite3.OperationalError:
        fts_available = False

    # 7. Confidence distribution and staleness/taint statistics
    fresh = conn.execute("SELECT count(*) FROM functions WHERE confidence >= 1.0;").fetchone()[0]
    decayed_once = conn.execute("SELECT count(*) FROM functions WHERE confidence >= 0.5 AND confidence < 1.0;").fetchone()[0]
    low_confidence = conn.execute("SELECT count(*) FROM functions WHERE confidence >= 0.2 AND confidence < 0.5;").fetchone()[0]
    likely_stale = conn.execute("SELECT count(*) FROM functions WHERE confidence < 0.2;").fetchone()[0]

    stale_functions_count = conn.execute("SELECT count(*) FROM functions WHERE is_stale = 1;").fetchone()[0]
    stale_files_count = conn.execute("SELECT count(*) FROM files WHERE is_stale = 1;").fetchone()[0]
    tainted_functions_count = conn.execute("SELECT count(*) FROM functions WHERE is_tainted = 1;").fetchone()[0]
    taint_queue_count = conn.execute("SELECT count(*) FROM taint_queue;").fetchone()[0]

    # 8. Git hook status
    git_dir = repo_root / ".git"
    pre_status = _hook_status(git_dir, "pre-commit", PRE_COMMIT_HOOK)
    post_status = _hook_status(git_dir, "post-commit", POST_COMMIT_HOOK)

    # 9. Recent changes
    recent_changes = conn.execute(
        "SELECT commit_hash, summary, timestamp FROM changes ORDER BY timestamp DESC LIMIT 5"
    ).fetchall()

    conn.close()

    # Output status report
    repo_name = repo_root.name
    print(f"ctx status — {repo_name}")
    print()
    print("  database: .ctx/index.db (WAL mode)")
    print(f"  fts5: {'available' if fts_available else 'unavailable'}")
    print()
    print("  tables:")
    for table, count in counts.items():
        print(f"    {table:13}: {count}")
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
    print("  staleness:")
    print(f"    is_stale   : {stale_functions_count} functions, {stale_files_count} files")
    print(f"    is_tainted : {tainted_functions_count} functions")
    print(f"    taint_queue: {taint_queue_count} entries")
    print()
    print("  git hooks:")
    print(f"    pre-commit  : {pre_status}")
    print(f"    post-commit : {post_status}")
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
