from pathlib import Path
from ctx_engine.languages.base import ImportStatement

def get_go_module_name(repo_root: Path) -> str | None:
    """Extract the module name from the go.mod file at the repo root."""
    go_mod_path = repo_root / "go.mod"
    if not go_mod_path.exists():
        return None
    try:
        content = go_mod_path.read_text(encoding="utf-8")
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("module "):
                return line.split("module ", 1)[1].strip()
    except Exception:
        pass
    return None

def normalize_repo_path(base_dir: Path, rel_path_str: str) -> Path:
    """Normalize a path relative to repo root, handling '.' and '..' segments."""
    if rel_path_str.startswith("/"):
        p = Path(rel_path_str.lstrip("/"))
    else:
        p = base_dir / rel_path_str
    
    parts = []
    for part in p.parts:
        if part == ".":
            continue
        elif part == "..":
            if parts:
                parts.pop()
        else:
            parts.append(part)
    return Path(*parts)

def get_rust_parent_module(file_path: str, files_set: set[str]) -> str | None:
    """Resolve the parent module file of a Rust module file."""
    p = Path(file_path)
    if p.name == "mod.rs":
        parent_dir = p.parent.parent
    else:
        parent_dir = p.parent
        
    candidates = [
        parent_dir.with_suffix(".rs"),
        parent_dir / "mod.rs",
        parent_dir / "lib.rs",
        parent_dir / "main.rs",
    ]
    for cand in candidates:
        cand_str = cand.as_posix()
        if cand_str in files_set:
            return cand_str
    return None

def resolve_rust_segments(base_dir: Path, segments: list[str], files_set: set[str], exports_map: dict[str, list[str]]) -> str | None:
    """Resolve Rust path segments to a file by checking suffixes and exports."""
    for i in range(len(segments), 0, -1):
        mod_parts = segments[:i]
        item_parts = segments[i:]
        
        cand_dir = base_dir
        for part in mod_parts:
            cand_dir = cand_dir / part
            
        cand_files = [
            cand_dir.with_suffix(".rs"),
            cand_dir / "mod.rs"
        ]
        for file_cand in cand_files:
            file_cand_str = file_cand.as_posix()
            if file_cand_str in files_set:
                if not item_parts:
                    return file_cand_str
                first_item = item_parts[0]
                if first_item == "*":
                    return file_cand_str
                exports = exports_map.get(file_cand_str, [])
                if first_item in exports:
                    return file_cand_str
    return None

def resolve_rust_import(current_file: str, import_stmt: ImportStatement, files_set: set[str], exports_map: dict[str, list[str]]) -> list[str]:
    """Resolve a Rust ImportStatement to zero or more repo-relative paths."""
    resolved = []
    module = import_stmt.module
    names = import_stmt.names or [""]
    
    for name in names:
        full_path_str = f"{module}::{name}" if name else module
        segments = [s for s in full_path_str.split("::") if s]
        if not segments:
            continue
            
        if segments[0] == "self":
            continue
            
        elif segments[0] == "super":
            curr = current_file
            idx = 0
            while idx < len(segments) and segments[idx] == "super":
                curr = get_rust_parent_module(curr, files_set)
                if not curr:
                    break
                idx += 1
            if not curr:
                continue
                
            remaining = segments[idx:]
            if not remaining:
                resolved.append(curr)
            else:
                first_item = remaining[0]
                if first_item in exports_map.get(curr, []):
                    resolved.append(curr)
                    continue

                curr_path = Path(curr)
                if curr_path.name in ("mod.rs", "lib.rs", "main.rs"):
                    base_dir = curr_path.parent
                else:
                    base_dir = curr_path.parent / curr_path.stem

                resolved_path = resolve_rust_segments(base_dir, remaining, files_set, exports_map)
                if resolved_path:
                    resolved.append(resolved_path)
                    
        elif segments[0] == "crate":
            remaining = segments[1:]
            resolved_path = resolve_rust_segments(Path("src"), remaining, files_set, exports_map)
            if resolved_path:
                resolved.append(resolved_path)
                
        else:
            # Fallback to absolute/crate relative lookup under src/
            resolved_path = resolve_rust_segments(Path("src"), segments, files_set, exports_map)
            if resolved_path:
                resolved.append(resolved_path)
                
    return resolved

