from tree_sitter import Node, Tree
from ctx_engine.languages.base import FileStructure, FunctionRecord, ImportStatement, extract_signature

def extract_rust_imports(node: Node, imports_list: list[ImportStatement]) -> None:
    if node.type == "use_declaration":
        arg = node.child_by_field_name("argument")
        if arg:
            def walk(n: Node, prefix: str):
                if n.type == "scoped_identifier":
                    path_str = n.text.decode("utf-8")
                    full = f"{prefix}::{path_str}" if prefix else path_str
                    if "::" in full:
                        mod, name = full.rsplit("::", 1)
                        imports_list.append(ImportStatement(module=mod, names=[name]))
                    else:
                        imports_list.append(ImportStatement(module=full, names=[]))
                elif n.type == "scoped_use_list":
                    path_node = n.child_by_field_name("path")
                    list_node = n.child_by_field_name("list")
                    if path_node and list_node:
                        path_str = path_node.text.decode("utf-8")
                        new_prefix = f"{prefix}::{path_str}" if prefix else path_str
                        for child in list_node.children:
                            if child.type in ("identifier", "use_as_clause", "scoped_identifier", "scoped_use_list", "self"):
                                walk(child, new_prefix)
                elif n.type == "use_as_clause":
                    path_node = n.child_by_field_name("path")
                    alias_node = n.child_by_field_name("alias")
                    path_str = path_node.text.decode("utf-8") if path_node else n.children[0].text.decode("utf-8")
                    alias_str = alias_node.text.decode("utf-8") if alias_node else None
                    full = f"{prefix}::{path_str}" if prefix else path_str
                    if "::" in full:
                        mod, name = full.rsplit("::", 1)
                        imports_list.append(ImportStatement(module=mod, names=[name], alias=alias_str))
                    else:
                        imports_list.append(ImportStatement(module=full, names=[], alias=alias_str))
                elif n.type == "wildcard_import" or n.text.decode("utf-8") == "*":
                    imports_list.append(ImportStatement(module=prefix, names=["*"]))
                elif n.type in ("identifier", "self"):
                    val = n.text.decode("utf-8")
                    full = f"{prefix}::{val}" if prefix else val
                    if val == "self":
                        imports_list.append(ImportStatement(module=prefix, names=[]))
                    elif "::" in full:
                        mod, name = full.rsplit("::", 1)
                        imports_list.append(ImportStatement(module=mod, names=[name]))
                    else:
                        imports_list.append(ImportStatement(module=full, names=[]))
            
            walk(arg, "")

def extract_rust_exports(node: Node, exports_list: list[str]) -> None:
    has_pub = False
    for child in node.children:
        if child.type == "visibility_modifier":
            if child.text.decode("utf-8").startswith("pub"):
                has_pub = True
                break
    if has_pub:
        name_node = node.child_by_field_name("name")
        if name_node:
            exports_list.append(name_node.text.decode("utf-8"))

def find_rust_mutations(node: Node) -> list[str]:
    muts = []
    def walk(n: Node):
        if n.type == "assignment_expression":
            left = n.child_by_field_name("left")
            if left and left.type == "field_expression":
                val = left.child_by_field_name("value")
                field = left.child_by_field_name("field")
                if val and field and val.text.decode("utf-8") == "self":
                    muts.append(f"self.{field.text.decode('utf-8')}")
        for child in n.children:
            walk(child)
    walk(node)
    return sorted(list(set(muts)))

class RustAdapter:
    """Language adapter for Rust files."""

    def extract(self, tree: Tree, source: bytes) -> FileStructure:
        imports_raw: list[ImportStatement] = []
        exports: list[str] = []
        functions: list[FunctionRecord] = []

        def walk_tree(node: Node, inside_impl_type: str | None = None):
            # Parse imports
            if node.type == "use_declaration":
                extract_rust_imports(node, imports_raw)
                return

            # Parse exports at the top level
            if inside_impl_type is None:
                extract_rust_exports(node, exports)

            # Parse top level functions
            if node.type == "function_item" and inside_impl_type is None:
                name_node = node.child_by_field_name("name")
                if name_node:
                    func_name = name_node.text.decode("utf-8")
                    sig = extract_signature(node, source)
                    line_start = node.start_point[0] + 1
                    line_end = node.end_point[0] + 1
                    body_node = node.child_by_field_name("body")
                    
                    functions.append(FunctionRecord(
                        name=func_name,
                        class_name=None,
                        signature=sig,
                        line_start=line_start,
                        line_end=line_end,
                        node=node,
                        body_node=body_node,
                        mutates=[]
                    ))
                return

            # Parse impl blocks
            if node.type == "impl_item":
                impl_type_node = node.child_by_field_name("type")
                if impl_type_node:
                    raw_type = impl_type_node.text.decode("utf-8")
                    if "<" in raw_type:
                        class_name = raw_type.split("<", 1)[0].strip()
                    else:
                        class_name = raw_type.strip()
                    
                    # Recurse into impl declarations list
                    body = node.child_by_field_name("body")
                    if body:
                        for child in body.children:
                            if child.type == "function_item":
                                name_node = child.child_by_field_name("name")
                                if name_node:
                                    func_name = name_node.text.decode("utf-8")
                                    sig = extract_signature(child, source)
                                    line_start = child.start_point[0] + 1
                                    line_end = child.end_point[0] + 1
                                    body_node = child.child_by_field_name("body")
                                    muts = find_rust_mutations(child)
                                    
                                    functions.append(FunctionRecord(
                                        name=func_name,
                                        class_name=class_name,
                                        signature=sig,
                                        line_start=line_start,
                                        line_end=line_end,
                                        node=child,
                                        body_node=body_node,
                                        mutates=muts
                                    ))
                return

            # Top level module recursions
            if inside_impl_type is None:
                for child in node.children:
                    walk_tree(child, None)

        walk_tree(tree.root_node, None)

        return FileStructure(
            exports=sorted(list(set(exports))),
            imports_raw=imports_raw,
            functions=functions
        )
