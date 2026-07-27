import hashlib
import click
from datetime import datetime, timezone

from ctx_engine.db import connect
from ctx_engine.intelligence.heuristics import run_heuristic_detection


def _gen_id(*parts: str) -> str:
    combined = "".join(parts)
    return hashlib.sha256(combined.encode()).hexdigest()[:12]


def danger_add(
    conn,
    scope: str,
    description: str,
    reason: str,
) -> str:
    danger_id = _gen_id(f"{scope}:{description}")

    conn.execute(
        """INSERT INTO dangers (id, scope, description, reason, added_by, created_at)
           VALUES (?, ?, ?, ?, 'human', ?)
           ON CONFLICT(id) DO UPDATE SET
               description = excluded.description,
               reason = excluded.reason""",
        (danger_id, scope, description, reason, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return danger_id


def danger_remove(conn, danger_id: str, confirmed: bool = False) -> str:
    row = conn.execute(
        "SELECT added_by, description FROM dangers WHERE id = ?", (danger_id,)
    ).fetchone()

    if row is None:
        return f"No danger zone found with id '{danger_id}'."

    if row["added_by"] == "human" and not confirmed:
        return (
            f"This danger was added by {row['added_by']}. "
            f"Use --confirm to remove it."
        )

    conn.execute("DELETE FROM dangers WHERE id = ?", (danger_id,))
    conn.commit()
    return f"Removed: {row['description']}"


def danger_list(conn, scope: str | None = None) -> list[dict]:
    if scope:
        rows = conn.execute(
            "SELECT id, scope, description, reason, added_by, created_at FROM dangers "
            "WHERE scope = ? ORDER BY added_by DESC, rowid",
            (scope,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, scope, description, reason, added_by, created_at FROM dangers "
            "ORDER BY added_by DESC, rowid"
        ).fetchall()
    return list(rows)


def danger_detect(conn, repo_root, dry_run: bool = False) -> dict:
    report = run_heuristic_detection(conn, repo_root, dry_run=dry_run)
    return {
        "detected": report.detected,
        "added": report.added,
        "removed": report.removed,
    }
