from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from ctx_engine.mcp_server.tools.renderers import (
    extract_project_snapshot,
    render_claude_md,
    render_copilot_instructions,
    render_opencode_config,
    render_generation_timestamp,
    latest_index_timestamp,
)

GITATTRIBUTES_ENTRIES = {
    "CLAUDE.md": "linguist-generated=true",
    ".github/copilot-instructions.md": "linguist-generated=true",
    ".ctx/opencode.md": "linguist-generated=true",
}

OUTPUT_FILES = {
    "claude": ("CLAUDE.md", render_claude_md),
    "copilot": (".github/copilot-instructions.md", render_copilot_instructions),
    "opencode": (".ctx/opencode.md", render_opencode_config),
}


@dataclass
class ExportReport:
    written: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _parse_ts(ts: str) -> datetime | None:
    """Parse a ``<!-- Generated: ... -->`` ISO-8601 timestamp (Z-suffixed)."""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _ensure_gitattributes(repo_root: Path) -> None:
    gitattributes_path = repo_root / ".gitattributes"
    existing = set()
    if gitattributes_path.exists():
        for line in gitattributes_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                existing.add(stripped)

    missing = []
    for filename, annotation in GITATTRIBUTES_ENTRIES.items():
        entry = f"{filename} {annotation}"
        if entry not in existing:
            missing.append(entry)

    if missing:
        with gitattributes_path.open("a", encoding="utf-8") as f:
            f.write("\n")
            for entry in missing:
                f.write(entry + "\n")


def run_export(
    conn: sqlite3.Connection,
    repo_root: Path,
    targets: set[str] | None = None,
) -> ExportReport:
    if targets is None:
        targets = {"claude", "copilot", "opencode"}

    snapshot = extract_project_snapshot(conn, repo_root)

    # One generation timestamp per export pass keeps all three files in lockstep
    # and makes re-runs byte-idempotent: nothing is rewritten unless the database
    # changed after the file's stored generation time.
    # File timestamps are second-precision while DB carries microseconds, so
    # ceiling the generation time to DB+1s. This guarantees a file written in
    # the same sync pass is never flagged OUTDATED by second-truncation.
    from datetime import timedelta as _td2
    now_dt = datetime.now(timezone.utc)
    db_latest = latest_index_timestamp(conn)
    db_dt = _parse_ts(db_latest) if db_latest else None
    if db_dt is not None and db_dt.tzinfo is None:
        db_dt = db_dt.replace(tzinfo=timezone.utc)
    if db_dt is not None and db_dt + _td2(seconds=1) > now_dt:
        gen_dt = db_dt + _td2(seconds=1)
    else:
        gen_dt = now_dt
    gen_ts = gen_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    written = []
    skipped = []

    for target in targets:
        if target not in OUTPUT_FILES:
            continue
        rel_path, renderer = OUTPUT_FILES[target]
        path = repo_root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)

        stored_ts = render_generation_timestamp(path)
        stored_dt = _parse_ts(stored_ts) if stored_ts else None
        db_changed = stored_dt is None or (db_dt is not None and stored_dt < db_dt)

        if not db_changed:
            # Database unchanged: only rewrite if a manual edit polluted the file.
            try:
                content = renderer(snapshot, gen_ts=stored_ts)
                if path.exists() and path.read_text(encoding="utf-8") == content:
                    skipped.append(str(path.relative_to(repo_root)))
                    continue
            except (IOError, OSError):
                pass

        content = renderer(snapshot, gen_ts=gen_ts)
        path.write_text(content, encoding="utf-8")
        written.append(str(path.relative_to(repo_root)))

    _ensure_gitattributes(repo_root)

    return ExportReport(written=written, skipped=skipped)
