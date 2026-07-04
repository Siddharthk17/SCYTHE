import json
import logging
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from ctx_engine.db import connect, init_schema
from ctx_engine.discovery import (
    assert_inside_git_repo,
    discover_all_tracked_paths,
    discover_parseable_files,
)
from ctx_engine.hashing import (
    file_content_hash,
    file_semantic_hash,
    function_semantic_hash,
)
from ctx_engine.languages import (
    PythonAdapter,
    JavaScriptAdapter,
    TypeScriptAdapter,
    GoAdapter,
    RustAdapter,
)
from ctx_engine.imports_graph import resolve_imports_graph
from ctx_engine.call_graph import resolve_calls
from ctx_engine.directories import build_directory_counts

logger = logging.getLogger("ctx")

ADAPTERS = {
    "python": PythonAdapter(),
    "javascript": JavaScriptAdapter(),
    "typescript": TypeScriptAdapter(),
    "tsx": TypeScriptAdapter(),
    "go": GoAdapter(),
    "rust": RustAdapter(),
}

def current_timestamp() -> str:
    """Return the current timezone-aware UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()

def run_init(repo_root: Path) -> None:
    """Run the complete initialization and indexing flow for the repository."""
    # 1. Assert inside a Git repo
    assert_inside_git_repo(repo_root)

    # 2. Database path and directory creation
    ctx_dir = repo_root / ".ctx"
    ctx_dir.mkdir(exist_ok=True)
    db_path = ctx_dir / "index.db"

    # 3. Connection and schema initialization
    conn = connect(db_path)
    init_schema(conn)

    # 4. Discover files
    tracked = discover_all_tracked_paths(repo_root)
    parseable = discover_parseable_files(repo_root)

    # Stats counters
    parse_errors = []
    function_count = 0
    language_counts = Counter()

    # Data collection for later passes
    files_languages = {}
    files_raw_imports = {}
    files_exports = {}
    class_superclasses = {}
    functions_for_calls = []

    now = current_timestamp()

    # 5. Sequential single-file parsing and DB insertion
    for path, language in parseable.items():
        language_counts[language] += 1
        files_languages[path] = language

        # Read content and compute hash
        file_abspath = repo_root / path
        try:
            source = file_abspath.read_bytes()
        except IOError as err:
            logger.warning("Could not read file %s: %s", path, err)
            continue

        content_hash_val = file_content_hash(source)

        # Parse file
        from ctx_engine.languages.registry import get_parser
        parser = get_parser(language)
        tree = parser.parse(source)

        if tree.root_node.has_error:
            parse_errors.append(path)
            # We still attempt extraction

        # Run language adapter
        adapter = ADAPTERS[language]
        try:
            struct = adapter.extract(tree, source)
        except Exception as err:
            logger.warning("Failed to extract structure from %s: %s", path, err)
            parse_errors.append(path)
            continue

        # File-level semantic hash
        file_sem_hash = file_semantic_hash(tree, source, language)

        # Cache imports, exports, and superclasses
        files_raw_imports[path] = struct.imports_raw
        files_exports[path] = struct.exports
        if hasattr(struct, "class_superclasses") and struct.class_superclasses:
            class_superclasses[path] = struct.class_superclasses

        # Idempotency block: delete functions and calls for this file
        with conn:
            conn.execute(
                "DELETE FROM call_graph WHERE caller_id IN (SELECT id FROM functions WHERE file = ?)",
                (path,)
            )
            conn.execute("DELETE FROM functions WHERE file = ?", (path,))

            # Insert or replace file record
            conn.execute(
                """
                INSERT OR REPLACE INTO files (
                    path, system, purpose, exports, imports, used_by, used_by_count,
                    summary, danger, last_change, semantic_hash, content_hash,
                    confidence, is_stale, updated_at, indexed_at
                ) VALUES (?, NULL, NULL, ?, NULL, NULL, 0, NULL, NULL, NULL, ?, ?, 1.0, 0, ?, ?)
                """,
                (
                    path,
                    json.dumps(struct.exports),
                    file_sem_hash,
                    content_hash_val,
                    now,
                    now
                )
            )

            # Insert functions for this file
            seen_ids = set()
            for func in struct.functions:
                func_sem_hash = function_semantic_hash(func.node, source, language)
                qualified_name = f"{func.class_name}.{func.name}" if func.class_name else func.name
                func_id = f"{path}::{qualified_name}"
                
                # Handle ID collisions in the same file
                if func_id in seen_ids:
                    func_id = f"{func_id}@{func.line_start}"
                seen_ids.add(func_id)

                conn.execute(
                    """
                    INSERT INTO functions (
                        id, file, class_name, name, signature, summary, summary_long,
                        mutates, danger, line_start, line_end, semantic_hash, is_tainted,
                        taint_source, confidence, updated_at
                    ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, NULL, ?, ?, ?, 0, NULL, 1.0, ?)
                    """,
                    (
                        func_id,
                        path,
                        func.class_name,
                        func.name,
                        func.signature,
                        json.dumps(func.mutates),
                        func.line_start,
                        func.line_end,
                        func_sem_hash,
                        now
                    )
                )

                function_count += 1
                functions_for_calls.append({
                    "id": func_id,
                    "file": path,
                    "class_name": func.class_name,
                    "name": func.name,
                    "signature": func.signature,
                    "_node": func.node,
                    "_language": language
                })

    # 6. Import graph pass
    resolved_imports, used_by = resolve_imports_graph(
        files_languages,
        files_raw_imports,
        files_exports,
        repo_root
    )

    import_edges_count = 0
    with conn:
        for f in files_languages:
            imps = resolved_imports.get(f, [])
            ub = used_by.get(f, [])
            import_edges_count += len(imps)

            conn.execute(
                """
                UPDATE files SET
                    imports = ?,
                    used_by = ?,
                    used_by_count = ?
                WHERE path = ?
                """,
                (
                    json.dumps(imps),
                    json.dumps(ub),
                    len(ub),
                    f
                )
            )

    # 7. Call graph pass
    call_edges = resolve_calls(
        functions_for_calls,
        resolved_imports,
        files_raw_imports,
        class_superclasses,
        files_exports
    )

    ambiguous_calls = 0
    unresolved_calls = 0

    with conn:
        for edge in call_edges:
            if edge["is_ambiguous"]:
                ambiguous_calls += 1
            if edge["callee_id"] is None:
                unresolved_calls += 1

            conn.execute(
                """
                INSERT INTO call_graph (
                    caller_id, callee_id, callee_name, callee_file, is_ambiguous, candidates
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    edge["caller_id"],
                    edge["callee_id"],
                    edge["callee_name"],
                    edge["callee_file"],
                    edge["is_ambiguous"],
                    edge["candidates"]
                )
            )

    # 8. Directories pass
    dir_counts = build_directory_counts(tracked)
    with conn:
        for dir_path, count in dir_counts.items():
            conn.execute(
                """
                INSERT OR REPLACE INTO directories (
                    path, system, summary, file_count, updated_at
                ) VALUES (?, NULL, NULL, ?, ?)
                """,
                (dir_path, count, now)
            )

    conn.close()

    # 9. Print summary report
    repo_name = repo_root.name
    parsed_lang_str = ", ".join(f"{lang}: {count}" for lang, count in sorted(language_counts.items()))
    skipped_count = len(tracked) - len(parseable)
    parse_errors_count = len(parse_errors)

    print(f"ctx init — {repo_name}")
    print()
    print(f"  {len(tracked)} files tracked by git")
    print(f"  {len(parseable)} files parsed ({parsed_lang_str})")
    print(f"  {skipped_count} files skipped (unsupported extension)")
    if parse_errors_count > 0:
        print(f"  {parse_errors_count} files with parse errors:")
        for err_path in sorted(parse_errors):
            print(f"      - {err_path}")
    else:
        print("  0 files with parse errors")
    print(f"  {function_count} functions extracted")
    print(f"  {import_edges_count} import edges resolved")
    print(f"  {len(call_edges)} call graph edges ({ambiguous_calls} ambiguous, {unresolved_calls} unresolved)")
    print(f"  {len(dir_counts)} directories indexed")
    print()
    print("  .ctx/index.db ready (WAL mode)")
