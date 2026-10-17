import json
import logging
import random
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ctx_engine.db import connect
from ctx_engine.discovery import EXTENSION_TO_LANGUAGE
from ctx_engine.languages.registry import parse_bytes
from ctx_engine.hashing import file_semantic_hash

logger = logging.getLogger("ctx")


@dataclass
class ValidateReport:
    checked: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    not_indexed: list[str] = field(default_factory=list)
    skipped_large: list[str] = field(default_factory=list)
    timed_out_before_checking: list[str] = field(default_factory=list)
    degraded: bool = False
    elapsed_ms: float = 0.0


def get_staged_blob(repo_root: Path, path: str) -> bytes | None:
    """Return the git staged blob for path, or None if not staged."""
    result = subprocess.run(
        ["git", "show", f":0:{path}"],
        cwd=repo_root,
        capture_output=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def get_staged_paths(repo_root: Path) -> list[str]:
    """Return staged file paths for added, copied, or modified files."""
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    return [p.strip() for p in result.stdout.splitlines() if p.strip()]


def validate_exit_code(report: ValidateReport) -> int:
    """A confirmed mismatch blocks; a timeout never does on its own."""
    if report.stale or report.not_indexed:
        return 1
    return 0


def _hook_timings_path(repo_root: Path) -> Path:
    return repo_root / ".ctx" / "hook-timings.jsonl"


def _log_hook_timing(repo_root: Path, report: ValidateReport) -> None:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "elapsed_ms": round(report.elapsed_ms, 2),
        "checked_count": len(report.checked),
        "degraded": report.degraded,
        "stale_count": len(report.stale),
    }
    try:
        path = _hook_timings_path(repo_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
        _trim_hook_timings_if_needed(path)
    except OSError as err:
        logger.warning("Failed to write hook timing log: %s", err)


def _trim_hook_timings_if_needed(path: Path, _force: bool = False) -> None:
    """Keep only the most recent 200 entries.

    Checked via a cheap modulo counter so the append path itself stays fast.
    Pass _force=True in tests for determinism.
    """
    if not _force and random.randint(1, 20) != 1:
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) > 200:
        try:
            path.write_text("\n".join(lines[-200:]) + "\n", encoding="utf-8")
        except OSError as err:
            logger.warning("Failed to trim hook timing log: %s", err)


def _resolve_timeout_ms(repo_root: Path, timeout_ms: float | None) -> float:
    if timeout_ms is not None:
        return float(timeout_ms)
    try:
        from ctx_engine.config import get_config_value as _get

        return float(_get(repo_root, "hooks.validate_timeout_ms"))
    except (ValueError, TypeError):
        return 100.0


def _resolve_max_size(repo_root: Path) -> int:
    try:
        from ctx_engine.config import get_config_value as _get

        return int(_get(repo_root, "hooks.max_file_size_bytes"))
    except (ValueError, TypeError):
        return 2_000_000


