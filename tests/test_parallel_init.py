import pickle
import pytest
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor
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


def test_parse_one_file_is_picklable(tmp_path):
    (tmp_path / "picklable.py").write_text("def foo():\n    return 42\n")
    result = parse_one_file(("picklable.py", "python", str(tmp_path)))
    data = pickle.dumps(result)
    restored = pickle.loads(data)
    assert restored.rel_path == "picklable.py"
    assert restored.language == "python"
    assert len(restored.function_hashes) == 1
    assert "foo" in restored.function_hashes
    assert restored.content_hash
    assert restored.mtime > 0
    assert restored.parse_had_errors is False


def test_parse_one_file_works_in_process_pool(tmp_path):
    (tmp_path / "pooltest.py").write_text("def bar():\n    return 99\n")
    with ProcessPoolExecutor(max_workers=1) as pool:
        future = pool.submit(parse_one_file, ("pooltest.py", "python", str(tmp_path)))
        result = future.result(timeout=10)
    assert isinstance(result, ParseResult)
    assert result.rel_path == "pooltest.py"
    assert "bar" in result.function_hashes


def test_worker_count_default():
    import os
    expected = max(1, os.cpu_count() or 1)
    worker_count = max(1, os.cpu_count() or 1)
    assert worker_count == expected
    assert worker_count >= 1
