from tree_sitter import Node, Tree
from ctx_engine.languages.base import (
    FileStructure,
    FunctionRecord,
    ImportStatement,
    extract_signature,
)


# C# visibility modifiers that mark an item as exported.
# 'public' is the default, 'internal' is visible within the assembly and treated
# as exported for single-repo import-graph purposes.
_EXPORTED_MODIFIERS = {"public", "internal"}


def _has_modifier(node: Node, modifier: str) -> bool:
    """True if the node has a child of type 'modifier' containing the given keyword."""
    for child in node.children:
        if child.type == "modifier":
            text = child.text.decode("utf-8") if child.text else ""
            if modifier in text.split():
                return True
    return False


def _is_exported(node: Node) -> bool:
    """True if the node carries any of the exported visibility modifiers."""
    for child in node.children:
        if child.type == "modifier":
            text = child.text.decode("utf-8") if child.text else ""
            if any(m in text.split() for m in _EXPORTED_MODIFIERS):
                return True
    return False


def extract_csharp_namespace(node: Node) -> str | None:
    """Extract the namespace name from a namespace_declaration or file_scoped_namespace_declaration."""
    name = node.child_by_field_name("name")
    if name and name.text:
        return name.text.decode("utf-8")
    return None


def extract_csharp_using(node: Node) -> ImportStatement | None:
    """Convert a using_directive to an ImportStatement.

    C# shapes (from the tree-sitter-c-sharp grammar):
      using System;                          -> module='System', names=[]
      using System.Collections.Generic;     -> module='System.Collections.Generic', names=[]
      using Alias = System.Text;            -> module='System.Text', names=[], alias='Alias'
      global using System.Linq;             -> module='System.Linq', names=[]
    """
    if node.type != "using_directive":
        return None

    module_text = None
    alias_text = None

    for child in node.children:
        if child.type == "alias_qualified_name":
            alias_node = child.child_by_field_name("alias")
            name_node = child.child_by_field_name("name")
            if alias_node and alias_node.text:
                alias_text = alias_node.text.decode("utf-8")
            if name_node and name_node.text:
                module_text = name_node.text.decode("utf-8")
            break
        elif child.type in ("qualified_name", "identifier"):
            if module_text is None and child.text:
                module_text = child.text.decode("utf-8")
            # Check if the next sibling is '=' to detect alias pattern.
            # 'using X = Y' produces children: identifier(X), '=' , qualified_name(Y)
            if child.type == "identifier" and alias_text is None:
                idx = list(node.children).index(child)
                siblings = list(node.children)
                if idx + 2 < len(siblings) and siblings[idx + 1].type == "=":
                    alias_text = child.text.decode("utf-8")
                    # Reset module_text; the qualified_name after = is the real one
                    module_text = None

    if module_text is None:
        return None

    return ImportStatement(module=module_text, names=[], alias=alias_text)


def find_csharp_mutations(node: Node) -> list[str]:
    """Walk a function body and return the list of mutated fields.

    Captures:
      this.field = value       -> "this.field"
      ClassName.field = value  -> "static:ClassName.field"
    """
    muts: set[str] = set()

    def walk(n: Node) -> None:
        if n.type == "assignment_expression":
            left = n.child_by_field_name("left")
            if left and left.type == "member_access_expression":
                obj = left.child_by_field_name("expression")
                field = left.child_by_field_name("name")
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


