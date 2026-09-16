"""`ctx ci` \u2014 CI-mode validation for GitHub Actions and other CI systems.

Combines `ctx validate` logic with `changes`-table audit to produce a
pass/fail result suitable for blocking a PR. Emits machine-readable JSON
for the CI pipeline to consume.
"""
import difflib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from ctx_engine.db import connect
from ctx_engine.discovery import discover_parseable_files
from ctx_engine.hashing import file_content_hash, file_semantic_hash
from ctx_engine.languages.registry import get_parser
from ctx_engine.mcp_server.tools.renderers import render_generation_timestamp

WORKFLOW_TEMPLATE = """name: ctx validate

on:
  pull_request:
    branches: [main, master]
  push:
    branches: [main, master]

jobs:
  ctx-validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install ctx
        run: pip install ctx-codebase

      - name: Restore ctx index cache
        uses: actions/cache@v4
        with:
          path: .ctx/index.db
          key: ctx-index-${{{{ hashFiles('**/*.py', '**/*.ts', '**/*.go', '**/*.rs', '**/*.java', '**/*.cs') }}}}
          restore-keys: |
            ctx-index-

      - name: Run ctx init (fast \u2014 mtime cache)
        run: ctx init
        env:
          ANTHROPIC_API_KEY: ${{{{ secrets.ANTHROPIC_API_KEY }}}}

      - name: Validate index against changed files
        # Validation-only CI does not call the LLM, so ANTHROPIC_API_KEY is optional here
        # (required: false). It is only needed if this job also runs ctx summarize/sync.
        run: ctx ci --json > ctx-report.json && cat ctx-report.json
        env:
          ANTHROPIC_API_KEY: ${{{{ secrets.ANTHROPIC_API_KEY }}}} # required: false for validation-only CI

      - name: Upload ctx report
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: ctx-report
          path: ctx-report.json
"""


def _get_changed_files(repo_root: Path, base_ref: str | None = None) -> list[str]:
    """Return the list of files changed in the current PR (vs base_ref or origin/main)."""
    if base_ref is None:
        base_ref = os.environ.get("GITHUB_BASE_REF")
        if base_ref:
            # GitHub Actions sets GITHUB_BASE_REF to the base branch name.
            # We need to fetch it as origin/<base_ref>.
            try:
                subprocess.run(
                    ["git", "fetch", "origin", base_ref],
                    cwd=repo_root, capture_output=True, text=True, check=True,
                )
            except subprocess.CalledProcessError:
                pass
            base_ref = f"origin/{base_ref}"
        else:
            base_ref = "origin/main"

    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base_ref}...HEAD"],
        cwd=repo_root, capture_output=True, text=True,
    )
    changed = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not changed:
        # Fallback: working-tree modifications + staged files (for local dev and tests).
        # We do NOT strip lines here \u2014 the porcelain v1 format is exactly
        # 'XY filename' with 2 status chars at positions 0-1, a space at 2,
        # and the filename starting at 3. Status '?' is rendered as '??' which
        # preserves the alignment. Stripping would shift positions and break parsing.
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root, capture_output=True, text=True,
        )
        changed_set: set[str] = set()
        for line in result.stdout.splitlines():
            if len(line) < 4:
                continue
            # Filename starts at byte 3, may be 'old -> new' for renames.
            fname_part = line[3:]
            if " -> " in fname_part:
                fname_part = fname_part.split(" -> ", 1)[1]
            fname = fname_part.strip()
            if fname:
                changed_set.add(fname)
        changed = sorted(changed_set)
    return changed


def _get_pr_commits(repo_root: Path, base_ref: str | None = None) -> list[str]:
    """Return commit hashes in the current PR."""
    if base_ref is None:
        base_ref = os.environ.get("GITHUB_BASE_REF")
        if base_ref:
            try:
                subprocess.run(
                    ["git", "fetch", "origin", base_ref],
                    cwd=repo_root, capture_output=True, text=True, check=True,
                )
            except subprocess.CalledProcessError:
                pass
            base_ref = f"origin/{base_ref}"
        else:
            base_ref = "origin/main"
    result = subprocess.run(
        ["git", "log", "--format=%H", f"{base_ref}..HEAD"],
        cwd=repo_root, capture_output=True, text=True,
    )
    commits = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not commits:
        # Fallback: use staged diff's commits by inspecting HEAD..HEAD~5
        # (this matches the test scenario where there's no remote)
        result = subprocess.run(
            ["git", "log", "--format=%H", "-5", "HEAD"],
            cwd=repo_root, capture_output=True, text=True,
        )
        commits = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return commits


