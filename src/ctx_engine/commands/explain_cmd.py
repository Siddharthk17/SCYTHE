"""`ctx explain` \u2014 deep, structured LLM explanation of a file, function, or system.

Unlike `ctx summarize` (which produces compact 15-word metadata for the index),
`ctx explain` produces a rich, structured analysis intended to be read by a
developer or agent who is about to make non-trivial changes to the code.
"""
import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ctx_engine.db import connect
from ctx_engine.intelligence.llm_client import (
    call_llm_with_retry,
    get_anthropic_client,
    get_model_name,
)

logger = logging.getLogger("ctx")


# Default explanation model is larger than the summarization model.
# Quality matters more than cost here \u2014 this is human-facing.
DEFAULT_EXPLAIN_MODEL = "claude-sonnet-4-6"


EXPLAIN_FUNCTION_PROMPT = """You are analyzing a specific function in a codebase indexed by ctx.
Your explanation will be read by a developer who is about to modify this function.
Be concrete and specific \u2014 not generic. Reference actual variable names, types, and behaviors.

FUNCTION TO EXPLAIN:
{function_record}

FULL SOURCE:
{source}

FILE CONTEXT:
{file_record}

THIS FUNCTION'S CALLERS (may depend on its current behavior):
{callers}

THIS FUNCTION'S DEPENDENCIES (functions it calls):
{callees}

DANGER ZONES AND DECISIONS FOR THIS FILE:
{dangers_and_decisions}

Provide a structured explanation with exactly these sections:
1. WHAT IT DOES (2-3 sentences, step-by-step)
2. WHY DESIGNED THIS WAY (explain the key design choice if non-obvious; skip if straightforward)
3. PRECONDITIONS (what must be true when this is called; null if none)
4. POSTCONDITIONS (what is guaranteed to be true after it returns; null if none)
5. THREE DANGEROUS EDITS (specific things that would silently break callers)
6. HOW TO CALL CORRECTLY (a concrete example with actual types/values)

Be direct and specific. Do not use filler phrases. If something is unknown, say so."""


EXPLAIN_FILE_PROMPT = """You are analyzing a single file in a codebase indexed by ctx.
Your explanation will be read by a developer who is about to make changes touching this file.
Be concrete and specific \u2014 reference actual functions, types, and behaviors.

FILE PATH: {file_path}

FILE METADATA:
{file_record}

DANGER ZONES AND DECISIONS:
{dangers_and_decisions}

FULL SOURCE:
{source}

FILES THAT IMPORT THIS FILE (depend on it):
{importers}

Provide a structured explanation with exactly these sections:
1. WHAT THIS FILE DOES (2-3 sentences, what problem it solves and why it's separate)
2. KEY EXPORTS (the main public surface \u2014 not just a list, but how they interact)
3. INTERNAL DATA FLOW (how the key functions compose)
4. WHAT WOULD BREAK (specific consequences of changing the file's API)
5. DANGER ZONES (real-world consequences of the documented constraints)

Be direct and specific. Do not use filler phrases."""


EXPLAIN_SYSTEM_PROMPT = """You are analyzing a system (a logical grouping of files) in a codebase indexed by ctx.
Your explanation will be read by a developer trying to understand the system before making changes.

SYSTEM NAME: {system_name}

FILES IN THIS SYSTEM ({file_count}):
{files_summary}

DANGER ZONES FOR THIS SYSTEM:
{dangers_and_decisions}

Provide a structured explanation with exactly these sections:
1. WHAT THIS SYSTEM DOES (high-level purpose, 2-3 sentences)
2. KEY FILES (the 3-5 most important files in this system, and their roles)
3. ENTRY POINTS (the public API of the system \u2014 what callers typically use)
4. INTERNAL DATA FLOW (how the files compose to deliver the system's purpose)
5. KNOWN LIMITATIONS (technical debt, fragile spots, scalability concerns)

Be direct and specific. Do not use filler phrases."""


@dataclass
class ExplainTarget:
    kind: str  # "function" | "file" | "system"
    identifier: str  # the function id, file path, or system name


