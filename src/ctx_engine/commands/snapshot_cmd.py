"""`ctx snapshot` / `ctx restore` — database state management.

Snapshots are consistent hot copies of `.ctx/index.db` taken with SQLite's
online backup API (never a filesystem copy — the WAL-mode database may be
mid-write). Restoring always auto-saves the current state first, so every
restore is undoable.
"""
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ctx_engine.db import connect

SNAPSHOT_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


class SnapshotError(ValueError):
    pass


def snapshots_dir(repo_root: Path) -> Path:
    return repo_root / ".ctx" / "snapshots"


def validate_snapshot_name(name: str) -> None:
    if not SNAPSHOT_NAME_RE.match(name):
        raise SnapshotError(
            f"Invalid snapshot name: '{name}'. "
            "Use only letters, digits, '-' and '_' (no spaces or slashes)."
        )


def snapshot_database(db_path: Path, snapshot_path: Path) -> None:
    """Copy a live SQLite database consistently via the backup API."""
    src = sqlite3.connect(str(db_path))
    try:
        dst = sqlite3.connect(str(snapshot_path))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def db_counts(db_path: Path) -> tuple[int, int]:
    conn = connect(db_path)
    try:
        files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        funcs = conn.execute("SELECT COUNT(*) FROM functions").fetchone()[0]
    finally:
        conn.close()
    return files, funcs


def snapshot_metadata_path(repo_root: Path, name: str) -> Path:
    return snapshots_dir(repo_root) / f"{name}.json"


def snapshot_db_path(repo_root: Path, name: str) -> Path:
    return snapshots_dir(repo_root) / f"{name}.db"


def read_snapshot_metadata(repo_root: Path, name: str) -> dict:
    meta_path = snapshot_metadata_path(repo_root, name)
    if not meta_path.exists():
        raise SnapshotError(f"Snapshot not found: '{name}'.")
    return json.loads(meta_path.read_text(encoding="utf-8"))


def list_snapshots(repo_root: Path) -> list[dict]:
    """Return snapshot metadata dicts sorted newest first."""
    directory = snapshots_dir(repo_root)
    if not directory.exists():
        return []
    entries: list[dict] = []
    for meta_path in sorted(directory.glob("*.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        db_file = meta_path.with_suffix(".db")
        meta["_size"] = db_file.stat().st_size if db_file.exists() else 0
        entries.append(meta)
    entries.sort(key=lambda m: m.get("created_at", ""), reverse=True)
    return entries


def format_size(num_bytes: int) -> str:
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.1f}MB"
    if num_bytes >= 1024:
        return f"{num_bytes / 1024:.1f}KB"
    return f"{num_bytes}B"


def ensure_snapshots_gitignored(repo_root: Path) -> None:
    entry = ".ctx/snapshots/"
    gitignore = repo_root / ".gitignore"
    if gitignore.exists():
        existing = gitignore.read_text(encoding="utf-8")
        if entry in existing:
            return
        with open(gitignore, "a", encoding="utf-8") as handle:
            if existing and not existing.endswith("\n"):
                handle.write("\n")
            handle.write(entry + "\n")
    else:
        gitignore.write_text(entry + "\n", encoding="utf-8")


def create_snapshot(
    repo_root: Path, name: str, description: str = ""
) -> dict:
    """Take a snapshot. Returns the metadata dict that was written."""
    validate_snapshot_name(name)
    db_path = repo_root / ".ctx" / "index.db"
    if not db_path.exists():
        raise FileNotFoundError("Database not found. Run 'ctx init' first.")
    target = snapshot_db_path(repo_root, name)
    if target.exists():
        raise SnapshotError(
            f"Snapshot '{name}' already exists. "
            f"Delete it first ('ctx snapshot delete {name}') or pick another name."
        )
    snapshots_dir(repo_root).mkdir(parents=True, exist_ok=True)
    snapshot_database(db_path, target)
    file_count, function_count = db_counts(target)
    meta = {
        "name": name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "file_count": file_count,
        "function_count": function_count,
        "db_size_bytes": target.stat().st_size,
        "description": description,
    }
    snapshot_metadata_path(repo_root, name).write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )
    ensure_snapshots_gitignored(repo_root)
    return meta


