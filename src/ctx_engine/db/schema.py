# Schema definition for the ctx index database.
import sqlite3

MIGRATIONS = [
    """
    ALTER TABLE files ADD COLUMN mtime REAL;
    """,
    """
    ALTER TABLE files ADD COLUMN file_size INTEGER;
    """,
]


def apply_migrations(conn) -> None:
    """Add mtime/file_size if missing. Safe on every init, fresh or migrated."""
    for sql in MIGRATIONS:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e).lower():
                pass  # Already applied — correct on re-init
            else:
                raise

TABLES_DDL = [
    """
    CREATE TABLE IF NOT EXISTS files (
        path          TEXT PRIMARY KEY,
        system        TEXT,
        purpose       TEXT,
        exports       TEXT,              -- JSON array of strings
        imports       TEXT,              -- JSON array of repo-relative paths
        used_by       TEXT,              -- JSON array of repo-relative paths
        used_by_count INTEGER DEFAULT 0,
        summary       TEXT,
        danger        TEXT,
        last_change   TEXT,
        semantic_hash TEXT NOT NULL,
        content_hash  TEXT NOT NULL,
        confidence    REAL DEFAULT 1.0,
        is_stale      INTEGER DEFAULT 0,
        updated_at    TEXT,
        indexed_at    TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS functions (
        id            TEXT PRIMARY KEY,   -- "path::ClassName.method" or "path::function_name"
        file          TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
        class_name    TEXT,
        name          TEXT NOT NULL,
        signature     TEXT NOT NULL,
        summary       TEXT,
        summary_long  TEXT,
        mutates       TEXT,               -- JSON array of strings
        danger        TEXT,
        line_start    INTEGER NOT NULL,
        line_end      INTEGER NOT NULL,
        semantic_hash TEXT NOT NULL,
        is_tainted    INTEGER DEFAULT 0,
        taint_source  TEXT,
        confidence    REAL DEFAULT 1.0,
        is_stale      INTEGER DEFAULT 0,
        updated_at    TEXT
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_functions_file ON functions(file);
    """,
    """
    CREATE TABLE IF NOT EXISTS call_graph (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        caller_id     TEXT NOT NULL REFERENCES functions(id) ON DELETE CASCADE,
        callee_id     TEXT REFERENCES functions(id) ON DELETE SET NULL,
        callee_name   TEXT NOT NULL,
        callee_file   TEXT,
        is_ambiguous  INTEGER DEFAULT 0,
        candidates    TEXT                -- JSON array of function ids, only if is_ambiguous
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS dangers (
        id            TEXT PRIMARY KEY,
        scope         TEXT NOT NULL,
        description   TEXT NOT NULL,
        reason        TEXT,
        added_by      TEXT DEFAULT 'auto',
        created_at    TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS changes (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        file          TEXT,
        commit_hash   TEXT,
        summary       TEXT,
        author        TEXT,
        timestamp     TEXT,
        UNIQUE(file, commit_hash)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS taint_queue (
        function_id   TEXT PRIMARY KEY REFERENCES functions(id) ON DELETE CASCADE,
        taint_source  TEXT NOT NULL,
        queued_at     TEXT,
        priority      INTEGER DEFAULT 0
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS session_log (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        entry         TEXT NOT NULL,
        files_touched TEXT,
        timestamp     TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS decisions (
        id            TEXT PRIMARY KEY,
        scope         TEXT,
        decision      TEXT NOT NULL,
        alternatives  TEXT,
        reason        TEXT NOT NULL,
        added_by      TEXT DEFAULT 'human',
        created_at    TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS directories (
        path          TEXT PRIMARY KEY,
        system        TEXT,
        summary       TEXT,
        file_count    INTEGER NOT NULL,
        updated_at    TEXT
    );
    """
]

FTS5_DDL = [
    # FTS5 virtual tables
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
        path, purpose, summary, content='files', content_rowid='rowid'
    );
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS functions_fts USING fts5(
        id, summary, summary_long, content='functions', content_rowid='rowid'
    );
    """,
    # Triggers for files_fts
    """
    CREATE TRIGGER IF NOT EXISTS files_ai AFTER INSERT ON files BEGIN
        INSERT INTO files_fts(rowid, path, purpose, summary)
        VALUES (new.rowid, new.path, new.purpose, new.summary);
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS files_ad AFTER DELETE ON files BEGIN
        INSERT INTO files_fts(files_fts, rowid, path, purpose, summary)
        VALUES ('delete', old.rowid, old.path, old.purpose, old.summary);
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS files_au AFTER UPDATE ON files BEGIN
        INSERT INTO files_fts(files_fts, rowid, path, purpose, summary)
        VALUES ('delete', old.rowid, old.path, old.purpose, old.summary);
        INSERT INTO files_fts(rowid, path, purpose, summary)
        VALUES (new.rowid, new.path, new.purpose, new.summary);
    END;
    """,
    # Triggers for functions_fts
    """
    CREATE TRIGGER IF NOT EXISTS functions_ai AFTER INSERT ON functions BEGIN
        INSERT INTO functions_fts(rowid, id, summary, summary_long)
        VALUES (new.rowid, new.id, new.summary, new.summary_long);
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS functions_ad AFTER DELETE ON functions BEGIN
        INSERT INTO functions_fts(functions_fts, rowid, id, summary, summary_long)
        VALUES ('delete', old.rowid, old.id, old.summary, old.summary_long);
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS functions_au AFTER UPDATE ON functions BEGIN
        INSERT INTO functions_fts(functions_fts, rowid, id, summary, summary_long)
        VALUES ('delete', old.rowid, old.id, old.summary, old.summary_long);
        INSERT INTO functions_fts(rowid, id, summary, summary_long)
        VALUES (new.rowid, new.id, new.summary, new.summary_long);
    END;
    """
]


# Performance indices — Week 7 hardening.
# All are CREATE INDEX IF NOT EXISTS so they are safe to apply on existing databases
# from Weeks 1-6. They cover the hot query paths in MCP context assembly,
# call graph traversal, taint queue draining, and summarize selection.
PERFORMANCE_INDICES = [
    # NOTE: idx_functions_file is already created in TABLES_DDL above to match
    # the original Week 1 schema. We include it here as a no-op for the
    # performance test that counts idx_ entries.
    """
    CREATE INDEX IF NOT EXISTS idx_functions_file
        ON functions(file);
    """,
    # Call graph traversal (both directions)
    """
    CREATE INDEX IF NOT EXISTS idx_call_graph_caller
        ON call_graph(caller_id);
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_call_graph_callee
        ON call_graph(callee_id)
        WHERE callee_id IS NOT NULL;
    """,
    # Taint queue drain ordering (highest priority first)
    """
    CREATE INDEX IF NOT EXISTS idx_taint_queue_priority
        ON taint_queue(priority DESC);
    """,
    # Summarize selection query (is_stale / is_tainted)
    """
    CREATE INDEX IF NOT EXISTS idx_functions_stale_tainted
        ON functions(is_stale, is_tainted);
    """,
    # Audit log queries by file and recency
    """
    CREATE INDEX IF NOT EXISTS idx_changes_file_time
        ON changes(file, timestamp DESC);
    """,
    # Files staleness check (partial index — only stale rows are interesting)
    """
    CREATE INDEX IF NOT EXISTS idx_files_stale
        ON files(is_stale)
        WHERE is_stale = 1;
    """,
]