# Anthropic pricing (per million tokens) \u2014 best-known rates as of 2026-08.
# Used to give the user a cost estimate at the end of the explanation.
_PRICING = {
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
}


def _classify_target(target: str, conn: sqlite3.Connection) -> ExplainTarget:
    """Determine the target kind from the input string.

    Heuristic:
      - Contains '::' \u2192 function id
      - Matches a row in files \u2192 file path
      - Matches a system name (files.system = ?) \u2192 system name
      - Otherwise raise ValueError
    """
    if "::" in target:
        return ExplainTarget(kind="function", identifier=target)
    row = conn.execute("SELECT 1 FROM files WHERE path = ?", (target,)).fetchone()
    if row is not None:
        return ExplainTarget(kind="file", identifier=target)
    sys_row = conn.execute(
        "SELECT 1 FROM files WHERE system = ? LIMIT 1", (target,)
    ).fetchone()
    if sys_row is not None:
        return ExplainTarget(kind="system", identifier=target)
    raise ValueError(
        f"Target '{target}' is neither a function id (path::Class.method), a file path, nor a system name. "
        f"Run 'ctx status' to see indexed systems."
    )


def _resolve_callers(conn: sqlite3.Connection, function_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT caller_id, callee_name FROM call_graph WHERE callee_id = ?",
        (function_id,),
    ).fetchall()
    return [{"caller_id": r["caller_id"], "callee_name": r["callee_name"]} for r in rows]


def _resolve_callees(conn: sqlite3.Connection, function_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT callee_id, callee_name, callee_file FROM call_graph WHERE caller_id = ?",
        (function_id,),
    ).fetchall()
    return [
        {"callee_id": r["callee_id"], "callee_name": r["callee_name"], "callee_file": r["callee_file"]}
        for r in rows
    ]


def _read_source_lines(repo_root: Path, file_path: str, line_start: int, line_end: int) -> str:
    try:
        abs_path = repo_root / file_path
        lines = abs_path.read_text(encoding="utf-8").splitlines()
        return "\n".join(lines[line_start - 1:line_end])
    except (FileNotFoundError, PermissionError, OSError, UnicodeDecodeError) as err:
        return f"(source not available: {err})"


def _dangers_for_file(conn: sqlite3.Connection, file_path: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, scope, description, reason, added_by FROM dangers "
        "WHERE scope = ? OR scope = '*' ORDER BY scope, rowid",
        (file_path,),
    ).fetchall()
    return [dict(r) for r in rows]


def _decisions_for_file(conn: sqlite3.Connection, file_path: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, scope, decision, alternatives, reason, added_by FROM decisions "
        "WHERE scope = ? OR scope IS NULL ORDER BY scope, rowid",
        (file_path,),
    ).fetchall()
    return [dict(r) for r in rows]


