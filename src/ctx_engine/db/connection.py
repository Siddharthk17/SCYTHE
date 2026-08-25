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
# The MCP server opens a connection per tool call. For a busy session where many
# sequential MCP calls are made, the per-call connect overhead adds up. A simple
# thread-local pool keeps one connection alive per thread for the lifetime of
# the MCP request handler. This is safe because MCP tool calls are sequential
# within a session — the pool degenerates to "one connection per request thread",
# which is exactly what we want.
#
# For ctx init / sync / other CLI commands, connect() is still used directly —
# the pool is opt-in via get_pooled_connection() and used only by the MCP server.

_thread_local = threading.local()


def get_pooled_connection(db_path: Path) -> sqlite3.Connection:
    """Return a thread-local SQLite connection, creating one if needed.

    The connection lives for the lifetime of the calling thread. Callers that
    write must commit explicitly; WAL mode allows concurrent reads from other
    processes (e.g. ctx sync running while the MCP server is active).
    """
    conn = getattr(_thread_local, "conn", None)
    if conn is None:
        conn = connect(db_path)
        _thread_local.conn = conn
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
