"""Tests for ctx review (Week 8) — mocked LLM and git subprocesses where needed."""
import json
import sqlite3
import subprocess

import pytest

from ctx_engine.commands.review_cmd import (
    FileReviewPayload,
    build_file_review_payload,
    build_batch_prompt,
    get_changed_line_ranges,
    parse_batch_response,
    parse_verdict,
    print_structural_report,
    print_review_report,
    run_review,
)
from ctx_engine.db import init_schema


@pytest.fixture
def review_repo(tmp_path):
    """Git repo with a committed base file, a staged change, and an index DB."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "mcts.py").write_text(
        "def expand_batch():\n"
        "    undo_virtual_loss()\n"
        "    return 1\n"
        + "".join(f"pass  # pad {i}\n" for i in range(10))
        + "def untouched():\n"
        "    return 2\n"
    )
    (tmp_path / "targets.py").write_text(
        "def pick_move():\n    return 0\n"
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@t.io",
         "commit", "-q", "-m", "base"],
        cwd=tmp_path, check=True,
    )

    # Stage a real change to mcts.py (modifies expand_batch's body).
    (tmp_path / "mcts.py").write_text(
        "def expand_batch():\n"
        "    if not resigned:\n"
        "        undo_virtual_loss()\n"
        "    return 1\n"
        + "".join(f"pass  # pad {i}\n" for i in range(10))
        + "def untouched():\n"
        "    return 2\n"
    )
    subprocess.run(["git", "add", "mcts.py"], cwd=tmp_path, check=True)

    db_path = tmp_path / ".ctx" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    for path, purpose in (("mcts.py", "search"), ("targets.py", "training")):
        conn.execute(
            "INSERT INTO files (path, system, semantic_hash, content_hash, "
            "exports, imports, used_by, used_by_count, is_stale) "
            "VALUES (?, ?, 'sh', 'ch', '[]', '[]', '[]', 0, 0)",
            (path, purpose),
        )
    conn.execute(
        "INSERT INTO functions (id, file, name, signature, line_start, line_end, "
        "semantic_hash, is_tainted, is_stale, confidence) "
        "VALUES ('mcts.py::expand_batch', 'mcts.py', 'expand_batch', "
        "'def expand_batch()', 1, 3, 'sh', 0, 0, 1.0)"
    )
    conn.execute(
        "INSERT INTO functions (id, file, name, signature, line_start, line_end, "
        "semantic_hash, is_tainted, is_stale, confidence) "
        "VALUES ('mcts.py::untouched', 'mcts.py', 'untouched', "
        "'def untouched()', 14, 15, 'sh', 0, 0, 1.0)"
    )
    conn.execute(
        "INSERT INTO dangers (id, scope, description, reason, added_by, created_at) "
        "VALUES ('d1', 'mcts.py::expand_batch', 'undo_virtual_loss must run unconditionally', "
        "'biases Q-values', 'human', '2026-06-14T10:00:00Z')"
    )
    conn.commit()
    yield conn, tmp_path
    conn.close()


# ── Hunk parsing / function overlap ───────────────────────────────────────────


def test_changed_line_ranges_parsing():
    diff = "@@ -142,15 +142,17 @@ def f():\n context\n"
    ranges = get_changed_line_ranges(diff)
    assert len(ranges) == 1
    assert ranges[0].start == 142
    assert ranges[0].end == 158  # 142 + 17 - 1


def test_function_overlap_detection():
    """Function at lines 142–189; hunk @@ -156,3 +156,5 @@ → changed."""
    diff = "diff --git a/f.py b/f.py\n@@ -156,3 +156,5 @@\n context\n"
    ranges = get_changed_line_ranges(diff)
    assert ranges[0].overlaps(142, 189)
    assert not ranges[0].overlaps(1, 100)
    assert not ranges[0].overlaps(200, 300)


def test_function_with_no_lines_in_diff_excluded(review_repo):
    conn, repo_root = review_repo
    diff = "@@ -1,3 +1,3 @@\n context\n"
    payload = build_file_review_payload(conn, "mcts.py", diff, repo_root=repo_root)
    assert "mcts.py::expand_batch" in payload.changed_functions
    assert "mcts.py::untouched" not in payload.changed_functions


# ── Staged payload construction ───────────────────────────────────────────────


def _staged_diffs(repo_root):
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        cwd=repo_root, capture_output=True, text=True,
    )
    paths = [p.strip() for p in result.stdout.splitlines() if p.strip()]
    diffs = {}
    for p in paths:
        d = subprocess.run(
            ["git", "diff", "--cached", "--", p],
            cwd=repo_root, capture_output=True, text=True,
        )
        diffs[p] = d.stdout
    return paths, diffs


def test_staged_two_file_payload_construction(review_repo):
    """Stage a second file too; both files get complete payloads."""
    conn, repo_root = review_repo
    (repo_root / "targets.py").write_text("def pick_move():\n    return 3\n")
    subprocess.run(["git", "add", "targets.py"], cwd=repo_root, check=True)

    paths, diffs = _staged_diffs(repo_root)
    assert set(paths) == {"mcts.py", "targets.py"}

    payloads = [
        build_file_review_payload(conn, p, diffs[p], repo_root=repo_root)
        for p in paths
    ]
    by_path = {pl.path: pl for pl in payloads}

    for p in paths:
        assert isinstance(by_path[p], FileReviewPayload)
        assert by_path[p].diff == diffs[p]
        assert by_path[p].file_record["path"] == p

    mcts = by_path["mcts.py"]
    assert mcts.changed_functions == ["mcts.py::expand_batch"]
    # Danger zone scoped to the changed function applies.
    assert any(d["id"] == "d1" for d in mcts.danger_zones)
    assert mcts.is_stale is False


def test_staged_blob_not_working_tree(review_repo):
    """The staged blob getter must not leak unstaged working-tree edits."""
    conn, repo_root = review_repo
    (repo_root / "mcts.py").write_text("totally different working tree\n")
    from ctx_engine.commands.review_cmd import get_staged_blob
    blob = get_staged_blob(repo_root, "mcts.py")
    assert "undo_virtual_loss" in blob
    assert "totally different" not in blob


# ── LLM interaction (mocked) ──────────────────────────────────────────────────

MOCK_RESPONSE = """=== FILE: mcts.py ===
1. SUMMARY
Changes expand_batch to conditionally undo virtual loss.

