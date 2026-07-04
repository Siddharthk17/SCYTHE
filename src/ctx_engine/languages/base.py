from dataclasses import dataclass, field
from typing import Protocol
from tree_sitter import Node, Tree

@dataclass
class ImportStatement:
    """Represents a raw import statement extracted from a source file."""
    module: str
    names: list[str] = field(default_factory=list)
    level: int = 0  # Python relative import level (number of leading dots)
    alias: str | None = None

@dataclass
class FunctionRecord:
    """Represents a function or method definition record."""
    name: str
    class_name: str | None
    signature: str
    line_start: int  # 1-indexed
    line_end: int    # 1-indexed, inclusive
    node: Node       # Keep definition node for AST hashing & call extraction
    body_node: Node | None
    mutates: list[str] = field(default_factory=list)

@dataclass
class FileStructure:
    """Represents the complete extracted structure of a source file."""
    exports: list[str]
    imports_raw: list[ImportStatement]
    functions: list[FunctionRecord]
    class_superclasses: dict[str, str] = field(default_factory=dict)


class LanguageAdapter(Protocol):
    """Protocol for language-specific metadata extractors."""
    def extract(self, tree: Tree, source: bytes) -> FileStructure:
        ...

def extract_signature(node: Node, source: bytes) -> str:
    """Extract a function signature by taking the source slice before the body block."""
    body = node.child_by_field_name("body")
    end = body.start_byte if body is not None else node.end_byte
    text = source[node.start_byte:end].decode("utf-8").strip()
    # Normalize ending formatting characters
    if text.endswith(":") or text.endswith("{"):
        text = text[:-1].rstrip()
    return text
