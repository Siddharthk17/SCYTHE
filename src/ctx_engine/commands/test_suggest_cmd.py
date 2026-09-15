"""`ctx test-suggest` — concrete test-case suggestions from index structure.

Pure static analysis by default (danger zones, mutations, callers, call
depth, staleness) — instant and free. With --llm, the static findings plus
the function's source are sent to the model for generated test code.
"""
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from ctx_engine.db import connect
from ctx_engine.commands.review_cmd import (
    get_changed_line_ranges,
    resolve_review_paths,
)


@dataclass
class TestSuggestion:
    category: str  # "danger" | "mutation" | "caller" | "depth" | "staleness"
    priority: str  # "CRITICAL" | "HIGH" | "MEDIUM" | "LOW"
    title: str
    detail: str


PRIORITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}


def _parse_mutations(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [i for i in items if isinstance(i, str)]


def _is_test_file(path: str) -> bool:
    lowered = path.lower()
    return (
        "test" in Path(path).name.lower()
        or "/tests/" in lowered
        or lowered.startswith("tests/")
    )


def suggest_for_function(
    conn: sqlite3.Connection, function_id: str
) -> list[TestSuggestion]:
    """Generate suggestions for one function from five static sources."""
    row = conn.execute(
        "SELECT * FROM functions WHERE id = ?", (function_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"Function not found in index: '{function_id}'.")
    fn = dict(row)
    suggestions: list[TestSuggestion] = []
    mutations = _parse_mutations(fn.get("mutates"))

    # Source 1 — danger zones: function-level danger plus scoped records.
    danger_texts: list[str] = []
    if fn.get("danger"):
        danger_texts.append(fn["danger"])
    for dz in conn.execute(
        "SELECT scope, description FROM dangers WHERE scope IN (?, ?)",
        (function_id, fn["file"]),
    ).fetchall():
        if dz["description"] not in danger_texts:
            danger_texts.append(dz["description"])
    for i, danger in enumerate(danger_texts, start=1):
        suggestions.append(TestSuggestion(
            category="danger",
            priority="CRITICAL",
            title=f"DZ-{i}: {danger[:80]}",
            detail=(
                f"Danger: \"{danger}\"\n"
                f"→ Test: write a test that drives {function_id} into the "
                f"condition above and asserts the invariant holds. "
                f"Check the function's signature ({fn['signature']}) for "
                f"how to trigger it."
            ),
        ))

    # Source 2 — mutations: one suggestion per mutated item.
    for i, target in enumerate(mutations, start=1):
        suggestions.append(TestSuggestion(
            category="mutation",
            priority="MEDIUM",
            title=f"MUT-{i}: {target}",
            detail=(
                f"Mutates: {target}\n"
                f"→ Test: assert the post-call state of `{target}` after "
                f"{function_id} returns for a valid input. "
                f"Test the boundary: what happens on empty or invalid input?"
            ),
        ))

    # Source 3 — callers: existing test coverage vs. gaps.
    callers = conn.execute(
        "SELECT f.id, f.file, f.line_start, f.line_end FROM call_graph cg "
        "JOIN functions f ON f.id = cg.caller_id "
        "WHERE cg.callee_id = ? ORDER BY f.file",
        (function_id,),
    ).fetchall()
    for n, caller in enumerate(callers, start=1):
        caller_id = caller["id"]
        if _is_test_file(caller["file"]):
            suggestions.append(TestSuggestion(
                category="caller",
                priority="LOW",
                title=f"CALLER-{n}: {caller_id} already covered by tests",
                detail=(
                    f"→ Verify: {caller['file']} exercises {function_id} via "
                    f"{caller_id} (lines {caller['line_start']}–{caller['line_end']}). "
                    f"Keep this path covered when changing behavior."
                ),
            ))
        else:
            suggestions.append(TestSuggestion(
                category="caller",
                priority="HIGH",
                title=f"CALLER-{n}: {caller_id} calls {function_id} with no test coverage",
                detail=(
                    f"→ Gap: {caller_id} ({caller['file']}) calls {function_id} "
                    f"but no test exercises this path. "
                    f"Add a test that calls {caller_id} and asserts the "
                    f"behavior it assumes from {function_id}. "
                    f"Also assert the empty-input case for {function_id} itself."
                ),
            ))

    # Source 4 — call graph depth: many callers warrant parameterized tests.
    caller_count = len(callers)
    if caller_count >= 2:
        suggestions.append(TestSuggestion(
            category="depth",
            priority="MEDIUM" if caller_count > 5 else "LOW",
            title=f"DEPTH: {function_id} has {caller_count} direct callers",
            detail=(
                f"→ Consider: parameterized tests covering the distinct call "
                f"patterns ({caller_count} callers"
                f"{' — above the 5-caller threshold' if caller_count > 5 else ''}). "
                f"Cover single-item, batched, and direct-call patterns."
            ),
        ))

    # Source 5 — confidence/staleness.
    confidence = fn.get("confidence")
    if fn.get("is_stale") or (confidence is not None and confidence < 0.8):
        suggestions.append(TestSuggestion(
            category="staleness",
            priority="CRITICAL",
            title=f"STALE: {function_id} metadata may be outdated",
            detail=(
                "Warning: function metadata may be stale — verify test "
                "assertions against current code, not the indexed summary. "
                "Run 'ctx sync' first."
            ),
        ))

    suggestions.sort(key=lambda s: (PRIORITY_ORDER[s.priority], s.category, s.title))
    return suggestions


def functions_for_staged(
    conn: sqlite3.Connection, repo_root: Path
) -> list[str]:
    """Function ids whose line ranges overlap the staged diff."""
    paths, diffs, _ = resolve_review_paths(repo_root, True, None, None)
    found: list[str] = []
    for path in paths:
        ranges = get_changed_line_ranges(diffs.get(path, ""))
        for fn in conn.execute(
            "SELECT id, line_start, line_end FROM functions WHERE file = ? "
            "ORDER BY line_start",
            (path,),
        ).fetchall():
            if any(r.overlaps(fn["line_start"], fn["line_end"]) for r in ranges):
                found.append(fn["id"])
    return found


def suggestions_to_json(
    targets: dict[str, list[TestSuggestion]]
) -> dict:
    return {
        "targets": list(targets),
        "suggestions": {
            fn_id: [
                {
                    "category": s.category,
                    "priority": s.priority,
                    "title": s.title,
                    "detail": s.detail,
                }
                for s in items
            ]
            for fn_id, items in targets.items()
        },
        "action_list": [
            {"function": fn_id, "priority": s.priority, "title": s.title}
            for fn_id, items in targets.items()
            for s in items
        ],
    }


def print_text_suggestions(
    targets: dict[str, list[TestSuggestion]], llm: bool = False
) -> None:
    for fn_id, items in targets.items():
        print(f"ctx test-suggest \"{fn_id}\"")
        print()
        print(f"  TEST SUGGESTIONS FOR: {fn_id}")
        if not llm:
            print("  (Pure static analysis — use --llm for generated test code)")
        by_category: dict[str, list[TestSuggestion]] = {}
        for item in items:
            by_category.setdefault(item.category, []).append(item)
        labels = {
            "danger": "FROM DANGER ZONES",
            "mutation": "FROM MUTATIONS",
            "caller": "FROM CALLERS",
            "depth": "FROM CALL GRAPH DEPTH",
            "staleness": "STALENESS WARNINGS",
        }
        for category, label in labels.items():
            group = by_category.get(category, [])
            if not group:
                continue
            print()
            print("  " + "═" * 46)
            print(f"  {label} ({len(group)} suggestions)")
            print("  " + "═" * 46)
            print()
            for item in group:
                print(f"  {item.title}")
                for line in item.detail.splitlines():
                    print(f"    {line}")
                print()
        print("  " + "═" * 46)
        print("  PRIORITIZED ACTION LIST")
        print("  " + "═" * 46)
        print()
        if not items:
            print("  No suggestions — function has no danger zones, mutations, or callers.")
        for n, item in enumerate(items, start=1):
            print(f"  {n}. [{item.priority}] {item.title}")
        print()
        print(f"  Total suggested tests: {len(items)}")


def run_test_suggest(
    repo_root: Path,
    function_id: str | None = None,
    staged: bool = False,
    format: str = "text",
    llm: bool = False,
) -> None:
    db_path = repo_root / ".ctx" / "index.db"
    if not db_path.exists():
        raise FileNotFoundError("Database not found. Run 'ctx init' first.")

    conn = connect(db_path)
    try:
        if function_id:
            targets = {function_id: suggest_for_function(conn, function_id)}
        elif staged:
            ids = functions_for_staged(conn, repo_root)
            if not ids:
                print("ctx test-suggest: no indexed functions overlap the staged diff.")
                return
            targets = {fn_id: suggest_for_function(conn, fn_id) for fn_id in ids}
        else:
            raise ValueError("Pass a <function-id> or use --staged.")
    finally:
        conn.close()

    if format == "json":
        print(json.dumps(suggestions_to_json(targets), indent=2))
        return

    if llm:
        print_llm_suggestions(repo_root, targets)
    else:
        print_text_suggestions(targets)


def print_llm_suggestions(
    repo_root: Path, targets: dict[str, list[TestSuggestion]]
) -> None:
    """Send static findings plus source to the LLM for generated test code."""
    from ctx_engine.intelligence.llm_client import (
        call_llm_with_retry,
        get_anthropic_client,
    )
    import os

    client = get_anthropic_client()
    model = os.environ.get("CTX_LLM_MODEL") or "claude-haiku-4-5-20251001"
    for fn_id, items in targets.items():
        static_block = "\n".join(f"- [{s.priority}] {s.title}: {s.detail}" for s in items)
        prompt = (
            f"Function under test: {fn_id}\n\n"
            f"Static analysis findings:\n{static_block}\n\n"
            "Generate concise Python test functions (pytest style) covering "
            "the findings above. Match the existing test style of the repo. "
            "Return test code only."
        )
        response, _, _ = call_llm_with_retry(
            client, model,
            "You generate focused pytest test stubs from static analysis findings.",
            prompt, max_tokens=3000,
        )
        print(f"ctx test-suggest \"{fn_id}\" --llm")
        print()
        print(response.strip())
        print()
