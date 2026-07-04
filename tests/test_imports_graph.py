import subprocess
from pathlib import Path
from ctx_engine.languages.base import ImportStatement
from ctx_engine.imports_graph import resolve_imports_graph

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
    }
    
    for relative_path in files_languages:
        full_path = tmp_path / relative_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.touch()
        
    files_exports = {
        "src/utils.rs": ["util_func"],
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
    
    # Assert used_by reverse edges are correctly populated
    assert used_by["pkg/b.py"] == ["pkg/a.py"]
    assert used_by["src/utils.ts"] == ["src/components/Button.tsx"]
    assert sorted(used_by["db/db.go"]) == ["main.go"]
