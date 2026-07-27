import hashlib
from datetime import datetime, timezone


def _gen_id(*parts: str) -> str:
    combined = "".join(parts)
    return hashlib.sha256(combined.encode()).hexdigest()[:12]


def decision_add(
    conn,
    scope: str | None,
    decision: str,
    alternatives: str | None,
    reason: str,
) -> str:
    decision_id = _gen_id(f"{scope or '*'}:{decision}")

    conn.execute(
        """INSERT INTO decisions (id, scope, decision, alternatives, reason, added_by, created_at)
           VALUES (?, ?, ?, ?, ?, 'human', ?)
           ON CONFLICT(id) DO UPDATE SET
               decision = excluded.decision,
               alternatives = excluded.alternatives,
               reason = excluded.reason""",
        (
            decision_id,
            scope,
            decision,
            alternatives,
            reason,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    return decision_id


def decision_remove(conn, decision_id: str, confirmed: bool = False) -> str:
    row = conn.execute(
        "SELECT added_by, decision FROM decisions WHERE id = ?", (decision_id,)
    ).fetchone()

    if row is None:
        return f"No decision found with id '{decision_id}'."

    if row["added_by"] == "human" and not confirmed:
        return (
            f"This decision was added by {row['added_by']}. "
            f"Use --confirm to remove it."
        )

    conn.execute("DELETE FROM decisions WHERE id = ?", (decision_id,))
    conn.commit()
    return f"Removed: {row['decision']}"


def decision_list(conn, scope: str | None = None) -> list[dict]:
    if scope:
        rows = conn.execute(
            "SELECT id, scope, decision, alternatives, reason, added_by, created_at "
            "FROM decisions WHERE scope = ? ORDER BY added_by DESC, rowid",
            (scope,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, scope, decision, alternatives, reason, added_by, created_at "
            "FROM decisions ORDER BY added_by DESC, scope NULLS LAST, rowid"
        ).fetchall()
    return list(rows)
