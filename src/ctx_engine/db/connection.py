import logging
import sqlite3
from pathlib import Path
from ctx_engine.db.schema import TABLES_DDL, FTS5_DDL

logger = logging.getLogger("ctx")

def connect(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with WAL mode, foreign keys, and dict-like rows enabled."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn

def init_schema(conn: sqlite3.Connection) -> None:
    """Initialize database tables and FTS5 search structures with a fallback mechanism."""
    with conn:
        for table_ddl in TABLES_DDL:
            conn.execute(table_ddl)

        try:
            for fts_ddl in FTS5_DDL:
                conn.execute(fts_ddl)
        except sqlite3.OperationalError as err:
            if "no such module: fts5" in str(err):
                logger.warning("FTS5 unavailable — search will fall back to LIKE queries")
            else:
                raise
