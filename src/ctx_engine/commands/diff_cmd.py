"""`ctx diff` — state comparison between on-disk code and the indexed state.

Two modes:
1. No arguments: compares the on-disk files against .ctx/index.db (what has
   changed since the last `ctx init` or `ctx update`).
2. Two commit args: shows what the `changes` table records between two git
   commits (the AI oversight view).
"""
import hashlib
import sqlite3
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from ctx_engine.db import connect
from ctx_engine.discovery import discover_parseable_files
from ctx_engine.hashing import file_semantic_hash, function_semantic_hash
from ctx_engine.languages.registry import get_parser
from ctx_engine.reindex import ADAPTERS


@dataclass
class FileDiff:
    path: str
    change_type: str  # "semantic" | "formatting" | "added" | "removed" | "new"
    added_functions: list[str] = field(default_factory=list)
    removed_functions: list[str] = field(default_factory=list)
    changed_functions: list[str] = field(default_factory=list)


@dataclass
class DiffReport:
    mode: str
    changed: list[FileDiff] = field(default_factory=list)
    new: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)


@dataclass
class AuditReport:
    commit1: str
    commit2: str
    by_author: dict[str, list[dict]] = field(default_factory=dict)
    total: int = 0
    not_recorded: list[dict] = field(default_factory=list)


def _build_function_id(path: str, fn, seen: set[str] | None = None) -> str:
    """Build a function id matching the convention used by the reindex pipeline.

    Format: '<file_path>::<ClassName>.<name>' (or '<file_path>::<name>' if no class).
    Disambiguated by line_start when collisions occur.
    """
    qualified = f"{fn.class_name}.{fn.name}" if fn.class_name else fn.name
    base = f"{path}::{qualified}"
    if seen is not None:
        if base in seen:
            return f"{base}@{fn.line_start}"
        seen.add(base)
    return base


