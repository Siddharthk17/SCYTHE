"""`ctx pr-description` — structured PR description generation.

Unlike `ctx review` (which finds problems), pr-description explains what was
done and why: it assembles the same structural payload per changed file and
asks the LLM for a six-section description aimed at the reviewer.
"""
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ctx_engine.db import connect
from ctx_engine.commands.review_cmd import (
    build_file_review_payload,
    get_staged_blob,
    resolve_review_paths,
)

PR_SYSTEM_PROMPT = (
    "You are writing a pull request description for a code change. "
    "You have full structural context from the codebase index. "
    "Write a PR description that gives reviewers everything they need "
    "without reading the diff. Be specific. Do not use generic PR "
    "template language."
)

PR_PROMPT_TEMPLATE = """PROJECT: {repo_name}
BRANCH: {branch}
COMMIT RANGE: {mode_desc}

CHANGED FILES ({count} files):
{file_summaries}

STRUCTURAL CONTEXT:
{danger_context}
{decision_context}
{system_context}
{taint_context}

Write a pull request description with exactly these sections:

## Summary
(2-4 sentences: what this PR does and why)

## Changes
(One bullet per changed file: what changed and the key decision behind it)

## Systems Affected
(Which architectural systems this touches and how)

## Testing Approach
(What tests cover this change, and what edge cases the reviewer should probe)

## Danger Zones Touched
(List each danger zone from the index that this change interacts with,
and confirm whether the invariant was preserved or intentionally changed)

## Review Notes
(What the reviewer should focus on — specific functions, specific lines, specific risks)

Use the actual function names and file names. Be specific. Do not use generic PR template language.
"""

OUTPUT_FILENAME = "PR_DESCRIPTION.md"


@dataclass
class PrContext:
    paths: list[str]
    mode_desc: str
    branch: str
    file_summaries: str = ""
    danger_context: str = ""
    decision_context: str = ""
    system_context: str = ""
    taint_context: str = ""
    payloads: list = field(default_factory=list)