def _explain_function(
    target: ExplainTarget, conn: sqlite3.Connection, repo_root: Path,
    model: str,
) -> tuple[str, int, int]:
    row = conn.execute("SELECT * FROM functions WHERE id = ?", (target.identifier,)).fetchone()
    if row is None:
        raise ValueError(f"Function not found in index: {target.identifier}")

    file_row = conn.execute("SELECT * FROM files WHERE path = ?", (row["file"],)).fetchone()
    if file_row is None:
        raise ValueError(f"File '{row['file']}' (containing {target.identifier}) is not in the index.")

    source = _read_source_lines(repo_root, row["file"], row["line_start"], row["line_end"])
    callers = _resolve_callers(conn, target.identifier)
    callees = _resolve_callees(conn, target.identifier)
    dangers = _dangers_for_file(conn, row["file"])
    decisions = _decisions_for_file(conn, row["file"])

    # Make caller / callee human-readable (resolve to function records)
    if callers:
        caller_ids = list({c["caller_id"] for c in callers if c["caller_id"]})
        if caller_ids:
            placeholders = ",".join("?" for _ in caller_ids)
            cr = conn.execute(
                f"SELECT id, file, name, class_name, signature FROM functions WHERE id IN ({placeholders})",
                caller_ids,
            ).fetchall()
            for c in callers:
                match = next((m for m in cr if m["id"] == c["caller_id"]), None)
                if match:
                    c["signature"] = match["signature"]
                    c["file"] = match["file"]
                    name = f"{match['class_name']}.{match['name']}" if match["class_name"] else match["name"]
                    c["display_name"] = name

    if callees:
        callee_ids = list({c["callee_id"] for c in callees if c["callee_id"]})
        if callee_ids:
            placeholders = ",".join("?" for _ in callee_ids)
            cr = conn.execute(
                f"SELECT id, file, name, class_name, signature FROM functions WHERE id IN ({placeholders})",
                callee_ids,
            ).fetchall()
            for c in callees:
                match = next((m for m in cr if m["id"] == c["callee_id"]), None)
                if match:
                    c["signature"] = match["signature"]
                    c["file"] = match["file"]
                    name = f"{match['class_name']}.{match['name']}" if match["class_name"] else match["name"]
                    c["display_name"] = name

    dangers_and_decisions = json.dumps({"dangers": dangers, "decisions": decisions}, indent=2)

    if not callers:
        callers_str = "(none)"
    else:
        callers_str = "\n".join(
            f"  {c.get('display_name', c.get('caller_id', '?'))} ({c.get('file', '?')})"
            for c in callers
        )

    if not callees:
        callees_str = "(none)"
    else:
        callees_str = "\n".join(
            f"  {c.get('display_name', c.get('callee_name', '?'))} ({c.get('callee_file') or c.get('file', '?')})"
            for c in callees
        )

    prompt = EXPLAIN_FUNCTION_PROMPT.format(
        function_record=json.dumps(dict(row), indent=2, default=str),
        source=source,
        file_record=json.dumps(dict(file_row), indent=2, default=str),
        callers=callers_str,
        callees=callees_str,
        dangers_and_decisions=dangers_and_decisions,
    )

    client = get_anthropic_client()
    response, in_tok, out_tok = call_llm_with_retry(client, model, "", prompt, max_tokens=2000)
    return response, in_tok, out_tok


def _explain_file(
    target: ExplainTarget, conn: sqlite3.Connection, repo_root: Path,
    model: str,
) -> tuple[str, int, int]:
    file_row = conn.execute("SELECT * FROM files WHERE path = ?", (target.identifier,)).fetchone()
    if file_row is None:
        raise ValueError(f"File not found in index: {target.identifier}")

    abs_path = repo_root / target.identifier
    try:
        source = abs_path.read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, OSError) as err:
        source = f"(source not available: {err})"

    importers = conn.execute(
        "SELECT path FROM files WHERE imports LIKE ?",
        (f'%"{target.identifier}"%',),
    ).fetchall()
    importers_str = "\n".join(f"  {r['path']}" for r in importers) or "(none)"

    dangers = _dangers_for_file(conn, target.identifier)
    decisions = _decisions_for_file(conn, target.identifier)
    dangers_and_decisions = json.dumps({"dangers": dangers, "decisions": decisions}, indent=2)

    prompt = EXPLAIN_FILE_PROMPT.format(
        file_path=target.identifier,
        file_record=json.dumps(dict(file_row), indent=2, default=str),
        dangers_and_decisions=dangers_and_decisions,
        source=source,
        importers=importers_str,
    )

    client = get_anthropic_client()
    response, in_tok, out_tok = call_llm_with_retry(client, model, "", prompt, max_tokens=2000)
    return response, in_tok, out_tok


