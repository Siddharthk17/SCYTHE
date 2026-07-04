from tree_sitter import Node, Tree
from ctx_engine.languages.base import FileStructure, FunctionRecord, ImportStatement, extract_signature

def process_go_imports(node: Node, imports_list: list[ImportStatement]) -> None:
    def process_spec(spec: Node):
        path_node = spec.child_by_field_name("path")
        if path_node:
            path_val = path_node.text.decode("utf-8").strip("\"")
            alias_node = spec.child_by_field_name("name")
            alias_val = alias_node.text.decode("utf-8") if alias_node else None
            imports_list.append(ImportStatement(
                module=path_val,
                alias=alias_val
            ))

    if node.type == "import_declaration":
        for child in node.children:
            if child.type == "import_spec":
                process_spec(child)
            elif child.type == "import_spec_list":
                for spec in child.children:
                    if spec.type == "import_spec":
                        process_spec(spec)

def extract_go_exports_from_node(node: Node, exports_list: list[str]) -> None:
    if node.type == "function_declaration":
        name_node = node.child_by_field_name("name")
        if name_node:
            name_str = name_node.text.decode("utf-8")
            if name_str and name_str[0].isupper():
                exports_list.append(name_str)
    elif node.type == "type_declaration":
        for spec in node.children:
            if spec.type == "type_spec":
                name_node = spec.child_by_field_name("name")
                if name_node:
                    name_str = name_node.text.decode("utf-8")
                    if name_str and name_str[0].isupper():
                        exports_list.append(name_str)
    elif node.type in ("const_declaration", "var_declaration"):
        for spec in node.children:
            if spec.type in ("const_spec", "var_spec", "value_spec"):
                for child in spec.children:
                    if child.type == "identifier":
                        name_str = child.text.decode("utf-8")
                        if name_str and name_str[0].isupper():
                            exports_list.append(name_str)

def find_go_mutations(node: Node, receiver_name: str) -> list[str]:
    muts = []
    if not receiver_name:
        return []
    
    def walk_lhs(n: Node):
        if n.type == "selector_expression":
            operand = n.child_by_field_name("operand")
            field = n.child_by_field_name("field")
            if operand and field and operand.text.decode("utf-8") == receiver_name:
                muts.append(f"{receiver_name}.{field.text.decode('utf-8')}")
        for child in n.children:
            walk_lhs(child)
            
    def walk_body(n: Node):
        if n.type == "assignment_statement":
            left = n.child_by_field_name("left")
            if left:
                walk_lhs(left)
        for child in n.children:
            walk_body(child)
            
    walk_body(node)
    return sorted(list(set(muts)))

class GoAdapter:
    """Language adapter for Go files."""

    def extract(self, tree: Tree, source: bytes) -> FileStructure:
        imports_raw: list[ImportStatement] = []
        exports: list[str] = []
        functions: list[FunctionRecord] = []

        # Find receiver identifier helper
        def find_receiver_name(receiver_node: Node) -> str | None:
            def find_ident(n: Node):
                if n.type == "parameter_declaration":
                    for child in n.children:
                        if child.type == "identifier":
                            return child.text.decode("utf-8")
                for child in n.children:
                    res = find_ident(child)
                    if res:
                        return res
                return None
            return find_ident(receiver_node)

        # Find receiver type identifier helper
        def find_receiver_type(receiver_node: Node) -> str | None:
            def find_type_ident(n: Node):
                if n.type == "type_identifier":
                    return n.text.decode("utf-8")
                for child in n.children:
                    res = find_type_ident(child)
                    if res:
                        return res
                return None
            return find_type_ident(receiver_node)

        for child in tree.root_node.children:
            # Extract package level exports
            extract_go_exports_from_node(child, exports)

            # Extract package level imports
            if child.type == "import_declaration":
                process_go_imports(child, imports_raw)

            # Extract functions and methods
            elif child.type == "function_declaration":
                name_node = child.child_by_field_name("name")
                if name_node:
                    func_name = name_node.text.decode("utf-8")
                    sig = extract_signature(child, source)
                    line_start = child.start_point[0] + 1
                    line_end = child.end_point[0] + 1
                    body_node = child.child_by_field_name("body")
                    
                    functions.append(FunctionRecord(
                        name=func_name,
                        class_name=None,
                        signature=sig,
                        line_start=line_start,
                        line_end=line_end,
                        node=child,
                        body_node=body_node,
                        mutates=[]
                    ))

            elif child.type == "method_declaration":
                name_node = child.child_by_field_name("name")
                receiver_node = child.child_by_field_name("receiver")
                if name_node and receiver_node:
                    func_name = name_node.text.decode("utf-8")
                    
                    receiver_name = find_receiver_name(receiver_node)
                    class_name = find_receiver_type(receiver_node)
                    if class_name:
                        class_name = class_name.lstrip("*")

                    sig = extract_signature(child, source)
                    line_start = child.start_point[0] + 1
                    line_end = child.end_point[0] + 1
                    body_node = child.child_by_field_name("body")
                    
                    muts = find_go_mutations(child, receiver_name) if receiver_name else []

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

        return FileStructure(
            exports=sorted(list(set(exports))),
            imports_raw=imports_raw,
            functions=functions
        )
