"""Tests for ctx push / ctx pull team metadata sync (Week 8)."""
import json
import sqlite3

import pytest

from ctx_engine.commands.push_cmd import push_metadata
from ctx_engine.commands.pull_cmd import pull_metadata, SharedMetadataError, print_pull_report
from ctx_engine.commands.export_cmd import run_export
from ctx_engine.db import init_schema


DANGER_COLUMNS = "(id, scope, description, reason, added_by, created_at)"
DECISION_COLUMNS = "(id, scope, decision, alternatives, reason, added_by, created_at)"


@pytest.fixture
def repo(tmp_path):
    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    conn.execute(
        "INSERT INTO files (path, semantic_hash, content_hash, exports, imports, used_by) "
        "VALUES ('a.py', 'sh1', 'ch1', '[\"add\"]', '[]', '[]')"
    )
    conn.commit()
    yield conn, tmp_path
    conn.close()


def _add_danger(conn, did, description, added_by="human"):
    conn.execute(
        f"INSERT INTO dangers {DANGER_COLUMNS} VALUES (?, ?, ?, ?, ?, ?)",
        (did, "a.py", description, "reason", added_by, "2026-06-14T10:00:00Z"),
    )


def _add_decision(conn, did, decision, added_by="human"):
    conn.execute(
        f"INSERT INTO decisions {DECISION_COLUMNS} VALUES (?, ?, ?, ?, ?, ?, ?)",
        (did, "src/", decision, "alts", "reason", added_by, "2026-06-12T14:00:00Z"),
    )


def _write_shared(repo_root, dangers, decisions):
    doc = {
        "schema_version": 1,
        "repo": repo_root.name,
        "exported_at": "2026-06-14T16:00:00Z",
        "exported_by": "teammate",
        "dangers": dangers,
        "decisions": decisions,
    }
    (repo_root / ".ctx" / "shared-metadata.json").write_text(json.dumps(doc))


def _shared_danger(did, description):
    return {
        "id": did, "scope": "a.py", "description": description,
        "reason": "reason", "added_by": "human",
        "created_at": "2026-06-14T10:00:00Z",
    }


# ── ctx push ──────────────────────────────────────────────────────────────────


def test_push_exports_only_human_records(repo):
    conn, repo_root = repo
    _add_danger(conn, "d1", "Human danger 1")
    _add_danger(conn, "d2", "Human danger 2")
    _add_danger(conn, "d3", "Human danger 3")
    _add_danger(conn, "m1", "Model danger", added_by="model")
    _add_danger(conn, "a1", "Auto danger", added_by="auto")
    _add_decision(conn, "dec1", "Human decision 1")
    _add_decision(conn, "dec2", "Human decision 2")
    _add_decision(conn, "mdec", "Model decision", added_by="model")
    conn.commit()

    result = push_metadata(conn, repo_root)
    assert result["danger_count"] == 3
    assert result["decision_count"] == 2

    doc = json.loads((repo_root / ".ctx" / "shared-metadata.json").read_text())
    assert doc["schema_version"] == 1
    assert {d["id"] for d in doc["dangers"]} == {"d1", "d2", "d3"}
    assert {d["id"] for d in doc["decisions"]} == {"dec1", "dec2"}
    assert all(d["added_by"] == "human" for d in doc["dangers"])


def test_push_is_idempotent(repo):
    conn, repo_root = repo
    _add_danger(conn, "d1", "Human danger 1")
    _add_decision(conn, "dec1", "Human decision 1")
    conn.commit()

    first = push_metadata(conn, repo_root)
    assert not first["already_current"]
    path = repo_root / ".ctx" / "shared-metadata.json"
    content1 = path.read_text()
    mtime1 = path.stat().st_mtime_ns

    second = push_metadata(conn, repo_root)
    assert second["already_current"]
    assert path.read_text() == content1
    assert path.stat().st_mtime_ns == mtime1  # file untouched


def test_push_updates_gitignore_additively(repo):
    conn, repo_root = repo
    (repo_root / ".gitignore").write_text("__pycache__/\n.ctx/\n*.pyc\n", encoding="utf-8")
    push_metadata(conn, repo_root)
    lines = (repo_root / ".gitignore").read_text().splitlines()
    # Existing entries preserved, exception added right after the .ctx/ exclusion.
    assert lines[0] == "__pycache__/"
    assert ".ctx/" in lines
    assert "!.ctx/shared-metadata.json" in lines
    assert lines.index("!.ctx/shared-metadata.json") == lines.index(".ctx/") + 1
    assert "*.pyc" in lines


def test_push_adds_block_when_no_gitignore(repo):
    conn, repo_root = repo
    push_metadata(conn, repo_root)
    lines = (repo_root / ".gitignore").read_text().splitlines()
    assert ".ctx/" in lines
    assert "!.ctx/shared-metadata.json" in lines


# ── ctx pull ──────────────────────────────────────────────────────────────────