def delete_snapshot(
    repo_root: Path, name: str, no_confirm: bool = False
) -> None:
    validate_snapshot_name(name)
    target = snapshot_db_path(repo_root, name)
    meta_path = snapshot_metadata_path(repo_root, name)
    if not target.exists():
        raise SnapshotError(f"Snapshot not found: '{name}'.")
    size = target.stat().st_size
    created = ""
    if meta_path.exists():
        try:
            created = json.loads(meta_path.read_text(encoding="utf-8")).get("created_at", "")
        except (json.JSONDecodeError, OSError):
            pass
    if not no_confirm:
        answer = input(
            f"\n  Delete snapshot '{name}' ({format_size(size)}, created {created})?\n"
            "  This cannot be undone. [y/N]: "
        ).strip().lower()
        if answer not in ("y", "yes"):
            print("  Aborted.")
            return
    target.unlink()
    if meta_path.exists():
        meta_path.unlink()
    print(f"  Deleted: .ctx/snapshots/{name}.db")


def restore_snapshot(
    repo_root: Path, name: str, no_confirm: bool = False
) -> None:
    validate_snapshot_name(name)
    db_path = repo_root / ".ctx" / "index.db"
    if not db_path.exists():
        raise FileNotFoundError("Database not found. Run 'ctx init' first.")
    target = snapshot_db_path(repo_root, name)
    if not target.exists():
        raise SnapshotError(f"Snapshot not found: '{name}'. No changes made.")
    meta = read_snapshot_metadata(repo_root, name)
    cur_files, cur_funcs = db_counts(db_path)

    if not no_confirm:
        print()
        print(f"  WARNING: This will replace your current index with snapshot '{name}'")
        print(f"  Created: {meta.get('created_at')}")
        print(f"  Current index: {cur_files} files, {cur_funcs} functions")
        print(
            f"  Snapshot:      {meta.get('file_count')} files, "
            f"{meta.get('function_count')} functions"
        )
        print()
        confirm = input("  Type 'yes' to confirm: ").strip()
        if confirm != "yes":
            print("  Aborted.")
            return

    auto_name = f"pre-restore-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
    auto_path = snapshots_dir(repo_root) / f"{auto_name}.db"
    snapshots_dir(repo_root).mkdir(parents=True, exist_ok=True)
    snapshot_database(db_path, auto_path)
    auto_files, auto_funcs = db_counts(auto_path)
    snapshot_metadata_path(repo_root, auto_name).write_text(
        json.dumps({
            "name": auto_name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "file_count": auto_files,
            "function_count": auto_funcs,
            "db_size_bytes": auto_path.stat().st_size,
            "description": f"auto-saved before restoring '{name}'",
        }, indent=2),
        encoding="utf-8",
    )
    print(f"  Auto-saved current state to: {auto_name}")

    snapshot_database(target, db_path)
    print(f"  Restored: .ctx/index.db  ← {name}")
    print()
    print(f"  Index restored to {meta.get('created_at')} state.")
    print(f"  Run 'ctx status' to verify, or 'ctx restore {auto_name}' to undo.")


def run_snapshot_create(repo_root: Path, name: str) -> None:
    try:
        meta = create_snapshot(repo_root, name)
    except (SnapshotError, FileNotFoundError) as err:
        print(f"Error: {err}")
        raise SystemExit(1)
    print(f"ctx snapshot {name}")
    print()
    print(f"  Snapshot created: .ctx/snapshots/{name}.db")
    print(
        f"  Files: {meta['file_count']}  "
        f"Functions: {meta['function_count']}  "
        f"Size: {format_size(meta['db_size_bytes'])}"
    )
    print(f"  Time: {meta['created_at']}")
    print()
    print(f"  To restore: ctx restore {name}")


def run_snapshot_list(repo_root: Path) -> None:
    entries = list_snapshots(repo_root)
    if not entries:
        print("No snapshots found. Run 'ctx snapshot <name>' to create one.")
        return
    total = sum(e["_size"] for e in entries)
    print(f"ctx snapshot list")
    print()
    print(f"  SNAPSHOTS ({len(entries)})")
    print()
    for meta in entries:
        print(
            f"  {meta['name']:<24} {meta.get('created_at', '?'):<26} "
            f"{format_size(meta['_size']):>7}  "
            f"{meta.get('file_count', '?')} files, {meta.get('function_count', '?')} functions"
        )
    print()
    print(f"  Total: {format_size(total)} in .ctx/snapshots/")


def run_snapshot_delete(repo_root: Path, name: str, no_confirm: bool = False) -> None:
    try:
        print(f"ctx snapshot delete {name}")
        delete_snapshot(repo_root, name, no_confirm=no_confirm)
    except (SnapshotError, FileNotFoundError) as err:
        print(f"Error: {err}")
        raise SystemExit(1)