def _walk_declaration_list(
    decl_list: Node,
    class_stack: list[str],
    source: bytes,
    exports: list[str],
    functions: list[FunctionRecord],
) -> None:
    """Recurse through a declaration_list (class/struct/record body) and extract members."""
    for member in decl_list.children:
        if member.type == "method_declaration":
            name_node = member.child_by_field_name("name")
            if name_node is None:
                continue
            func_name = name_node.text.decode("utf-8")
            sig = extract_signature(member, source)
            line_start = member.start_point[0] + 1
            line_end = member.end_point[0] + 1
            body_node = member.child_by_field_name("body")
            muts = find_csharp_mutations(member)
            is_exported = _is_exported(member)

            functions.append(FunctionRecord(
                name=func_name,
                class_name=".".join(class_stack) if class_stack else None,
                signature=sig,
                line_start=line_start,
                line_end=line_end,
                node=member,
                body_node=body_node,
                mutates=muts,
            ))

            if is_exported and func_name not in exports:
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
            muts = find_csharp_mutations(member)
            is_exported = _is_exported(member)

            functions.append(FunctionRecord(
                name=func_name,
                class_name=".".join(class_stack) if class_stack else None,
                signature=sig,
                line_start=line_start,
                line_end=line_end,
                node=member,
                body_node=body_node,
                mutates=muts,
            ))

            if is_exported and func_name not in exports:
                exports.append(func_name)

        elif member.type == "property_declaration":
            # C# auto-properties are treated as function records for call-graph purposes
            # so the MCP server can locate and summarise them.
            name_node = member.child_by_field_name("name")
            if name_node is None:
                continue
            prop_name = name_node.text.decode("utf-8")
            sig = _extract_property_signature(member, source)
            line_start = member.start_point[0] + 1
            line_end = member.end_point[0] + 1
            is_exported = _is_exported(member)

            functions.append(FunctionRecord(
                name=prop_name,
                class_name=".".join(class_stack) if class_stack else None,
                signature=sig,
                line_start=line_start,
                line_end=line_end,
                node=member,
                body_node=None,
                mutates=[],
            ))

            if is_exported and prop_name not in exports:
                exports.append(prop_name)

        elif member.type in (
            "class_declaration", "struct_declaration", "interface_declaration",
            "record_declaration",
        ):
            if _is_exported(member):
                name_node = member.child_by_field_name("name")
                if name_node is not None:
                    nested_name = name_node.text.decode("utf-8")
                    if nested_name not in exports:
                        exports.append(nested_name)
            nested_name_node = member.child_by_field_name("name")
            if nested_name_node and nested_name_node.text:
                class_stack.append(nested_name_node.text.decode("utf-8"))
            try:
                body = member.child_by_field_name("body")
                if body is None:
                    body = member.child_by_field_name("declaration_list")
                if body is not None:
                    _walk_declaration_list(body, class_stack, source, exports, functions)
            finally:
                if nested_name_node and nested_name_node.text:
                    class_stack.pop()


def _extract_property_signature(node: Node, source: bytes) -> str:
    """Build a readable signature for a property_declaration: '<modifiers> <type> Name { get; set; }'."""
    text = source[node.start_byte:node.end_byte].decode("utf-8").strip()
    if text.endswith(";"):
        text = text[:-1].rstrip()
    return text


class CSharpAdapter:
    """Language adapter for C# source files.

    Recognises:
      - using directives (including alias and global using)
      - namespace_declaration and file_scoped_namespace_declaration
      - classes, structs, interfaces, records (including nested)
      - public and internal methods, constructors, properties
      - this.field mutations and static ClassName.field mutations
    """

    def extract(self, tree: Tree, source: bytes) -> FileStructure:
        imports_raw: list[ImportStatement] = []
        exports: list[str] = []
        functions: list[FunctionRecord] = []
        class_stack: list[str] = []
        namespace: str | None = None

        file_scoped_ns_in_use = False
        for child in tree.root_node.children:
            if child.type == "using_directive":
                imp = extract_csharp_using(child)
                if imp is not None:
                    imports_raw.append(imp)
                continue

            if child.type == "file_scoped_namespace_declaration":
                namespace = extract_csharp_namespace(child)
                if namespace is not None:
                    class_stack.append(namespace)
                    file_scoped_ns_in_use = True
                continue

            if child.type == "namespace_declaration":
                namespace = extract_csharp_namespace(child)
                if namespace is not None:
                    class_stack.append(namespace)
                try:
                    decl = child.child_by_field_name("body")
                    if decl is not None:
                        _walk_declaration_list(decl, class_stack, source, exports, functions)
                finally:
                    if namespace is not None:
                        class_stack.pop()
                continue

            if child.type in (
                "class_declaration", "struct_declaration",
                "interface_declaration", "record_declaration",
            ):
                if _is_exported(child):
                    name_node = child.child_by_field_name("name")
                    if name_node is not None:
                        top_name = name_node.text.decode("utf-8")
                        if top_name not in exports:
                            exports.append(top_name)
                nested_name_node = child.child_by_field_name("name")
                if nested_name_node and nested_name_node.text:
                    class_stack.append(nested_name_node.text.decode("utf-8"))
                try:
                    body = child.child_by_field_name("body")
                    if body is None:
                        body = child.child_by_field_name("declaration_list")
                    if body is not None:
                        _walk_declaration_list(body, class_stack, source, exports, functions)
                finally:
                    if nested_name_node and nested_name_node.text:
                        class_stack.pop()

        # Pop the file-scoped namespace if we pushed one
        if file_scoped_ns_in_use and class_stack:
            class_stack.pop()

        return FileStructure(
            exports=sorted(set(exports)),
            imports_raw=imports_raw,
            functions=functions,
        )
