import json
from ctx_engine.languages.registry import get_parser
from ctx_engine.languages.python_adapter import PythonAdapter
from ctx_engine.languages.base import ImportStatement
from ctx_engine.call_graph import resolve_calls

def test_resolve_calls_integration():
    src = """
class Client:
    def __init__(self):
        self.execute()
        
    def execute(self):
        helper_func()
        
class Manager:
    def execute(self):
        pass

def helper_func():
    other_module.run()

def run_duck(obj):
    obj.execute()
"""
    parser = get_parser("python")
    source_bytes = src.encode("utf-8")
    tree = parser.parse(source_bytes)

    adapter = PythonAdapter()
    struct = adapter.extract(tree, source_bytes)

    # 1. Build functions_data
    functions_data = []
    for func in struct.functions:
        qualified = f"{func.class_name}.{func.name}" if func.class_name else func.name
        functions_data.append({
            "id": f"main.py::{qualified}",
            "file": "main.py",
            "class_name": func.class_name,
            "name": func.name,
            "signature": func.signature,
            "_node": func.node,
            "_language": "python"
        })

    # Add a mock function for other.py::run
    functions_data.append({
        "id": "other.py::run",
        "file": "other.py",
        "class_name": None,
        "name": "run",
        "signature": "def run()",
        "_node": None,
        "_language": "python"
    })

    # 2. Setup resolution context
    resolved_imports = {"main.py": ["other.py"]}
    raw_imports = {
        "main.py": [
            ImportStatement(module="other", names=["run"], alias="other_module")
        ]
    }
    class_superclasses = {"main.py": struct.class_superclasses}
    files_exports = {
        "other.py": ["run"],
        "main.py": struct.exports
    }

    # 3. Resolve calls
    edges = resolve_calls(
        functions_data,
        resolved_imports,
        raw_imports,
        class_superclasses,
        files_exports
    )

    # Convert edges to dict keyed by caller_id & callee_name
    edges_map = {}
    for edge in edges:
        key = (edge["caller_id"], edge["callee_name"])
        edges_map[key] = edge

    # 4. Assertions
    # Case 2: self.execute() in Client.__init__
    key = ("main.py::Client.__init__", "execute")
    assert key in edges_map
    assert edges_map[key]["callee_id"] == "main.py::Client.execute"
    assert edges_map[key]["callee_file"] == "main.py"

    # Case 1: helper_func() in Client.execute
    key = ("main.py::Client.execute", "helper_func")
    assert key in edges_map
    assert edges_map[key]["callee_id"] == "main.py::helper_func"
    assert edges_map[key]["callee_file"] == "main.py"

    # Case 3: other_module.run() in helper_func
    key = ("main.py::helper_func", "run")
    assert key in edges_map
    assert edges_map[key]["callee_id"] == "other.py::run"
    assert edges_map[key]["callee_file"] == "other.py"

    # Case 5: obj.execute() in run_duck (ambiguous)
    key = ("main.py::run_duck", "execute")
    assert key in edges_map
    edge = edges_map[key]
    assert edge["is_ambiguous"] == 1
    assert edge["callee_id"] is None
    candidates = json.loads(edge["candidates"])
    assert "main.py::Client.execute" in candidates
    assert "main.py::Manager.execute" in candidates