def run_validate(
    repo_root: Path,
    files: list[str] | None = None,
    timeout_ms: float | None = None,
) -> ValidateReport:
    """Validate staged file content against the index.

    Returns a ValidateReport. On a confirmed mismatch (stale or not_indexed)
    raises SystemExit(1) to preserve the Week 3 blocking contract; the
    report is attached as ``exc.report`` for programmatic callers. A pure
    timeout (degraded) never raises — exit code 0.
    """
    start = time.monotonic()
    timeout_value = _resolve_timeout_ms(repo_root, timeout_ms)
    max_size = _resolve_max_size(repo_root)
    deadline = start + (timeout_value / 1000.0)

    db_path = repo_root / ".ctx" / "index.db"
    if not db_path.exists():
        raise FileNotFoundError(
            "Database not found. Please run 'ctx init' first."
        )

    try:
        subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=repo_root, capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as err:
        raise ValueError(
            f"Not inside a git repository: {repo_root}"
        ) from err

    if files is not None:
        staged_paths = files
    else:
        staged_paths = get_staged_paths(repo_root)

    report = ValidateReport()

    if not staged_paths:
        print("ctx validate: all staged files match the index.")
        print("  Checked: 0 files")
        report.elapsed_ms = (time.monotonic() - start) * 1000
        _log_hook_timing(repo_root, report)
        return report

    conn = connect(db_path)
    conn.row_factory = sqlite3.Row

    stale_paths: list[str] = []
    not_indexed_paths: list[str] = []
    skipped_large: list[str] = []
    timed_out: list[str] = []
    checked: list[str] = []
    skipped_count = 0

    try:
        for path in staged_paths:
            if time.monotonic() >= deadline:
                timed_out.append(path)
                continue

            ext = Path(path).suffix.lower()
            if ext not in EXTENSION_TO_LANGUAGE:
                skipped_count += 1
                continue

            blob = get_staged_blob(repo_root, path)
            if blob is None:
                skipped_count += 1
                continue

            if len(blob) > max_size:
                skipped_large.append(path)
                logger.warning(
                    "Skipping full validation of %s (%d bytes > %d byte limit) — "
                    "treat as best-effort. Consider adding it to .gitignore if it "
                    "is generated or vendored code.",
                    path, len(blob), max_size,
                )
                continue

            row = conn.execute(
                "SELECT semantic_hash FROM files WHERE path = ?", (path,)
            ).fetchone()
            if row is None:
                not_indexed_paths.append(path)
                continue

            language = EXTENSION_TO_LANGUAGE[ext]
            tree, source = parse_bytes(blob, language)

            if tree.root_node.has_error:
                msg = (
                    f"WARNING: parse errors in staged {path}"
                    " — validating against best-effort hash"
                )
                logger.warning(msg)
                print(msg, file=sys.stderr)

            try:
                staged_hash = file_semantic_hash(tree, source, language)
            except Exception:
                msg = f"WARNING: could not parse staged {path} — skipping validation"
                logger.warning(msg)
                print(msg, file=sys.stderr)
                skipped_count += 1
                continue

            if staged_hash != row["semantic_hash"]:
                stale_paths.append(path)

            checked.append(path)
    finally:
        conn.close()

    report.checked = checked
    report.stale = stale_paths
    report.not_indexed = not_indexed_paths
    report.skipped_large = skipped_large
    report.timed_out_before_checking = timed_out
    report.degraded = bool(timed_out)
    report.elapsed_ms = (time.monotonic() - start) * 1000
    _log_hook_timing(repo_root, report)

    exit_code = validate_exit_code(report)

    # Degraded but nothing confirmed wrong: allowed through with loud warning.
    if report.degraded and exit_code == 0:
        total = len(staged_paths)
        print(
            f"ctx validate: degraded — timed out after checking "
            f"{len(checked)} of {total} staged files."
        )
        print()
        print("  Checked (%d):     all match the index." % len(checked))
        print(
            f"  Not checked ({len(timed_out)}): ran out of time "
            f"({timeout_value:g}ms budget) before reaching these:"
        )
        for p in timed_out[:10]:
            print(f"    - {p}")
        if len(timed_out) > 10:
            print(f"    - ... ({len(timed_out) - 10} more)")
        print()
        print("  Commit ALLOWED — nothing confirmed wrong, but "
              f"{len(timed_out)} files were not verified.")
        print("  Run 'ctx sync' soon, and consider 'ctx bench-hooks' to see if your")
        print(f"  hooks.validate_timeout_ms (currently {timeout_value:g}ms) needs "
              "raising for commits")
        print("  of this size.")
        return report

    if exit_code != 0:
        print("ctx validate: index is out of date for staged files.")
        print()
        if stale_paths:
            print("  STALE (metadata exists but doesn't match staged content):")
            for p in stale_paths:
                print(f"    - {p}")
            print()
        if not_indexed_paths:
            print("  NOT INDEXED (never run 'ctx init' or 'ctx update' on these):")
            for p in not_indexed_paths:
                print(f"    - {p}")
            print()
        if skipped_large:
            print(f"  SKIPPED (large, best-effort only): {len(skipped_large)} file(s)")
            for p in skipped_large:
                print(f"    - {p}")
            print()
        if timed_out:
            print(f"  NOT CHECKED (timed out): {len(timed_out)} file(s)")
            for p in timed_out[:10]:
                print(f"    - {p}")
            if len(timed_out) > 10:
                print(f"    - ... ({len(timed_out) - 10} more)")
            print()
        print("  To fix:")
        print("    ctx sync           # re-index and re-summarize everything")
        print("    ctx update <file>  # re-index and re-summarize a single file")
        print()
        print("  Commit blocked.", file=sys.stderr)
        exc = SystemExit(1)
        # Attach the report for programmatic callers (tests) without
        # changing the SystemExit contract Week 3 established.
        exc.report = report  # type: ignore[attr-defined]
        raise exc

    print("ctx validate: all staged files match the index.")
    if checked or skipped_count or skipped_large:
        extra = ""
        if skipped_count:
            extra = f" ({skipped_count} non-parseable skipped)"
        if skipped_large:
            extra += f" ({len(skipped_large)} large skipped best-effort)"
        print(f"  Checked: {len(checked)} files{extra}")
    return report
