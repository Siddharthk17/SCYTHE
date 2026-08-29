from tree_sitter import Node, Tree

from ctx_engine.languages.base import (
    FileStructure,
    FunctionRecord,
    ImportStatement,
)


def _modifier_tokens(node: Node) -> set[str]:
    """Collect visibility/other modifier tokens from the declaration's modifiers children.

    The tree-sitter-kotlin grammar represents modifiers either as a single
    `modifiers` wrapper node or as direct `modifier`-style children
    (visibility_modifier, suspend_modifier, ...). Tokenizing the raw text of
    those children covers both shapes without depending on grammar version
    internals.
    """
    tokens: set[str] = set()
    for child in node.children:
        if child.type in ("modifiers", "visibility_modifier", "suspend_modifier", "modifier"):
            text = child.text.decode("utf-8") if child.text else ""
            tokens.update(text.split())
    return tokens


def _visibility(node: Node) -> str:
    """Return the Kotlin visibility of a declaration: public, internal, private, or protected.

    Kotlin's default visibility is public (explicit or implicit).
    """
    tokens = _modifier_tokens(node)
    for vis in ("public", "internal", "private", "protected"):
        if vis in tokens:
            return vis
    return "public"


def _is_publicly_visible(node: Node) -> bool:
    """True for public (explicit or implicit) and internal declarations."""
    return _visibility(node) in ("public", "internal")


def _extract_kotlin_signature(node: Node, source: bytes) -> str:
    """Extract a Kotlin function signature by slicing up to the function_body.

    Kotlin function bodies are a dedicated `function_body` child node (starting
    at `{` or `=`), not a named `body` field, so the shared base helper cannot
    find them. The slice from node start to body start includes modifiers
    (e.g. `suspend`), the receiver type for extension functions, the parameter
    list, and the return type — and excludes the body itself.
    """
    end = node.end_byte
    for child in node.children:
        if child.type == "function_body":
            end = child.start_byte
            break
    text = source[node.start_byte:end].decode("utf-8").strip()
    # The slice ends just before `{` or `=`; strip any stray separators or
    # dangling whitespace left at the boundary.
    return text.rstrip("{=: \t\n").rstrip()


def _find_kotlin_mutations(node: Node, in_singleton: bool) -> list[str]:
    """Walk a function body and return the list of mutated fields.

    Captures:
      this.field = value       -> "this.field"
      X.field = value inside   -> "companion:field" (companion object / object
      a companion or object       singleton bodies)
      ClassName.field = value  -> "static:ClassName.field"
    """
    muts: set[str] = set()

    def walk(n: Node) -> None:
        if n.type == "assignment":
            left = n.child_by_field_name("left")
            if left and left.type == "navigation_expression" and left.children:
                first = left.children[0]
                field_node = left.children[-1]
                if first.type == "this_expression" and field_node.type == "identifier":
                    muts.add(f"this.{field_node.text.decode('utf-8')}")
                elif field_node.type == "identifier" and in_singleton:
                    muts.add(f"companion:{field_node.text.decode('utf-8')}")
                elif field_node.type == "identifier" and first.type == "identifier":
                    first_text = first.text.decode("utf-8")
                    if first_text[0:1].isupper():
                        muts.add(f"static:{first_text}.{field_node.text.decode('utf-8')}")
        for child in n.children:
            walk(child)

    walk(node)
    return sorted(muts)


def _extract_kotlin_imports(node: Node, imports_list: list[ImportStatement]) -> None:
    """Convert an `import` node to an ImportStatement.

    Kotlin shapes (from tree-sitter-kotlin):
      import com.example.Foo      -> module='com.example.Foo', names=[]
      import com.example.model.*  -> module='com.example.model', names=['*']
      import com.example.Foo as B -> module='com.example.Foo', alias='B'
    """
    module_path = ""
    has_wildcard = False
    alias: str | None = None
    pending_as = False
    for child in node.children:
        if child.type == "qualified_identifier":
            module_path = child.text.decode("utf-8") if child.text else ""
        elif child.type == "*":
            has_wildcard = True
        elif child.type == "as":
            # Grammar shape A: `as` keyword followed by an identifier sibling.
            pending_as = True
        elif child.type == "import_alias":
            # Grammar shape B: a dedicated import_alias node.
            alias_node = child.child_by_field_name("name")
            if alias_node is None and child.children:
                alias_node = child.children[-1]
            if alias_node and alias_node.text:
                alias = alias_node.text.decode("utf-8")
        elif child.type == "identifier" and pending_as:
            alias = child.text.decode("utf-8")
            pending_as = False
    if not module_path:
        return
    imports_list.append(
        ImportStatement(
            module=module_path,
            names=["*"] if has_wildcard else [],
            alias=alias,
        )
    )


