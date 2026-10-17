import logging
import random
import sqlite3
import statistics
import time
from pathlib import Path

from ctx_engine.db import connect
from ctx_engine.discovery import EXTENSION_TO_LANGUAGE
from ctx_engine.languages.registry import parse_bytes
from ctx_engine.hashing import file_semantic_hash

logger = logging.getLogger("ctx")


def select_benchmark_sample(
    conn: sqlite3.Connection, repo_root: Path, n: int = 20
) -> list[str]:
    rows = conn.execute("SELECT path FROM files").fetchall()
    all_files = [r["path"] if isinstance(r, sqlite3.Row) else r[0] for r in rows]
    if len(all_files) <= n:
        return list(all_files)

    def _size(p: str) -> int:
        try:
            fp = repo_root / p
            if fp.exists():
                return fp.stat().st_size
        except OSError:
            pass
        return 0

    by_size = sorted(all_files, key=_size, reverse=True)
    largest = by_size[: min(5, n // 4)]
    remaining_pool = [f for f in all_files if f not in largest]
    random_sample = random.sample(
        remaining_pool, min(n - len(largest), len(remaining_pool))
    )
    return largest + random_sample


def _per_file_ms(conn: sqlite3.Connection, repo_root: Path, sample: list[str]) -> list[float]:
    timings: list[float] = []
    for path in sample:
        ext = Path(path).suffix.lower()
        language = EXTENSION_TO_LANGUAGE.get(ext)
        if not language:
            continue
        fp = repo_root / path
        try:
            blob = fp.read_bytes()
        except OSError:
            continue
        t0 = time.perf_counter()
        try:
            tree, source = parse_bytes(blob, language)
            file_semantic_hash(tree, source, language)
        except Exception:
            continue
        timings.append((time.perf_counter() - t0) * 1000)
    return timings


def _timed_validate(
    conn: sqlite3.Connection, repo_root: Path, files: list[str]
) -> float:
    """Time validate's dominant cost (parse + semantic hash + DB lookup).

    Deliberately avoids calling run_validate: that path reads staged blobs
    via ``git show`` (requiring staging) and appends to hook-timings.jsonl.
    Bench must never stage anything and never write — so it times the exact
    per-file work validate performs once the blob bytes are in hand.
    """
    t0 = time.perf_counter()
    for path in files:
        ext = Path(path).suffix.lower()
        language = EXTENSION_TO_LANGUAGE.get(ext)
        if not language:
            continue
        fp = repo_root / path
        try:
            blob = fp.read_bytes()
        except OSError:
            continue
        try:
            tree, source = parse_bytes(blob, language)
            row = conn.execute(
                "SELECT semantic_hash FROM files WHERE path = ?", (path,)
            ).fetchone()
            if row is None:
                continue
            file_semantic_hash(tree, source, language)
        except Exception:
            continue
    return (time.perf_counter() - t0) * 1000


def run_bench_hooks(repo_root: Path, samples: int = 20) -> None:
    db_path = repo_root / ".ctx" / "index.db"
    if not db_path.exists():
        raise FileNotFoundError("Database not found. Please run 'ctx init' first.")

    conn = connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        sample = select_benchmark_sample(conn, repo_root, samples)
    finally:
        # Keep the connection open for timing queries below; closed at end.
        pass

    if not sample:
        print("ctx bench-hooks: no indexed files to sample.")
        conn.close()
        return

    largest_count = min(5, len(sample) // 4) if len(sample) >= 4 else min(5, len(sample))
    print(f"ctx bench-hooks — {repo_root.name}")
    print()
    print(
        f"  Sampling {len(sample)} files from the tracked set "
        f"({largest_count} largest, {len(sample) - largest_count} random)..."
    )
    print()

    per_file = _per_file_ms(conn, repo_root, sample)
    if per_file:
        ordered = sorted(per_file)
        p50 = statistics.median(ordered)
        idx95 = min(len(ordered) - 1, int(len(ordered) * 0.95))
        p95 = ordered[idx95]
        mx = max(ordered)
        # Largest file label for the max row.
        max_label = sample[0] if sample else "?"
        try:
            sized = sorted(
                sample,
                key=lambda p: (repo_root / p).stat().st_size
                if (repo_root / p).exists()
                else 0,
                reverse=True,
            )
            if sized:
                max_label = sized[0]
                try:
                    line_count = len(
                        (repo_root / max_label).read_text(
                            encoding="utf-8", errors="replace"
                        ).splitlines()
                    )
                    max_label = f"{max_label}, {line_count:,} lines"
                except OSError:
                    pass
        except OSError:
            pass
        print("  Per-file timing:")
        print(f"    p50: {p50:.1f}ms   p95: {p95:.1f}ms   max: {mx:.1f}ms ({max_label})")
        print()

    try:
        from ctx_engine.config import get_config_value as _get

        budget = float(_get(repo_root, "hooks.validate_timeout_ms"))
    except (ValueError, TypeError):
        budget = 100.0

    print("  Simulated commit sizes (unbounded timing, 3 runs averaged):")
    for k in (1, 5, 20, 50):
        subset = sample[: min(k, len(sample))]
        # If the repo has fewer than k files, report on what exists.
        runs = [_timed_validate(conn, repo_root, subset) for _ in range(3)]
        avg = sum(runs) / len(runs) if runs else 0.0
        if avg <= budget * 0.5:
            marker = "✓ well under"
            note = f"{budget:g}ms budget"
        elif avg <= budget:
            marker = "✓ under"
            headroom = budget - avg
            note = f"{budget:g}ms budget ({headroom:.0f}ms headroom)"
        else:
            marker = "⚠ EXCEEDS"
            note = f"{budget:g}ms budget"
        print(f"    {k} file{'s' if k != 1 else ''}:   avg {avg:.0f}ms    {marker} {note}")

    conn.close()

    print()
    print(f"  Current hooks.validate_timeout_ms: {budget:g}")
    print()
    print("  Your typical commit size (check 'git log --stat' over recent history)")
    print("  determines whether this matters day to day. Large multi-file commits")
    print("  (50+) may occasionally degrade gracefully — see the WHAT'S NEW section")
    print("  in this project — this is expected, safe behavior, not an error.")
    print()
    print("  If large commits are common in your workflow, consider:")
    print(f"    ctx config set hooks.validate_timeout_ms {int(budget * 2)}")