def _git_branch(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo_root, capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def collect_pr_context(
    conn: sqlite3.Connection,
    repo_root: Path,
    paths: list[str],
    diffs: dict[str, str],
    mode_desc: str,
    staged: bool,
    range_expr: str | None,
) -> PrContext:
    """Assemble per-file structural payloads and render the prompt blocks."""
    ctx = PrContext(paths=paths, mode_desc=mode_desc, branch=_git_branch(repo_root))
    summaries: list[str] = []
    dangers: list[str] = []
    decisions: list[str] = []
    systems: dict[str, list[str]] = {}
    tainted: list[str] = []

    for path in paths:
        payload = build_file_review_payload(
            conn, path, diffs.get(path, ""),
            repo_root=repo_root,
            file_getter=(lambda p: get_staged_blob(repo_root, p))
            if staged and not range_expr
            else None,
        )
        ctx.payloads.append(payload)
        row = conn.execute(
            "SELECT purpose, summary, system FROM files WHERE path = ?", (path,)
        ).fetchone()
        purpose = row["purpose"] if row and row["purpose"] else "(no file summary)"
        system = row["system"] if row and row["system"] else "unknown"
        systems.setdefault(system or "unknown", []).append(path)
        changed = ", ".join(payload.changed_functions) or "(no indexed functions changed)"
        summaries.append(f"- {path} [{system}]: {purpose} Changed: {changed}")
        for dz in payload.danger_zones:
            dangers.append(f"- [{dz['scope']}] {dz['description']} (reason: {dz.get('reason')})")
        for dec in payload.decisions:
            decisions.append(f"- [{dec.get('scope') or 'global'}] {dec['decision']}")
        tainted.extend(
            r["id"] for r in payload.function_records if r.get("is_tainted")
        )

    ctx.file_summaries = "\n".join(summaries) or "(no files)"
    ctx.danger_context = (
        "Danger zones affected:\n" + "\n".join(dangers)
        if dangers
        else "Danger zones affected: None — this change does not touch any indexed danger zones."
    )
    ctx.decision_context = (
        "Architectural decisions in scope:\n" + "\n".join(decisions)
        if decisions
        else "Architectural decisions in scope: none."
    )
    crossed = ", ".join(f"{s} ({len(p)} files)" for s, p in sorted(systems.items()))
    ctx.system_context = f"Systems crossed: {crossed}."
    ctx.taint_context = (
        "Tainted functions in this change: " + ", ".join(sorted(set(tainted)))
        if tainted
        else "Taint state: clean (no changed function is tainted)."
    )
    return ctx


def build_pr_prompt(ctx: PrContext, repo_name: str) -> str:
    return PR_PROMPT_TEMPLATE.format(
        repo_name=repo_name,
        branch=ctx.branch,
        mode_desc=ctx.mode_desc,
        count=len(ctx.paths),
        file_summaries=ctx.file_summaries,
        danger_context=ctx.danger_context,
        decision_context=ctx.decision_context,
        system_context=ctx.system_context,
        taint_context=ctx.taint_context,
    )


def ensure_gitignored(repo_root: Path, entry: str) -> None:
    """Append an entry to .gitignore unless already present."""
    gitignore = repo_root / ".gitignore"
    if gitignore.exists():
        existing = gitignore.read_text(encoding="utf-8")
        if entry in existing:
            return
        with open(gitignore, "a", encoding="utf-8") as handle:
            if existing and not existing.endswith("\n"):
                handle.write("\n")
            handle.write(entry + "\n")
    else:
        gitignore.write_text(entry + "\n", encoding="utf-8")


def generate_pr_description(
    conn: sqlite3.Connection,
    repo_root: Path,
    staged: bool = False,
    range_expr: str | None = None,
) -> tuple[str, PrContext]:
    """Build context and call the LLM once. Returns (description, context)."""
    from ctx_engine.intelligence.llm_client import (
        call_llm_with_retry,
        get_anthropic_client,
    )

    changed_paths, diffs, mode_desc = resolve_review_paths(
        repo_root, staged or not range_expr, range_expr, None
    )
    if not changed_paths:
        raise ValueError(f"No changes found ({mode_desc}). Nothing to describe.")
    ctx = collect_pr_context(
        conn, repo_root, changed_paths, diffs, mode_desc,
        staged=staged or not range_expr, range_expr=range_expr,
    )
    client = get_anthropic_client()
    import os

    model = os.environ.get("CTX_PR_MODEL") or os.environ.get("CTX_LLM_MODEL") or "claude-haiku-4-5-20251001"
    response, in_tok, out_tok = call_llm_with_retry(
        client, model, PR_SYSTEM_PROMPT,
        build_pr_prompt(ctx, repo_root.name), max_tokens=4000,
    )
    return response, ctx


def run_pr_description(
    repo_root: Path,
    staged: bool = False,
    range_expr: str | None = None,
    output: str | None = None,
) -> None:
    db_path = repo_root / ".ctx" / "index.db"
    if not db_path.exists():
        raise FileNotFoundError("Database not found. Run 'ctx init' first.")

    conn = connect(db_path)
    try:
        try:
            description, ctx = generate_pr_description(
                conn, repo_root, staged=staged, range_expr=range_expr
            )
        except ValueError as err:
            print(f"ctx pr-description: {err}")
            return
    finally:
        conn.close()

    print(f"ctx pr-description — {repo_root.name}")
    print()
    print(f"  Generating PR description for: {ctx.mode_desc} "
          f"({len(ctx.paths)} file(s))")
    print()
    print("  " + "═" * 52)
    print()
    for line in description.strip().splitlines():
        print(f"  {line}")
    print()
    print("  " + "═" * 52)

    out_path = Path(output) if output else repo_root / OUTPUT_FILENAME
    if not out_path.is_absolute():
        out_path = repo_root / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(description.strip() + "\n", encoding="utf-8")
    ensure_gitignored(repo_root, OUTPUT_FILENAME)
    print()
    print(f"  Output written to: {out_path}")
