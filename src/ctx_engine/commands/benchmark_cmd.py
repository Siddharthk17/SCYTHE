"""`ctx benchmark` — quantitative index quality metrics.

No LLM calls: pure SQL and arithmetic over the index. Runs in under five
seconds and scores summary quality, call-graph resolution, import coverage,
token efficiency (real assembly algorithm), and danger coverage into a
0–100 overall score.
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ctx_engine.db import connect

TOKEN_BUDGET = 8000
BENCHMARK_SAMPLE_SIZE = 10
HIGH_FANIN_CALLERS = 10

# Weights must sum to 100.
SCORE_WEIGHTS = {
    "summary_coverage": 30,
    "confidence": 25,
    "call_resolution": 20,
    "token_efficiency": 15,
    "danger_coverage": 10,
}


def _pct(part: int, total: int) -> float:
    return (part / total) if total else 0.0


def collect_metrics(conn: sqlite3.Connection, repo_root: Path) -> dict:
    """Gather all raw metrics. Returns a dict matching the documented JSON keys."""
    # ── Summary quality ──
    total_functions = conn.execute("SELECT COUNT(*) FROM functions").fetchone()[0]
    null_summary = conn.execute(
        "SELECT COUNT(*) FROM functions WHERE summary IS NULL"
    ).fetchone()[0]
    high_confidence = conn.execute(
        "SELECT COUNT(*) FROM functions WHERE confidence >= 0.9"
    ).fetchone()[0]
    low_confidence = conn.execute(
        "SELECT COUNT(*) FROM functions WHERE confidence < 0.5"
    ).fetchone()[0]
    likely_stale = conn.execute(
        "SELECT COUNT(*) FROM functions WHERE confidence < 0.2"
    ).fetchone()[0]

    # ── Call graph resolution ──
    total_calls = conn.execute("SELECT COUNT(*) FROM call_graph").fetchone()[0]
    resolved = conn.execute(
        "SELECT COUNT(*) FROM call_graph WHERE callee_id IS NOT NULL AND is_ambiguous = 0"
    ).fetchone()[0]
    ambiguous = conn.execute(
        "SELECT COUNT(*) FROM call_graph WHERE is_ambiguous = 1"
    ).fetchone()[0]
    unresolved = total_calls - resolved - ambiguous
    resolution_rate = _pct(resolved, total_calls)

    # ── Import graph coverage ──
    total_files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    files_with_imports = conn.execute(
        "SELECT COUNT(*) FROM files WHERE imports IS NOT NULL AND imports != '[]'"
    ).fetchone()[0]
    total_import_edges = 0
    for row in conn.execute("SELECT imports FROM files").fetchall():
        try:
            total_import_edges += len(json.loads(row["imports"] or "[]"))
        except json.JSONDecodeError:
            pass

    return {
        "summary_quality": {
            "total": total_functions,
            "summarized": total_functions - null_summary,
            "high_confidence": high_confidence,
            "low_confidence": low_confidence,
            "likely_stale": likely_stale,
        },
        "call_graph": {
            "total": total_calls,
            "resolved": resolved,
            "ambiguous": ambiguous,
            "unresolved": unresolved,
            "resolution_rate": round(resolution_rate, 3),
        },
        "import_graph": {
            "files_with_imports": files_with_imports,
            "total_files": total_files,
            "total_edges": total_import_edges,
        },
    }

def measure_token_efficiency(conn: sqlite3.Connection, repo_root: Path) -> dict:
    """Token cost of the real assembly algorithm on a random file sample.

    Uses assemble_context() — the actual Week 4 pruning cascade, not an
    estimate. A result over the 8k budget means the pruning has a bug and
    benchmark will surface it.
    """
    from ctx_engine.mcp_server.tools.assembly import assemble_context

    sample_files = conn.execute(
        f"SELECT path FROM files ORDER BY RANDOM() LIMIT {BENCHMARK_SAMPLE_SIZE}"
    ).fetchall()
    token_counts = []
    for row in sample_files:
        ctx_str = assemble_context(conn, repo_root, row["path"])
        token_counts.append(len(ctx_str) // 4)
    avg = int(sum(token_counts) / len(token_counts)) if token_counts else 0
    mx = max(token_counts) if token_counts else 0
    under = sum(1 for t in token_counts if t <= TOKEN_BUDGET)
    return {
        "avg": avg,
        "max": mx,
        "sampled": len(token_counts),
        "under_budget": under,
        "all_under_budget": under == len(token_counts),
    }


def measure_danger_coverage(conn: sqlite3.Connection) -> dict:
    """Percentage of high fan-in functions that have a danger zone."""
    high_fanin_fns = conn.execute(
        """SELECT callee_id FROM call_graph
           WHERE callee_id IS NOT NULL
           GROUP BY callee_id HAVING COUNT(*) >= ?""",
        (HIGH_FANIN_CALLERS,),
    ).fetchall()
    uncovered: list[tuple[str, int]] = []
    covered = 0
    for row in high_fanin_fns:
        count = conn.execute(
            "SELECT COUNT(*) FROM dangers WHERE scope = ?", (row["callee_id"],)
        ).fetchone()[0]
        if count > 0:
            covered += 1
        else:
            caller_count = conn.execute(
                "SELECT COUNT(*) FROM call_graph WHERE callee_id = ?",
                (row["callee_id"],),
            ).fetchone()[0]
            uncovered.append((row["callee_id"], caller_count))
    return {
        "high_fanin_count": len(high_fanin_fns),
        "covered": covered,
        "coverage_rate": _pct(covered, len(high_fanin_fns)),
        "uncovered": uncovered,
    }


def compute_score(metrics: dict, token_eff: dict, danger_cov: dict) -> tuple[int, dict]:
    """Weighted 0–100 score. Component scores round to one decimal."""
    sq = metrics["summary_quality"]
    cg = metrics["call_graph"]
    summary_cov = _pct(sq["summarized"], sq["total"])
    high_conf_pct = _pct(sq["high_confidence"], sq["total"])
    token_pct = _pct(token_eff["under_budget"], token_eff["sampled"])

    components = {
        "summary_coverage": round(summary_cov * SCORE_WEIGHTS["summary_coverage"], 1),
        "confidence": round(high_conf_pct * SCORE_WEIGHTS["confidence"], 1),
        "call_resolution": round(cg["resolution_rate"] * SCORE_WEIGHTS["call_resolution"], 1),
        "token_efficiency": round(token_pct * SCORE_WEIGHTS["token_efficiency"], 1),
        "danger_coverage": round(danger_cov["coverage_rate"] * SCORE_WEIGHTS["danger_coverage"], 1),
    }
    return int(round(sum(components.values()))), components


def last_index_update(conn: sqlite3.Connection) -> str | None:
    return conn.execute(
        """SELECT MAX(updated_at) FROM (
               SELECT MAX(updated_at) AS updated_at FROM files
               UNION ALL
               SELECT MAX(updated_at) FROM functions
           )"""
    ).fetchone()[0]

# ── Rendering ─────────────────────────────────────────────────────────────────


def _mark(ok: bool) -> str:
    return "✓" if ok else "⚠"


def print_benchmark_report(
    repo_root: Path,
    metrics: dict,
    token_eff: dict,
    danger_cov: dict,
    score: int,
    components: dict,
) -> None:
    sq = metrics["summary_quality"]
    cg = metrics["call_graph"]
    ig = metrics["import_graph"]
    total_files = ig["total_files"]

    print(f"ctx benchmark — {repo_root.name}")
    print()
    sep = "  " + "═" * 52
    print(sep)
    print("  SUMMARY QUALITY")
    print(sep)
    print(f"  Functions indexed:          {sq['total']}")
    print(f"  Functions summarized:       {sq['summarized']} "
          f"({_pct(sq['summarized'], sq['total']) * 100:.1f}%)")
    print(f"  Confidence ≥ 0.9:           {sq['high_confidence']} "
          f"({_pct(sq['high_confidence'], sq['total']) * 100:.1f}%)    "
          f"{_mark(sq['total'] == 0 or sq['high_confidence'] / sq['total'] >= 0.9)}")
    print(f"  Confidence < 0.5:           {sq['low_confidence']} "
          f"({_pct(sq['low_confidence'], sq['total']) * 100:.1f}%)    "
          f"{_mark(sq['low_confidence'] == 0)}")
    print(f"  Confidence < 0.2:           {sq['likely_stale']} "
          f"({_pct(sq['likely_stale'], sq['total']) * 100:.1f}%)    "
          f"{_mark(sq['likely_stale'] == 0)}")
    print()

    print(sep)
    print("  CALL GRAPH RESOLUTION")
    print(sep)
    print(f"  Total call edges:           {cg['total']}")
    print(f"  Resolved (exact):           {cg['resolved']} "
          f"({_pct(cg['resolved'], cg['total']) * 100:.1f}%)  "
          f"{_mark(cg['total'] == 0 or cg['resolution_rate'] >= 0.8)}")
    print(f"  Ambiguous (duck typing):    {cg['ambiguous']} "
          f"({_pct(cg['ambiguous'], cg['total']) * 100:.1f}%)")
    print(f"  Unresolved:                 {cg['unresolved']} "
          f"({_pct(cg['unresolved'], cg['total']) * 100:.1f}%)")
    print()
    print(f"  Resolution rate: {cg['resolution_rate'] * 100:.1f}%")
    print()
    print(sep)
    print("  IMPORT GRAPH COVERAGE")
    print(sep)
    print(f"  Files with import data:     {ig['files_with_imports']} of {total_files} "
          f"({_pct(ig['files_with_imports'], total_files) * 100:.0f}%)")
    print(f"  Total import edges:         {ig['total_edges']}")
    avg_imports = ig["total_edges"] / total_files if total_files else 0
    print(f"  Average imports per file:   {avg_imports:.1f}")
    print()
    print(sep)
    print(f"  TOKEN EFFICIENCY ({token_eff['sampled']}-file sample)")
    print(sep)
    print(f"  Average tokens per get_context():  {token_eff['avg']:,}")
    print(f"  Maximum tokens (worst case):       {token_eff['max']:,}  "
          f"{_mark(token_eff['all_under_budget'])} (under {TOKEN_BUDGET:,} budget)")
    print(f"  Files within budget:               {token_eff['under_budget']} of "
          f"{token_eff['sampled']}")
    print()
    print(sep)
    print("  DANGER COVERAGE")
    print(sep)
    print(f"  High fan-in functions (≥{HIGH_FANIN_CALLERS} callers):   "
          f"{danger_cov['high_fanin_count']}")
    print(f"  With danger zones:                     {danger_cov['covered']} "
          f"({danger_cov['coverage_rate'] * 100:.1f}%)  "
          f"{_mark(danger_cov['coverage_rate'] == 1.0)}")
    if danger_cov["uncovered"]:
        print(f"  Without danger zones:                  {len(danger_cov['uncovered'])}")
        for fn_id, callers in danger_cov["uncovered"][:5]:
            print(f"    - {fn_id} (called by {callers} callers)")
            print(f"      → consider: ctx danger add \"{fn_id}\" \"...\" --reason \"...\"")
    print()
    print(sep)
    print(f"  OVERALL SCORE: {score}/100")
    print(sep)
    print(f"  Summary coverage:   +{components['summary_coverage']:>5} "
          f"({_pct(sq['summarized'], sq['total']) * 100:.1f}% summarized)")
    print(f"  Confidence:         +{components['confidence']:>5} "
          f"({_pct(sq['high_confidence'], sq['total']) * 100:.1f}% high-confidence)")
    print(f"  Call resolution:    +{components['call_resolution']:>5} "
          f"({cg['resolution_rate'] * 100:.1f}% resolved)")
    print(f"  Token efficiency:   +{components['token_efficiency']:>5} "
          f"({'all' if token_eff['all_under_budget'] else token_eff['under_budget']} files under budget)")
    print(f"  Danger coverage:    +{components['danger_coverage']:>5} "
          f"({danger_cov['coverage_rate'] * 100:.0f}% coverage)")
    print("  " + "─" * 52)


def _suggestions(metrics: dict, token_eff: dict, danger_cov: dict) -> list[str]:
    """Improvement suggestions for the lowest-scoring dimensions."""
    suggestions = []
    sq = metrics["summary_quality"]
    cg = metrics["call_graph"]
    if sq["summarized"] < sq["total"]:
        suggestions.append("Run ctx sync to summarize remaining functions")
    if sq["low_confidence"] > 0:
        suggestions.append(
            f"Run ctx sync to refresh {sq['low_confidence']} low-confidence functions"
        )
    if cg["total"] > 0 and cg["resolution_rate"] < 0.8:
        suggestions.append(
            "Call resolution below 80% — check ambiguous call sites in ctx diff"
        )
    if not token_eff["all_under_budget"]:
        suggestions.append(
            "Some files exceed the 8k assembly budget — inspect the pruning cascade"
        )
    for fn_id, _callers in danger_cov["uncovered"][:2]:
        suggestions.append(
            f"ctx danger add for {fn_id.rsplit('::', 1)[-1]} "
            f"(high fan-in, no danger zone)"
        )
    return suggestions


def run_benchmark(repo_root: Path, json_output: bool = False) -> None:
    db_path = repo_root / ".ctx" / "index.db"
    if not db_path.exists():
        raise FileNotFoundError("Database not found. Run 'ctx init' first.")

    conn = connect(db_path)
    try:
        metrics = collect_metrics(conn, repo_root)
        token_eff = measure_token_efficiency(conn, repo_root)
        danger_cov = measure_danger_coverage(conn)
        score, components = compute_score(metrics, token_eff, danger_cov)

        if json_output:
            print(json.dumps({
                "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "repo": repo_root.name,
                "overall_score": score,
                "summary_quality": metrics["summary_quality"],
                "call_graph": metrics["call_graph"],
                "import_graph": metrics["import_graph"],
                "token_efficiency": {
                    "avg": token_eff["avg"],
                    "max": token_eff["max"],
                    "all_under_budget": token_eff["all_under_budget"],
                },
                "danger_coverage": {
                    "high_fanin_count": danger_cov["high_fanin_count"],
                    "covered": danger_cov["covered"],
                    "coverage_rate": round(danger_cov["coverage_rate"], 3),
                },
            }, indent=2))
            return

        print_benchmark_report(
            repo_root, metrics, token_eff, danger_cov, score, components
        )

        last_update = last_index_update(conn)
        if last_update:
            print(f"  Last updated: {last_update}")
        else:
            print("  Last updated: (never)")

        suggestions = _suggestions(metrics, token_eff, danger_cov)
        if suggestions:
            print()
            print("  Suggested improvements:")
            for i, s in enumerate(suggestions, start=1):
                print(f"  {i}. {s}")
    finally:
        conn.close()



