import pytest
from pathlib import Path
from ctx_engine.reindex import parse_one_file, ParseResult


def test_parse_one_file_returns_parse_result(tmp_path):
    (tmp_path / "test.py").write_text("def foo():\n    return 1\n")
    result = parse_one_file(("test.py", "python", str(tmp_path)))
    assert isinstance(result, ParseResult)
    assert result.rel_path == "test.py"
    assert result.language == "python"
    assert result.file_structure is not None
    assert len(result.function_hashes) == 1
    assert "foo" in result.function_hashes
    assert result.content_hash
    assert result.mtime > 0
    assert result.file_size > 0
    assert result.parse_had_errors is False
    assert result.error is None


def test_parse_one_file_no_functions(tmp_path):
    (tmp_path / "empty.py").write_text("import os\n")
    result = parse_one_file(("empty.py", "python", str(tmp_path)))
    assert len(result.function_hashes) == 0


def test_parse_one_file_missing_file(tmp_path):
    result = parse_one_file(("missing.py", "python", str(tmp_path)))
    assert result.parse_had_errors is True
    assert result.error is not None
    assert result.content_hash == ""


def test_parse_one_file_parse_error(tmp_path):
    (tmp_path / "bad.py").write_text("def foo(:\n")
    result = parse_one_file(("bad.py", "python", str(tmp_path)))
    assert result.parse_had_errors is True


def test_parse_one_file_strips_tree_sitter_nodes(tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    pass\n")
    result = parse_one_file(("a.py", "python", str(tmp_path)))
    for fn in result.file_structure.functions:
        assert fn.node is None
        assert fn.body_node is None
        assert fn.name == "f"
