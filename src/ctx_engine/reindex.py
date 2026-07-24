# Reindexing pipeline for ctx index database.

import hashlib
import json
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ctx_engine.discovery import EXTENSION_TO_LANGUAGE
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
from ctx_engine.languages.base import FileStructure, FunctionRecord
from ctx_engine.languages.registry import get_parser, parse_file
from ctx_engine.imports_graph import resolve_imports_graph
from ctx_engine.call_graph import resolve_calls
from ctx_engine.intelligence.confidence import decay
from ctx_engine.intelligence.taint import propagate_taint

logger = logging.getLogger("ctx")

ADAPTERS = {
    "python": PythonAdapter(),
    "javascript": JavaScriptAdapter(),
    "typescript": TypeScriptAdapter(),
    "tsx": TypeScriptAdapter(),
    "go": GoAdapter(),
    "rust": RustAdapter(),
}


def extension_to_language(ext: str) -> str | None:
    return EXTENSION_TO_LANGUAGE.get(ext)


def can_skip_file(
    conn: sqlite3.Connection,
    abs_path: Path,
    rel_path: str,
) -> bool:
    """
    Returns True if the file's mtime and size match the cached values,
    meaning its content is guaranteed unchanged since last index.
    Known limitation: rsync / git checkout can preserve original mtime,
    making a replaced file look unchanged. Combining mtime + size check
    makes false cache hits negligible on developer workstations.
    """
    try:
        stat = abs_path.stat()
    except (FileNotFoundError, PermissionError, OSError):
        return False
    current_mtime = stat.st_mtime
    current_size = stat.st_size

    row = conn.execute(
        "SELECT mtime, file_size, content_hash FROM files WHERE path = ?",
        (rel_path,),
    ).fetchone()

    if row is None:
        return False

    if row["mtime"] is None or row["file_size"] is None:
        return False

    if abs(current_mtime - row["mtime"]) < 0.01 and current_size == row["file_size"]:
        return True

    return False


@dataclass
class ParseResult:
    rel_path: str
    language: str
    file_structure: FileStructure
    file_semantic_hash: str
    content_hash: str
    mtime: float
    file_size: int
    function_hashes: dict[str, str]
    parse_had_errors: bool
    error: str | None = None


def parse_one_file(args: tuple[str, str, str]) -> ParseResult:
    rel_path, language, repo_root_str = args
    repo_root = Path(repo_root_str)
    abs_path = repo_root / rel_path
    try:
        raw_bytes = abs_path.read_bytes()
        stat = abs_path.stat()
        content_hash = hashlib.sha256(raw_bytes).hexdigest()
        tree, source = parse_file(abs_path, language)
        adapter = ADAPTERS[language]
        file_structure = adapter.extract(tree, source)
        file_sem_hash = file_semantic_hash(tree, source, language)
        fn_hashes = {
            fn.name: function_semantic_hash(fn.node, source, language)
            for fn in file_structure.functions
        }

        # Strip unpicklable AST node references for cross-process transport
        stripped_funcs = []
        for fn in file_structure.functions:
            stripped_funcs.append(
                FunctionRecord(
                    name=fn.name,
                    class_name=fn.class_name,
                    signature=fn.signature,
                    line_start=fn.line_start,
                    line_end=fn.line_end,
                    node=None,
                    body_node=None,
                    mutates=fn.mutates,
                )
            )
        stripped = FileStructure(
            exports=file_structure.exports,
            imports_raw=file_structure.imports_raw,
            functions=stripped_funcs,
            class_superclasses=file_structure.class_superclasses,
        )

        return ParseResult(
            rel_path=rel_path,
            language=language,
            file_structure=stripped,
            file_semantic_hash=file_sem_hash,
            content_hash=content_hash,
            mtime=stat.st_mtime,
            file_size=stat.st_size,
            function_hashes=fn_hashes,
            parse_had_errors=tree.root_node.has_error,
        )
    except Exception as e:
        return ParseResult(
            rel_path=rel_path,
            language=language,
            file_structure=FileStructure(exports=[], imports_raw=[], functions=[]),
            file_semantic_hash="",
            content_hash="",
            mtime=0.0,
            file_size=0,
            function_hashes={},
            parse_had_errors=True,
            error=str(e),
        )

def apply_parse_result(conn: sqlite3.Connection, result: ParseResult) -> None:
    conn.execute(
        "UPDATE files SET mtime = ?, file_size = ? WHERE path = ?",
        (result.mtime, result.file_size, result.rel_path),
    )


def reindex_single_file(
    conn: sqlite3.Connection,
    repo_root: Path,
    rel_path: str,
    language: str,
) -> list[str]:
    changed_ids, _, _, _ = reindex_file(conn, repo_root, rel_path, language)
    return list(changed_ids)


