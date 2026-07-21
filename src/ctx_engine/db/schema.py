# Schema definition for the ctx index database.

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
