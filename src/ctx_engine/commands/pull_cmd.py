"""`ctx pull` — import team-shared metadata into the local index database.

Conflict resolution is conservative by default: a shared human record that
conflicts with a local human record is skipped with a warning (the local human
may have intentionally refined it). --overwrite-human opts in to preferring the
shared version. Human records always win over local model/auto records.
"""
import json
import sqlite3
from pathlib import Path

from ctx_engine.commands.push_cmd import (
    SHARED_METADATA_SCHEMA_VERSION,
    read_shared_metadata,
)


class SharedMetadataError(ValueError):
    """Raised when shared-metadata.json is missing or its schema is unsupported."""


def _record_text(table: str, record: dict) -> str:
    return record["description"] if table == "dangers" else record["decision"]


def _local_text(table: str, row) -> str:
    return row["description"] if table == "dangers" else row["decision"]


def _record_matches(local_row, shared: dict) -> bool:
    """True if the local row's content equals the shared record's content."""
    local_keys = set(local_row.keys())
    for key, value in shared.items():
        if key == "id":
            continue
        local_value = local_row[key] if key in local_keys else None
        if (local_value or None) != (value or None):
            return False
    return True


def _upsert_record(conn: sqlite3.Connection, table: str, record: dict) -> None:
    created_at = record.get("created_at")
    if table == "dangers":
        conn.execute(
            """INSERT INTO dangers (id, scope, description, reason, added_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   scope = excluded.scope,
                   description = excluded.description,
                   reason = excluded.reason,
                   added_by = excluded.added_by,
                   created_at = excluded.created_at""",
            (
                record["id"], record.get("scope"), record.get("description"),
                record.get("reason"), "human", created_at,
            ),
        )
    else:
        conn.execute(
            """INSERT INTO decisions (id, scope, decision, alternatives, reason, added_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   scope = excluded.scope,
                   decision = excluded.decision,
                   alternatives = excluded.alternatives,
                   reason = excluded.reason,
                   added_by = excluded.added_by,
                   created_at = excluded.created_at""",
            (
                record["id"], record.get("scope"), record.get("decision"),
                record.get("alternatives"), record.get("reason"), "human", created_at,
            ),
        )


def _import_table(
    conn: sqlite3.Connection,
    table: str,
    records: list[dict],
    overwrite_human: bool,
) -> dict:
    """Import one table's shared records. Returns per-record outcome details."""
    imported: list[str] = []
    skipped: list[str] = []
    conflicts: list[dict] = []

    for record in records:
        record_id = record.get("id")
        if not record_id:
            continue
        local = conn.execute(
            f"SELECT * FROM {table} WHERE id = ?", (record_id,)
        ).fetchone()

        if local is None:
            _upsert_record(conn, table, record)
            imported.append(record_id)
        elif _record_matches(local, record):
            skipped.append(record_id)
        elif local["added_by"] == "human" and not overwrite_human:
            conflicts.append({
                "id": record_id,
                "shared_text": _record_text(table, record),
                "local_text": _local_text(table, local),
            })
        else:
            # Human-curated shared records always replace local model/auto
            # records; with --overwrite-human they also replace local humans.
            _upsert_record(conn, table, record)
            imported.append(record_id)

    return {"imported": imported, "skipped": skipped, "conflicts": conflicts}


def pull_metadata(
    conn: sqlite3.Connection,
    repo_root: Path,
    overwrite_human: bool = False,
) -> dict:
    """Import .ctx/shared-metadata.json into the local database.

    Raises SharedMetadataError when the file is missing or its schema_version
    is not recognized. Nothing is committed unless the import succeeds.
    """
    doc = read_shared_metadata(repo_root)
    if doc is None:
        raise SharedMetadataError(
            "No shared metadata found. Run 'ctx push' on a team member's machine first."
        )

    schema_version = doc.get("schema_version")
    if schema_version != SHARED_METADATA_SCHEMA_VERSION:
        raise SharedMetadataError(
            f"Unsupported shared-metadata schema_version: {schema_version!r} "
            f"(this ctx supports version {SHARED_METADATA_SCHEMA_VERSION}). "
            "Upgrade ctx and try again."
        )

    results = {}
    with conn:
        for table in ("dangers", "decisions"):
            records = doc.get(table) or []
            results[table] = _import_table(conn, table, records, overwrite_human)

    return {
        "exported_by": doc.get("exported_by"),
        "exported_at": doc.get("exported_at"),
        "dangers": results["dangers"],
        "decisions": results["decisions"],
    }


def print_pull_report(repo_root: Path, result: dict, export_written: list[str]) -> None:
    repo_name = repo_root.name
    print(f"ctx pull — {repo_name}")
    print()
    print("  Reading: .ctx/shared-metadata.json")
    print(f"    exported by: {result['exported_by']} at {result['exported_at']}")
    print()
    for table in ("dangers", "decisions"):
        res = result[table]
        print(f"  Importing {table}:")
        for rid in res["imported"]:
            print(f"    ✓ {rid} — imported (new)")
        for rid in res["skipped"]:
            print(f"    ✓ {rid} — already present (no change)")
        for conflict in res["conflicts"]:
            print(f"    ⚠ {conflict['id']} — CONFLICT")
            print(f"      Shared:  \"{conflict['shared_text']}\"")
            print(f"      Local:   \"{conflict['local_text']}\"")
            print("      → Keeping local version. Use --overwrite-human to prefer shared.")
        if not (res["imported"] or res["skipped"] or res["conflicts"]):
            print("    (no records)")
        print()

    total_new = len(result["dangers"]["imported"]) + len(result["decisions"]["imported"])
    total_skip = len(result["dangers"]["skipped"]) + len(result["decisions"]["skipped"])
    total_conflict = len(result["dangers"]["conflicts"]) + len(result["decisions"]["conflicts"])
    print(
        f"  Imported: {total_new} new records, {total_skip} skipped (identical), "
        f"{total_conflict} conflict (kept local)"
    )
    print()
    print("  Regenerating output files...")
    for path in export_written:
        print(f"  {path} updated.")
    print()
    print("  Tip: commit .ctx/shared-metadata.json after pushing your own metadata:")
    print('       ctx push && git add .ctx/shared-metadata.json && git commit -m "ctx: sync metadata"')