def resolve_file_imports(
    current_file: str,
    language: str,
    raw_imports: list[ImportStatement],
    files_set: set[str],
    exports_map: dict[str, list[str]],
    go_module_name: str | None
) -> list[str]:
    """Resolve raw imports of a file to repo-relative paths present in files_set."""
    resolved = []
    current_dir = Path(current_file).parent

    for imp in raw_imports:
        if language == "python":
            # absolute or relative
            if imp.level > 0:
                # relative import
                base_dir = current_dir
                for _ in range(imp.level - 1):
                    base_dir = base_dir.parent
                
                # Try module as file/dir
                parts = [p for p in imp.module.split(".") if p]
                target_dir = base_dir
                for part in parts:
                    target_dir = target_dir / part
                
                # Check target_dir or target_dir/__init__.py
                # If we import names, they can be submodules
                names = imp.names or [""]
                for name in names:
                    if name and name != "*":
                        # Check target_dir/name.py or target_dir/name/__init__.py
                        cand1 = (target_dir / name).with_suffix(".py").as_posix()
                        cand2 = (target_dir / name / "__init__.py").as_posix()
                        if cand1 in files_set:
                            resolved.append(cand1)
                        elif cand2 in files_set:
                            resolved.append(cand2)
                    
                    # Also check target_dir.py or target_dir/__init__.py
                    cand3 = target_dir.with_suffix(".py").as_posix()
                    cand4 = (target_dir / "__init__.py").as_posix()
                    if cand3 in files_set:
                        resolved.append(cand3)
                    elif cand4 in files_set:
                        resolved.append(cand4)
            else:
                # absolute import
                # Try normal, then src/
                parts = imp.module.split(".")
                for prefix in (Path(""), Path("src")):
                    target = prefix
                    for part in parts:
                        target = target / part
                    
                    # check target.py and target/__init__.py
                    cand1 = target.with_suffix(".py").as_posix()
                    cand2 = (target / "__init__.py").as_posix()
                    if cand1 in files_set:
                        resolved.append(cand1)
                    elif cand2 in files_set:
                        resolved.append(cand2)

                    # If names exist, check submodules
                    names = imp.names or [""]
                    for name in names:
                        if name and name != "*":
                            cand3 = (target / name).with_suffix(".py").as_posix()
                            cand4 = (target / name / "__init__.py").as_posix()
                            if cand3 in files_set:
                                resolved.append(cand3)
                            elif cand4 in files_set:
                                resolved.append(cand4)

        elif language in ("javascript", "typescript", "tsx"):
            # relative or root-relative
            if imp.module.startswith(".") or imp.module.startswith("/"):
                norm = normalize_repo_path(current_dir, imp.module)
                # Try direct and with suffixes
                exts = ["", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"]
                found = False
                for ext in exts:
                    cand = norm.with_suffix(norm.suffix + ext) if ext else norm
                    cand_str = cand.as_posix()
                    if cand_str in files_set:
                        resolved.append(cand_str)
                        found = True
                        break
                
                # Try index files
                if not found:
                    for ext in (".ts", ".tsx", ".js", ".jsx"):
                        cand = norm / f"index{ext}"
                        cand_str = cand.as_posix()
                        if cand_str in files_set:
                            resolved.append(cand_str)
                            break

        elif language == "go":
            if go_module_name and imp.module.startswith(go_module_name):
                # strip prefix
                rel_dir = imp.module[len(go_module_name):].lstrip("/")
                if not rel_dir:
                    rel_dir = "."
                # Find all go files in this directory (non-recursive)
                for f in files_set:
                    f_path = Path(f)
                    if f_path.parent.as_posix() == rel_dir and f_path.suffix == ".go":
                        resolved.append(f)

        elif language == "rust":
            resolved.extend(resolve_rust_import(current_file, imp, files_set, exports_map))

    # Remove duplicates and self-imports
    return sorted(list(set([r for r in resolved if r != current_file])))

def resolve_imports_graph(
    files_languages: dict[str, str],
    files_raw_imports: dict[str, list[ImportStatement]],
    files_exports: dict[str, list[str]],
    repo_root: Path
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Resolve raw imports and construct the import graph (imports & used_by lists)."""
    files_set = set(files_languages.keys())
    go_module_name = get_go_module_name(repo_root)

    resolved_imports = {}
    used_by = {f: [] for f in files_set}

    for f, lang in files_languages.items():
        raw_imps = files_raw_imports.get(f, [])
        resolved = resolve_file_imports(f, lang, raw_imps, files_set, files_exports, go_module_name)
        resolved_imports[f] = resolved
        for target in resolved:
            if target in used_by:
                used_by[target].append(f)

    # Sort used_by lists
    for f in used_by:
        used_by[f] = sorted(list(set(used_by[f])))

    return resolved_imports, used_by
