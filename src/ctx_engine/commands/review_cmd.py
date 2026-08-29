"""`ctx review` — AI-assisted code review with full structural awareness.

Unlike a generic diff reviewer, ctx review assembles the index's structural
context for every changed file: which danger zones the changed functions
touch, which functions call into the changed code, which architectural
decisions may be violated, and whether the index was maintained after the
edit. With --no-llm it runs the structural checks alone at zero API cost.
"""
import json
import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ctx_engine.db import connect

BATCH_SIZE = 5
MAX_CALLER_CALLEE_RECORDS = 8

# Matches unified-diff hunk headers: @@ -142,15 +142,17 @@ ...
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

REVIEW_SYSTEM_PROMPT = (
    "You are a senior code reviewer with full structural awareness of the "
    "codebase under review, provided via the ctx index. Be precise. Only flag "
    "real issues. Do not invent problems."
)


@dataclass
class ChangedRange:
    """A changed line range in the new version of a file (1-based, inclusive)."""

    start: int
    end: int

    def overlaps(self, line_start: int, line_end: int) -> bool:
        return self.start <= line_end and line_start <= self.end


@dataclass
class FileReviewPayload:
    path: str
    diff: str
    file_record: dict
    changed_functions: list[str] = field(default_factory=list)
    function_records: list[dict] = field(default_factory=list)
    caller_records: list[dict] = field(default_factory=list)
    callee_records: list[dict] = field(default_factory=list)
    danger_zones: list[dict] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)
    session_context: str | None = None
    is_stale: bool = False
    is_tainted: bool = False
    confidence_issues: list[str] = field(default_factory=list)
    file_content: str | None = None  # staged blob / on-disk, truncated for the prompt


# ── Git helpers ───────────────────────────────────────────────────────────────


