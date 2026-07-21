from pathlib import Path
import tree_sitter_python as tspython
import tree_sitter_javascript as tsjavascript
import tree_sitter_typescript as tstypescript
import tree_sitter_go as tsgo
import tree_sitter_rust as tsrust
from tree_sitter import Language, Parser, Tree

LANGUAGES: dict[str, Language] = {
    "python":     Language(tspython.language()),
    "javascript": Language(tsjavascript.language()),
    "typescript": Language(tstypescript.language_typescript()),
    "tsx":        Language(tstypescript.language_tsx()),
    "go":         Language(tsgo.language()),
    "rust":       Language(tsrust.language()),
}


def get_parser(language: str) -> Parser:
    """Return a configured tree-sitter Parser for the specified language.

    Raises ValueError if the language is not supported.
    """
    if language not in LANGUAGES:
        raise ValueError(f"Unsupported language: {language}")
    return Parser(LANGUAGES[language])


def parse_file(path: Path, language: str) -> tuple[Tree, bytes]:
    """Read a file and parse it with the specified language's tree-sitter parser.

    Returns (tree, source_bytes).
    """
    source = path.read_bytes()
    tree = get_parser(language).parse(source)
    return tree, source
