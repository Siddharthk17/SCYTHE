"""Tests for ctx pr-description (Week 9). LLM and git are mocked/real as noted."""
import json
import sqlite3
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from ctx_engine.commands.pr_description_cmd import (
    build_pr_prompt,
    collect_pr_context,
    run_pr_description,
)
from ctx_engine.db import init_schema

CANNED_DESCRIPTION = """## Summary
Moves contempt to the display layer.

## Changes
- **mcts.py**: relocated contempt application.

## Systems Affected
- **search**: core loop touched.

## Testing Approach
Run arena evaluation for 100 games.

## Danger Zones Touched
None — this change does not touch any indexed danger zones.

## Review Notes
Focus on mcts.py lines 156–172.
"""


@pytest.fixture
def pr_repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.t"], cwd=root, check=True
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "mcts.py").write_text("def search():\n    return 1\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True
    )

    ctx_dir = root / ".ctx"
    ctx_dir.mkdir(exist_ok=True)
    conn = sqlite3.connect(ctx_dir / "index.db")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    conn.execute(
        "INSERT INTO files (path, system, purpose, semantic_hash, content_hash, "
        "exports, imports, used_by, used_by_count, is_stale) "
        "VALUES ('mcts.py', 'search', 'MCTS search loop', 'sh', 'ch', "
        "'[\"search\"]', '[]', '[]', 0, 0)"
    )
    conn.execute(
        "INSERT INTO functions (id, file, name, signature, line_start, "
        "line_end, semantic_hash) "
        "VALUES ('mcts.py::search', 'mcts.py', 'search', 'def search()', 1, 2, 'sh')"
    )
    conn.commit()
    conn.close()

    # Stage a real change.
    (root / "mcts.py").write_text("def search():\n    return 2\n")
    subprocess.run(["git", "add", "mcts.py"], cwd=root, check=True)
    yield root


def _mock_llm():
    client = MagicMock()
    return patch(
        "ctx_engine.intelligence.llm_client.get_anthropic_client", return_value=client
    ), patch(
        "ctx_engine.intelligence.llm_client.call_llm_with_retry",
        return_value=(CANNED_DESCRIPTION, 100, 50),
    )


def test_staged_builds_payload_with_diff(pr_repo):
    from ctx_engine.db import connect

    conn = connect(pr_repo / ".ctx" / "index.db")
    try:
        from ctx_engine.commands.review_cmd import resolve_review_paths

        paths, diffs, mode = resolve_review_paths(pr_repo, True, None, None)
        ctx = collect_pr_context(
            conn, pr_repo, paths, diffs, mode, staged=True, range_expr=None
        )
    finally:
        conn.close()
    assert ctx.paths == ["mcts.py"]
    assert "mcts.py::search" in ctx.file_summaries
    prompt = build_pr_prompt(ctx, "repo")
    assert "mcts.py" in prompt
    assert "## Danger Zones Touched" in prompt


def test_run_writes_pr_description_and_gitignore(pr_repo):
    get_patch, call_patch = _mock_llm()
    with get_patch, call_patch:
        run_pr_description(pr_repo, staged=True)
    content = (pr_repo / "PR_DESCRIPTION.md").read_text()
    assert "## Summary" in content
    assert "mcts.py" in content
    assert "PR_DESCRIPTION.md" in (pr_repo / ".gitignore").read_text()


def test_run_output_flag(pr_repo, tmp_path):
    get_patch, call_patch = _mock_llm()
    custom = tmp_path / "custom.md"
    with get_patch, call_patch:
        run_pr_description(pr_repo, staged=True, output=str(custom))
    assert "## Summary" in custom.read_text()


def test_no_danger_zones_section(pr_repo):
    from ctx_engine.db import connect

    conn = connect(pr_repo / ".ctx" / "index.db")
    try:
        from ctx_engine.commands.review_cmd import resolve_review_paths

        paths, diffs, mode = resolve_review_paths(pr_repo, True, None, None)
        ctx = collect_pr_context(
            conn, pr_repo, paths, diffs, mode, staged=True, range_expr=None
        )
    finally:
        conn.close()
    assert "does not touch any indexed danger zones" in ctx.danger_context


def test_danger_zones_appear_in_context(pr_repo):
    conn = sqlite3.connect(pr_repo / ".ctx" / "index.db")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO dangers (id, scope, description, reason, added_by) "
        "VALUES ('dz1', 'mcts.py::search', 'virtual loss invariant', 'must hold', 'human')"
    )
    conn.commit()

    from ctx_engine.db import connect as _connect

    iconn = _connect(pr_repo / ".ctx" / "index.db")
    try:
        from ctx_engine.commands.review_cmd import resolve_review_paths

        paths, diffs, mode = resolve_review_paths(pr_repo, True, None, None)
        ctx = collect_pr_context(
            iconn, pr_repo, paths, diffs, mode, staged=True, range_expr=None
        )
    finally:
        iconn.close()
        conn.close()
    assert "virtual loss invariant" in ctx.danger_context


def test_system_crossings_in_context(pr_repo):
    conn = sqlite3.connect(pr_repo / ".ctx" / "index.db")
    conn.execute(
        "INSERT INTO files (path, system, semantic_hash, content_hash) "
        "VALUES ('train.py', 'training', 'sh', 'ch')"
    )
    conn.commit()
    conn.close()
    (pr_repo / "train.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "train.py"], cwd=pr_repo, check=True)

    from ctx_engine.db import connect as _connect

    iconn = _connect(pr_repo / ".ctx" / "index.db")
    try:
        from ctx_engine.commands.review_cmd import resolve_review_paths

        paths, diffs, mode = resolve_review_paths(pr_repo, True, None, None)
        ctx = collect_pr_context(
            iconn, pr_repo, paths, diffs, mode, staged=True, range_expr=None
        )
    finally:
        iconn.close()
    assert "search" in ctx.system_context
    assert "training" in ctx.system_context


def test_range_mode(pr_repo):
    subprocess.run(["git", "commit", "-m", "bump"], cwd=pr_repo, check=True,
                   capture_output=True)
    get_patch, call_patch = _mock_llm()
    with get_patch, call_patch:
        run_pr_description(pr_repo, range_expr="HEAD~1..HEAD")
    assert "## Summary" in (pr_repo / "PR_DESCRIPTION.md").read_text()


def test_no_changes_prints_message(pr_repo, capsys):
    subprocess.run(["git", "commit", "-m", "bump"], cwd=pr_repo, check=True,
                   capture_output=True)
    run_pr_description(pr_repo, staged=True)
    assert "No changes found" in capsys.readouterr().out
