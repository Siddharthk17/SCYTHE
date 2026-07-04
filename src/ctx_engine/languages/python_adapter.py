from tree_sitter import Node, Tree
from ctx_engine.languages.base import FileStructure, FunctionRecord, ImportStatement, extract_signature

class PythonAdapter:
    """Language adapter for Python files."""

    def extract(self, tree: Tree, source: bytes) -> FileStructure:
        imports_raw: list[ImportStatement] = []
        functions: list[FunctionRecord] = []
        top_level_defs: list[str] = []
        all_override: list[str] | None = None
        class_superclasses: dict[str, str] = {}

        class_stack: list[str] = []

        def strip_quotes(text: str) -> str:
            if len(text) >= 6 and ((text.startswith("'''") and text.endswith("'''")) or (text.startswith('"""') and text.endswith('"""'))):
                return text[3:-3]
            if len(text) >= 2 and ((text.startswith("'") and text.endswith("'")) or (text.startswith('"') and text.endswith('"'))):
                return text[1:-1]
            return text

        def extract_all_assignment(node: Node) -> list[str] | None:
            left = node.child_by_field_name("left") or node.child_by_field_name("target")
            if not left and node.child_count >= 2:
                left = node.children[0]
            if left and left.text.decode("utf-8") == "__all__":
                right = node.child_by_field_name("right") or node.child_by_field_name("value")
                if not right and node.child_count >= 3:
                    right = node.children[-1]
                if right:
                    strings = []
                    def walk(n: Node):
                        if n.type == "string":
                            strings.append(strip_quotes(n.text.decode("utf-8")))
                        for child in n.children:
                            walk(child)
                    walk(right)
                    return strings
            return None

        def find_mutations(node: Node) -> list[str]:
            muts: list[str] = []

            def walk_lhs(lhs: Node):
                if lhs.type == "attribute":
                    obj = lhs.child_by_field_name("object")
                    attr = lhs.child_by_field_name("attribute")
                    if obj and attr:
                        obj_text = obj.text.decode("utf-8")
                        attr_text = attr.text.decode("utf-8")
                        if obj_text in ("self", "cls"):
                            muts.append(f"{obj_text}.{attr_text}")
                elif lhs.type in ("expression_list", "pattern_list", "tuple", "list"):
                    for child in lhs.children:
                        walk_lhs(child)

            def walk_body(n: Node):
                if n.type == "assignment":
                    left = n.child_by_field_name("left") or n.child_by_field_name("target")
                    if not left and n.child_count >= 2:
                        left = n.children[0]
                    if left:
                        walk_lhs(left)
                elif n.type in ("global_statement", "nonlocal_statement"):
                    for child in n.children:
                        if child.type == "identifier":
                            muts.append(f"global:{child.text.decode('utf-8')}")
                for child in n.children:
                    walk_body(child)

            walk_body(node)
            return sorted(list(set(muts)))

        def process_import(node: Node):
            if node.type == "import_statement":
                for child in node.children:
                    if child.type == "dotted_name":
                        imports_raw.append(ImportStatement(module=child.text.decode("utf-8")))
                    elif child.type == "aliased_import":
                        name_node = child.child_by_field_name("name")
                        alias_node = child.child_by_field_name("alias")
                        if name_node and alias_node:
                            imports_raw.append(ImportStatement(
                                module=name_node.text.decode("utf-8"),
                                alias=alias_node.text.decode("utf-8")
                            ))
            elif node.type == "import_from_statement":
                level = 0
                module_name = ""
                from_alias: str | None = None
                
                for child in node.children:
                    if child.type == "import":
                        break
                    
                    if child.type == "relative_import":
                        level += child.text.decode("utf-8").count(".")
                        for subchild in child.children:
                            if subchild.type == "dotted_name":
                                module_name = subchild.text.decode("utf-8")
                    elif child.type in ("import_prefix", "dots"):
                        level += child.text.decode("utf-8").count(".")
                    elif child.type == ".":
                        level += 1
                    elif child.type == "dotted_name":
                        module_name = child.text.decode("utf-8")

                names = []
                def collect_names(n: Node):
                    nonlocal from_alias
                    if n.type == "dotted_name":
                        names.append(n.text.decode("utf-8"))
                    elif n.type == "aliased_import":
                        name_node = n.child_by_field_name("name")
                        alias_node = n.child_by_field_name("alias")
                        if name_node:
                            names.append(name_node.text.decode("utf-8"))
                        if alias_node:
                            from_alias = alias_node.text.decode("utf-8")
                    elif n.type == "wildcard_import" or n.text.decode("utf-8") == "*":
                        names.append("*")
                    for child_node in n.children:
                        if child_node.type not in ("import", "dotted_name", "aliased_import", "relative_import"):
                            collect_names(child_node)

                import_found = False
                for child in node.children:
                    if import_found:
                        collect_names(child)
                    elif child.type == "import":
                        import_found = True

                imports_raw.append(ImportStatement(
                    module=module_name,
                    names=names,
                    level=level,
                    alias=from_alias
                ))

        def walk_tree(node: Node, inside_class_depth: int = 0):
            nonlocal all_override
            
            actual_node = node
            if node.type == "decorated_definition":
                for child in node.children:
                    if child.type in ("function_definition", "class_definition"):
                        actual_node = child
                        break

            if actual_node.type == "class_definition":
                name_node = actual_node.child_by_field_name("name")
                if name_node:
                    class_name = name_node.text.decode("utf-8")
                    if inside_class_depth == 0:
                        top_level_defs.append(class_name)
                    
                    superclasses_node = actual_node.child_by_field_name("superclasses")
                    if superclasses_node:
                        for child in superclasses_node.children:
                            if child.type == "identifier":
                                class_superclasses[".".join(class_stack + [class_name])] = child.text.decode("utf-8")
                                break
                    
                    class_stack.append(class_name)
                    body = actual_node.child_by_field_name("body")
                    if body and inside_class_depth < 2:
                        for child in body.children:
                            walk_tree(child, inside_class_depth + 1)
                    class_stack.pop()

            elif actual_node.type == "function_definition":
                name_node = actual_node.child_by_field_name("name")
                if name_node:
                    func_name = name_node.text.decode("utf-8")
                    if inside_class_depth == 0:
                        top_level_defs.append(func_name)
                    
                    sig = extract_signature(actual_node, source)
                    line_start = actual_node.start_point[0] + 1
                    line_end = actual_node.end_point[0] + 1
                    body_node = actual_node.child_by_field_name("body")
                    muts = find_mutations(actual_node)
                    
                    functions.append(FunctionRecord(
                        name=func_name,
                        class_name=".".join(class_stack) if class_stack else None,
                        signature=sig,
                        line_start=line_start,
                        line_end=line_end,
                        node=actual_node,
                        body_node=body_node,
                        mutates=muts
                    ))

            elif actual_node.type in ("import_statement", "import_from_statement"):
                process_import(actual_node)

            elif actual_node.type == "assignment" and inside_class_depth == 0:
                maybe_all = extract_all_assignment(actual_node)
                if maybe_all is not None:
                    all_override = maybe_all

            elif inside_class_depth == 0:
                for child in actual_node.children:
                    walk_tree(child, 0)

        for child in tree.root_node.children:
            walk_tree(child, 0)

        if all_override is not None:
            exports = all_override
        else:
            exports = [name for name in top_level_defs if not name.startswith("_")]

        return FileStructure(
            exports=exports,
            imports_raw=imports_raw,
            functions=functions,
            class_superclasses=class_superclasses
        )