def reindex_file(
    conn: sqlite3.Connection,
    repo_root: Path,
    path: str,
    language: str
) -> tuple[set[str], list[tuple[str, list[str]]], dict, bool]:
    """
    Run Pass 1 of the reindexing pipeline for a single file.
    Compares the new hashes to the stored ones, preserves/decays metadata,
    and returns (changed_function_ids, removed_function_ids_with_caller_snapshots, ext_data, has_parse_errors).
    """
    file_abspath = repo_root / path
    try:
        source = file_abspath.read_bytes()
    except (FileNotFoundError, PermissionError, OSError) as err:
        logger.error("Cannot read %s: %s — skipping", path, err)
        return set(), [], {"exports": [], "imports_raw": [], "class_superclasses": {}, "functions": []}, False
    content_hash_val = file_content_hash(source)

    # Parse and extract
    parser = get_parser(language)
    tree = parser.parse(source)
    has_parse_errors = tree.root_node.has_error
    
    if has_parse_errors:
        logger.warning("File %s contains parse errors", path)

    adapter = ADAPTERS[language]
    struct = adapter.extract(tree, source)
    file_sem_hash = file_semantic_hash(tree, source, language)

    # 1. Fetch old file metadata
    old_file = conn.execute(
        "SELECT semantic_hash, confidence, purpose, summary, danger, is_stale, indexed_at FROM files WHERE path = ?",
        (path,)
    ).fetchone()

    now = datetime.now(timezone.utc).isoformat()

    # 2. Determine new file metadata values
    if old_file is None:
        file_purpose = None
        file_summary = None
        file_danger = None
        file_confidence = 1.0
        file_is_stale = 1
        file_indexed_at = now
    elif old_file["semantic_hash"] == file_sem_hash:
        file_purpose = old_file["purpose"]
        file_summary = old_file["summary"]
        file_danger = old_file["danger"]
        file_confidence = old_file["confidence"]
        file_is_stale = old_file["is_stale"]
        file_indexed_at = old_file["indexed_at"] or now
    else:
        file_purpose = old_file["purpose"]
        file_summary = old_file["summary"]
        file_danger = old_file["danger"]
        file_confidence = decay(old_file["confidence"])
        file_is_stale = 1
        file_indexed_at = old_file["indexed_at"] or now

    # 3. Fetch old functions metadata
    old_funcs = {
        row["id"]: dict(row) for row in conn.execute(
            "SELECT id, semantic_hash, confidence, summary, summary_long, danger, is_stale, is_tainted, taint_source FROM functions WHERE file = ?",
            (path,)
        ).fetchall()
    }

    # 4. Map new functions to IDs
    seen_ids = set()
    new_funcs_to_insert = []
    changed_function_ids = set()

    for func in struct.functions:
        func_sem_hash = function_semantic_hash(func.node, source, language)
        qualified_name = f"{func.class_name}.{func.name}" if func.class_name else func.name
        func_id = f"{path}::{qualified_name}"
        if func_id in seen_ids:
            func_id = f"{func_id}@{func.line_start}"
        seen_ids.add(func_id)

        # Check old function metadata
        if func_id not in old_funcs:
            # New function
            func_summary = None
            func_summary_long = None
            func_danger = None
            func_confidence = 1.0
            func_is_stale = 1
            func_is_tainted = 0
            func_taint_source = None
            changed_function_ids.add(func_id)
        else:
            old_func = old_funcs[func_id]
            if old_func["semantic_hash"] == func_sem_hash:
                # Unchanged function
                func_summary = old_func["summary"]
                func_summary_long = old_func["summary_long"]
                func_danger = old_func["danger"]
                func_confidence = old_func["confidence"]
                func_is_stale = old_func["is_stale"]
                func_is_tainted = old_func["is_tainted"]
                func_taint_source = old_func["taint_source"]
            else:
                # Changed function
                func_summary = old_func["summary"]
                func_summary_long = old_func["summary_long"]
                func_danger = old_func["danger"]
                func_confidence = decay(old_func["confidence"])
                func_is_stale = 1
                func_is_tainted = old_func["is_tainted"]
                func_taint_source = old_func["taint_source"]
                changed_function_ids.add(func_id)

        new_funcs_to_insert.append({
            "id": func_id,
            "class_name": func.class_name,
            "name": func.name,
            "signature": func.signature,
            "summary": func_summary,
            "summary_long": func_summary_long,
            "mutates": json.dumps(func.mutates),
            "danger": func_danger,
            "line_start": func.line_start,
            "line_end": func.line_end,
            "semantic_hash": func_sem_hash,
            "is_tainted": func_is_tainted,
            "taint_source": func_taint_source,
            "confidence": func_confidence,
            "is_stale": func_is_stale,
            "func_record": func
        })

    # 5. Capture caller snapshots for removed functions
    removed_function_ids_with_caller_snapshots = []
    for old_id, old_func in old_funcs.items():
        if old_id not in seen_ids:
            # This function was removed or renamed
            callers = [
                row[0] for row in conn.execute(
                    "SELECT caller_id FROM call_graph WHERE callee_id = ?", (old_id,)
                ).fetchall()
            ]
            removed_function_ids_with_caller_snapshots.append((old_id, callers))

    # 6. Apply database writes inside transaction
    with conn:
        # Delete old calls & functions
        conn.execute(
            "DELETE FROM call_graph WHERE caller_id IN (SELECT id FROM functions WHERE file = ?)",
            (path,)
        )
        conn.execute("DELETE FROM functions WHERE file = ?", (path,))

        # Insert or replace file record
        mtime = float(file_abspath.stat().st_mtime)
        file_size = int(file_abspath.stat().st_size)
        conn.execute(
            """
            INSERT OR REPLACE INTO files (
                path, system, purpose, exports, imports, used_by, used_by_count,
                summary, danger, last_change, semantic_hash, content_hash,
                confidence, is_stale, updated_at, indexed_at,
                mtime, file_size
            ) VALUES (?, NULL, ?, ?, NULL, NULL, 0, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                path,
                file_purpose,
                json.dumps(struct.exports),
                file_summary,
                file_danger,
                file_sem_hash,
                content_hash_val,
                file_confidence,
                file_is_stale,
                now,
                file_indexed_at,
                mtime,
                file_size,
            )
        )

        # Insert new functions
        for f_data in new_funcs_to_insert:
            conn.execute(
                """
                INSERT INTO functions (
                    id, file, class_name, name, signature, summary, summary_long,
                    mutates, danger, line_start, line_end, semantic_hash, is_tainted,
                    taint_source, confidence, is_stale, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f_data["id"],
                    path,
                    f_data["class_name"],
                    f_data["name"],
                    f_data["signature"],
                    f_data["summary"],
                    f_data["summary_long"],
                    f_data["mutates"],
                    f_data["danger"],
                    f_data["line_start"],
                    f_data["line_end"],
                    f_data["semantic_hash"],
                    f_data["is_tainted"],
                    f_data["taint_source"],
                    f_data["confidence"],
                    f_data["is_stale"],
                    now
                )
            )

        # Re-populate taint_queue for preserved taint state
        for f_data in new_funcs_to_insert:
            if f_data["is_tainted"] and f_data["taint_source"]:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO taint_queue (function_id, taint_source, queued_at, priority)
                    VALUES (?, ?, ?, 0)
                    """,
                    (f_data["id"], f_data["taint_source"], now)
                )

    return (
        changed_function_ids,
        removed_function_ids_with_caller_snapshots,
        {
            "exports": struct.exports,
            "imports_raw": struct.imports_raw,
            "class_superclasses": struct.class_superclasses,
            "functions": new_funcs_to_insert,
        },
        has_parse_errors,
    )

def run_reindex_pipeline(
    conn: sqlite3.Connection,
    repo_root: Path,
    files_to_reindex: dict[str, str]
) -> tuple[int, list[str], set[str]]:
    """Run the 4-pass reindexing pipeline for the specified files.

    Returns (parse_error_count, parse_error_paths, all_changed_func_ids).
    """
    conn.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc).isoformat()
    # PASS 1: Extract & Hash Compare
    all_changed_func_ids = set()
    all_removed_funcs_snapshots = []
    reindexed_extractions = {}
    parse_error_count = 0
    parse_error_paths: list[str] = []

    for path, language in files_to_reindex.items():
        c_ids, r_snapshots, ext_data, has_parse_errors = reindex_file(conn, repo_root, path, language)
        all_changed_func_ids.update(c_ids)
        all_removed_funcs_snapshots.extend(r_snapshots)
        reindexed_extractions[path] = ext_data
        if has_parse_errors:
            parse_error_count += 1
            parse_error_paths.append(path)

    # PASS 2: Import Graph Rebuild
    db_files = [row["path"] for row in conn.execute("SELECT path FROM files").fetchall()]
    all_paths = sorted(list(set(db_files + list(files_to_reindex.keys()))))

    files_languages = {}
    files_raw_imports = {}
    files_exports = {}
    class_superclasses = {}

    for p in all_paths:
        ext = Path(p).suffix
        lang = EXTENSION_TO_LANGUAGE.get(ext)
        if not lang:
            continue
        files_languages[p] = lang

        if p in reindexed_extractions:
            ext_data = reindexed_extractions[p]
            files_raw_imports[p] = ext_data["imports_raw"]
            files_exports[p] = ext_data["exports"]
            class_superclasses[p] = ext_data["class_superclasses"]
        else:
            try:
                file_abspath = repo_root / p
                source = file_abspath.read_bytes()
                parser = get_parser(lang)
                tree = parser.parse(source)
                adapter = ADAPTERS[lang]
                struct = adapter.extract(tree, source)
                files_raw_imports[p] = struct.imports_raw
                files_exports[p] = struct.exports
                class_superclasses[p] = struct.class_superclasses
            except Exception as err:
                logger.warning("Could not parse file %s for import resolution: %s", p, err)
                files_raw_imports[p] = []
                # Fallback exports from DB
                exports_row = conn.execute("SELECT exports FROM files WHERE path = ?", (p,)).fetchone()
                files_exports[p] = json.loads(exports_row["exports"]) if (exports_row and exports_row["exports"]) else []
                class_superclasses[p] = {}

    resolved_imports, used_by = resolve_imports_graph(
        files_languages,
        files_raw_imports,
        files_exports,
        repo_root
    )

    with conn:
        for p in all_paths:
            imps = resolved_imports.get(p, [])
            ub = used_by.get(p, [])
            conn.execute(
                """
                UPDATE files SET
                    imports = ?,
                    used_by = ?,
                    used_by_count = ?,
                    updated_at = ?
                WHERE path = ?
                """,
                (json.dumps(imps), json.dumps(ub), len(ub), now, p)
            )

    # PASS 3: Call Graph Rebuild
    reindexed_files_list = list(files_to_reindex.keys())
    caller_ids_to_resolve = set()
    for p in reindexed_files_list:
        for row in conn.execute("SELECT id FROM functions WHERE file = ?", (p,)).fetchall():
            caller_ids_to_resolve.add(row[0])

    if reindexed_files_list:
        placeholders = ",".join("?" for _ in reindexed_files_list)
        external_callers = [
            row[0] for row in conn.execute(
                f"SELECT DISTINCT caller_id FROM call_graph WHERE callee_file IN ({placeholders})",
                reindexed_files_list
            ).fetchall()
        ]
        caller_ids_to_resolve.update(external_callers)

    with conn:
        if caller_ids_to_resolve:
            caller_list = list(caller_ids_to_resolve)
            for i in range(0, len(caller_list), 500):
                chunk = caller_list[i:i+500]
                conn.execute(
                    "DELETE FROM call_graph WHERE caller_id IN ({})".format(",".join("?" for _ in chunk)),
                    chunk
                )

    all_db_funcs = [
        dict(row) for row in conn.execute(
            "SELECT id, file, class_name, name, signature FROM functions"
        ).fetchall()
    ]

    callers_by_file = {}
    for cid in caller_ids_to_resolve:
        func_info = next((f for f in all_db_funcs if f["id"] == cid), None)
        if func_info:
            callers_by_file.setdefault(func_info["file"], []).append(func_info)

    for file_path, funcs_to_populate in callers_by_file.items():
        if file_path in reindexed_extractions:
            ext_funcs = reindexed_extractions[file_path]["functions"]
            for f_info in funcs_to_populate:
                match = next((ef for ef in ext_funcs if ef["id"] == f_info["id"]), None)
                if match:
                    f_info["_node"] = match["func_record"].node
                    f_info["_language"] = files_languages.get(file_path)
        else:
            lang = files_languages.get(file_path)
            if lang:
                try:
                    file_abspath = repo_root / file_path
                    source = file_abspath.read_bytes()
                    parser = get_parser(lang)
                    tree = parser.parse(source)
                    adapter = ADAPTERS[lang]
                    struct = adapter.extract(tree, source)
                    
                    seen_ids = set()
                    for func in struct.functions:
                        qualified_name = f"{func.class_name}.{func.name}" if func.class_name else func.name
                        func_id = f"{file_path}::{qualified_name}"
                        if func_id in seen_ids:
                            func_id = f"{func_id}@{func.line_start}"
                        seen_ids.add(func_id)
                        
                        for f_info in funcs_to_populate:
                            if f_info["id"] == func_id:
                                f_info["_node"] = func.node
                                f_info["_language"] = lang
                except Exception as err:
                    logger.warning("Could not parse file %s for call graph resolution: %s", file_path, err)

    call_edges = resolve_calls(
        all_db_funcs,
        resolved_imports,
        files_raw_imports,
        class_superclasses,
        files_exports
    )

    with conn:
        for edge in call_edges:
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

    # PASS 4: Taint Propagation
    for cid in all_changed_func_ids:
        propagate_taint(conn, cid)
    for rid, snapshot_callers in all_removed_funcs_snapshots:
        propagate_taint(conn, rid, caller_snapshot=snapshot_callers)

    return parse_error_count, parse_error_paths, all_changed_func_ids
