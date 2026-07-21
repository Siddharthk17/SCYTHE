import codecs
import hashlib
import json
from tree_sitter import Node, Tree

def unescape_string(text: str) -> str:
    """Unescape standard backslash escape sequences in a string literal."""
    try:
        raw = text.encode("utf-8")
        return codecs.escape_decode(raw)[0].decode("utf-8")
    except (LookupError, ValueError, UnicodeDecodeError, TypeError):
        return text

def normalize_string_literal(text: str) -> str:
    """Strip quote prefixes/boundaries and re-escape string contents to a canonical format.
    Returns a valid JSON-encoded string literal.
    """
    is_raw = False
    s = text

    # Rust raw strings: r"...", r#"..."#
    if s.startswith('r"') or s.startswith('r#'):
        is_raw = True
        first_quote = s.find('"')
        last_quote = s.rfind('"')
        if first_quote != -1 and last_quote != -1 and last_quote > first_quote:
            s = s[first_quote + 1:last_quote]
    else:
        # Python raw strings prefix check (e.g. r"...", rf"...")
        prefix = ""
        quote_start = 0
        for i, char in enumerate(s[:5]):
            if char in ("'", '"', '`'):
                quote_start = i
                break
            prefix += char
        if "r" in prefix.lower():
            is_raw = True

        s = s[quote_start:]

        # Strip outer quotes
        if s.startswith('"""') and s.endswith('"""'):
            s = s[3:-3]
        elif s.startswith("'''") and s.endswith("'''"):
            s = s[3:-3]
        elif s.startswith('"') and s.endswith('"'):
            s = s[1:-1]
        elif s.startswith("'") and s.endswith("'"):
            s = s[1:-1]
        elif s.startswith('`') and s.endswith('`'):
            s = s[1:-1]

    if not is_raw:
        s = unescape_string(s)

    return json.dumps(s)

def is_docstring_node(node: Node, language: str) -> bool:
    """Check if the node is a Python docstring statement."""
    if language != "python":
        return False

    if node.type != "expression_statement":
        return False

    if node.child_count != 1:
        return False

    child = node.children[0]
    if child.type != "string":
        return False

    parent = node.parent
    if not parent:
        return False

    # Check if first non-comment statement in module
    if parent.type == "module":
        for sibling in parent.children:
            if sibling.type == "comment":
                continue
            return sibling.id == node.id

    # Check if first non-comment statement in class or function body block
    elif parent.type == "block":
        grandparent = parent.parent
        if grandparent and grandparent.type in ("class_definition", "function_definition"):
            for sibling in parent.children:
                if sibling.type == "comment":
                    continue
                return sibling.id == node.id

    return False

def canonicalize(node: Node, source: bytes, language: str) -> list[str]:
    """Serialize the AST subtree into a format-insensitive token list."""
    tokens: list[str] = []

    def visit(n: Node) -> None:
        if n.type == "comment" or n.type.endswith("_comment"):
            return

        if is_docstring_node(n, language):
            return

        if n.child_count == 0:
            try:
                text = n.text.decode("utf-8")
            except (UnicodeDecodeError, AttributeError):
                text = ""
            if n.type == "string" or n.type.endswith("string_literal"):
                text = normalize_string_literal(text)
            tokens.append(f"{n.type}:{text}")
        else:
            tokens.append(f"({n.type}")
            for child in n.children:
                visit(child)
            tokens.append(f"{n.type})")

    visit(node)
    return tokens

def hash_subtree(node: Node, source: bytes, language: str) -> str:
    """Compute a SHA-256 hash of the canonicalized AST subtree."""
    canonical = " ".join(canonicalize(node, source, language))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

def file_semantic_hash(tree: Tree, source: bytes, language: str) -> str:
    """Compute the AST-based semantic hash of the entire file."""
    return hash_subtree(tree.root_node, source, language)

def function_semantic_hash(function_node: Node, source: bytes, language: str) -> str:
    """Compute the AST-based semantic hash of a function node."""
    return hash_subtree(function_node, source, language)

def file_content_hash(source: bytes) -> str:
    """Compute a SHA-256 hash of the raw file content bytes."""
    return hashlib.sha256(source).hexdigest()
