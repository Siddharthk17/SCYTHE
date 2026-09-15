from tree_sitter import Node, Tree

from ctx_engine.languages.base import (
    FileStructure,
    FunctionRecord,
    ImportStatement,
)

_REQUIRE_METHODS = ("require", "require_relative")
_VISIBILITY_SWITCHES = ("private", "protected", "public")


def _text(node: Node | None) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.decode("utf-8", errors="replace")


def _string_arg(call: Node) -> str | None:
    """Return the raw content of the first string argument to a call, if any."""
    for child in call.children:
        if child.type == "argument_list":
            for arg in child.children:
                if arg.type == "string":
                    for part in arg.children:
                        if part.type == "string_content":
                            return _text(part)
                    return ""
            return None
    return None


def _symbol_names(call: Node) -> list[str]:
    """Collect `:symbol` argument names (without the colon) from a call."""
    names: list[str] = []
    for child in call.children:
        if child.type != "argument_list":
            continue
        for arg in child.children:
            if arg.type == "simple_symbol":
                names.append(_text(arg).lstrip(":"))
    return names


def _extract_ruby_signature(node: Node, source: bytes) -> str:
    """Slice the `def ...` header, stopping before the body.

    The `body` field covers both `body_statement ... end` and endless-method
    (`def foo = expr`) bodies, so slicing to its start keeps modifiers,
    receiver, name, and parameters while excluding the implementation.
    """
    body = node.child_by_field_name("body")
    end = body.start_byte if body is not None else node.end_byte
    text = source[node.start_byte:end].decode("utf-8", errors="replace").strip()
    return text.rstrip("= \t\n").rstrip()


def _find_ruby_mutations(node: Node) -> list[str]:
    """Collect `@ivar`, `@@cvar`, and `$gvar` writes within a method body.

    Blocks (`do...end`, `{...}`) are anonymous, so calls and writes inside
    them belong to the enclosing method — walking the whole method node
    attributes them correctly without extra handling.
    """
    muts: set[str] = set()

    def walk(n: Node) -> None:
        if n.type in ("assignment", "operator_assignment"):
            left = n.child_by_field_name("left")
            if left is not None and left.type in (
                "instance_variable",
                "class_variable",
                "global_variable",
            ):
                var = _text(left)
                if var:
                    muts.add(var)
        for child in n.children:
            walk(child)

    walk(node)
    return sorted(muts)


class _Scope:
    """Per-class/module visibility state while walking one body."""

    def __init__(self, qualified: str | None) -> None:
        self.qualified = qualified
        self.public_by_default = True


