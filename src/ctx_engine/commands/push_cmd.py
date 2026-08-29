"""`ctx push` — export human-curated metadata to .ctx/shared-metadata.json.

Only human-added danger zones and architectural decisions are exported:
auto-detected records are regenerated locally and model-added records are
session-specific. The shared file is git-trackable so teams can commit the
knowledge layer while keeping the database itself local.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

SHARED_METADATA_SCHEMA_VERSION = 1

GITIGNORE_EXCEPTION = "!.ctx/shared-metadata.json"


def read_shared_metadata(repo_root: Path) -> dict | None:
    """Read and return the parsed shared-metadata.json, or None if absent."""
    path = repo_root / ".ctx" / "shared-metadata.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def build_shared_metadata(conn, repo_root: Path) -> dict:
    """Build the shared-metadata document from human-curated records."""
    danger_rows = conn.execute(
        "SELECT id, scope, description, reason, added_by, created_at "
        "FROM dangers WHERE added_by = 'human' ORDER BY rowid"
    ).fetchall()
    decision_rows = conn.execute(
        "SELECT id, scope, decision, alternatives, reason, added_by, created_at "
        "FROM decisions WHERE added_by = 'human' ORDER BY rowid"
    ).fetchall()
    return {
        "schema_version": SHARED_METADATA_SCHEMA_VERSION,
        "repo": repo_root.name,
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "exported_by": Path.home().name,
        "dangers": [dict(r) for r in danger_rows],
        "decisions": [dict(r) for r in decision_rows],
    }


def _canonical(doc: dict) -> str:
    """Stable serialization used for the idempotency check (ignores exported_at/by)."""
    stripped = {k: v for k, v in doc.items() if k not in ("exported_at", "exported_by")}
    return json.dumps(stripped, sort_keys=True, indent=2)


def _update_gitignore(repo_root: Path) -> bool:
    """Ensure .ctx/shared-metadata.json is trackable despite a .ctx/ exclusion.

    Additive only: never removes or reorders existing entries. Returns True if
    the file was modified.
    """
    gitignore_path = repo_root / ".gitignore"
    current = gitignore_path.read_text(encoding="utf-8") if gitignore_path.exists() else ""
    lines = current.splitlines()

    if any(line.strip() == GITIGNORE_EXCEPTION for line in lines):
        return False

    # Find the last line that excludes .ctx/ so the exception lands directly
    # after it (git negation must come after the exclusion it overrides).
    exclusion_idx = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped in (".ctx/", ".ctx/*") or stripped.startswith(".ctx/*,"):
            exclusion_idx = i

    if exclusion_idx is not None:
        lines.insert(exclusion_idx + 1, GITIGNORE_EXCEPTION)
        gitignore_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return True

    if current and not current.endswith("\n"):
        current += "\n"
    current += ".ctx/\n" + GITIGNORE_EXCEPTION + "\n"
    gitignore_path.write_text(current, encoding="utf-8")
    return True


def push_metadata(conn, repo_root: Path) -> dict:
    """Export human-curated metadata. Returns a summary dict.

    Idempotent: if the on-disk file already matches the current records
    (ignoring export timestamps), nothing is rewritten.
    """
    doc = build_shared_metadata(conn, repo_root)
    out_path = repo_root / ".ctx" / "shared-metadata.json"

    already_current = False
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
            already_current = _canonical(existing) == _canonical(doc)
        except (json.JSONDecodeError, OSError):
            already_current = False

    gitignore_changed = False
    if not already_current:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        gitignore_changed = _update_gitignore(repo_root)

    return {
        "danger_count": len(doc["dangers"]),
        "decision_count": len(doc["decisions"]),
        "already_current": already_current,
        "gitignore_changed": gitignore_changed,
        "path": out_path,
    }


def print_push_report(repo_root: Path, result: dict) -> None:
    repo_name = repo_root.name
    print(f"ctx push — {repo_name}")
    print()
    if result["already_current"]:
        print("  already current — no changes.")
        return
    print("  Exported:")
    print(f"    {result['danger_count']} human-added danger zones")
    print(f"    {result['decision_count']} human-added architectural decisions")
    print()
    print(f"  Written: {result['path'].relative_to(repo_root)}")
    print()
    if result["gitignore_changed"]:
        print("  .gitignore updated: .ctx/shared-metadata.json is now trackable.")
        print()
    print("  Stage and commit to share with your team:")
    print("    git add .ctx/shared-metadata.json")
    print('    git commit -m "ctx: share team metadata"')