def _check_stale_files(repo_root: Path, conn: sqlite3.Connection, changed_files: list[str]) -> dict:
    """Files in the PR whose content_hash differs from the index \u2014 they're stale."""
    parseable = discover_parseable_files(repo_root)
    stale: list[str] = []
    for path in changed_files:
        if path not in parseable:
            continue
        abs_path = repo_root / path
        if not abs_path.exists():
            continue
        row = conn.execute(
            "SELECT content_hash FROM files WHERE path = ?", (path,)
        ).fetchone()
        if row is None:
            continue  # not yet indexed, but new files are not 'stale' per se
        try:
            current = file_content_hash(abs_path.read_bytes())
        except (IOError, OSError):
            continue
        if current != row["content_hash"]:
            # Confirm semantic diff (not just formatting)
            indexed_sem = conn.execute(
                "SELECT semantic_hash FROM files WHERE path = ?", (path,)
            ).fetchone()
            if indexed_sem is None:
                stale.append(path)
                continue
            try:
                source = abs_path.read_bytes()
                tree = get_parser(parseable[path]).parse(source)
                new_sem = file_semantic_hash(tree, source, parseable[path])
                if new_sem != indexed_sem["semantic_hash"]:
                    stale.append(path)
            except Exception:
                stale.append(path)
    return {
        "passed": len(stale) == 0,
        "count": len(stale),
        "files": stale,
    }


def _check_commits_logged(
    repo_root: Path, conn: sqlite3.Connection, pr_commits: list[str],
) -> dict:
    """Count PR commits that have at least one entry in the changes table."""
    if not pr_commits:
        return {"passed": True, "logged": 0, "total": 0}
    placeholders = ",".join("?" for _ in pr_commits)
    rows = conn.execute(
        f"SELECT DISTINCT commit_hash FROM changes WHERE commit_hash IN ({placeholders})",
        pr_commits,
    ).fetchall()
    logged = {r["commit_hash"] for r in rows}
    logged_count = len(logged)
    total = len(pr_commits)
    pct_unlogged = (total - logged_count) / total if total else 0
    return {
        "passed": pct_unlogged <= 0.5,  # WARN, not FAIL \u2014 don't block on missing hooks
        "logged": logged_count,
        "total": total,
    }


def _check_confidence(conn: sqlite3.Connection) -> dict:
    """Count low-confidence functions. > 5% is a warning."""
    total = conn.execute("SELECT COUNT(*) FROM functions").fetchone()[0]
    if total == 0:
        return {"passed": True, "low_confidence_pct": 0.0}
    low = conn.execute(
        "SELECT COUNT(*) FROM functions WHERE confidence < 0.5"
    ).fetchone()[0]
    pct = low / total * 100
    return {
        "passed": pct <= 5.0,
        "low_confidence_pct": round(pct, 2),
    }


def _normalize_ts(value: str) -> datetime:
    """Parse an ISO timestamp and drop sub-second precision.

    Export generation timestamps are second-precision while DB updated_at
    values carry microseconds. Normalizing both sides to whole seconds
    prevents a fresh export (generated within the same second as the last
    index write) from being flagged stale.
    """
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.replace(microsecond=0)


