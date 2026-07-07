# status command implementation for ctx.

import sqlite3
from pathlib import Path
from ctx_engine.discovery import EXTENSION_TO_LANGUAGE

def run_status(repo_root: Path) -> None:
    """Read the SQLite index database and display a summary of current repository state."""
    db_path = repo_root / ".ctx" / "index.db"
    if not db_path.exists():
        raise FileNotFoundError("Database not found. Please run 'ctx init' first.")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # 1. Get journal mode
    journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]

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
    stale_files = [r["path"] for r in conn.execute("SELECT path FROM files WHERE is_stale = 1;").fetchall()]

    # 5. call_graph unresolved and ambiguous count
    unresolved_calls = conn.execute("SELECT count(*) FROM call_graph WHERE callee_id IS NULL;").fetchone()[0]
    ambiguous_calls = conn.execute("SELECT count(*) FROM call_graph WHERE is_ambiguous = 1;").fetchone()[0]

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

    conn.close()

    # Output status report
    print("ctx status")
    print()
    print("  database: .ctx/index.db")
    print(f"  journal mode: {journal_mode}")
    print(f"  fts5 search: {'enabled' if fts_available else 'disabled'}")
    print()
    print("  table records:")
    for table, count in counts.items():
        print(f"    {table:13}: {count}")
    print()
    print("  parsed files by language:")
    if lang_counts:
        for lang, count in sorted(lang_counts.items()):
            print(f"    {lang:13}: {count}")
    else:
        print("    None")
    print()
    print("  stale files:")
    if stale_files:
        for f in stale_files:
            print(f"    - {f}")
    else:
        print("    None")
    print()
    print("  call graph:")
    print(f"    unresolved calls: {unresolved_calls}")
    print(f"    ambiguous calls: {ambiguous_calls}")
    print()
    print("  confidence distribution:")
    print(f"    1.0          : {fresh:<6} (fresh)")
    print(f"    0.5 - 1.0    : {decayed_once:<6} (decayed once)")
    print(f"    0.2 - 0.5    : {low_confidence:<6} [LOW CONFIDENCE]")
    print(f"    0.0 - 0.2    : {likely_stale:<6} [LIKELY STALE]")
    print()
    print(f"  is_stale: {stale_functions_count} functions, {stale_files_count} files")
    print(f"  is_tainted: {tainted_functions_count} functions")
    print(f"  taint_queue: {taint_queue_count} entries")
