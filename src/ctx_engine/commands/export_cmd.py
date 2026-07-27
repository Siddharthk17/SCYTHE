from dataclasses import dataclass, field
from pathlib import Path
import sqlite3

from ctx_engine.mcp_server.tools.renderers import (
    extract_project_snapshot,
    render_claude_md,
    render_copilot_instructions,
    render_opencode_config,
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
    written = []
    skipped = []

    for target in targets:
        if target not in OUTPUT_FILES:
            continue
        rel_path, renderer = OUTPUT_FILES[target]
        path = repo_root / rel_path
        content = renderer(snapshot)
        path.parent.mkdir(parents=True, exist_ok=True)

        if path.exists() and path.read_text(encoding="utf-8") == content:
            skipped.append(str(path.relative_to(repo_root)))
        else:
            path.write_text(content, encoding="utf-8")
            written.append(str(path.relative_to(repo_root)))

    _ensure_gitattributes(repo_root)

    return ExportReport(written=written, skipped=skipped)
