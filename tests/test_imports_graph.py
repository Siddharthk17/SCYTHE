import subprocess

import pytest

from ctx_engine.languages.base import ImportStatement
from ctx_engine.imports_graph import (
    _csharp_namespace_cache,
    _csharp_namespace_of,
    resolve_imports_graph,
)


@pytest.fixture(autouse=True)
def _clear_csharp_namespace_cache():
    """Namespace scanning is cached per-process; keep tests isolated."""
    _csharp_namespace_cache.clear()
    yield
    _csharp_namespace_cache.clear()

def test_resolve_imports_graph(tmp_path):
    # 1. Initialize a git repo in the temp path
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    
    # 2. Write a dummy go.mod
    (tmp_path / "go.mod").write_text("module github.com/user/project\n", encoding="utf-8")
    
    # 3. Create file structure in files_languages
    files_languages = {
        # Python
        "pkg/a.py": "python",
        "pkg/b.py": "python",
        "pkg/sub/__init__.py": "python",
        
        # JS/TS
        "src/utils.ts": "typescript",
        "src/components/Button.tsx": "typescript",
        
        # Go
        "main.go": "go",
        "db/db.go": "go",
        "db/helper.go": "go",
        
        # Rust
        "src/lib.rs": "rust",
        "src/utils.rs": "rust",
        "src/utils/helper.rs": "rust",
        "src/sub/mod.rs": "rust",
    }
    
    for relative_path in files_languages:
        full_path = tmp_path / relative_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.touch()
        
    files_exports = {
        "src/utils.rs": ["util_func"],
        "src/lib.rs": ["lib_func"],
        "pkg/b.py": ["FuncB"],
    }
    
    files_raw_imports = {
        "pkg/a.py": [
            ImportStatement(module="pkg.b", names=["FuncB"], level=0),
            ImportStatement(module="sub", names=[], level=1),
            ImportStatement(module="os", names=[], level=0),
        ],
        "src/components/Button.tsx": [
            ImportStatement(module="../utils", names=[]),
            ImportStatement(module="react", names=[]),
        ],
        "main.go": [
            ImportStatement(module="github.com/user/project/db"),
            ImportStatement(module="fmt"),
        ],
        "src/utils/helper.rs": [
            ImportStatement(module="crate::utils", names=["util_func"]),
            ImportStatement(module="std::io", names=["Read"]),
        ],
        "src/sub/mod.rs": [
            ImportStatement(module="super::utils", names=["util_func"]),
            ImportStatement(module="super::lib_func", names=[]),
        ],
    }
    
    resolved_imports, used_by = resolve_imports_graph(
        files_languages,
        files_raw_imports,
        files_exports,
        tmp_path
    )
    
    # Assert Python resolution
    assert set(resolved_imports["pkg/a.py"]) == {"pkg/b.py", "pkg/sub/__init__.py"}
    
    # Assert JS/TS resolution
    assert set(resolved_imports["src/components/Button.tsx"]) == {"src/utils.ts"}
    
    # Assert Go resolution
    assert set(resolved_imports["main.go"]) == {"db/db.go", "db/helper.go"}
    
    # Assert Rust resolution
    assert set(resolved_imports["src/utils/helper.rs"]) == {"src/utils.rs"}
    
    # Assert Rust super:: resolution — src/sub/mod.rs should resolve to parent files
    # super::utils → src/utils.rs, super::lib_func → src/lib.rs
    assert set(resolved_imports["src/sub/mod.rs"]) == {"src/utils.rs", "src/lib.rs"}
    
    # Assert used_by reverse edges are correctly populated
    assert used_by["pkg/b.py"] == ["pkg/a.py"]
    assert used_by["src/utils.ts"] == ["src/components/Button.tsx"]
    assert sorted(used_by["db/db.go"]) == ["main.go"]
    # Rust super:: used_by edges
    assert "src/sub/mod.rs" in used_by["src/utils.rs"]
    assert "src/sub/mod.rs" in used_by["src/lib.rs"]

# ── Java import resolution ─────────────────────────────────────────────────────


def test_java_import_resolution_maven_layout(tmp_path):
    """src/main/java (Maven layout): package path maps to a repo-relative file."""
    files_languages = {
        "src/main/java/com/example/service/UserService.java": "java",
        "src/main/java/com/example/model/User.java": "java",
        "src/main/java/com/example/model/Extra.java": "java",
    }
    files_raw_imports = {
        "src/main/java/com/example/service/UserService.java": [
            ImportStatement(module="com.example.model.User", names=[]),
        ],
    }
    resolved, used_by = resolve_imports_graph(
        files_languages, files_raw_imports, {}, tmp_path
    )
    edges = resolved["src/main/java/com/example/service/UserService.java"]
    assert "src/main/java/com/example/model/User.java" in edges
    assert "src/main/java/com/example/model/Extra.java" not in edges
    assert "src/main/java/com/example/service/UserService.java" in used_by[
        "src/main/java/com/example/model/User.java"
    ]


