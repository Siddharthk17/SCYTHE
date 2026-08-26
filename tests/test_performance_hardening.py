"""Tests for Week 7 performance hardening: indices, WAL tuning, optimize, pool."""
import sqlite3
import time
import threading

import pytest

from ctx_engine.db import (
    close_pooled_connection,
    connect,
    get_pooled_connection,
    init_schema,
)
from ctx_engine.db.schema import PERFORMANCE_INDICES


def _populate_function_rows(conn: sqlite3.Connection, count: int) -> None:
    """Insert `count` function rows pointing at a single file path."""
    conn.execute(
        "INSERT INTO files (path, semantic_hash, content_hash, purpose, summary) "
        "VALUES (?, ?, ?, ?, ?)",
        ("sample.py", "h0", "c0", "test", "test"),
    )
    for i in range(count):
        conn.execute(
            "INSERT INTO functions (id, file, name, signature, line_start, line_end, semantic_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"sample.py::func_{i}", "sample.py", f"func_{i}", f"def func_{i}()", i + 1, i + 2, f"sh{i}"),
        )
    conn.commit()


def test_indices_present_after_init(tmp_path):
    db_path = tmp_path / "index.db"
    conn = connect(db_path)
    init_schema(conn)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%' ORDER BY name"
    ).fetchall()
    names = {r[0] for r in rows}
    expected = {
        "idx_functions_file",
        "idx_call_graph_caller",
        "idx_call_graph_callee",
        "idx_taint_queue_priority",
        "idx_functions_stale_tainted",
        "idx_changes_file_time",
        "idx_files_stale",
    }
    assert expected.issubset(names), f"missing indices: {expected - names}"
    conn.close()


def test_indices_idempotent(tmp_path):
    """Running init_schema() twice does not error and produces the same set of indices."""
    db_path = tmp_path / "index.db"
    conn = connect(db_path)
    init_schema(conn)
    init_schema(conn)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%' ORDER BY name"
    ).fetchall()
    names = {r[0] for r in rows}
    assert "idx_functions_file" in names
    conn.close()


def test_performance_indices_constant_count():
    """The PERFORMANCE_INDICES list in schema.py matches the documented set of 7 indices."""
    assert len(PERFORMANCE_INDICES) == 7


def test_wal_autocheckpoint_default(tmp_path):
    """connect() sets wal_autocheckpoint to 1000 (Week 7 hardening)."""
    db_path = tmp_path / "index.db"
    conn = connect(db_path)
    val = conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0]
    assert val == 1000
    conn.close()


def test_pragma_optimize_runs(tmp_path):
    """PRAGMA optimize runs without error after a populated init."""
    db_path = tmp_path / "index.db"
    conn = connect(db_path)
    init_schema(conn)
    _populate_function_rows(conn, 50)
    try:
        conn.execute("PRAGMA optimize;")
    except sqlite3.OperationalError as err:
        pytest.fail(f"PRAGMA optimize raised: {err}")
    conn.close()


def test_query_timing_functions_for_file(tmp_path):
    """The functions_for_file query runs in < 5ms on a 1,000-function DB."""
    db_path = tmp_path / "index.db"
    conn = connect(db_path)
    init_schema(conn)
    _populate_function_rows(conn, 1000)

    t0 = time.perf_counter()
    conn.execute(
        "SELECT * FROM functions WHERE file = ? ORDER BY line_start",
        ("sample.py",),
    ).fetchall()
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert elapsed_ms < 5.0, f"query took {elapsed_ms:.2f}ms (expected < 5ms)"
    conn.close()


def test_query_timing_call_graph_traversal(tmp_path):
    """The call_graph traversal query runs in < 5ms on a small DB."""
    db_path = tmp_path / "index.db"
    conn = connect(db_path)
    init_schema(conn)
    _populate_function_rows(conn, 100)
    # Add some call graph edges
    for i in range(50):
        conn.execute(
            "INSERT INTO call_graph (caller_id, callee_id, callee_name, callee_file) "
            "VALUES (?, ?, ?, ?)",
            (f"sample.py::func_{i}", f"sample.py::func_{i + 1}", f"func_{i + 1}", "sample.py"),
        )
    conn.commit()

    t0 = time.perf_counter()
    conn.execute(
        "SELECT callee_id FROM call_graph WHERE caller_id IN "
        "(SELECT id FROM functions WHERE file = ? LIMIT 20)",
        ("sample.py",),
    ).fetchall()
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert elapsed_ms < 5.0, f"query took {elapsed_ms:.2f}ms (expected < 5ms)"
    conn.close()


def test_connection_pool_same_thread(tmp_path):
    """get_pooled_connection returns the same connection object on repeated calls from one thread."""
    db_path = tmp_path / "index.db"
    conn1 = get_pooled_connection(db_path)
    conn2 = get_pooled_connection(db_path)
    assert conn1 is conn2
    close_pooled_connection()


def test_connection_pool_independent_threads(tmp_path):
    """Different threads get different connection objects (no cross-thread sharing)."""
    db_path = tmp_path / "index.db"
    init_schema(connect(db_path))

    results: dict[str, int] = {}

    def worker(name: str) -> None:
        conn = get_pooled_connection(db_path)
        results[name] = id(conn)
        close_pooled_connection()

    t1 = threading.Thread(target=worker, args=("a",))
    t2 = threading.Thread(target=worker, args=("b",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # If they share the same thread-local, both threads would get the same id
    # and the second close would null it for the first. Different ids means
    # each thread has its own connection.
    assert results["a"] != results["b"]


def test_connection_pool_close_is_safe(tmp_path):
    """close_pooled_connection is idempotent and safe to call when no connection exists."""
    db_path = tmp_path / "index.db"
    close_pooled_connection()  # no conn yet
    close_pooled_connection()  # twice
    get_pooled_connection(db_path)  # create a pooled conn (side effect, binding unused)
    close_pooled_connection()  # with a conn
    close_pooled_connection()  # again
