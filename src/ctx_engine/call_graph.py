import json
from tree_sitter import Node
from ctx_engine.languages.base import ImportStatement

def collect_calls_in_subtree(node: Node, language: str) -> list[tuple[str, str | None]]:
    """Traverse AST subtree to collect all calls as (callee_name, object_name) tuples."""
    calls = []

    def walk(n: Node):
        is_call = False
        callee_node = None

        if language == "python" and n.type == "call":
            is_call = True
            callee_node = n.child_by_field_name("function")
        elif language in ("javascript", "typescript", "tsx", "go", "rust") and n.type == "call_expression":
            is_call = True
            callee_node = n.child_by_field_name("function") or n.child_by_field_name("callee")
            if not callee_node and n.child_count > 0:
                callee_node = n.children[0]
        elif language == "rust" and n.type == "method_call_expression":
            receiver = n.child_by_field_name("value")
            method = n.child_by_field_name("name")
            if method:
                receiver_text = receiver.text.decode("utf-8") if receiver else None
                calls.append((method.text.decode("utf-8"), receiver_text))

        if is_call and callee_node:
            obj_node = None
            prop_node = None

            if callee_node.type == "attribute":
                obj_node = callee_node.child_by_field_name("object")
                prop_node = callee_node.child_by_field_name("attribute")
            elif callee_node.type == "member_expression":
                obj_node = callee_node.child_by_field_name("object")
                prop_node = callee_node.child_by_field_name("property")
            elif callee_node.type == "selector_expression":
                obj_node = callee_node.child_by_field_name("operand")
                prop_node = callee_node.child_by_field_name("field")
            elif callee_node.type == "field_expression":
                obj_node = callee_node.child_by_field_name("value")
                prop_node = callee_node.child_by_field_name("field")

            if obj_node and prop_node:
                calls.append((prop_node.text.decode("utf-8"), obj_node.text.decode("utf-8")))
            else:
                text = callee_node.text.decode("utf-8")
                if "::" in text:
                    parts = text.rsplit("::", 1)
                    calls.append((parts[1], parts[0]))
                else:
                    calls.append((text, None))

        for child in n.children:
            walk(child)

    walk(node)
    return calls