def _resolve_commit(repo_root: Path, ref: str) -> str:
    """Resolve a git ref to a full commit hash."""
    result = subprocess.run(
        ["git", "rev-parse", ref],
        cwd=repo_root, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise ValueError(f"Cannot resolve commit ref '{ref}'")
    return result.stdout.strip()


def _get_commits_between(repo_root: Path, commit1: str, commit2: str) -> list[str]:
    """Return the list of commit hashes between commit1 (exclusive) and commit2 (inclusive)."""
    # `git log commit1..commit2` gives commit1's successors up to commit2.
    result = subprocess.run(
        ["git", "log", "--format=%H", f"{commit1}..{commit2}"],
        cwd=repo_root, capture_output=True, text=True,
    )
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _get_commit_subject(repo_root: Path, commit_hash: str) -> str:
    """Return the subject (first line) of a commit message."""
    result = subprocess.run(
        ["git", "log", "--format=%s", "-1", commit_hash],
        cwd=repo_root, capture_output=True, text=True,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _diff_file_semantic(
    path: str,
    abs_path: Path,
    language: str,
    new_content_hash: str,
    indexed_content_hash: str,
    indexed_sem_hash: str,
    conn: sqlite3.Connection,
) -> FileDiff | None:
    """Compute the per-function diff for a single file that has changed content."""
    try:
        source = abs_path.read_bytes()
    except (IOError, OSError):
        return None
    if indexed_content_hash == new_content_hash:
        return None  # bytes-identical; nothing to do

    parser = get_parser(language)
    tree = parser.parse(source)
    new_sem_hash = file_semantic_hash(tree, source, language)
    if new_sem_hash == indexed_sem_hash:
        # Identical AST, only formatting changed
        return FileDiff(path=path, change_type="formatting")

    # Semantic change — compute per-function diff
    adapter = ADAPTERS[language]
    struct = adapter.extract(tree, source)
    seen: set[str] = set()
    new_fn_ids = {_build_function_id(path, fn, seen) for fn in struct.functions}

    old_fn_rows = conn.execute(
        "SELECT id, line_start, semantic_hash FROM functions WHERE file = ?", (path,)
    ).fetchall()
    old_fn_ids = {row["id"] for row in old_fn_rows}
    old_fn_hashes = {row["id"]: row["semantic_hash"] for row in old_fn_rows}

    added = sorted(new_fn_ids - old_fn_ids)
    removed = sorted(old_fn_ids - new_fn_ids)
    changed: list[str] = []
    fn_by_id = {_build_function_id(path, fn): fn for fn in struct.functions}
    for fn_id in new_fn_ids & old_fn_ids:
        fn = fn_by_id.get(fn_id)
        if fn is None:
            continue
        new_fn_hash = function_semantic_hash(fn.node, source, language)
        if new_fn_hash != old_fn_hashes.get(fn_id):
            changed.append(fn_id)
    changed.sort()

    return FileDiff(
        path=path,
        change_type="semantic",
        added_functions=added,
        removed_functions=removed,
        changed_functions=changed,
    )


def diff_current_vs_indexed(
    conn: sqlite3.Connection,
    repo_root: Path,
) -> DiffReport:
    """Compare on-disk code against .ctx/index.db.

    Returns a DiffReport with: changed (semantic+formatting), new (unindexed files),
    deleted (indexed but no longer on disk).
    """
    parseable = discover_parseable_files(repo_root)
    indexed = {
        row["path"]: row
        for row in conn.execute(
            "SELECT path, content_hash, semantic_hash FROM files"
        ).fetchall()
    }
    indexed_paths = set(indexed.keys())

    # New files: parseable + actually exists on disk + not yet indexed
    disk_paths: set[str] = set()
    for p, lang in parseable.items():
        if (repo_root / p).exists():
            disk_paths.add(p)

    new_files = sorted(disk_paths - indexed_paths)
    # Deleted: indexed but no longer on disk (regardless of git tracking)
    deleted_files = sorted(p for p in indexed_paths if not (repo_root / p).exists())
    changed: list[FileDiff] = []

    for path in sorted(disk_paths & indexed_paths):
        abs_path = repo_root / path
        try:
            raw = abs_path.read_bytes()
        except (IOError, OSError):
            continue
        new_content_hash = hashlib.sha256(raw).hexdigest()
        row = indexed[path]
        fd = _diff_file_semantic(
            path, abs_path, parseable[path],
            new_content_hash, row["content_hash"], row["semantic_hash"],
            conn,
        )
        if fd is not None:
            changed.append(fd)

    return DiffReport(
        mode="current_vs_indexed",
        changed=changed,
        new=new_files,
        deleted=deleted_files,
    )


def diff_between_commits(
    conn: sqlite3.Connection,
    repo_root: Path,
    commit1: str,
    commit2: str,
) -> AuditReport:
    """Build an audit report from the `changes` table between two commits."""
    resolved1 = _resolve_commit(repo_root, commit1)
    resolved2 = _resolve_commit(repo_root, commit2)
    commits_between = _get_commits_between(repo_root, resolved1, resolved2)

    if not commits_between:
        return AuditReport(commit1=resolved1[:7], commit2=resolved2[:7])

    placeholders = ",".join("?" for _ in commits_between)
    rows = conn.execute(
        f"SELECT file, commit_hash, summary, author, timestamp FROM changes "
        f"WHERE commit_hash IN ({placeholders}) ORDER BY timestamp",
        commits_between,
    ).fetchall()

    by_author: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_author[r["author"]].append({
            "file": r["file"],
            "commit_hash": r["commit_hash"],
            "summary": r["summary"],
            "timestamp": r["timestamp"],
        })

    recorded_hashes = {r["commit_hash"] for r in rows}
    not_recorded = []
    for h in commits_between:
        if h not in recorded_hashes:
            not_recorded.append({
                "commit_hash": h,
                "summary": _get_commit_subject(repo_root, h),
            })

    return AuditReport(
        commit1=resolved1[:7],
        commit2=resolved2[:7],
        by_author=dict(by_author),
        total=len(rows),
        not_recorded=not_recorded,
    )


# ── Print formatters ──────────────────────────────────────────────────────────


def _print_current_diff(report: DiffReport, repo_root: Path) -> None:
    repo_name = repo_root.name
    semantic_files = [d for d in report.changed if d.change_type == "semantic"]
    formatting_files = [d for d in report.changed if d.change_type == "formatting"]
    print(f"ctx diff \u2014 {repo_name}")
    print()
    print("  Comparing: on-disk code vs. .ctx/index.db")
    print()

    if not semantic_files and not formatting_files and not report.new and not report.deleted:
        print("  Everything is current \u2014 no changes since the last ctx init.")
        return

    if semantic_files:
        print("  CHANGED (semantic):")
        for d in semantic_files:
            print(f"    {d.path}")
            if d.changed_functions:
                print("      changed functions:")
                for fn in d.changed_functions:
                    print(f"        {fn}  \u2190 logic changed")
            if d.added_functions:
                print("      added functions:")
                for fn in d.added_functions:
                    print(f"        {fn}")
            if d.removed_functions:
                print("      removed functions:")
                for fn in d.removed_functions:
                    print(f"        {fn}")
        print()

    if formatting_files:
        print("  CHANGED (formatting only):")
        for d in formatting_files:
            print(f"    {d.path}  \u2190 whitespace/style only, metadata preserved")
        print()

    if report.new:
        print("  NEW (not yet indexed):")
        for p in report.new:
            print(f"    {p}")
        print()

    if report.deleted:
        print("  DELETED (was indexed, no longer on disk):")
        for p in report.deleted:
            print(f"    {p}")
        print()

    print("  " + "\u2500" * 56)
    print(
        f"  {len(semantic_files)} semantic change(s), "
        f"{len(formatting_files)} formatting change(s), "
        f"{len(report.new)} new file(s), "
        f"{len(report.deleted)} deleted"
    )
    print()
    print("  To update the index: ctx sync")


def _print_audit_diff(report: AuditReport, repo_root: Path) -> None:
    repo_name = repo_root.name
    print(f"ctx diff {report.commit1}..{report.commit2} \u2014 {repo_name}")
    print()
    print(f"  Changes recorded between {report.commit1} and {report.commit2}")
    print()

    if not report.by_author and not report.not_recorded:
        print("  No changes recorded in this range.")
        return

    # Group commits by author with subjects — pull a per-commit grouping
    # by collecting files under each commit_hash.
    by_commit: dict[str, dict] = {}
    for author, entries in report.by_author.items():
        for e in entries:
            ch = e["commit_hash"]
            if ch not in by_commit:
                by_commit[ch] = {
                    "author": author,
                    "summary": e["summary"] or "",
                    "files": [],
                }
            by_commit[ch]["files"].append(e["file"])

    human_commits = [c for c in by_commit.values() if c["author"] == "human"]
    model_commits = [c for c in by_commit.values() if c["author"] == "model"]
    other_commits = [c for c in by_commit.values() if c["author"] not in ("human", "model")]

    if human_commits:
        print(f"  HUMAN commits ({len(human_commits)}):")
        for c in human_commits:
            short_hash = next(h[:7] for h, info in by_commit.items() if info is c)
            print(f"    {short_hash}  \"{c['summary']}\"")
            for f in c["files"]:
                print(f"      - {f}")
        print()

    if model_commits:
        print(f"  MODEL commits ({len(model_commits)}):")
        for c in model_commits:
            short_hash = next(h[:7] for h, info in by_commit.items() if info is c)
            print(f"    {short_hash}  \"{c['summary']}\"")
            for f in c["files"]:
                print(f"      - {f}    \u2190 AI-authored via ctx_log_change")
        print()

    if other_commits:
        print(f"  OTHER commits ({len(other_commits)}):")
        for c in other_commits:
            short_hash = next(h[:7] for h, info in by_commit.items() if info is c)
            print(f"    {short_hash}  \"{c['summary']}\"  (author: {c['author']})")
            for f in c["files"]:
                print(f"      - {f}")
        print()

    if report.not_recorded:
        print(f"  NOT RECORDED in changes table ({len(report.not_recorded)} commit(s)):")
        for c in report.not_recorded:
            print(f"    {c['commit_hash'][:7]}  \"{c['summary']}\"")
        print("    \u2139  These commits were made without ctx log-commit running.")
        print("       Install hooks to ensure all commits are recorded: ctx install-hooks")
        print()

    recorded_count = sum(1 for c in by_commit.values() if c["author"] == "human") + sum(
        1 for c in by_commit.values() if c["author"] == "model"
    )
    total = recorded_count + len(report.not_recorded)
    print("  " + "\u2500" * 56)
    print(
        f"  {total} commit(s) between {report.commit1} and {report.commit2}"
    )
    print(
        f"  {recorded_count} recorded ({sum(1 for c in by_commit.values() if c['author'] == 'human')} human, "
        f"{sum(1 for c in by_commit.values() if c['author'] == 'model')} model), "
        f"{len(report.not_recorded)} not recorded"
    )


# ── Public entry point ────────────────────────────────────────────────────────


def run_diff(repo_root: Path, commit1: str | None, commit2: str | None) -> None:
    db_path = repo_root / ".ctx" / "index.db"
    if not db_path.exists():
        raise FileNotFoundError("Database not found. Run 'ctx init' first.")

    conn = connect(db_path)
    try:
        if commit1 is None and commit2 is None:
            report = diff_current_vs_indexed(conn, repo_root)
            _print_current_diff(report, repo_root)
        else:
            if commit1 is None or commit2 is None:
                raise ValueError("ctx diff requires either zero or two commit arguments.")
            report = diff_between_commits(conn, repo_root, commit1, commit2)
            _print_audit_diff(report, repo_root)
    finally:
        conn.close()