def _explain_system(
    target: ExplainTarget, conn: sqlite3.Connection, repo_root: Path,
    model: str,
) -> tuple[str, int, int]:
    file_rows = conn.execute(
        "SELECT path, purpose, summary, exports, danger FROM files "
        "WHERE system = ? ORDER BY path",
        (target.identifier,),
    ).fetchall()
    if not file_rows:
        raise ValueError(f"No files indexed under system '{target.identifier}'.")

    files_summary_lines = []
    for r in file_rows:
        purpose = r["purpose"] or r["summary"] or "(not summarized)"
        files_summary_lines.append(f"  - {r['path']}: {purpose}")
    files_summary = "\n".join(files_summary_lines)

    danger_rows = conn.execute(
        "SELECT id, scope, description, reason FROM dangers "
        "WHERE scope IN (SELECT path FROM files WHERE system = ?) OR scope = '*'",
        (target.identifier,),
    ).fetchall()
    decision_rows = conn.execute(
        "SELECT id, scope, decision, alternatives, reason FROM decisions "
        "WHERE scope IN (SELECT path FROM files WHERE system = ?) OR scope IS NULL",
        (target.identifier,),
    ).fetchall()
    dangers_and_decisions = json.dumps({
        "dangers": [dict(r) for r in danger_rows],
        "decisions": [dict(r) for r in decision_rows],
    }, indent=2)

    prompt = EXPLAIN_SYSTEM_PROMPT.format(
        system_name=target.identifier,
        file_count=len(file_rows),
        files_summary=files_summary,
        dangers_and_decisions=dangers_and_decisions,
    )

    client = get_anthropic_client()
    response, in_tok, out_tok = call_llm_with_retry(client, model, "", prompt, max_tokens=2000)
    return response, in_tok, out_tok


def _estimate_cost(model: str, in_tok: int, out_tok: int) -> tuple[float, str]:
    rates = _PRICING.get(model)
    if rates is None:
        return (0.0, "(unknown model rate)")
    cost = (in_tok / 1_000_000) * rates["input"] + (out_tok / 1_000_000) * rates["output"]
    return cost, f"${cost:.4f}"


def _resolve_explain_model() -> str:
    """Model precedence: CTX_EXPLAIN_MODEL > CTX_LLM_MODEL > DEFAULT_EXPLAIN_MODEL."""
    return (
        os.environ.get("CTX_EXPLAIN_MODEL")
        or os.environ.get("CTX_LLM_MODEL")
        or DEFAULT_EXPLAIN_MODEL
    )


def run_explain(repo_root: Path, target: str, depth: str | None = None) -> None:
    db_path = repo_root / ".ctx" / "index.db"
    if not db_path.exists():
        raise FileNotFoundError("Database not found. Run 'ctx init' first.")

    conn = connect(db_path)
    try:
        classified = _classify_target(target, conn)
        kind = depth or classified.kind

        model = _resolve_explain_model()
        if kind == "function":
            response, in_tok, out_tok = _explain_function(classified, conn, repo_root, model)
        elif kind == "file":
            response, in_tok, out_tok = _explain_file(classified, conn, repo_root, model)
        elif kind == "system":
            response, in_tok, out_tok = _explain_system(classified, conn, repo_root, model)
        else:
            raise ValueError(f"Unknown depth: {kind}")

        # Print the explanation
        if kind == "function":
            func_row = conn.execute(
                "SELECT file, line_start, line_end, class_name, name FROM functions WHERE id = ?",
                (classified.identifier,),
            ).fetchone()
            if func_row:
                display = f"{func_row['class_name']}.{func_row['name']}" if func_row["class_name"] else func_row["name"]
                print(f"ctx explain \"{classified.identifier}\"")
                print()
                print(f"  {'=' * 60}")
                print(f"  {display}  ({func_row['file']}, lines {func_row['line_start']}-{func_row['line_end']})")
                print(f"  {'=' * 60}")
        elif kind == "file":
            print(f"ctx explain \"{classified.identifier}\"")
            print()
            print(f"  {'=' * 60}")
            print(f"  {classified.identifier}")
            print(f"  {'=' * 60}")
        else:
            print(f"ctx explain \"{classified.identifier}\" --depth system")
            print()
            print(f"  {'=' * 60}")
            print(f"  System: {classified.identifier}")
            print(f"  {'=' * 60}")

        print()
        print(response)

        # Cost summary
        cost, cost_str = _estimate_cost(model, in_tok, out_tok)
        print()
        print(f"  model: {model}  |  tokens: in={in_tok:,} out={out_tok:,}  |  cost: {cost_str}")
    finally:
        conn.close()
