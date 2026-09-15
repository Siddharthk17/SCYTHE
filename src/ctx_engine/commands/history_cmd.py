"""`ctx history` — timeline queries over the `changes` table.

The longitudinal view of what changed and who changed it, complementing
`ctx diff`'s point-in-time view. Filters: file, author, date range,
system, limit. Supports text and JSON output.
"""
import json
import sqlite3
from pathlib import Path

from ctx_engine.db import connect

DEFAULT_LIMIT = 50


def build_history_query(
    file: str | None,
    author: str | None,
    since: str | None,
    until: str | None,
    system: str | None,
    limit: int,
) -> tuple[str, list]:
    conditions: list[str] = []
    params: list = []

    if file:
        conditions.append("c.file = ?")
        params.append(file)

    if author and author != "all":
        conditions.append("c.author = ?")
        params.append(author)

    if since:
        conditions.append("c.timestamp >= ?")
        params.append(since)

    if until:
        conditions.append("c.timestamp <= ?")
        params.append(until)

    if system:
        conditions.append("c.file IN (SELECT path FROM files WHERE system = ?)")
        params.append(system)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = f"""
        SELECT c.file, c.commit_hash, c.summary, c.author, c.timestamp
        FROM changes c
        {where}
        ORDER BY c.timestamp DESC
        LIMIT ?
    """
    params.append(limit)
    return sql, params


def count_history_matches(
    conn: sqlite3.Connection,
    file: str | None,
    author: str | None,
    since: str | None,
    until: str | None,
    system: str | None,
) -> int:
    conditions: list[str] = []
    params: list = []
    if file:
        conditions.append("c.file = ?")
        params.append(file)
    if author and author != "all":
        conditions.append("c.author = ?")
        params.append(author)
    if since:
        conditions.append("c.timestamp >= ?")
        params.append(since)
    if until:
        conditions.append("c.timestamp <= ?")
        params.append(until)
    if system:
        conditions.append("c.file IN (SELECT path FROM files WHERE system = ?)")
        params.append(system)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    row = conn.execute(
        f"SELECT COUNT(*) FROM changes c {where}", params
    ).fetchone()
    return row[0]


def metadata_updated_for(
    conn: sqlite3.Connection, file: str, timestamp: str | None
) -> bool:
    """True when the file's metadata postdates the change.

    A model-authored change followed by no index update leaves the file
    stale — the `← ctx audit-model shows:` annotation surfaces that inline.
    """
    row = conn.execute(
        "SELECT is_stale, updated_at FROM files WHERE path = ?", (file,)
    ).fetchone()
    if row is None:
        return False
    if row["is_stale"]:
        return False
    updated_at = row["updated_at"]
    if not updated_at or not timestamp:
        return True
    return updated_at >= timestamp


def describe_query(
    file: str | None, author: str | None, system: str | None
) -> str:
    if file:
        return f"file: {file}"
    if system:
        return f"system: {system}"
    if author and author != "all":
        return f"author: {author}"
    return "all files"


def print_text_history(
    conn: sqlite3.Connection,
    entries: list[dict],
    total: int,
    limit: int,
    file: str | None,
    author: str | None,
    system: str | None,
) -> None:
    scope = describe_query(file, author, system)
    header = "MODEL-AUTHORED CHANGES" if author == "model" else "HISTORY"
    print(f"ctx history — {scope} (last {len(entries)} entries)")
    print()
    print(f"  {header} — {scope} (last {len(entries)} entries)")
    print()
    for entry in entries:
        short_hash = (entry["commit_hash"] or "?")[:7]
        print(
            f"  {entry['timestamp'] or '?'}  {short_hash}  "
            f"{entry['author'] or '?':<7} {entry['file']}"
        )
        print(f"    \"{entry['summary'] or ''}\"")
        if entry["author"] == "model":
            if metadata_updated_for(conn, entry["file"], entry["timestamp"]):
                print("    ← metadata is current (ctx_update_function was called)")
            else:
                print("    ← ctx audit-model shows: metadata was NOT updated after this change")
        print()
    print(f"  Showing {len(entries)} of {total} total entries for {scope}")
    if total > len(entries):
        print(f"  Run with --limit {total} to see all.")


def run_history(
    repo_root: Path,
    file: str | None = None,
    author: str | None = None,
    since: str | None = None,
    until: str | None = None,
    system: str | None = None,
    limit: int = DEFAULT_LIMIT,
    format: str = "text",
) -> None:
    db_path = repo_root / ".ctx" / "index.db"
    if not db_path.exists():
        raise FileNotFoundError("Database not found. Run 'ctx init' first.")

    if author not in (None, "human", "model", "all"):
        raise ValueError("--author must be one of: human, model, all.")

    conn = connect(db_path)
    try:
        sql, params = build_history_query(file, author, since, until, system, limit)
        rows = conn.execute(sql, params).fetchall()
        entries = [dict(r) for r in rows]
        total = count_history_matches(conn, file, author, since, until, system)

        if not entries:
            print(
                "No history found. Install hooks and make commits to populate the history."
            )
            return

        if format == "json":
            payload = {
                "query": {
                    "file": file,
                    "author": author,
                    "since": since,
                    "until": until,
                    "system": system,
                    "limit": limit,
                },
                "total_matching": total,
                "shown": len(entries),
                "entries": [
                    {
                        "file": e["file"],
                        "commit_hash": e["commit_hash"],
                        "summary": e["summary"],
                        "author": e["author"],
                        "timestamp": e["timestamp"],
                        "metadata_updated": metadata_updated_for(
                            conn, e["file"], e["timestamp"]
                        ),
                    }
                    for e in entries
                ],
            }
            print(json.dumps(payload, indent=2))
        else:
            # Fetch staleness annotations before closing the connection.
            print_text_history(conn, entries, total, limit, file, author, system)
    finally:
        conn.close()