def resolve_calls(
    functions_list: list[dict],
    resolved_imports: dict[str, list[str]],
    raw_imports: dict[str, list[ImportStatement]],
    class_superclasses: dict[str, dict[str, str]],
    files_exports: dict[str, list[str]]
) -> list[dict]:
    """Resolve call edges between functions based on import scope and duck typing rules."""
    # Build lookups
    funcs_by_id = {f["id"]: f for f in functions_list}
    funcs_by_file = {}
    funcs_by_name = {}

    for f in functions_list:
        file = f["file"]
        funcs_by_file.setdefault(file, []).append(f)
        funcs_by_name.setdefault(f["name"], []).append(f)

    call_edges = []

    for caller in functions_list:
        caller_id = caller["id"]
        caller_file = caller["file"]
        caller_class = caller["class_name"]
        
        # Get raw AST node and language of caller
        caller_node = caller.get("_node")
        caller_lang = caller.get("_language")
        if caller_node is None:
            continue

        # Collect all calls within caller function body
        raw_calls = collect_calls_in_subtree(caller_node, caller_lang)


        for callee_name, obj_name in raw_calls:
            callee_id = None
            callee_file = None
            is_ambiguous = 0
            candidates = []

            # 1. Same-file module-level function
            if obj_name is None:
                # Check for matching name with no class in caller's file
                same_file_funcs = funcs_by_file.get(caller_file, [])
                match = next((f for f in same_file_funcs if f["name"] == callee_name and f["class_name"] is None), None)
                if match:
                    callee_id = match["id"]
                    callee_file = match["file"]

            # 2. Same-class method (self/this/cls/receiver)
            if callee_id is None and obj_name in ("self", "this", "cls"):
                if caller_class:
                    same_file_funcs = funcs_by_file.get(caller_file, [])
                    # Search current class
                    match = next((f for f in same_file_funcs if f["class_name"] == caller_class and f["name"] == callee_name), None)
                    
                    # Search superclasses in the same file if not found
                    curr_class = caller_class
                    file_supers = class_superclasses.get(caller_file, {})
                    while match is None and curr_class in file_supers:
                        curr_class = file_supers[curr_class]
                        match = next((f for f in same_file_funcs if f["class_name"] == curr_class and f["name"] == callee_name), None)

                    if match:
                        callee_id = match["id"]
                        callee_file = match["file"]

            # 3. Imported-module function (module.foo())
            if callee_id is None and obj_name is not None and obj_name not in ("self", "this", "cls"):
                # Find matching import statement
                file_raw_imps = raw_imports.get(caller_file, [])
                target_imp = None
                for imp in file_raw_imps:
                    # Check if imported module directly or via alias
                    if imp.alias == obj_name or (imp.module and imp.module.split(".")[-1] == obj_name):
                        target_imp = imp
                        break
                    elif obj_name in imp.names:
                        target_imp = imp
                        break

                if target_imp:
                    # We found a raw import that matches obj_name.
                    # Let's resolve it to target file paths.
                    # Since resolve_imports_graph resolves the whole file, we can look at files in resolved_imports[caller_file].
                    # Filter resolved_imports that match target_imp's resolution.
                    # To keep it simple: we check which of the resolved imports of caller_file match target_imp's path.
                    # Let's check which resolved import files export callee_name.
                    caller_res_imports = resolved_imports.get(caller_file, [])
                    for res_file in caller_res_imports:
                        # Check if this res_file exports callee_name
                        exports = files_exports.get(res_file, [])
                        if callee_name in exports:
                            # Verify if there is a function with callee_name in that file
                            res_file_funcs = funcs_by_file.get(res_file, [])
                            match = next((f for f in res_file_funcs if f["name"] == callee_name and f["class_name"] is None), None)
                            if match:
                                callee_id = match["id"]
                                callee_file = match["file"]
                                break

            # 4. Receiver/associated-function patterns (Go receiver, Rust Type::method())
            if callee_id is None and obj_name is not None and obj_name not in ("self", "this", "cls"):
                # Go receiver method call or Rust associated method (Type::method)
                # Check if obj_name corresponds to a class/receiver type in caller_file
                same_file_funcs = funcs_by_file.get(caller_file, [])
                match = next((f for f in same_file_funcs if f["class_name"] == obj_name and f["name"] == callee_name), None)
                if match:
                    callee_id = match["id"]
                    callee_file = match["file"]
                else:
                    # Check imported files for Type::method
                    caller_res_imports = resolved_imports.get(caller_file, [])
                    for res_file in caller_res_imports:
                        res_file_funcs = funcs_by_file.get(res_file, [])
                        match = next((f for f in res_file_funcs if f["class_name"] == obj_name and f["name"] == callee_name), None)
                        if match:
                            callee_id = match["id"]
                            callee_file = match["file"]
                            break

            # 5. Namespace export matching (duck typing fallback)
            if callee_id is None and obj_name is not None and obj_name not in ("self", "this", "cls"):
                # Find all functions in codebase with name == callee_name
                matches = funcs_by_name.get(callee_name, [])
                if len(matches) == 1:
                    callee_id = matches[0]["id"]
                    callee_file = matches[0]["file"]
                elif len(matches) > 1:
                    is_ambiguous = 1
                    candidates = [m["id"] for m in matches[:5]]

            # Append the resolved call edge
            call_edges.append({
                "caller_id": caller_id,
                "callee_id": callee_id,
                "callee_name": callee_name,
                "callee_file": callee_file,
                "is_ambiguous": is_ambiguous,
                "candidates": json.dumps(candidates) if is_ambiguous else None
            })

    return call_edges
