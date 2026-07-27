import hashlib
import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("ctx")

CALL_FANIN_THRESHOLD = 10
IMPORT_FANIN_THRESHOLD = 15
INVARIANT_KEYWORDS = [
    "must", "never", "always", "invariant", "critical",
    "important", "warning", "assert", "do not", "don't",
]


@dataclass
class DangerRecord:
    id: str
    scope: str
    description: str
    reason: str | None


@dataclass
class HeuristicReport:
    detected: list[DangerRecord] = field(default_factory=list)
    added: list[DangerRecord] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


def _gen_id(*parts: str) -> str:
    combined = "".join(parts)
    return hashlib.sha256(combined.encode()).hexdigest()[:12]


def detect_high_call_fanin(conn) -> list[DangerRecord]:
    rows = conn.execute(
        """SELECT callee_id, COUNT(*) as caller_count
           FROM call_graph
           WHERE callee_id IS NOT NULL
           GROUP BY callee_id
           HAVING caller_count >= ?
           ORDER BY caller_count DESC""",
        (CALL_FANIN_THRESHOLD,),
    ).fetchall()

    dangers = []
    for row in rows:
        fn_row = conn.execute(
            "SELECT id, name, file FROM functions WHERE id = ?", (row["callee_id"],)
        ).fetchone()
        if fn_row is None:
            continue
        danger_id = _gen_id("call_fanin:", fn_row["id"])
        dangers.append(DangerRecord(
            id=danger_id,
            scope=fn_row["id"],
            description=(
                f"High-frequency function: called by {row['caller_count']} callers. "
                f"Changes to signature or behavior may have wide cascading impact."
            ),
            reason=(
                f"Automatic detection: {row['caller_count']} call graph edges point to "
                f"this function. Validate all callers after any modification."
            ),
        ))
    return dangers


def detect_high_import_fanin(conn) -> list[DangerRecord]:
    rows = conn.execute(
        "SELECT path, used_by_count FROM files WHERE used_by_count >= ?",
        (IMPORT_FANIN_THRESHOLD,),
    ).fetchall()

    dangers = []
    for row in rows:
        danger_id = _gen_id("import_fanin:", row["path"])
        dangers.append(DangerRecord(
            id=danger_id,
            scope=row["path"],
            description=(
                f"High-frequency module: imported by {row['used_by_count']} files. "
                f"API or export changes require updates across the project."
            ),
            reason=(
                f"Automatic detection: {row['used_by_count']} import edges point to "
                f"this file. Changing its public API is a project-wide breaking change."
            ),
        ))
    return dangers


def detect_global_mutations(conn) -> list[DangerRecord]:
    rows = conn.execute(
        "SELECT id, file, mutates FROM functions "
        "WHERE mutates IS NOT NULL AND mutates LIKE '%global:%'"
    ).fetchall()

    dangers = []
    for row in rows:
        try:
            mutates = json.loads(row["mutates"])
        except (json.JSONDecodeError, TypeError):
            continue
        global_vars = [m for m in mutates if m.startswith("global:")]
        if not global_vars:
            continue
        var_list = ", ".join(global_vars)
        danger_id = _gen_id("global_mutation:", row["id"])
        dangers.append(DangerRecord(
            id=danger_id,
            scope=row["id"],
            description=(
                f"Mutates global state: {var_list}. "
                f"Concurrent callers or re-entrant calls may see inconsistent state."
            ),
            reason=(
                f"Automatic detection: this function writes to module-level variables. "
                f"Ensure mutations are intentional and thread-safe if applicable."
            ),
        ))
    return dangers


def detect_invariant_comments(conn, repo_root: Path) -> list[DangerRecord]:
    fn_rows = conn.execute(
        "SELECT id, file, line_start, line_end FROM functions"
    ).fetchall()

    by_file = defaultdict(list)
    for row in fn_rows:
        by_file[row["file"]].append(row)

    dangers = []
    for file_path, fns in by_file.items():
        try:
            lines = (repo_root / file_path).read_text(encoding="utf-8").splitlines()
        except (IOError, UnicodeDecodeError):
            continue

        for fn_row in fns:
            start = fn_row["line_start"] - 1
            end = fn_row["line_end"]
            fn_lines = lines[start:end]

            comment_lines = []
            for line in fn_lines:
                stripped = line.strip()
                if stripped.startswith("#"):
                    comment_lines.append(stripped.lstrip("#").strip())

            for comment in comment_lines:
                comment_lower = comment.lower()
                if any(kw in comment_lower for kw in INVARIANT_KEYWORDS):
                    truncated = comment[:120] + ("..." if len(comment) > 120 else "")
                    danger_id = _gen_id(
                        "invariant_comment:", fn_row["id"], ":", comment[:60]
                    )
                    dangers.append(DangerRecord(
                        id=danger_id,
                        scope=fn_row["id"],
                        description=f"Invariant comment: \"{truncated}\"",
                        reason=(
                            "Automatic detection: function contains a comment with "
                            "invariant language. Review before modifying."
                        ),
                    ))
                    break

    return dangers


def run_heuristic_detection(
    conn,
    repo_root: Path,
    dry_run: bool = False,
) -> HeuristicReport:
    all_dangers: list[DangerRecord] = []

    all_dangers.extend(detect_high_call_fanin(conn))
    all_dangers.extend(detect_high_import_fanin(conn))
    all_dangers.extend(detect_global_mutations(conn))
    all_dangers.extend(detect_invariant_comments(conn, repo_root))

    if dry_run:
        return HeuristicReport(detected=all_dangers, added=[], removed=[])

    existing_auto = set(
        row["id"] for row in conn.execute(
            "SELECT id FROM dangers WHERE added_by = 'auto'"
        ).fetchall()
    )

    added = []
    now = datetime.now(timezone.utc).isoformat()
    for danger in all_dangers:
        is_new = danger.id not in existing_auto
        conn.execute(
            """INSERT INTO dangers (id, scope, description, reason, added_by, created_at)
               VALUES (?, ?, ?, ?, 'auto', ?)
               ON CONFLICT(id) DO UPDATE SET
                   description = excluded.description,
                   reason = excluded.reason""",
            (danger.id, danger.scope, danger.description, danger.reason, now),
        )
        if is_new:
            added.append(danger)

    new_ids = {d.id for d in all_dangers}
    stale_ids = existing_auto - new_ids
    removed = []
    for stale_id in stale_ids:
        row = conn.execute(
            "SELECT description FROM dangers WHERE id = ?", (stale_id,)
        ).fetchone()
        conn.execute("DELETE FROM dangers WHERE id = ?", (stale_id,))
        if row:
            removed.append(row["description"])

    conn.commit()
    return HeuristicReport(detected=all_dangers, added=added, removed=removed)