class RubyAdapter:
    """Language adapter for Ruby source files (.rb).

    Recognises:
      - `require` / `require_relative` calls (relative ones resolve to repo
        files; bare `require` is treated as external unless it matches a
        file under `lib/` or the repo root)
      - `class` (with superclass), `module`, and `class << self` bodies
      - instance methods (`def foo`), singleton methods (`def self.foo`),
        top-level methods, `alias` / `alias_method` renames
      - `private` / `protected` / `public` visibility, both the bare switch
        form (applies to subsequently defined methods) and the symbol form
        (`private :name`, applied retroactively)

    Function ids follow Ruby convention: `path::Class#method` for instance
    methods, `path::Class.method` for singleton methods, `path::method`
    for top-level methods.
    """

    def extract(self, tree: Tree, source: bytes) -> FileStructure:
        imports_raw: list[ImportStatement] = []
        exports: list[str] = []
        functions: list[FunctionRecord] = []
        class_superclasses: dict[str, str] = {}

        self._collect_requires(tree.root_node, imports_raw)
        self._walk_body(
            tree.root_node, [], None, source,
            imports_raw, exports, functions, class_superclasses,
            in_singleton=False,
        )
        return FileStructure(
            exports=sorted(set(exports)),
            imports_raw=imports_raw,
            functions=functions,
            class_superclasses=class_superclasses,
        )

    def _collect_requires(self, node: Node, imports_raw: list[ImportStatement]) -> None:
        """Collect bare `require` / `require_relative` calls anywhere in the file."""
        if node.type == "call":
            method = node.child_by_field_name("method")
            receiver = node.child_by_field_name("receiver")
            if (
                method is not None
                and receiver is None
                and _text(method) in _REQUIRE_METHODS
            ):
                arg = _string_arg(node)
                if arg:
                    imports_raw.append(
                        ImportStatement(
                            module=arg,
                            level=1 if _text(method) == "require_relative" else 0,
                        )
                    )
        for child in node.children:
            self._collect_requires(child, imports_raw)

    def _walk_body(
        self,
        scope_node: Node,
        stack: list[str],
        scope: _Scope | None,
        source: bytes,
        imports_raw: list[ImportStatement],
        exports: list[str],
        functions: list[FunctionRecord],
        class_superclasses: dict[str, str],
        in_singleton: bool,
    ) -> None:
        for child in scope_node.children:
            if child.type in ("class", "module"):
                self._walk_type(
                    child, stack, source, imports_raw, exports,
                    functions, class_superclasses,
                )
            elif child.type == "singleton_class":
                qualified = "::".join(stack) if stack else None
                body = self._body_of(child)
                if body is not None and qualified is not None:
                    self._walk_body(
                        body, stack, _Scope(qualified), source,
                        imports_raw, exports, functions, class_superclasses,
                        in_singleton=True,
                    )
            elif child.type == "method":
                self._walk_method(
                    child, stack, scope, source, exports, functions,
                    singleton=in_singleton,
                )
            elif child.type == "singleton_method":
                self._walk_method(
                    child, stack, scope, source, exports, functions,
                    singleton=True,
                )
            elif child.type == "alias":
                self._walk_alias(child, stack, exports, functions)
            elif child.type == "call":
                self._walk_visibility_call(child, stack, scope, exports, functions)
            elif child.type == "identifier" and _text(child) in _VISIBILITY_SWITCHES:
                # Bare `private` / `protected` / `public` flips the default
                # visibility for subsequently defined methods.
                if scope is not None:
                    scope.public_by_default = _text(child) == "public"
            elif child.type == "body_statement":
                self._walk_body(
                    child, stack, scope, source, imports_raw, exports,
                    functions, class_superclasses,
                    in_singleton=in_singleton,
                )

    def _body_of(self, node: Node) -> Node | None:
        body = node.child_by_field_name("body")
        if body is not None:
            return body
        for child in node.children:
            if child.type == "body_statement":
                return child
        return None

    def _walk_type(
        self,
        node: Node,
        stack: list[str],
        source: bytes,
        imports_raw: list[ImportStatement],
        exports: list[str],
        functions: list[FunctionRecord],
        class_superclasses: dict[str, str],
    ) -> None:
        name = _text(node.child_by_field_name("name"))
        if not name:
            return
        qualified = "::".join(stack + [name]) if stack else name
        if name not in exports:
            exports.append(name)
        superclass = node.child_by_field_name("superclass")
        if superclass is not None:
            parent = _text(superclass).lstrip("<").strip()
            if parent:
                class_superclasses[qualified] = parent
        body = self._body_of(node)
        if body is not None:
            self._walk_body(
                body, stack + [name], _Scope(qualified), source,
                imports_raw, exports, functions, class_superclasses,
                in_singleton=False,
            )

    def _walk_method(
        self,
        node: Node,
        stack: list[str],
        scope: _Scope | None,
        source: bytes,
        exports: list[str],
        functions: list[FunctionRecord],
        singleton: bool,
    ) -> None:
        name = _text(node.child_by_field_name("name"))
        if not name:
            return
        qualified = "::".join(stack) if stack else None
        exported = scope is None or scope.public_by_default
        separator = "." if singleton or not qualified else "#"
        functions.append(FunctionRecord(
            name=name,
            class_name=qualified,
            signature=_extract_ruby_signature(node, source),
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            node=node,
            body_node=node.child_by_field_name("body"),
            mutates=_find_ruby_mutations(node),
            name_separator=separator,
        ))
        if exported:
            if qualified and not singleton:
                label = name
            elif qualified:
                label = f"{qualified}.{name}"
            else:
                label = name
            if label not in exports:
                exports.append(label)

    def _walk_alias(
        self,
        node: Node,
        stack: list[str],
        exports: list[str],
        functions: list[FunctionRecord],
    ) -> None:
        idents = [_text(c) for c in node.children if c.type == "identifier"]
        if len(idents) < 2:
            return
        new_name, old_name = idents[0], idents[1]
        qualified = "::".join(stack) if stack else None
        functions.append(FunctionRecord(
            name=new_name,
            class_name=qualified,
            signature=f"alias {new_name} {old_name}",
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            node=node,
            body_node=None,
            mutates=[],
        ))
        if new_name not in exports:
            exports.append(new_name)

    def _walk_visibility_call(
        self,
        node: Node,
        stack: list[str],
        scope: _Scope | None,
        exports: list[str],
        functions: list[FunctionRecord],
    ) -> None:
        """Handle `private :name`, `alias_method`, and `private_class_method`."""
        method = _text(node.child_by_field_name("method"))
        if method == "alias_method":
            names = _symbol_names(node)
            if len(names) >= 2:
                new_name, old_name = names[0], names[1]
                qualified = "::".join(stack) if stack else None
                functions.append(FunctionRecord(
                    name=new_name,
                    class_name=qualified,
                    signature=f"alias {new_name} {old_name}",
                    line_start=node.start_point[0] + 1,
                    line_end=node.end_point[0] + 1,
                    node=node,
                    body_node=None,
                    mutates=[],
                ))
                if new_name not in exports:
                    exports.append(new_name)
            return
        if scope is None or method not in (
            "private", "protected", "public", "private_class_method",
            "module_function",
        ):
            return
        for target in _symbol_names(node):
            if method == "private_class_method":
                qualified = scope.qualified or ""
                exports[:] = [
                    e for e in exports if e != f"{qualified}.{target}"
                ]
            elif method in ("private", "protected"):
                exports[:] = [e for e in exports if e != target]
            else:
                if target not in exports:
                    exports.append(target)