2. DANGER ZONE VIOLATIONS
✗ MAJOR — undo_virtual_loss must run unconditionally

3. CALL GRAPH IMPACT
✗ callers may break

4. ARCHITECTURAL DECISION CONFLICTS
✓ none

5. MISSING INDEX UPDATES
✗ is_stale not updated

6. SPECIFIC CONCERNS
• none

7. VERDICT: REQUEST_CHANGES
The conditional breaks the invariant.
"""


def _install_fake_llm(monkeypatch, calls):
    from ctx_engine.intelligence import llm_client

    class _FakeClient:
        pass

    def fake_call(client, model, system_prompt, user_content, max_tokens=4000):
        calls.append(user_content)
        return MOCK_RESPONSE, 100, 50

    monkeypatch.setattr(llm_client, "get_anthropic_client", lambda: _FakeClient())
    monkeypatch.setattr(llm_client, "call_llm_with_retry", fake_call)


def test_review_staged_with_mocked_llm(review_repo, monkeypatch, capsys):
    conn, repo_root = review_repo
    calls = []
    _install_fake_llm(monkeypatch, calls)

    run_review(repo_root, staged=True)
    out = capsys.readouterr().out

    assert "OVERALL: REQUEST_CHANGES" in out
    assert "SUMMARY" in out
    assert "Tokens used" in out
    # The prompt contains the structural payload.
    assert "STRUCTURAL CODE REVIEW REQUEST" in calls[0]
    assert "mcts.py" in calls[0]


def test_review_no_llm_zero_api_calls(review_repo, monkeypatch, capsys):
    conn, repo_root = review_repo

    def _boom(*a, **kw):
        raise AssertionError("LLM must not be called in --no-llm mode")

    from ctx_engine.intelligence import llm_client
    monkeypatch.setattr(llm_client, "call_llm_with_retry", _boom)

    run_review(repo_root, staged=True, no_llm=True)
    out = capsys.readouterr().out
    assert "structural report" in out
    assert "mcts.py" in out
    assert "expand_batch" in out


def test_review_no_llm_flags_stale_and_taint(review_repo, monkeypatch, capsys):
    conn, repo_root = review_repo
    conn.execute("UPDATE files SET is_stale = 1 WHERE path = 'mcts.py'")
    conn.execute(
        "UPDATE functions SET is_tainted = 1 WHERE id = 'mcts.py::expand_batch'"
    )
    conn.commit()

    run_review(repo_root, staged=True, no_llm=True)
    out = capsys.readouterr().out
    assert "is_stale" in out
    assert "is_tainted" in out
    assert "ctx_log_session" in out  # no session entry → flagged


def test_review_batching_six_files_two_calls(review_repo, monkeypatch, capsys):
    """6 changed files → 2 batches → exactly 2 LLM calls."""
    conn, repo_root = review_repo
    (repo_root / "targets.py").write_text("def pick_move():\n    return 3\n")
    subprocess.run(["git", "add", "targets.py"], cwd=repo_root, check=True)
    for i in range(4):
        name = f"mod{i}.py"
        (repo_root / name).write_text(f"x{i} = 0\n")
        subprocess.run(["git", "add", name], cwd=repo_root, check=True)
        conn.execute(
            "INSERT INTO files (path, system, semantic_hash, content_hash, "
            "exports, imports, used_by, used_by_count, is_stale) "
            "VALUES (?, ?, 'sh', 'ch', '[]', '[]', '[]', 0, 0)",
            (name, f"sys{i}"),
        )
    conn.commit()

    calls = []
    _install_fake_llm(monkeypatch, calls)
    run_review(repo_root, staged=True)
    out = capsys.readouterr().out

    assert len(calls) == 2
    assert "batch 1/2" in out and "batch 2/2" in out
    assert "OVERALL: REQUEST_CHANGES" in out


def test_review_verdict_aggregation(review_repo, monkeypatch, capsys):
    """All files approving → overall APPROVE (aggregation across payloads)."""
    conn, repo_root = review_repo
    (repo_root / "targets.py").write_text("def pick_move():\n    return 3\n")
    subprocess.run(["git", "add", "targets.py"], cwd=repo_root, check=True)

    from ctx_engine.intelligence import llm_client

    class _FakeClient:
        pass

    def fake_call(client, model, system_prompt, user_content, max_tokens=4000):
        # Echo a review for every file the batch prompt requested.
        import re as _re
        paths = _re.findall(r"^=== FILE: (.+?) ===$", user_content, _re.MULTILINE)
        response = "\n".join(
            f"=== FILE: {p} ===\n1. SUMMARY\nFine.\n\n7. VERDICT: APPROVE\nOK."
            for p in paths
        )
        return response, 100, 50

    monkeypatch.setattr(llm_client, "get_anthropic_client", lambda: _FakeClient())
    monkeypatch.setattr(llm_client, "call_llm_with_retry", fake_call)

    run_review(repo_root, staged=True)
    out = capsys.readouterr().out
    assert "OVERALL: APPROVE" in out


# ── Unit: prompt building / parsing ───────────────────────────────────────────


def test_parse_batch_response_splits_by_marker():
    payload_a = FileReviewPayload(path="a.py", diff="", file_record={})
    payload_b = FileReviewPayload(path="b.py", diff="", file_record={})
    response = "=== FILE: a.py ===\nreview A\n=== FILE: b.py ===\nreview B\n"
    per_file = parse_batch_response(response, [payload_a, payload_b])
    assert per_file["a.py"] == "review A"
    assert per_file["b.py"] == "review B"


def test_parse_batch_response_missing_file_placeholder():
    payload = FileReviewPayload(path="a.py", diff="", file_record={})
    per_file = parse_batch_response("no markers at all", [payload])
    assert per_file["a.py"] == "(no review returned for this file)"


def test_parse_verdict():
    assert parse_verdict("7. VERDICT: APPROVE\nFine.") == "APPROVE"
    assert parse_verdict("VERDICT: NEEDS_DISCUSSION") == "NEEDS_DISCUSSION"
    assert parse_verdict("no verdict here") == "NEEDS_DISCUSSION"


def test_build_batch_prompt_contains_each_file():
    payloads = [
        FileReviewPayload(path=f"f{i}.py", diff="x", file_record={})
        for i in range(3)
    ]
    prompt = build_batch_prompt(payloads, "testrepo")
    for i in range(3):
        assert f"=== FILE: f{i}.py ===" in prompt
    assert "PROJECT: testrepo" in prompt