def _git(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo_root, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise ValueError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def get_changed_line_ranges(diff_text: str) -> list[ChangedRange]:
    """Parse unified diff hunk headers into new-version changed line ranges.

    `@@ -142,15 +142,17 @@` means the new file's lines 142–158 (17 lines) are
    touched. Pure deletions (new-count 0) mark an insertion point at the
    preceding line so that a function containing that boundary still overlaps.
    """
    ranges: list[ChangedRange] = []
    for line in diff_text.splitlines():
        match = HUNK_RE.match(line)
        if not match:
            continue
        new_start = int(match.group(3))
        new_count = int(match.group(4)) if match.group(4) is not None else 1
        if new_count == 0:
            # Pure deletion — nothing exists at new_start; treat the boundary
            # line as changed so overlapping functions are still detected.
            ranges.append(ChangedRange(max(new_start, 1), max(new_start, 1)))
        else:
            ranges.append(ChangedRange(new_start, new_start + new_count - 1))
    return ranges


def resolve_review_paths(
    repo_root: Path,
    staged: bool,
    range_expr: str | None,
    file_path: str | None,
) -> tuple[list[str], dict[str, str], str]:
    """Return (changed paths, per-path unified diffs, mode description).

    - staged (default): `git diff --cached`, file content from the staged blob.
    - range: `git diff <c1> <c2>` split per file.
    - file: `git diff HEAD -- <path>` (or `git diff --cached -- <path>` when
      --staged is also passed).
    """
    if range_expr:
        if ".." not in range_expr:
            raise ValueError("--range expects a commit range like HEAD~3..HEAD")
        commit1, commit2 = range_expr.split("..", 1)
        full_diff = _git(repo_root, "diff", commit1, commit2)
        paths, diffs = _split_full_diff(full_diff)
        return paths, diffs, f"git range {range_expr}"

    if file_path:
        if staged:
            diff = _git(repo_root, "diff", "--cached", "--", file_path)
        else:
            diff = _git(repo_root, "diff", "HEAD", "--", file_path)
        if not diff.strip():
            return [], {}, f"file {file_path}"
        return [file_path], {file_path: diff}, f"file {file_path}"

    # Staged mode (default)
    paths = _git(
        repo_root, "diff", "--cached", "--name-only", "--diff-filter=ACM"
    )
    changed = [p.strip() for p in paths.splitlines() if p.strip()]
    diffs: dict[str, str] = {}
    for p in changed:
        diffs[p] = _git(repo_root, "diff", "--cached", "--", p)
    return changed, diffs, "staged changes"


def _split_full_diff(full_diff: str) -> tuple[list[str], dict[str, str]]:
    """Split a full multi-file unified diff into per-file diffs."""
    per_file: dict[str, list[str]] = {}
    current: str | None = None
    for line in full_diff.splitlines():
        if line.startswith("diff --git"):
            # diff --git a/path b/path — prefer the b/ side (new path)
            parts = line.split(" b/", 1)
            current = parts[1] if len(parts) == 2 else parts[0].split(" a/", 1)[-1]
            per_file.setdefault(current, [])
        if current is not None:
            per_file[current].append(line)
    return (
        list(per_file.keys()),
        {p: "\n".join(lines) for p, lines in per_file.items()},
    )


# ── Payload assembly ──────────────────────────────────────────────────────────


def _dangers_for_file(conn: sqlite3.Connection, path: str, changed_functions: list[str]) -> list[dict]:
    """Danger zones scoped to this file, its changed functions, or global ('*')."""
    scopes = [path, "*"] + changed_functions
    placeholders = ",".join("?" for _ in scopes)
    rows = conn.execute(
        f"SELECT id, scope, description, reason, added_by FROM dangers "
        f"WHERE scope IN ({placeholders})",
        scopes,
    ).fetchall()
    return [dict(r) for r in rows]


def _decisions_for_file(conn: sqlite3.Connection, path: str) -> list[dict]:
    """Decisions whose scope prefixes this file (or global)."""
    rows = conn.execute(
        "SELECT id, scope, decision, alternatives, reason FROM decisions"
    ).fetchall()
    matching = []
    for r in rows:
        scope = r["scope"]
        if scope is None or scope == "*" or path.startswith(scope.rstrip("/") + "/") or path == scope:
            matching.append(dict(r))
    return matching


def build_file_review_payload(
    conn: sqlite3.Connection,
    path: str,
    diff: str,
    repo_root: Path | None = None,
    file_getter=None,
) -> FileReviewPayload:
    """Assemble the full structural review payload for one changed file.

    file_getter(path) -> str | None overrides on-disk file content (e.g. the
    staged blob via `git show :0:<path>`). Defaults to reading from disk.
    """
    ranges = get_changed_line_ranges(diff)

    file_row = conn.execute(
        "SELECT * FROM files WHERE path = ?", (path,)
    ).fetchone()
    file_record = dict(file_row) if file_row else {"path": path, "note": "not in index"}

    # Staged mode passes a blob getter so unstaged working-tree edits never
    # leak into the review payload; without a getter, fall back to disk.
    if file_getter is not None:
        content = file_getter(path)
    else:
        base = repo_root if repo_root is not None else Path(".")
        try:
            content = (base / path).read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            content = None

    function_rows = conn.execute(
        "SELECT * FROM functions WHERE file = ? ORDER BY line_start", (path,)
    ).fetchall()

    changed_functions: list[str] = []
    changed_rows: list[sqlite3.Row] = []
    for fn in function_rows:
        if any(r.overlaps(fn["line_start"], fn["line_end"]) for r in ranges):
            changed_functions.append(fn["id"])
            changed_rows.append(fn)

    caller_records: list[dict] = []
    callee_records: list[dict] = []
    for fn_id in changed_functions:
        caller_rows = conn.execute(
            "SELECT f.* FROM call_graph cg JOIN functions f ON f.id = cg.caller_id "
            "WHERE cg.callee_id = ? LIMIT ?",
            (fn_id, MAX_CALLER_CALLEE_RECORDS),
        ).fetchall()
        callee_rows = conn.execute(
            "SELECT f.* FROM call_graph cg JOIN functions f ON f.id = cg.callee_id "
            "WHERE cg.caller_id = ? LIMIT ?",
            (fn_id, MAX_CALLER_CALLEE_RECORDS),
        ).fetchall()
        for r in caller_rows:
            rec = dict(r)
            if rec not in caller_records:
                caller_records.append(rec)
        for r in callee_rows:
            rec = dict(r)
            if rec not in callee_records:
                callee_records.append(rec)
    caller_records = caller_records[:MAX_CALLER_CALLEE_RECORDS]
    callee_records = callee_records[:MAX_CALLER_CALLEE_RECORDS]

    session_context = None
    session_rows = conn.execute(
        "SELECT entry, files_touched, timestamp FROM session_log ORDER BY id DESC LIMIT 20"
    ).fetchall()
    for row in session_rows:
        touched = row["files_touched"] or ""
        try:
            touched_list = json.loads(touched) if touched else []
        except json.JSONDecodeError:
            touched_list = [p.strip() for p in touched.split(",") if p.strip()]
        if path in touched_list:
            session_context = f"[{row['timestamp']}] {row['entry']}"
            break

    is_stale = bool(file_row["is_stale"]) if file_row else False
    is_tainted = any(r["is_tainted"] for r in changed_rows)
    confidence_issues = [
        r["id"] for r in changed_rows
        if r["confidence"] is not None and r["confidence"] < 0.5
    ]

    return FileReviewPayload(
        path=path,
        diff=diff,
        file_record=file_record,
        changed_functions=changed_functions,
        function_records=[dict(r) for r in changed_rows],
        caller_records=caller_records,
        callee_records=callee_records,
        danger_zones=_dangers_for_file(conn, path, changed_functions),
        decisions=_decisions_for_file(conn, path),
        session_context=session_context,
        is_stale=is_stale,
        is_tainted=is_tainted,
        confidence_issues=confidence_issues,
        file_content=content,
    )


def get_staged_blob(repo_root: Path, path: str) -> str | None:
    """Return the staged blob content for path (same rule as ctx validate)."""
    result = subprocess.run(
        ["git", "show", f":0:{path}"],
        cwd=repo_root,
        capture_output=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", errors="replace")


# ── Prompt construction ───────────────────────────────────────────────────────

MAX_FILE_CONTENT_LINES = 200

REVIEW_PROMPT_TEMPLATE = """STRUCTURAL CODE REVIEW REQUEST

You are reviewing code changes in a codebase indexed by ctx. You have full structural
awareness of the project — not just the diff, but the call graph, danger zones,
and architectural decisions that apply to this change. Use this context to give
a review that a generic code review tool could not.

PROJECT: {repo_name}

CHANGED FILE: {path}
{file_record_formatted}

CURRENT FILE CONTENT (truncated to {content_lines} lines):
{file_content}

THE DIFF:
{diff}

FUNCTIONS CHANGED (with full records):
{function_records_formatted}

CALLERS (functions that depend on this code — check if they break):
{caller_records_formatted}

CALLEES (functions this code now calls — check for new assumptions):
{callee_records_formatted}

DANGER ZONES THAT APPLY:
{danger_zones_formatted}

ARCHITECTURAL DECISIONS THAT APPLY:
{decisions_formatted}

INDEX HEALTH FOR THIS FILE:
{index_health}

Provide a structured review with exactly these sections:

1. SUMMARY (2-3 sentences: what the change does and whether it's correct)

2. DANGER ZONE VIOLATIONS (list each violated invariant; "none" if clean)
   For each violation: which danger, what in the diff violates it, severity (critical/major/minor)

3. CALL GRAPH IMPACT (which callers may break and why; "none" if clean)
   Only include callers that could plausibly be broken by this specific change.

4. ARCHITECTURAL DECISION CONFLICTS (list any decisions contradicted; "none" if clean)

5. MISSING INDEX UPDATES (did the author update ctx metadata after this edit?)
   Check: is is_stale = true (not updated)? Does last_change description match the diff?
   List what the author should have called but didn't.

6. SPECIFIC CONCERNS (anything else structurally suspicious — max 3 bullets)

7. VERDICT: APPROVE | REQUEST_CHANGES | NEEDS_DISCUSSION
   One word on its own line, then one sentence explaining.

Be precise. Only flag real issues. Do not invent problems."""


def _format_block(title: str, records: list[dict]) -> str:
    if not records:
        return "(none)"
    return json.dumps(records, indent=2, default=str)


def build_review_prompt(payload: FileReviewPayload, repo_name: str) -> str:
    file_content = payload.file_content or "(unavailable)"
    if payload.file_content:
        lines = payload.file_content.splitlines()
        if len(lines) > MAX_FILE_CONTENT_LINES:
            file_content = "\n".join(lines[:MAX_FILE_CONTENT_LINES]) + "\n... (truncated)"

    health_parts = []
    health_parts.append(
        "Index is stale for this file — metadata was not updated after the edit."
        if payload.is_stale
        else "Index is current."
    )
    if payload.is_tainted:
        health_parts.append(
            "One or more changed functions are TAINTED (their dependencies changed "
            "and the summaries were not refreshed)."
        )
    if payload.session_context:
        health_parts.append(f"Latest session log entry touching this file: {payload.session_context}")
    else:
        health_parts.append("No session_log entry touches this file.")
    if payload.confidence_issues:
        health_parts.append(
            "Low-confidence functions (< 0.5): " + ", ".join(payload.confidence_issues)
        )

    return REVIEW_PROMPT_TEMPLATE.format(
        repo_name=repo_name,
        path=payload.path,
        file_record_formatted=json.dumps(payload.file_record, indent=2, default=str),
        content_lines=MAX_FILE_CONTENT_LINES,
        file_content=file_content,
        diff=payload.diff or "(no textual diff — new or binary file)",
        function_records_formatted=_format_block("FUNCTIONS", payload.function_records),
        caller_records_formatted=_format_block("CALLERS", payload.caller_records),
        callee_records_formatted=_format_block("CALLEES", payload.callee_records),
        danger_zones_formatted=_format_block("DANGERS", payload.danger_zones),
        decisions_formatted=_format_block("DECISIONS", payload.decisions),
        index_health="\n".join(health_parts),
    )


VERDICT_RE = re.compile(r"VERDICT:\s*(APPROVE|REQUEST_CHANGES|NEEDS_DISCUSSION)")


def parse_verdict(response: str) -> str:
    """Extract the verdict word from an LLM response; default to NEEDS_DISCUSSION."""
    match = VERDICT_RE.search(response)
    return match.group(1) if match else "NEEDS_DISCUSSION"


# ── Output rendering ──────────────────────────────────────────────────────────


def _structural_findings(payload: FileReviewPayload) -> list[str]:
    """Structural-only findings for one file (used by --no-llm and the report)."""
    findings: list[str] = []
    if payload.is_stale:
        findings.append("✗ is_stale = True for this file — index not updated after editing.")
    for fn in payload.function_records:
        if fn.get("is_tainted"):
            findings.append(
                f"✗ {fn['id']}: is_tainted = True (taint not cleared by ctx_update_function)"
            )
    if payload.session_context is None and payload.changed_functions:
        findings.append(
            "✗ session_log has no entry for this file — ctx_log_session was not called."
        )
    for fn_id in payload.confidence_issues:
        findings.append(f"✗ {fn_id}: confidence < 0.5")
    for dz in payload.danger_zones:
        findings.append(
            f"⚠ change touches danger zone [{dz['scope']}]: {dz['description']}"
        )
    return findings


def print_structural_report(payloads: list[FileReviewPayload], mode_desc: str) -> None:
    """--no-llm mode: structural-only report, zero API cost."""
    if not payloads:
        print(f"ctx review — no changes found ({mode_desc}).")
        return
    print("ctx review — structural report (no LLM)")
    print()
    print(f"  Reviewing {mode_desc} ({len(payloads)} file(s))")
    print()
    for payload in payloads:
        print("  " + "═" * 52)
        print(f"  {payload.path}")
        print("  " + "═" * 52)
        if payload.changed_functions:
            print(f"  Changed functions: {', '.join(payload.changed_functions)}")
        else:
            print("  Changed functions: (none in index — file may be new or unindexed)")
        findings = _structural_findings(payload)
        print()
        if findings:
            print("  Structural findings:")
            for f in findings:
                print(f"    {f}")
        else:
            print("  ✓ no structural issues detected (index current, no danger overlap)")
        print()
    stale = sum(1 for p in payloads if p.is_stale)
    print("  " + "─" * 52)
    if stale:
        print(f"  OVERALL: structural issues in {stale} of {len(payloads)} file(s) — run 'ctx sync'.")
    else:
        print("  OVERALL: structurally clean. Run with LLM mode for a full review.")


def print_review_report(
    payloads: list[FileReviewPayload],
    responses: list[tuple[FileReviewPayload, str, int, int]],
    mode_desc: str,
) -> str:
    """Print the full per-file LLM review. Returns the overall verdict.

    Aggregation rule: any REQUEST_CHANGES in any file makes the overall
    verdict REQUEST_CHANGES; otherwise any NEEDS_DISCUSSION propagates.
    """
    overall = "APPROVE"
    total_in = total_out = 0
    for payload, response, in_tok, out_tok in responses:
        total_in += in_tok
        total_out += out_tok
        verdict = parse_verdict(response)
        if verdict == "REQUEST_CHANGES":
            overall = "REQUEST_CHANGES"
        elif verdict == "NEEDS_DISCUSSION" and overall == "APPROVE":
            overall = "NEEDS_DISCUSSION"

    print(f"ctx review — {repo_label(payloads)}")
    print()
    print(f"  Reviewing {mode_desc} ({len(payloads)} file(s))")
    for payload, response, _, _ in responses:
        print()
        print("  " + "═" * 52)
        print(f"  {payload.path}")
        print("  " + "═" * 52)
        for line in response.strip().splitlines():
            print(f"  {line}")
    print()
    print("  " + "═" * 52)
    print(f"  OVERALL: {overall}")
    print("  " + "═" * 52)
    print(f"  Tokens used: {total_in:,} input, {total_out:,} output")
    return overall


def repo_label(payloads: list[FileReviewPayload]) -> str:
    return payloads[0].file_record.get("path", "") if payloads else ""


# ── Entry point ───────────────────────────────────────────────────────────────

BATCH_INTRO = """You are reviewing {count} changed file(s) in one pass. For EACH file,
produce the full 7-section structured review requested in that file's block.
Begin each file's review with the exact line:

=== FILE: <path> ===

Do not merge files. Do not skip any file."""


def build_batch_prompt(payloads: list[FileReviewPayload], repo_name: str) -> str:
    """Combine per-file payload blocks into one batch prompt."""
    blocks = []
    for payload in payloads:
        blocks.append(f"=== FILE: {payload.path} ===\n{build_review_prompt(payload, repo_name)}")
    return BATCH_INTRO.format(count=len(payloads)) + "\n\n" + "\n\n".join(blocks)


FILE_MARKER_RE = re.compile(r"^=== FILE:\s*(.+?)\s*===", re.MULTILINE)


def parse_batch_response(response: str, payloads: list[FileReviewPayload]) -> dict[str, str]:
    """Split a batch response into per-file review texts keyed by path.

    Files the model failed to cover map to a placeholder so downstream
    verdict aggregation stays deterministic.
    """
    matches = list(FILE_MARKER_RE.finditer(response))
    per_file: dict[str, str] = {}
    for i, match in enumerate(matches):
        path = match.group(1).strip()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(response)
        per_file[path] = response[match.end():end].strip()
    # Tolerate path formatting drift by falling back to positional order.
    if set(per_file) != {p.path for p in payloads} and len(matches) == len(payloads):
        per_file = {p.path: text for p, text in zip(payloads, per_file.values())}
    for payload in payloads:
        per_file.setdefault(payload.path, "(no review returned for this file)")
    return per_file


def run_review(
    repo_root: Path,
    staged: bool = False,
    range_expr: str | None = None,
    file_path: str | None = None,
    no_llm: bool = False,
) -> None:
    db_path = repo_root / ".ctx" / "index.db"
    if not db_path.exists():
        raise FileNotFoundError("Database not found. Run 'ctx init' first.")

    changed_paths, diffs, mode_desc = resolve_review_paths(
        repo_root, staged, range_expr, file_path
    )
    if not changed_paths:
        print(f"ctx review: no changes to review ({mode_desc}).")
        return

    conn = connect(db_path)
    try:
        payloads: list[FileReviewPayload] = []
        for path in changed_paths:
            payload = build_file_review_payload(
                conn, path, diffs.get(path, ""),
                repo_root=repo_root,
                # Staged mode reads the staged blob so unstaged working-tree
                # edits never leak into the review payload.
                file_getter=(lambda p: get_staged_blob(repo_root, p))
                if staged and not range_expr
                else None,
            )
            payloads.append(payload)
    finally:
        conn.close()

    if no_llm:
        print_structural_report(payloads, mode_desc)
        return

    from ctx_engine.intelligence.llm_client import (
        call_llm_with_retry,
        get_anthropic_client,
    )

    client = get_anthropic_client()
    model = _resolve_review_model()
    repo_name = repo_root.name

    # One LLM call per batch of 5 files; each call covers the full payload
    # for every file in the batch.
    batches = [payloads[i:i + BATCH_SIZE] for i in range(0, len(payloads), BATCH_SIZE)]
    responses: list[tuple[FileReviewPayload, str, int, int]] = []
    for batch_num, batch in enumerate(batches, start=1):
        if len(batches) > 1:
            print(
                f"  Reviewing batch {batch_num}/{len(batches)} ({len(batch)} files)...",
                flush=True,
            )
        prompt = build_batch_prompt(batch, repo_name)
        response, in_tok, out_tok = call_llm_with_retry(
            client, model, REVIEW_SYSTEM_PROMPT, prompt, max_tokens=6000
        )
        per_file = parse_batch_response(response, batch)
        for payload in batch:
            responses.append((payload, per_file[payload.path], in_tok, out_tok))

    print_review_report(payloads, responses, mode_desc)


def _resolve_review_model() -> str:
    """Model precedence: CTX_REVIEW_MODEL > CTX_LLM_MODEL > haiku default.

    Reviews are frequent and mechanical — the fast summarization model is the
    right default, unlike ctx explain (which defaults to a larger model).
    """
    import os

    return (
        os.environ.get("CTX_REVIEW_MODEL")
        or os.environ.get("CTX_LLM_MODEL")
        or "claude-haiku-4-5-20251001"
    )