from tree_sitter import Node, Tree
from ctx_engine.languages.base import FileStructure, FunctionRecord, ImportStatement, extract_signature

def process_js_import(node: Node) -> ImportStatement | None:
    source_node = node.child_by_field_name("source")
    if not source_node:
        return None
    source_val = source_node.text.decode("utf-8").strip("\"'")
    
    names = []
    # Find import_clause by type instead of field name as it doesn't have a field name in the JS grammar
    clause = next((c for c in node.children if c.type == "import_clause"), None)
    if clause:
        for child in clause.children:
            if child.type == "identifier":
                names.append(child.text.decode("utf-8"))
            elif child.type == "named_imports":
                for spec in child.children:
                    if spec.type == "import_specifier":
                        name_node = spec.child_by_field_name("name") or spec.child_by_field_name("value")
                        if not name_node and spec.child_count > 0:
                            name_node = spec.children[0]
                        if name_node:
                            names.append(name_node.text.decode("utf-8"))
            elif child.type == "namespace_import":
                names.append("*")
    return ImportStatement(module=source_val, names=names)

def extract_js_exports(node: Node, exports_list: list[str]) -> None:
    if "default" in node.text.decode("utf-8"):
        exports_list.append("default")
        return
        
    for child in node.children:
        if child.type in ("function_declaration", "class_declaration"):
            name_node = child.child_by_field_name("name")
            if name_node:
                exports_list.append(name_node.text.decode("utf-8"))
        elif child.type in ("lexical_declaration", "variable_declaration"):
            for desc in child.children:
                if desc.type == "variable_declarator":
                    name_node = desc.child_by_field_name("name")
                    if name_node and name_node.type == "identifier":
                        exports_list.append(name_node.text.decode("utf-8"))
        elif child.type == "export_clause":
            for spec in child.children:
                if spec.type == "export_specifier":
                    name_node = spec.child_by_field_name("name") or spec.child_by_field_name("value")
                    if not name_node and spec.child_count > 0:
                        name_node = spec.children[0]
                    if name_node:
                        exports_list.append(name_node.text.decode("utf-8"))

def find_js_mutations(node: Node) -> list[str]:
    muts = []
    def walk(n: Node):
        if n.type == "assignment_expression":
            left = n.child_by_field_name("left")
            if left and left.type == "member_expression":
                obj = left.child_by_field_name("object")
                prop = left.child_by_field_name("property")
                if obj and prop and obj.text.decode("utf-8") == "this":
                    muts.append(f"this.{prop.text.decode('utf-8')}")
        for child in n.children:
            walk(child)
    walk(node)
    return sorted(list(set(muts)))

def walk_js_tree(
    node: Node,
    source: bytes,
    imports_raw: list[ImportStatement],
    exports: list[str],
    functions: list[FunctionRecord],
    class_stack: list[str],
    class_superclasses: dict[str, str],
    inside_class_depth: int = 0
) -> None:
    if node.type == "import_statement":
        imp = process_js_import(node)
        if imp:
            imports_raw.append(imp)
        return

    if node.type == "export_statement":
        extract_js_exports(node, exports)

    if node.type == "class_declaration":
        name_node = node.child_by_field_name("name")
        if name_node:
            class_name = name_node.text.decode("utf-8")
            
            heritage = node.child_by_field_name("heritage") or next((c for c in node.children if c.type == "class_heritage"), None)
            if heritage:
                parent_class_node = None
                for c in heritage.children:
                    if c.type in ("identifier", "member_expression"):
                        parent_class_node = c
                        break
                if parent_class_node:
                    class_superclasses[".".join(class_stack + [class_name])] = parent_class_node.text.decode("utf-8")

            class_stack.append(class_name)
            body = node.child_by_field_name("body")
            if body:
                for child in body.children:
                    walk_js_tree(child, source, imports_raw, exports, functions, class_stack, class_superclasses, inside_class_depth + 1)
            class_stack.pop()
        return

    if node.type == "function_declaration":
        name_node = node.child_by_field_name("name")
        func_name = name_node.text.decode("utf-8") if name_node else "default"
        sig = extract_signature(node, source)
        line_start = node.start_point[0] + 1
        line_end = node.end_point[0] + 1
        body_node = node.child_by_field_name("body")
        muts = find_js_mutations(node)
        functions.append(FunctionRecord(
            name=func_name,
            class_name=".".join(class_stack) if class_stack else None,
            signature=sig,
            line_start=line_start,
            line_end=line_end,
            node=node,
            body_node=body_node,
            mutates=muts
        ))
        return

    if node.type == "method_definition" and len(class_stack) > 0:
        name_node = node.child_by_field_name("name")
        if name_node:
            func_name = name_node.text.decode("utf-8")
            sig = extract_signature(node, source)
            line_start = node.start_point[0] + 1
            line_end = node.end_point[0] + 1
            body_node = node.child_by_field_name("body")
            muts = find_js_mutations(node)
            functions.append(FunctionRecord(
                name=func_name,
                class_name=".".join(class_stack),
                signature=sig,
                line_start=line_start,
                line_end=line_end,
                node=node,
                body_node=body_node,
                mutates=muts
            ))
        return

    if node.type == "variable_declarator":
        val_node = node.child_by_field_name("value")
        if val_node and val_node.type in ("arrow_function", "function_expression"):
            name_node = node.child_by_field_name("name")
            if name_node and name_node.type == "identifier":
                func_name = name_node.text.decode("utf-8")
                sig = extract_signature(val_node, source)
                line_start = node.start_point[0] + 1
                line_end = node.end_point[0] + 1
                body_node = val_node.child_by_field_name("body")
                muts = find_js_mutations(val_node)
                functions.append(FunctionRecord(
                    name=func_name,
                    class_name=".".join(class_stack) if class_stack else None,
                    signature=sig,
                    line_start=line_start,
                    line_end=line_end,
                    node=node,
                    body_node=body_node,
                    mutates=muts
                ))
        return

    for child in node.children:
        walk_js_tree(child, source, imports_raw, exports, functions, class_stack, class_superclasses, inside_class_depth)

class JavaScriptAdapter:
    """Language adapter for JavaScript files."""

    def extract(self, tree: Tree, source: bytes) -> FileStructure:
        imports_raw: list[ImportStatement] = []
        exports: list[str] = []
        functions: list[FunctionRecord] = []
        class_stack: list[str] = []
        class_superclasses: dict[str, str] = {}

        walk_js_tree(tree.root_node, source, imports_raw, exports, functions, class_stack, class_superclasses)

        return FileStructure(
            exports=sorted(list(set(exports))),
            imports_raw=imports_raw,
            functions=functions,
            class_superclasses=class_superclasses
        )