def _class_like_name(node: Node) -> str | None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    return name_node.text.decode("utf-8") if name_node.text else None


def _walk_kotlin_scope(
    scope_node: Node,
    class_stack: list[str],
    source: bytes,
    imports_raw: list[ImportStatement],
    exports: list[str],
    functions: list[FunctionRecord],
    in_singleton: bool,
) -> None:
    """Recurse into a class body / companion object / file scope collecting functions.

    in_singleton is True inside companion_object and object_declaration bodies,
    where static-style assignments are recorded as companion mutations.
    """
    for member in scope_node.children:
        if member.type == "class_body":
            # Members of class/object/companion declarations live in a
            # class_body child — descend into it with the same scoping.
            _walk_kotlin_scope(
                member, class_stack, source,
                imports_raw, exports, functions, in_singleton,
            )
        elif member.type == "function_declaration":
            # Private and protected functions are compiler-scoped details:
            # they are neither exported nor recorded (Kotlin only — the other
            # adapters keep all functions and filter exports separately).
            if _visibility(member) in ("private", "protected"):
                continue
            name_node = member.child_by_field_name("name")
            if name_node is None:
                continue
            func_name = name_node.text.decode("utf-8")

            body_node = None
            for child in member.children:
                if child.type == "function_body":
                    body_node = child
                    break

            functions.append(FunctionRecord(
                name=func_name,
                class_name=".".join(class_stack) if class_stack else None,
                signature=_extract_kotlin_signature(member, source),
                line_start=member.start_point[0] + 1,
                line_end=member.end_point[0] + 1,
                node=member,
                body_node=body_node,
                mutates=(
                    _find_kotlin_mutations(member, in_singleton)
                    if body_node is not None
                    else []
                ),
            ))

            if _is_publicly_visible(member) and func_name not in exports:
                exports.append(func_name)

        elif member.type == "companion_object":
            # Companion object: a nested singleton whose members are qualified
            # as Outer.Companion.<member>.
            _walk_kotlin_scope(
                member, class_stack + ["Companion"], source,
                imports_raw, exports, functions, in_singleton=True,
            )

        elif member.type in ("class_declaration", "object_declaration"):
            nested_name = _class_like_name(member)
            nested_stack = class_stack + ([nested_name] if nested_name else [])
            if nested_name and _is_publicly_visible(member) and nested_name not in exports:
                exports.append(nested_name)
            _walk_kotlin_scope(
                member, nested_stack, source,
                imports_raw, exports, functions,
                in_singleton=(member.type == "object_declaration"),
            )


class KotlinAdapter:
    """Language adapter for Kotlin source files (.kt and .kts).

    Recognises:
      - import declarations (qualified, wildcard, aliased)
      - class / interface / object / data class / sealed class declarations
      - companion objects (qualified as ClassName.Companion)
      - top-level functions and extension functions (receiver type preserved
        in the signature)
      - suspend modifiers (naturally included in the signature slice)
      - this.field mutations and companion/static singleton mutations

    Visibility: public (explicit or default) and internal declarations are
    exported; private and protected are not.
    """

    def extract(self, tree: Tree, source: bytes) -> FileStructure:
        imports_raw: list[ImportStatement] = []
        exports: list[str] = []
        functions: list[FunctionRecord] = []

        for child in tree.root_node.children:
            if child.type == "import":
                _extract_kotlin_imports(child, imports_raw)

        # One walk over the file scope handles top-level functions (including
        # extension functions — the receiver type is part of the signature
        # slice, and the name field is the function name after the `.`),
        # classes, objects, and their nested scopes.
        _walk_kotlin_scope(
            tree.root_node, [], source,
            imports_raw, exports, functions, in_singleton=False,
        )

        return FileStructure(
            exports=sorted(set(exports)),
            imports_raw=imports_raw,
            functions=functions,
        )