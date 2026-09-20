import logging
import sqlite3
import threading
from pathlib import Path
from ctx_engine.db.schema import TABLES_DDL, FTS5_DDL, PERFORMANCE_INDICES, apply_migrations

logger = logging.getLogger("ctx")


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with WAL mode, foreign keys, and dict-like rows enabled.

    Week 7 hardening: wal_autocheckpoint is set to 1000 pages (default 100) to reduce
    lock contention between the MCP server's frequent reads and ctx init's writes.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 50;")
    conn.execute("PRAGMA wal_autocheckpoint = 1000;")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Initialize database tables, indices, and FTS5 search structures with a fallback mechanism."""
    with conn:
        for table_ddl in TABLES_DDL:
            conn.execute(table_ddl)

        # Week 7: performance indices for hot query paths.
        for index_ddl in PERFORMANCE_INDICES:
            conn.execute(index_ddl)

        try:
            for fts_ddl in FTS5_DDL:
                conn.execute(fts_ddl)
        except sqlite3.OperationalError as err:
            if "no such module: fts5" in str(err):
                logger.warning("FTS5 unavailable — search will fall back to LIKE queries")
            else:
                raise

    apply_migrations(conn)


# ── Connection pool ────────────────────────────────────────────────────────────
#
# Week 7 hardening: the MCP server reuses one SQLite connection per thread via
# get_pooled_connection(). For a busy session with hundreds of sequential MCP
# calls, this avoids per-call connect overhead. Thread-local storage keeps this
# safe (sqlite3 handles are not shared across threads); sequential dispatch in
# the current server means one live connection per session thread.
#
# For ctx init / sync / other CLI commands, connect() is still used directly —
# the pool is opt-in via get_pooled_connection() and used by the MCP server.

_thread_local = threading.local()


def get_pooled_connection(db_path: Path) -> sqlite3.Connection:
    """Return a thread-local SQLite connection, creating one if needed.

    The connection lives for the lifetime of the calling thread. Callers that
    write must commit explicitly; WAL mode allows concurrent reads from other
    processes (e.g. ctx sync running while the MCP server is active).

    Production-grade behavior:
    - Tracks the resolved db path per thread. If a different path is
      requested (e.g. tests using tmp_path fixtures in one thread), the old
      connection is closed and a new one is opened. The naive version
      returned the wrong DB in that case.
    - Validates liveness with SELECT 1; a closed/broken handle triggers
      transparent reconnect instead of ProgrammingError.
    """
    resolved = str(Path(db_path))
    conn = getattr(_thread_local, "conn", None)
    pooled_path = getattr(_thread_local, "db_path", None)
    if conn is not None and pooled_path != resolved:
        try:
            conn.close()
        except Exception:
            pass
        conn = None
        _thread_local.conn = None
        _thread_local.db_path = None
    if conn is not None:
        try:
            conn.execute("SELECT 1").fetchone()
            return conn
        except (sqlite3.ProgrammingError, sqlite3.OperationalError):
            try:
                conn.close()
            except Exception:
                pass
            conn = None
    conn = connect(Path(resolved))
    _thread_local.conn = conn
    _thread_local.db_path = resolved
    return conn


def close_pooled_connection() -> None:
    """Close the current thread's pooled connection, if any. Safe to call repeatedly."""
    conn = getattr(_thread_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _thread_local.conn = None
    try:
        _thread_local.db_path = None
    except Exception:
        pass
