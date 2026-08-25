from tree_sitter import Node, Tree
from ctx_engine.languages.base import (
    FileStructure,
    FunctionRecord,
    ImportStatement,
    extract_signature,
)


def _class_has_public_modifier(node: Node) -> bool:
    """True if a class / interface / enum declaration has a 'public' modifier child."""
    for child in node.children:
        if child.type == "modifiers":
            text = child.text.decode("utf-8") if child.text else ""
            if "public" in text.split():
                return True
    return False


def _method_has_public_modifier(node: Node) -> bool:
    """True if a method or constructor declaration has a 'public' modifier child."""
    for child in node.children:
        if child.type == "modifiers":
            text = child.text.decode("utf-8") if child.text else ""
            if "public" in text.split():
                return True
    return False


def extract_java_imports(node: Node, imports_list: list[ImportStatement]) -> None:
    """Convert an import_declaration to an ImportStatement.

    Java shapes (from the tree-sitter-java grammar):
      import java.util.List;        -> module='java.util.List', names=[]
      import com.example.model.*;   -> module='com.example.model', names=['*']
    """
    if node.type != "import_declaration":
        return

    for child in node.children:
        if child.type in ("scoped_identifier", "identifier"):
            module_path = child.text.decode("utf-8") if child.text else ""
            has_wildcard = any(c.type == "asterisk" for c in node.children)
            names = ["*"] if has_wildcard else []
            imports_list.append(ImportStatement(module=module_path, names=names))
            return


def find_java_mutations(node: Node) -> list[str]:
    """Walk a function body and return the list of mutated fields.

    Captures:
      this.field = value       -> "this.field"
      ClassName.field = value  -> "static:ClassName.field"
    """
    muts: set[str] = set()

    def walk(n: Node) -> None:
        if n.type == "assignment_expression":
            left = n.child_by_field_name("left")
            if left and left.type == "field_access":
                obj = left.child_by_field_name("object")
                field = left.child_by_field_name("field")
                if obj and field and obj.text:
                    obj_text = obj.text.decode("utf-8")
                    field_text = field.text.decode("utf-8")
                    if obj_text == "this":
                        muts.add(f"this.{field_text}")
                    elif obj_text and obj_text[0].isupper():
                        muts.add(f"static:{obj_text}.{field_text}")
        for child in n.children:
            walk(child)

    walk(node)
    return sorted(muts)


def _walk_class_body(
    class_node: Node,
    class_stack: list[str],
    source: bytes,
    imports_raw: list[ImportStatement],
    exports: list[str],
    functions: list[FunctionRecord],
) -> None:
    """Recurse into a class/interface/enum body and extract methods/constructors."""
    body = None
    for child in class_node.children:
        if child.type in ("class_body", "interface_body", "enum_body"):
            body = child
            break

    if body is None:
        return

    class_name_node = class_node.child_by_field_name("name")
    if class_name_node is None:
        return
    class_name = class_name_node.text.decode("utf-8")
    class_stack.append(class_name)

    try:
        for member in body.children:
            if member.type == "method_declaration":
                name_node = member.child_by_field_name("name")
                if name_node is None:
                    continue
                func_name = name_node.text.decode("utf-8")
                sig = extract_signature(member, source)
                line_start = member.start_point[0] + 1
                line_end = member.end_point[0] + 1
                body_node = member.child_by_field_name("body")
                muts = find_java_mutations(member)
                is_public = _method_has_public_modifier(member)

                functions.append(FunctionRecord(
                    name=func_name,
                    class_name=".".join(class_stack),
                    signature=sig,
                    line_start=line_start,
                    line_end=line_end,
                    node=member,
                    body_node=body_node,
                    mutates=muts,
                ))

                if is_public and func_name not in exports:
                    exports.append(func_name)

            elif member.type == "constructor_declaration":
                name_node = member.child_by_field_name("name")
                if name_node is None:
                    continue
                func_name = name_node.text.decode("utf-8")
                sig = extract_signature(member, source)
                line_start = member.start_point[0] + 1
                line_end = member.end_point[0] + 1
                body_node = member.child_by_field_name("body")
                muts = find_java_mutations(member)
                is_public = _method_has_public_modifier(member)

                functions.append(FunctionRecord(
                    name=func_name,
                    class_name=".".join(class_stack),
                    signature=sig,
                    line_start=line_start,
                    line_end=line_end,
                    node=member,
                    body_node=body_node,
                    mutates=muts,
                ))

                if is_public and func_name not in exports:
                    exports.append(func_name)

            elif member.type in ("class_declaration", "interface_declaration", "enum_declaration"):
                # Nested type — recurse, sharing the class stack
                if _class_has_public_modifier(member):
                    nested_name = member.child_by_field_name("name")
                    if nested_name is not None:
                        nested_text = nested_name.text.decode("utf-8")
                        if nested_text not in exports:
                            exports.append(nested_text)
                _walk_class_body(
                    member, class_stack, source,
                    imports_raw, exports, functions,
                )
    finally:
        class_stack.pop()


class JavaAdapter:
    """Language adapter for Java source files.

    Recognises:
      - import declarations (scoped imports and wildcard imports)
      - top-level and nested class / interface / enum declarations
      - public and private methods, constructors
      - this.field mutations and static ClassName.field mutations
    """

    def extract(self, tree: Tree, source: bytes) -> FileStructure:
        imports_raw: list[ImportStatement] = []
        exports: list[str] = []
        functions: list[FunctionRecord] = []
        class_stack: list[str] = []

        for child in tree.root_node.children:
            if child.type == "import_declaration":
                extract_java_imports(child, imports_raw)
                continue

            if child.type in ("class_declaration", "interface_declaration", "enum_declaration"):
                if _class_has_public_modifier(child):
                    name_node = child.child_by_field_name("name")
                    if name_node is not None:
                        class_text = name_node.text.decode("utf-8")
                        if class_text not in exports:
                            exports.append(class_text)
                _walk_class_body(
                    child, class_stack, source,
                    imports_raw, exports, functions,
                )
                continue

        return FileStructure(
            exports=sorted(set(exports)),
            imports_raw=imports_raw,
            functions=functions,
        )