def test_java_import_resolution_flat_src_root(tmp_path):
    """Gradle-style src/ root: resolution works without the Maven prefix."""
    files_languages = {
        "src/com/example/model/User.java": "java",
        "src/com/example/service/UserService.java": "java",
    }
    files_raw_imports = {
        "src/com/example/service/UserService.java": [
            ImportStatement(module="com.example.model.User", names=[]),
        ],
    }
    resolved, _ = resolve_imports_graph(files_languages, files_raw_imports, {}, tmp_path)
    assert "src/com/example/model/User.java" in resolved[
        "src/com/example/service/UserService.java"
    ]


def test_java_wildcard_import_resolves_package_edges(tmp_path):
    """com.example.model.* -> an edge to every .java file in the package dir."""
    (tmp_path / "src/main/java/com/example/model").mkdir(parents=True)
    files_languages = {
        "src/main/java/com/example/service/UserService.java": "java",
        "src/main/java/com/example/model/User.java": "java",
        "src/main/java/com/example/model/Extra.java": "java",
    }
    files_raw_imports = {
        "src/main/java/com/example/service/UserService.java": [
            ImportStatement(module="com.example.model", names=["*"]),
        ],
    }
    resolved, _ = resolve_imports_graph(files_languages, files_raw_imports, {}, tmp_path)
    edges = resolved["src/main/java/com/example/service/UserService.java"]
    assert "src/main/java/com/example/model/User.java" in edges
    assert "src/main/java/com/example/model/Extra.java" in edges


def test_java_unresolvable_import_is_skipped_not_fatal(tmp_path):
    """A missing package (e.g. org.apache.*) produces no edge and no exception."""
    files_languages = {"src/Main.java": "java"}
    files_raw_imports = {
        "src/Main.java": [ImportStatement(module="org.apache.Thing", names=[])],
    }
    resolved, _ = resolve_imports_graph(files_languages, files_raw_imports, {}, tmp_path)
    assert resolved["src/Main.java"] == []


# ── C# import resolution ────────────────────────────────────────────────────────


def test_csharp_namespace_import_resolution(tmp_path):
    """using MyApp.OtherLib; -> any .cs file declaring that namespace."""
    (tmp_path / "A.cs").write_text(
        "namespace MyApp.OtherLib { public class Helper {} }\n", encoding="utf-8"
    )
    (tmp_path / "B.cs").write_text(
        "using MyApp.OtherLib;\nnamespace MyApp { public class Main {} }\n",
        encoding="utf-8",
    )
    files_languages = {"A.cs": "csharp", "B.cs": "csharp"}
    files_raw_imports = {"B.cs": [ImportStatement(module="MyApp.OtherLib", names=[])]}
    resolved, used_by = resolve_imports_graph(
        files_languages, files_raw_imports, {}, tmp_path
    )
    assert "A.cs" in resolved["B.cs"]
    assert "B.cs" in used_by["A.cs"]


def test_csharp_system_namespace_skipped(tmp_path):
    """System.* / Microsoft.* are external -> never resolved to local files."""
    (tmp_path / "C.cs").write_text(
        "using System.Collections.Generic;\nnamespace App { public class X {} }\n",
        encoding="utf-8",
    )
    files_languages = {"C.cs": "csharp"}
    files_raw_imports = {
        "C.cs": [ImportStatement(module="System.Collections.Generic", names=[])],
    }
    resolved, _ = resolve_imports_graph(files_languages, files_raw_imports, {}, tmp_path)
    assert resolved["C.cs"] == []


def test_csharp_file_scoped_namespace_resolution(tmp_path):
    """C# 10 file-scoped namespace is discoverable by the import scanner."""
    (tmp_path / "Lib.cs").write_text(
        "namespace MyApp.Fancy;\npublic class Gadget {}\n", encoding="utf-8"
    )
    (tmp_path / "Use.cs").write_text(
        "using MyApp.Fancy;\nnamespace MyApp { public class Worker {} }\n",
        encoding="utf-8",
    )
    files_languages = {"Lib.cs": "csharp", "Use.cs": "csharp"}
    files_raw_imports = {
        "Use.cs": [ImportStatement(module="MyApp.Fancy", names=[])],
    }
    resolved, _ = resolve_imports_graph(files_languages, files_raw_imports, {}, tmp_path)
    assert "Lib.cs" in resolved["Use.cs"]


def test_csharp_namespace_cache_invalidates_on_file_change(tmp_path):
    """Cache is keyed on the file stat: changing a file refreshes its namespace."""
    lib = tmp_path / "Lib.cs"
    lib.write_text("namespace V1; public class A {}\n", encoding="utf-8")
    assert _csharp_namespace_of("Lib.cs", tmp_path) == "V1"
    # Change size + content so the cache fingerprint changes.
    lib.write_text("namespace V22; public class A {}\n", encoding="utf-8")
    assert _csharp_namespace_of("Lib.cs", tmp_path) == "V22"