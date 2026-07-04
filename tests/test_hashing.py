from tree_sitter import Parser
from ctx_engine.languages.registry import get_parser
from ctx_engine.hashing import file_semantic_hash, function_semantic_hash

def parse_source(src: str, lang_key: str) -> tuple[any, bytes]:
    source_bytes = src.encode("utf-8")
    parser = get_parser(lang_key)
    tree = parser.parse(source_bytes)
    return tree, source_bytes

def get_first_function_node(tree, lang_key: str):
    # Find the first function definition node in the tree
    func_types = {
        "python": "function_definition",
        "javascript": "function_declaration",
        "typescript": "function_declaration",
        "go": "function_declaration",
        "rust": "function_item"
    }
    target_type = func_types[lang_key]
    
    found = []
    def walk(n):
        if n.type == target_type:
            found.append(n)
            return
        for child in n.children:
            walk(child)
            
    walk(tree.root_node)
    return found[0] if found else None

def test_python_hashing_invariance():
    # 1. Base function
    src_base = """
def calculate_sum(a, b):
    \"\"\"This is a docstring.\"\"\"
    # This is a comment
    result = a + b
    return result
"""
    # 2. Re-formatted function (quote style and indentation change)
    src_reformatted = """
def calculate_sum(a, b):
          \"\"\"This is a docstring.\"\"\"
          # Different comment
          result = a + b
          return result
"""
    # 3. Comment added/edited
    src_comments = """
def calculate_sum(a, b):
    \"\"\"This is a docstring.\"\"\"
    # A completely different comment
    # and another one
    result = a + b
    return result
"""
    # 4. Docstring changed/removed
    src_docstring = """
def calculate_sum(a, b):
    result = a + b
    return result
"""
    # 5. Semantic change (logic changed)
    src_logic_changed = """
def calculate_sum(a, b):
    \"\"\"This is a docstring.\"\"\"
    result = a - b
    return result
"""
    # 6. Function renamed
    src_renamed = """
def calculate_diff(a, b):
    \"\"\"This is a docstring.\"\"\"
    result = a + b
    return result
"""

    t_base, b_base = parse_source(src_base, "python")
    t_ref, b_ref = parse_source(src_reformatted, "python")
    t_comm, b_comm = parse_source(src_comments, "python")
    t_doc, b_doc = parse_source(src_docstring, "python")
    t_logic, b_logic = parse_source(src_logic_changed, "python")
    t_renamed, b_renamed = parse_source(src_renamed, "python")

    n_base = get_first_function_node(t_base, "python")
    n_ref = get_first_function_node(t_ref, "python")
    n_comm = get_first_function_node(t_comm, "python")
    n_doc = get_first_function_node(t_doc, "python")
    n_logic = get_first_function_node(t_logic, "python")
    n_renamed = get_first_function_node(t_renamed, "python")

    h_base = function_semantic_hash(n_base, b_base, "python")
    h_ref = function_semantic_hash(n_ref, b_ref, "python")
    h_comm = function_semantic_hash(n_comm, b_comm, "python")
    h_doc = function_semantic_hash(n_doc, b_doc, "python")
    h_logic = function_semantic_hash(n_logic, b_logic, "python")
    h_renamed = function_semantic_hash(n_renamed, b_renamed, "python")

    # Reformatted, comments, and docstrings must not change the semantic hash
    assert h_base == h_ref
    assert h_base == h_comm
    assert h_base == h_doc
    # Logic change or renaming must change the semantic hash
    assert h_base != h_logic
    assert h_base != h_renamed

def test_javascript_hashing_invariance():
    # 1. Base function
    src_base = """
function getProduct(x, y) {
    // Calculate product
    const val = x * y;
    return val;
}
"""
    # 2. Re-formatted and comment changed
    src_reformatted = """
function getProduct(x,   y) {
    /* Different comment style */
    const val = x * y;
    return val;
}
"""
    # 3. Logic changed
    src_logic = """
function getProduct(x, y) {
    const val = x / y;
    return val;
}
"""

    t_base, b_base = parse_source(src_base, "javascript")
    t_ref, b_ref = parse_source(src_reformatted, "javascript")
    t_logic, b_logic = parse_source(src_logic, "javascript")

    n_base = get_first_function_node(t_base, "javascript")
    n_ref = get_first_function_node(t_ref, "javascript")
    n_logic = get_first_function_node(t_logic, "javascript")

    h_base = function_semantic_hash(n_base, b_base, "javascript")
    h_ref = function_semantic_hash(n_ref, b_ref, "javascript")
    h_logic = function_semantic_hash(n_logic, b_logic, "javascript")

    assert h_base == h_ref
    assert h_base != h_logic
