# Taint propagation logic for the ctx index database.

import sqlite3
from datetime import datetime, timezone

def propagate_taint(
    conn: sqlite3.Connection,
    source_id: str,
    caller_snapshot: list[str] | None = None,
) -> None:
    """Propagate taint to all functions calling the changed or removed source function."""
    if caller_snapshot is not None:
        callers = caller_snapshot
    else:
        callers = [
            row[0] for row in conn.execute(
                "SELECT caller_id FROM call_graph WHERE callee_id = ?", (source_id,)
            ).fetchall()
        ]

    now = datetime.now(timezone.utc).isoformat()
    for caller_id in callers:
        # Priority is the caller's own fan-in (how many functions call caller_id)
        fanin = conn.execute(
            "SELECT COUNT(*) FROM call_graph WHERE callee_id = ?", (caller_id,)
        ).fetchone()[0]

        conn.execute(
            "UPDATE functions SET is_tainted = 1, taint_source = ? WHERE id = ?",
            (source_id, caller_id),
        )
        conn.execute(
            """
            INSERT INTO taint_queue (function_id, taint_source, queued_at, priority)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(function_id) DO UPDATE SET
                taint_source = excluded.taint_source,
                queued_at = excluded.queued_at,
                priority = excluded.priority
            """,
            (caller_id, source_id, now, fanin),
        )
