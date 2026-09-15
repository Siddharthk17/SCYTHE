"""`ctx refactor` — refactoring impact planner and metadata fixer.

`plan` enumerates every call site of a function from the call graph and
prints a step-by-step change plan with an effort estimate. It never edits
code. `apply` runs after the rename is done and reindexed: it re-links
dangling call-graph rows to the new function id and fixes stale
`taint_source` references.
"""
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from ctx_engine.db import connect


@dataclass
class RefactorApplyReport:
    status: str  # "applied" | "not_applied" | "ambiguous"
    message: str
    relinked: int = 0
    taint_fixed: int = 0
    new_id: str | None = None


def find_call_sites(
    conn: sqlite3.Connection, old_id: str
) -> list[dict]:
    """Return caller function records that call old_id, ordered by file."""
    rows = conn.execute(
        "SELECT f.* FROM call_graph cg "
        "JOIN functions f ON f.id = cg.caller_id "
        "WHERE cg.callee_id = ? ORDER BY f.file, f.line_start",
        (old_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def short_name(function_id: str) -> str:
    return function_id.split("::")[-1].split(".")[-1].split("#")[-1]


def source_file(function_id: str) -> str:
    return function_id.split("::")[0]


def change_hint(old_short: str, new_name: str) -> str:
    """Render the 'Change to' hint for a call site.

    The call graph records callers at function granularity, so the exact
    call line is unknown — the hint names the rename to apply to every
    call of the old name within the caller's body.
    """
    return f"{new_name}(...) (was {old_short}(...))"


def signatures_differ(old_sig: str | None, new_sig: str) -> bool:
    """Heuristic parameter comparison between two signature strings."""
    def params(sig: str) -> list[str]:
        match = re.search(r"\((.*)\)", sig, re.DOTALL)
        if not match:
            return []
        inner = match.group(1).strip()
        if not inner:
            return []
        return [p.strip() for p in inner.split(",") if p.strip()]

    if not old_sig:
        return True
    old_params = params(old_sig)
    new_params = params(new_sig)
    if len(old_params) != len(new_params):
        return True
    old_names = [p.split("=")[0].split(":")[0].strip().split()[-1] for p in old_params]
    new_names = [p.split("=")[0].split(":")[0].strip().split()[-1] for p in new_params]
    return old_names != new_names


def plan_refactor(
    conn: sqlite3.Connection,
    old_id: str,
    new_name: str | None = None,
    new_signature: str | None = None,
) -> None:
    target = conn.execute(
        "SELECT * FROM functions WHERE id = ?", (old_id,)
    ).fetchone()
    if target is None:
        raise ValueError(
            f"Function not found in index: '{old_id}'. "
            "Run 'ctx init' first, or check the id format (path::Class.name)."
        )
    target = dict(target)
    old_short = short_name(old_id)

    display_new = new_name or f"(renamed) {old_short}"
    print("ctx refactor plan")
    print()
    print(f"  REFACTORING PLAN")
    print(f"  Old: {old_id}  →  New: {display_new}")
    print()
    print("  " + "═" * 46)
    call_sites = find_call_sites(conn, old_id)
    print(f"  CALL SITES TO UPDATE ({len(call_sites)} direct)")
    print("  " + "═" * 46)
    print()

    if not call_sites:
        print("  No call sites found — function appears to be unused or entry point.")
        print()
    else:
        by_file: dict[str, list[dict]] = defaultdict(list)
        for site in call_sites:
            by_file[site["file"]].append(site)
        step = 0
        for path in sorted(by_file):
            for site in by_file[path]:
                step += 1
                same = " (same file)" if path == target["file"] else ""
                print(f"  Step {step}: {path}{same}")
                print(
                    f"    Within {site['id']}'s body "
                    f"(lines {site['line_start']}–{site['line_end']})"
                )
                if new_name:
                    print(f"    Change to: {change_hint(old_short, new_name)}")
                if new_signature and signatures_differ(target.get("signature"), new_signature):
                    print(
                        "    ⚠ may need argument update — new signature changes "
                        "parameters; verify positional arguments at this call site."
                    )
                print()

    affected_files = sorted({s["file"] for s in call_sites} | {target["file"]})
    print("  " + "═" * 46)
    print("  METADATA TO UPDATE AFTER REFACTOR")
    print("  " + "═" * 46)
    print()
    print("  Run these ctx commands after making the code changes:")
    print()
    for path in affected_files:
        print(f"    ctx update {path}")
    print()
    print("  Or run all at once with:")
    print("    ctx sync")
    print()

    new_id = old_id
    if new_name:
        # Rebuild from the class part explicitly to keep separators intact.
        stem = old_id.split("::")[0]
        qual = old_id.split("::", 1)[1]
        for separator in (".", "#"):
            if separator in qual:
                cls, _, _ = qual.rpartition(separator)
                new_id = f"{stem}::{cls}{separator}{new_name}"
                break
        else:
            new_id = f"{stem}::{new_name}"
        print("  " + "═" * 46)
        print("  INDEX IMPACT")
        print("  " + "═" * 46)
        print()
        print("  Old function id will be deleted from the database (rename = remove + add).")
        print(f"  New function id will be: {new_id}")
        print("  Callers' taint state will propagate after ctx sync.")
        print()

    cross = sum(
        1 for s in call_sites
        if _file_system(conn, s["file"]) != _file_system(conn, target["file"])
        and _file_system(conn, s["file"]) is not None
    )
    print("  " + "═" * 46)
    print("  ESTIMATED EFFORT")
    print("  " + "═" * 46)
    print()
    print(
        f"  {len(affected_files)} files, {len(call_sites)} call sites, "
        f"{cross} cross-system changes."
    )
    if new_signature and signatures_differ(target.get("signature"), new_signature):
        risk = "MEDIUM (signature change — verify arguments at every call site)"
    elif cross:
        risk = "HIGH (cross-system change — coordinate with system owners)"
    elif all(s["file"] == target["file"] or "test" in s["file"] for s in call_sites):
        risk = "LOW (rename only, no signature change, all call sites in same and test file)"
    else:
        risk = "MEDIUM (rename across multiple non-test files)"
    print(f"  Risk: {risk}")
    print()
    print("  Run 'ctx refactor apply' after making the changes to update the ctx metadata.")


def _file_system(conn: sqlite3.Connection, path: str) -> str | None:
    row = conn.execute(
        "SELECT system FROM files WHERE path = ?", (path,)
    ).fetchone()
    return row["system"] if row else None


def apply_refactor(
    conn: sqlite3.Connection,
    old_id: str,
    new_name: str | None = None,
) -> RefactorApplyReport:
    old_row = conn.execute(
        "SELECT * FROM functions WHERE id = ?", (old_id,)
    ).fetchone()
    if old_row is not None:
        return RefactorApplyReport(
            status="not_applied",
            message=(
                f"Old function id '{old_id}' still exists in the database. "
                "Run ctx init first to pick up code changes, then retry."
            ),
        )

    old_short = short_name(old_id)
    old_file = source_file(old_id)
    dangling = conn.execute(
        "SELECT id, caller_id FROM call_graph "
        "WHERE callee_id IS NULL AND callee_name = ? "
        "AND (callee_file = ? OR callee_file IS NULL)",
        (old_short, old_file),
    ).fetchall()

    if new_name:
        stem = old_id.split("::")[0]
        candidates = conn.execute(
            "SELECT id FROM functions WHERE file = ? AND name = ?",
            (stem, new_name),
        ).fetchall()
        new_id = candidates[0]["id"] if candidates else None
        if new_id is None:
            return RefactorApplyReport(
                status="not_applied",
                message=(
                    f"No function named '{new_name}' found in {stem}. "
                    "Run ctx init first to pick up code changes, then retry."
                ),
            )
    else:
        same_file = conn.execute(
            "SELECT id, name FROM functions WHERE file = ? AND name != ?",
            (old_file, old_short),
        ).fetchall()
        # The successor is ambiguous when several sibling functions could be
        # the rename target — refuse to guess and ask for --new-name.
        if len(same_file) == 1:
            new_id = same_file[0]["id"]
        elif not same_file:
            return RefactorApplyReport(
                status="not_applied",
                message=(
                    f"No candidate successor found in {old_file}. "
                    "Run ctx init first to pick up code changes, then retry."
                ),
            )
        else:
            options = ", ".join(r["id"] for r in same_file[:10])
            return RefactorApplyReport(
                status="ambiguous",
                message=(
                    f"Multiple candidate successors in {old_file}: {options}. "
                    "Re-run with --new-name <name> to pick the rename target."
                ),
            )

    relinked = 0
    with conn:
        for row in dangling:
            conn.execute(
                "UPDATE call_graph SET callee_id = ?, callee_file = "
                "(SELECT file FROM functions WHERE id = ?) WHERE id = ?",
                (new_id, new_id, row["id"]),
            )
            relinked += 1
        taint_fixed = conn.execute(
            "UPDATE functions SET taint_source = ? WHERE taint_source = ?",
            (new_id, old_id),
        ).rowcount
        taint_fixed += conn.execute(
            "UPDATE taint_queue SET taint_source = ? WHERE taint_source = ?",
            (new_id, old_id),
        ).rowcount

    return RefactorApplyReport(
        status="applied",
        message=(
            f"Re-linked {relinked} call-graph edge(s) from '{old_id}' to '{new_id}'. "
            f"Fixed {taint_fixed} taint reference(s)."
        ),
        relinked=relinked,
        taint_fixed=taint_fixed,
        new_id=new_id,
    )


def print_apply_report(report: RefactorApplyReport) -> None:
    print("ctx refactor apply")
    print()
    if report.status == "not_applied":
        print(f"  Old function still exists — run ctx init first.")
        print(f"  {report.message}")
    elif report.status == "ambiguous":
        print(f"  {report.message}")
    else:
        print(f"  {report.message}")
        print("  Run 'ctx sync' to refresh summaries and propagate taint.")


def run_refactor_plan(
    repo_root: Path,
    old_id: str,
    new_name: str | None = None,
    new_signature: str | None = None,
) -> None:
    db_path = repo_root / ".ctx" / "index.db"
    if not db_path.exists():
        raise FileNotFoundError("Database not found. Run 'ctx init' first.")
    conn = connect(db_path)
    try:
        plan_refactor(conn, old_id, new_name=new_name, new_signature=new_signature)
    finally:
        conn.close()


def run_refactor_apply(
    repo_root: Path, old_id: str, new_name: str | None = None
) -> None:
    db_path = repo_root / ".ctx" / "index.db"
    if not db_path.exists():
        raise FileNotFoundError("Database not found. Run 'ctx init' first.")
    conn = connect(db_path)
    try:
        report = apply_refactor(conn, old_id, new_name=new_name)
    finally:
        conn.close()
    print_apply_report(report)