def test_pull_new_identical_and_conflict(repo):
    conn, repo_root = repo
    # 1 new (not present locally), 1 identical to local, 1 conflicting with a
    # local human record.
    _add_danger(conn, "d_same", "Same content danger")
    _add_danger(conn, "d_conflict", "Local version of the danger")
    conn.commit()

    _write_shared(repo_root, [
        _shared_danger("d_new", "Shared new danger"),
        _shared_danger("d_same", "Same content danger"),
        _shared_danger("d_conflict", "Shared version of the danger"),
    ], [])

    result = pull_metadata(conn, repo_root)
    res = result["dangers"]
    assert res["imported"] == ["d_new"]
    assert res["skipped"] == ["d_same"]
    assert [c["id"] for c in res["conflicts"]] == ["d_conflict"]

    # Conflict resolution keeps the LOCAL human version by default.
    row = conn.execute(
        "SELECT description FROM dangers WHERE id = 'd_conflict'"
    ).fetchone()
    assert row["description"] == "Local version of the danger"


def test_pull_overwrite_human(repo):
    conn, repo_root = repo
    _add_danger(conn, "d_conflict", "Local version of the danger")
    conn.commit()

    _write_shared(repo_root, [
        _shared_danger("d_conflict", "Shared version of the danger"),
    ], [])

    result = pull_metadata(conn, repo_root, overwrite_human=True)
    assert result["dangers"]["imported"] == ["d_conflict"]
    row = conn.execute(
        "SELECT description FROM dangers WHERE id = 'd_conflict'"
    ).fetchone()
    assert row["description"] == "Shared version of the danger"


def test_pull_human_wins_over_model_record(repo):
    conn, repo_root = repo
    _add_danger(conn, "d_mixed", "Model version", added_by="model")
    conn.commit()

    _write_shared(repo_root, [
        _shared_danger("d_mixed", "Human version"),
    ], [])

    result = pull_metadata(conn, repo_root)
    # Human-curated shared record always overwrites a local model record.
    assert result["dangers"]["imported"] == ["d_mixed"]
    row = conn.execute(
        "SELECT description, added_by FROM dangers WHERE id = 'd_mixed'"
    ).fetchone()
    assert row["description"] == "Human version"
    assert row["added_by"] == "human"


def test_pull_dry_run_writes_nothing(repo, capsys):
    """--dry-run previews identical outcomes but leaves the DB untouched."""
    conn, repo_root = repo
    _add_danger(conn, "d_same", "Same content danger")
    conn.commit()

    _write_shared(repo_root, [
        _shared_danger("d_new", "Shared new danger"),
        _shared_danger("d_same", "Same content danger"),
    ], [])

    result = pull_metadata(conn, repo_root, dry_run=True)
    assert result["dry_run"] is True
    assert result["dangers"]["imported"] == ["d_new"]
    assert result["dangers"]["skipped"] == ["d_same"]

    # Database unchanged: the "new" record was not inserted.
    assert conn.execute("SELECT COUNT(*) FROM dangers").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM dangers WHERE id = 'd_new'"
    ).fetchone()[0] == 0

    print_pull_report(repo_root, result, [], dry_run=True)
    out = capsys.readouterr().out
    assert "DRY RUN" in out


def test_pull_unrecognized_schema_version(repo):
    conn, repo_root = repo
    _write_shared(repo_root, [], [])
    path = repo_root / ".ctx" / "shared-metadata.json"
    doc = json.loads(path.read_text())
    doc["schema_version"] = 999
    path.write_text(json.dumps(doc))

    before = conn.execute("SELECT COUNT(*) FROM dangers").fetchone()[0]
    with pytest.raises(SharedMetadataError):
        pull_metadata(conn, repo_root)
    after = conn.execute("SELECT COUNT(*) FROM dangers").fetchone()[0]
    assert before == after  # no imports on unsupported schema


def test_pull_missing_file(repo):
    conn, repo_root = repo
    with pytest.raises(SharedMetadataError, match="No shared metadata"):
        pull_metadata(conn, repo_root)


def test_pull_then_export_updates_claude_md(repo):
    """After pull, running export writes the newly imported danger to CLAUDE.md."""
    conn, repo_root = repo
    _write_shared(repo_root, [_shared_danger("d_exp", "Exported shared danger")], [])

    (repo_root / "a.py").write_text("def add(a, b): return a + b\n")
    result = pull_metadata(conn, repo_root)
    export_report = run_export(conn, repo_root)

    assert export_report.written  # CLAUDE.md (and friends) were written
    claude_md = (repo_root / "CLAUDE.md").read_text()
    assert "Exported shared danger" in claude_md


def test_push_pull_roundtrip(repo):
    """Acceptance-criteria flow: push, delete locally, pull, records restored."""
    conn, repo_root = repo
    _add_danger(conn, "rt_d", "Roundtrip danger")
    _add_decision(conn, "rt_dec", "Roundtrip decision")
    conn.commit()

    push_metadata(conn, repo_root)

    conn.execute("DELETE FROM dangers WHERE id = 'rt_d'")
    conn.execute("DELETE FROM decisions WHERE id = 'rt_dec'")
    conn.commit()

    pull_metadata(conn, repo_root)

    assert conn.execute(
        "SELECT COUNT(*) FROM dangers WHERE id = 'rt_d'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM decisions WHERE id = 'rt_dec'"
    ).fetchone()[0] == 1


def test_pull_report_mentions_conflict(repo, capsys):
    conn, repo_root = repo
    _add_danger(conn, "d_conflict", "Local version")
    conn.commit()
    _write_shared(repo_root, [_shared_danger("d_conflict", "Shared version")], [])
    result = pull_metadata(conn, repo_root)
    print_pull_report(repo_root, result, [])
    out = capsys.readouterr().out
    assert "CONFLICT" in out
    assert "Keeping local version" in out
    assert "--overwrite-human" in out


