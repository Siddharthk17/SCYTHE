"""`ctx audit-model` — AI accountability for index maintenance.

When a model edits code, the ctx protocol requires it to call ctx_update_file,
ctx_update_function, ctx_log_session, and ctx_log_change. audit-model compares
what the model claimed (changes table, session_log) against what actually
changed in the git history, and flags coverage gaps, stale metadata, uncleared
taints, description mismatches, and missing session logs.
"""
import json
import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ctx_engine.db import connect


@dataclass
class AuditIssue:
    severity: str  # "warning" | "error"
    type: str
    file: str | None
    commit: str | None
    description: str


@dataclass
class AuditReport:
    range_expr: str
    commit_count: int = 0
    model_commits: int = 0
    model_file_changes: int = 0
    checks: dict = field(default_factory=dict)

    @property
    def issue_count(self) -> int:
        return sum(len(v.get("issues", [])) for v in self.checks.values())


# ── Git helpers ───────────────────────────────────────────────────────────────


def get_commits_since(repo_root: Path, since_commit: str | None) -> list[str]:
    """Return commit hashes in HEAD (or since_commit..HEAD), oldest first."""
    args = ["git", "log", "--format=%H", "--reverse"]
    if since_commit:
        args.append(f"{since_commit}..HEAD")
    result = subprocess.run(args, cwd=repo_root, capture_output=True, text=True)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def get_files_changed_in_commit(repo_root: Path, commit_hash: str) -> list[str]:
    result = subprocess.run(
        ["git", "show", "--name-only", "--format=", commit_hash],
        cwd=repo_root, capture_output=True, text=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def get_commit_author_name(repo_root: Path, commit_hash: str) -> str:
    result = subprocess.run(
        ["git", "show", "--format=%an", "--no-patch", commit_hash],
        cwd=repo_root, capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def get_commit_diff(repo_root: Path, commit_hash: str, file_path: str) -> str:
    result = subprocess.run(
        ["git", "show", "--format=", "--unified=0", commit_hash, "--", file_path],
        cwd=repo_root, capture_output=True, text=True,
    )
    return result.stdout if result.returncode == 0 else ""


# ── Check 4 helper: changed-function extraction from a commit diff ────────────

HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def get_changed_function_ids(
    conn: sqlite3.Connection,
    repo_root: Path,
    commit_hash: str,
    file_path: str,
) -> list[str]:
    """Function ids in file_path whose line ranges overlap the commit's diff."""
    diff = get_commit_diff(repo_root, commit_hash, file_path)
    ranges: list[tuple[int, int]] = []
    for line in diff.splitlines():
        match = HUNK_RE.match(line)
        if not match:
            continue
        start = int(match.group(1))
        count = int(match.group(2)) if match.group(2) is not None else 1
        ranges.append((start, max(start, start + count - 1)))

    rows = conn.execute(
        "SELECT id, line_start, line_end FROM functions WHERE file = ?", (file_path,)
    ).fetchall()
    changed = []
    for row in rows:
        for start, end in ranges:
            if start <= row["line_end"] and row["line_start"] <= end:
                changed.append(row["id"])
                break
    return changed


# ── The five checks ───────────────────────────────────────────────────────────


def check_model_change_coverage(
    conn: sqlite3.Connection,
    since_commit: str | None,
    repo_root: Path,
) -> list[AuditIssue]:
    """Check 1: every file changed in a commit has a changes-table row.

    Models that edit code but never call ctx_log_change leave no evidence of
    what they changed.
    """
    issues = []
    for commit_hash in get_commits_since(repo_root, since_commit):
        for file_path in get_files_changed_in_commit(repo_root, commit_hash):
            row = conn.execute(
                "SELECT author FROM changes WHERE file = ? AND commit_hash = ?",
                (file_path, commit_hash),
            ).fetchone()
            if row is None:
                issues.append(AuditIssue(
                    severity="warning",
                    type="missing_log",
                    file=file_path,
                    commit=commit_hash[:7],
                    description=(
                        f"File changed in commit {commit_hash[:7]} but "
                        f"no ctx_log_change call recorded for it."
                    ),
                ))
    return issues


def check_metadata_freshness(
    conn: sqlite3.Connection,
    since_commit: str | None,
    repo_root: Path,
) -> list[AuditIssue]:
    """Check 2: model-edited files must not be left stale.

    A changes row with author='model' means the model logged the edit; if the
    file or its functions are still stale, it never called ctx_update_file /
    ctx_update_function.
    """
    issues = []
    rows = conn.execute(
        "SELECT DISTINCT file FROM changes WHERE author = 'model' AND file IS NOT NULL"
    ).fetchall()
    for row in rows:
        file_path = row["file"]
        file_row = conn.execute(
            "SELECT is_stale FROM files WHERE path = ?", (file_path,)
        ).fetchone()
        if file_row and file_row["is_stale"]:
            issues.append(AuditIssue(
                severity="warning",
                type="stale_file",
                file=file_path,
                commit=None,
                description=(
                    f"{file_path} is_stale=1 after a model edit "
                    f"(ctx_update_file not called)."
                ),
            ))
        stale_fns = conn.execute(
            "SELECT COUNT(*) FROM functions WHERE file = ? AND is_stale = 1",
            (file_path,),
        ).fetchone()[0]
        if stale_fns:
            issues.append(AuditIssue(
                severity="warning",
                type="stale_functions",
                file=file_path,
                commit=None,
                description=(
                    f"{file_path}: {stale_fns} function(s) is_stale=1 "
                    f"(ctx_update_function not called)."
                ),
            ))
    return issues


def check_taint_clearance(
    conn: sqlite3.Connection,
    since_commit: str | None,
    repo_root: Path,
) -> list[AuditIssue]:
    """Check 3: taint_queue entries pointing at model-sourced taints.

    A taint whose source function lives in a model-edited file and is still
    queued means the model created a taint cascade and never cleaned it up.
    """
    issues = []
    rows = conn.execute(
        """SELECT tq.function_id, tq.taint_source
           FROM taint_queue tq
           JOIN functions src_fn ON src_fn.id = tq.taint_source
           WHERE src_fn.file IN (
               SELECT DISTINCT file FROM changes WHERE author = 'model'
                 AND file IS NOT NULL
           )"""
    ).fetchall()
    for row in rows:
        issues.append(AuditIssue(
            severity="warning",
            type="uncleared_taint",
            file=row["function_id"],
            commit=None,
            description=(
                f"{row['function_id']} is still tainted by "
                f"{row['taint_source']} (model-edited). The model changed the "
                f"source but never updated its dependent's summary."
            ),
        ))
    return issues


def check_description_accuracy(
    conn: sqlite3.Connection,
    since_commit: str | None,
    repo_root: Path,
) -> list[AuditIssue]:
    """Check 4: does the model's change summary mention what actually changed?

    Lightweight heuristic — no LLM call. A summary that names none of the
    functions actually touched by the diff is a description mismatch.
    """
    issues = []
    rows = conn.execute(
        "SELECT file, commit_hash, summary FROM changes "
        "WHERE author = 'model' AND file IS NOT NULL"
    ).fetchall()
    for row in rows:
        if not row["commit_hash"] or not row["summary"]:
            continue
        changed_ids = get_changed_function_ids(
            conn, repo_root, row["commit_hash"], row["file"]
        )
        if not changed_ids:
            continue
        summary_lower = row["summary"].lower()
        # Match on the bare function name (last path/qualification segment).
        mentioned = [
            fn_id for fn_id in changed_ids
            if fn_id.rsplit("/", 1)[-1].rsplit("::", 1)[-1].rsplit(".", 1)[-1].lower()
            in summary_lower
        ]
        if not mentioned:
            issues.append(AuditIssue(
                severity="warning",
                type="description_mismatch",
                file=row["file"],
                commit=row["commit_hash"][:7],
                description=(
                    f"Change summary \"{row['summary']}\" mentions none of the "
                    f"functions actually changed ({', '.join(changed_ids[:3])})."
                ),
            ))
    return issues


def check_session_log(
    conn: sqlite3.Connection,
    since_commit: str | None,
    repo_root: Path,
) -> list[AuditIssue]:
    """Check 5: model commits without any session_log entries in the window."""
    changes_rows = conn.execute(
        "SELECT COUNT(DISTINCT commit_hash) FROM changes "
        "WHERE author = 'model' AND commit_hash IS NOT NULL"
    ).fetchone()[0]
    if changes_rows == 0:
        return []
    session_count = conn.execute("SELECT COUNT(*) FROM session_log").fetchone()[0]
    if session_count == 0:
        return [AuditIssue(
            severity="warning",
            type="missing_session_log",
            file=None,
            commit=None,
            description=(
                f"The model made {changes_rows} commit(s) without calling "
                f"ctx_log_session."
            ),
        )]
    return []


CHECKS = (
    ("coverage", check_model_change_coverage),
    ("freshness", check_metadata_freshness),
    ("taint_clearance", check_taint_clearance),
    ("description_accuracy", check_description_accuracy),
    ("session_log", check_session_log),
)

# ── Report rendering ──────────────────────────────────────────────────────────

CHECK_LABELS = {
    "coverage": "Change coverage",
    "freshness": "Metadata freshness",
    "taint_clearance": "Taint clearance",
    "description_accuracy": "Description accuracy",
    "session_log": "Session log",
}


def run_audit_model(
    repo_root: Path,
    since_commit: str | None = None,
    json_output: bool = False,
) -> int:
    """Run the five accountability checks. Returns process exit code."""
    db_path = repo_root / ".ctx" / "index.db"
    if not db_path.exists():
        raise FileNotFoundError("Database not found. Run 'ctx init' first.")

    conn = connect(db_path)
    try:
        commits = get_commits_since(repo_root, since_commit)
        range_expr = f"{since_commit}..HEAD" if since_commit else "HEAD (all commits)"

        model_commit_rows = conn.execute(
            "SELECT COUNT(DISTINCT commit_hash) FROM changes WHERE author = 'model'"
        ).fetchone()[0]
        model_file_changes = conn.execute(
            "SELECT COUNT(*) FROM changes WHERE author = 'model'"
        ).fetchone()[0]

        report = AuditReport(range_expr=range_expr, commit_count=len(commits))
        for name, check_fn in CHECKS:
            issues = check_fn(conn, since_commit, repo_root)
            report.checks[name] = {"passed": not issues, "issues": issues}
            report.model_commits = model_commit_rows
            report.model_file_changes = model_file_changes

        if json_output:
            print(json.dumps(_report_to_dict(report, repo_root), indent=2))
        else:
            _print_report(report, repo_root)

        return 0 if report.issue_count == 0 else 1
    finally:
        conn.close()


def _report_to_dict(report: AuditReport, repo_root: Path) -> dict:
    def issues_of(name: str) -> list[dict]:
        return [
            {
                "severity": i.severity,
                "type": i.type,
                "file": i.file,
                "commit": i.commit,
                "description": i.description,
            }
            for i in report.checks[name]["issues"]
        ]

    return {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repo": repo_root.name,
        "range": report.range_expr,
        "model_commits": report.model_commits,
        "model_file_changes": report.model_file_changes,
        "checks": {
            name: {"passed": report.checks[name]["passed"], "issues": issues_of(name)}
            for name in report.checks
        },
    }


def _print_report(report: AuditReport, repo_root: Path) -> None:
    print(f"ctx audit-model — {repo_root.name}")
    print(f"  Range: {report.range_expr} ({report.commit_count} commits)")
    print()
    print(
        f"  Model-authored activity: {report.model_commits} commits, "
        f"{report.model_file_changes} file changes"
    )
    print()
    for name, _ in CHECKS:
        check = report.checks[name]
        mark = "✓" if check["passed"] else "✗"
        print(f"  {mark} Check — {CHECK_LABELS[name]}")
        if check["passed"]:
            print("    All clear.")
        else:
            for issue in check["issues"]:
                print(f"    - {issue.description}")
        print()
    print("  " + "─" * 56)
    if report.issue_count == 0:
        print("  Summary: no issues found — the model maintained the index.")
    else:
        print(f"  Summary: {report.issue_count} issue(s) found")
        print("    To fix: ctx sync (clears staleness and taint)")
        print("    To prevent: ensure the model calls ctx_log_session and "
              "ctx_update_function after every edit")


