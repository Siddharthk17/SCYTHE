import sqlite3
from ctx_engine.db import init_schema
from ctx_engine.intelligence.taint import propagate_taint

def test_taint_propagation_caller_cascade():
    """Verify that changing a callee marks its callers as tainted with correct priority."""
    conn = sqlite3.connect(":memory:")
    init_schema(conn)

    # 1. Insert dummy files and functions
    conn.execute("INSERT INTO files (path, semantic_hash, content_hash) VALUES ('a.py', 'h1', 'c1');")
    conn.execute("INSERT INTO files (path, semantic_hash, content_hash) VALUES ('b.py', 'h2', 'c2');")

    conn.execute(
        """
        INSERT INTO functions (id, file, name, signature, line_start, line_end, semantic_hash)
        VALUES ('a.py::A', 'a.py', 'A', 'def A()', 1, 5, 'hash_A');
        """
    )
    conn.execute(
        """
        INSERT INTO functions (id, file, name, signature, line_start, line_end, semantic_hash)
        VALUES ('b.py::B', 'b.py', 'B', 'def B()', 1, 5, 'hash_B');
        """
    )

    # A calls B
    conn.execute(
        """
        INSERT INTO call_graph (caller_id, callee_id, callee_name, callee_file)
        VALUES ('a.py::A', 'b.py::B', 'B', 'b.py');
        """
    )

    # Propagate taint from B
    propagate_taint(conn, 'b.py::B')

    # Assert A is tainted
    row = conn.execute("SELECT is_tainted, taint_source FROM functions WHERE id = 'a.py::A';").fetchone()
    assert row[0] == 1
    assert row[1] == 'b.py::B'

    # Assert taint_queue has A with priority 1 (since 1 function, which is A itself, calls it? No, wait:
    # "priority in taint_queue is the caller's own fan-in (how many things call the tainted function)"
    # A is the caller. How many functions call A? Zero. So A's fan-in is 0.)
    queue_row = conn.execute("SELECT function_id, taint_source, priority FROM taint_queue;").fetchone()
    assert queue_row is not None
    assert queue_row[0] == 'a.py::A'
    assert queue_row[1] == 'b.py::B'
    assert queue_row[2] == 0

    conn.close()

def test_taint_propagation_last_write_wins():
    """Verify that multiple taint propagations to the same caller resolve via last-write-wins."""
    conn = sqlite3.connect(":memory:")
    init_schema(conn)

    conn.execute("INSERT INTO files (path, semantic_hash, content_hash) VALUES ('a.py', 'h1', 'c1');")
    conn.execute("INSERT INTO files (path, semantic_hash, content_hash) VALUES ('b1.py', 'hb1', 'cb1');")
    conn.execute("INSERT INTO files (path, semantic_hash, content_hash) VALUES ('b2.py', 'hb2', 'cb2');")

    conn.execute("INSERT INTO functions (id, file, name, signature, line_start, line_end, semantic_hash) VALUES ('a.py::A', 'a.py', 'A', 'def A()', 1, 5, 'hA');")
    conn.execute("INSERT INTO functions (id, file, name, signature, line_start, line_end, semantic_hash) VALUES ('b1.py::B1', 'b1.py', 'B1', 'def B1()', 1, 5, 'hB1');")
    conn.execute("INSERT INTO functions (id, file, name, signature, line_start, line_end, semantic_hash) VALUES ('b2.py::B2', 'b2.py', 'B2', 'def B2()', 1, 5, 'hB2');")

    # A calls B1 and B2
    conn.execute("INSERT INTO call_graph (caller_id, callee_id, callee_name, callee_file) VALUES ('a.py::A', 'b1.py::B1', 'B1', 'b1.py');")
    conn.execute("INSERT INTO call_graph (caller_id, callee_id, callee_name, callee_file) VALUES ('a.py::A', 'b2.py::B2', 'B2', 'b2.py');")

    # Propagate from B1, then B2
    propagate_taint(conn, 'b1.py::B1')
    propagate_taint(conn, 'b2.py::B2')

    row = conn.execute("SELECT is_tainted, taint_source FROM functions WHERE id = 'a.py::A';").fetchone()
    assert row[0] == 1
    # Should point to B2 (last-write-wins)
    assert row[1] == 'b2.py::B2'

    queue_row = conn.execute("SELECT function_id, taint_source FROM taint_queue;").fetchone()
    assert queue_row[0] == 'a.py::A'
    assert queue_row[1] == 'b2.py::B2'

    conn.close()

def test_taint_propagation_removed_function():
    """Verify that removing a function uses the pre-deletion caller snapshot for taint propagation."""
    conn = sqlite3.connect(":memory:")
    init_schema(conn)

    conn.execute("INSERT INTO files (path, semantic_hash, content_hash) VALUES ('a.py', 'h1', 'c1');")
    conn.execute("INSERT INTO functions (id, file, name, signature, line_start, line_end, semantic_hash) VALUES ('a.py::A', 'a.py', 'A', 'def A()', 1, 5, 'hA');")

    # Pre-deletion caller snapshot of non-existent/deleted function 'b.py::B' is ['a.py::A']
    propagate_taint(conn, 'b.py::B', caller_snapshot=['a.py::A'])

    row = conn.execute("SELECT is_tainted, taint_source FROM functions WHERE id = 'a.py::A';").fetchone()
    assert row[0] == 1
    assert row[1] == 'b.py::B'

    queue_row = conn.execute("SELECT function_id, taint_source FROM taint_queue;").fetchone()
    assert queue_row[0] == 'a.py::A'
    assert queue_row[1] == 'b.py::B'

    conn.close()
