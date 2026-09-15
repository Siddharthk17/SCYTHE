"""`ctx restore` — replace the live index with a named snapshot.

Restoring always auto-saves the current state first, so even a hasty
"yes" is undoable via the generated `pre-restore-*` snapshot.
"""
from pathlib import Path

from ctx_engine.commands.snapshot_cmd import (
    SnapshotError,
    restore_snapshot,
)


def run_restore(repo_root: Path, name: str, no_confirm: bool = False) -> None:
    print(f"ctx restore {name}")
    try:
        restore_snapshot(repo_root, name, no_confirm=no_confirm)
    except (SnapshotError, FileNotFoundError) as err:
        print(f"Error: {err}")
        raise SystemExit(1)