def _check_export_fresh(repo_root: Path, conn: sqlite3.Connection) -> dict:
    """If CLAUDE.md exists, check it isn't stale relative to the DB."""
    p = repo_root / "CLAUDE.md"
    if not p.exists():
        return {"passed": True, "detail": "(no CLAUDE.md to check)"}
    gen_ts = render_generation_timestamp(p)
    if not gen_ts:
        return {"passed": True, "detail": "(no generation timestamp)"}
    try:
        latest = conn.execute(
            "SELECT MAX(updated_at) FROM ("
            "SELECT MAX(updated_at) AS updated_at FROM files "
            "UNION ALL SELECT MAX(updated_at) AS updated_at FROM functions)"
        ).fetchone()[0]
    except sqlite3.DatabaseError:
        return {"passed": True, "detail": "(DB error)"}
    if not latest:
        return {"passed": True}
    try:
        gen_dt = _normalize_ts(gen_ts)
        db_dt = _normalize_ts(latest)
    except (ValueError, TypeError):
        return {"passed": True, "detail": "(timestamp parse error)"}
    return {"passed": gen_dt >= db_dt}


def _write_workflow(repo_root: Path) -> tuple[Path, str]:
    """Write the .github/workflows/ctx-validate.yml file.

    Returns (path, action) where action is 'created' or 'would_overwrite'.
    """
    path = repo_root / ".github" / "workflows" / "ctx-validate.yml"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(WORKFLOW_TEMPLATE, encoding="utf-8")
        return path, "created"
    existing = path.read_text(encoding="utf-8")
    if existing == WORKFLOW_TEMPLATE:
        return path, "unchanged"
    # Show a unified diff
    diff = "".join(difflib.unified_diff(
        existing.splitlines(keepends=True),
        WORKFLOW_TEMPLATE.splitlines(keepends=True),
        fromfile="current", tofile="proposed", n=3,
    ))
    return path, f"diff:\n{diff}"


def run_ci(repo_root: Path, json_output: bool = False, output_workflow: bool = False) -> int:
    db_path = repo_root / ".ctx" / "index.db"
    if not db_path.exists():
        raise FileNotFoundError("Database not found. Run 'ctx init' first.")

    if output_workflow:
        path, action = _write_workflow(repo_root)
        if action == "created":
            print(f"ctx ci --output-workflow: wrote {path}")
            return 0
        if action == "unchanged":
            print(f"ctx ci --output-workflow: {path} already up to date")
            return 0
        # action is "diff:..." \u2014 show the diff and ask
        print(f"ctx ci --output-workflow: {path}")
        print(action)
        print()
        if not sys.stdin.isatty():
            print("(non-interactive: skipping overwrite. Re-run interactively to confirm.)")
            return 0
        try:
            response = input("Overwrite? [y/N] ").strip().lower()
        except EOFError:
            response = "n"
        if response == "y":
            path.write_text(WORKFLOW_TEMPLATE, encoding="utf-8")
            print("Wrote workflow file.")
        else:
            print("Skipped.")
        return 0

    changed_files = _get_changed_files(repo_root)
    pr_commits = _get_pr_commits(repo_root)

    conn = connect(db_path)
    try:
        stale_check = _check_stale_files(repo_root, conn, changed_files)
        commits_check = _check_commits_logged(repo_root, conn, pr_commits)
        confidence_check = _check_confidence(conn)
        export_check = _check_export_fresh(repo_root, conn)
    finally:
        conn.close()

    blocking_passed = stale_check["passed"]
    overall_passed = blocking_passed

    report = {
        "passed": overall_passed,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "repo": repo_root.name,
        "checks": {
            "stale_files": stale_check,
            "commits_logged": commits_check,
            "confidence": confidence_check,
            "export_current": export_check,
        },
    }
    if not overall_passed:
        if stale_check["count"] > 0:
            report["failure_reason"] = (
                f"{stale_check['count']} stale file(s): run ctx sync to fix"
            )
        else:
            report["failure_reason"] = "One or more blocking checks failed"

    if json_output:
        print(json.dumps(report, indent=2))
    else:
        # Human-readable
        print(f"ctx ci \u2014 {repo_root.name}")
        print()
        print(f"  passed: {report['passed']}")
        print()
        for name, check in report["checks"].items():
            mark = "\u2713" if check.get("passed", False) else "\u26a0"
            print(f"  {mark}  {name}: {check}")
        if "failure_reason" in report:
            print()
            print(f"  Failure: {report['failure_reason']}")

    return 0 if overall_passed else 1
