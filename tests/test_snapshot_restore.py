"""Tests for ctx snapshot / ctx restore (Week 9)."""
import json
import sqlite3

import pytest

from ctx_engine.commands.snapshot_cmd import (
    SnapshotError,
    create_snapshot,
    delete_snapshot,
    list_snapshots,
    restore_snapshot,
    run_snapshot_create,
    run_snapshot_delete,
    run_snapshot_list,
    snapshot_database,
    validate_snapshot_name,
)
from ctx_engine.commands.restore_cmd import run_restore
from ctx_engine.db import init_schema


@pytest.fixture
def snap_repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    ctx_dir = root / ".ctx"
    ctx_dir.mkdir()
    conn = sqlite3.connect(ctx_dir / "index.db")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    conn.execute(
        "INSERT INTO files (path, system, semantic_hash, content_hash) "
        "VALUES ('a.py', 'search', 'sh', 'ch')"
    )
    conn.execute(
        "INSERT INTO functions (id, file, name, signature, line_start, "
        "line_end, semantic_hash) "
        "VALUES ('a.py::f', 'a.py', 'f', 'sig', 1, 2, 'sh')"
    )
    conn.commit()
    conn.close()
    yield root


def _counts(path):
    conn = sqlite3.connect(path)
    files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    funcs = conn.execute("SELECT COUNT(*) FROM functions").fetchone()[0]
    conn.close()
    return files, funcs


def test_snapshot_creates_db_and_sidecar(snap_repo, capsys):
    run_snapshot_create(snap_repo, "before-refactor")
    assert (snap_repo / ".ctx" / "snapshots" / "before-refactor.db").exists()
    meta = json.loads(
        (snap_repo / ".ctx" / "snapshots" / "before-refactor.json").read_text()
    )
    assert meta["name"] == "before-refactor"
    assert meta["file_count"] == 1
    assert meta["function_count"] == 1
    assert meta["db_size_bytes"] > 0
    assert "created_at" in meta
    out = capsys.readouterr().out
    assert "Snapshot created" in out


def test_name_validation():
    validate_snapshot_name("my-snapshot")
    validate_snapshot_name("snap_01")
    validate_snapshot_name("ABC123-_")
    with pytest.raises(SnapshotError):
        validate_snapshot_name("my snapshot")
    with pytest.raises(SnapshotError):
        validate_snapshot_name("snap/shot")
    with pytest.raises(SnapshotError):
        validate_snapshot_name("")


def test_snapshot_preserves_row_counts(snap_repo):
    create_snapshot(snap_repo, "s1")
    live = _counts(snap_repo / ".ctx" / "index.db")
    snap = _counts(snap_repo / ".ctx" / "snapshots" / "s1.db")
    assert live == snap == (1, 1)


def test_snapshot_list_output(snap_repo, capsys):
    create_snapshot(snap_repo, "s1")
    create_snapshot(snap_repo, "s2")
    run_snapshot_list(snap_repo)
    out = capsys.readouterr().out
    assert "SNAPSHOTS (2)" in out
    assert "s1" in out and "s2" in out
    assert "Total:" in out


def test_snapshot_list_empty(tmp_path, capsys):
    root = tmp_path / "empty"
    root.mkdir()
    run_snapshot_list(root)
    assert "No snapshots found" in capsys.readouterr().out


def test_delete_no_confirm(snap_repo):
    create_snapshot(snap_repo, "gone")
    run_snapshot_delete(snap_repo, "gone", no_confirm=True)
    assert not (snap_repo / ".ctx" / "snapshots" / "gone.db").exists()
    assert not (snap_repo / ".ctx" / "snapshots" / "gone.json").exists()


def test_delete_with_confirmation(snap_repo, monkeypatch):
    create_snapshot(snap_repo, "gone")
    monkeypatch.setattr("builtins.input", lambda _: "y")
    delete_snapshot(snap_repo, "gone", no_confirm=False)
    assert not (snap_repo / ".ctx" / "snapshots" / "gone.db").exists()


def test_delete_aborted(snap_repo, monkeypatch):
    create_snapshot(snap_repo, "keep")
    monkeypatch.setattr("builtins.input", lambda _: "n")
    delete_snapshot(snap_repo, "keep", no_confirm=False)
    assert (snap_repo / ".ctx" / "snapshots" / "keep.db").exists()


def test_delete_missing_raises(snap_repo):
    with pytest.raises(SnapshotError):
        delete_snapshot(snap_repo, "ghost", no_confirm=True)


def test_restore_auto_saves_and_restores(snap_repo):
    create_snapshot(snap_repo, "base")
    conn = sqlite3.connect(snap_repo / ".ctx" / "index.db")
    conn.execute("UPDATE files SET system = 'test_override' WHERE path = 'a.py'")
    conn.commit()
    conn.close()

    restore_snapshot(snap_repo, "base", no_confirm=True)

    conn = sqlite3.connect(snap_repo / ".ctx" / "index.db")
    system = conn.execute("SELECT system FROM files WHERE path = 'a.py'").fetchone()[0]
    conn.close()
    assert system == "search"

    autos = list((snap_repo / ".ctx" / "snapshots").glob("pre-restore-*.db"))
    assert len(autos) == 1
    assert "pre-restore-" in list_snapshots(snap_repo)[0]["name"]


def test_restore_no_confirm_skips_prompt_but_autosaves(snap_repo, monkeypatch):
    create_snapshot(snap_repo, "base")
    prompted = []
    monkeypatch.setattr("builtins.input", lambda _: prompted.append(1) or "yes")
    run_restore(snap_repo, "base", no_confirm=True)
    assert prompted == []
    assert len(list((snap_repo / ".ctx" / "snapshots").glob("pre-restore-*.db"))) == 1


def test_restore_missing_leaves_db_untouched(snap_repo):
    before = _counts(snap_repo / ".ctx" / "index.db")
    with pytest.raises(SnapshotError):
        restore_snapshot(snap_repo, "ghost", no_confirm=True)
    assert _counts(snap_repo / ".ctx" / "index.db") == before


def test_gitignore_updated(snap_repo):
    create_snapshot(snap_repo, "s1")
    assert ".ctx/snapshots/" in (snap_repo / ".gitignore").read_text()


def test_snapshot_database_uses_backup_api(snap_repo):
    """Copied DB must be a valid, queryable database with identical tables."""
    create_snapshot(snap_repo, "s1")
    snap = snap_repo / ".ctx" / "snapshots" / "s1.db"
    live_tables = sorted(
        r[0] for r in sqlite3.connect(snap_repo / ".ctx" / "index.db").execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    )
    snap_tables = sorted(
        r[0] for r in sqlite3.connect(snap).execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    )
    assert live_tables == snap_tables
    assert "files" in snap_tables and "functions" in snap_tables
