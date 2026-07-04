from ctx_engine.languages.registry import get_parser
from ctx_engine.languages.python_adapter import PythonAdapter

def test_python_adapter_extraction():
    src = """
import os
from sys import argv
from . import local_mod
from ..parent_mod import parent_func

__all__ = ["export_one", "export_two"]

class User:
    def __init__(self, name):
        self.name = name
        self._cache = {}
        
    def update_name(self, new_name):
        self.name = new_name
        
    class Inner:
        def process(self):
            self.val = 42

def export_one(x):
    global count
    count = x
    return x

def _internal_helper():
    pass
"""
    parser = get_parser("python")
    source_bytes = src.encode("utf-8")
    tree = parser.parse(source_bytes)

    adapter = PythonAdapter()
    struct = adapter.extract(tree, source_bytes)

    # 1. Assert exports
    assert struct.exports == ["export_one", "export_two"]

    # 2. Assert raw imports
    assert len(struct.imports_raw) == 4
    # Check import os
    assert struct.imports_raw[0].module == "os"
    # Check from sys import argv
    assert struct.imports_raw[1].module == "sys"
    assert struct.imports_raw[1].names == ["argv"]
    # Check from . import local_mod
    assert struct.imports_raw[2].module == ""
    assert struct.imports_raw[2].names == ["local_mod"]
    assert struct.imports_raw[2].level == 1
    # Check from ..parent_mod import parent_func
    assert struct.imports_raw[3].module == "parent_mod"
    assert struct.imports_raw[3].names == ["parent_func"]
    assert struct.imports_raw[3].level == 2

    # 3. Assert functions and mutations
    funcs = {f"{f.class_name}::{f.name}" if f.class_name else f.name: f for f in struct.functions}
    
    assert "User::__init__" in funcs
    init_func = funcs["User::__init__"]
    assert init_func.signature == "def __init__(self, name)"
    assert set(init_func.mutates) == {"self.name", "self._cache"}

    assert "User::update_name" in funcs
    update_func = funcs["User::update_name"]
    assert update_func.signature == "def update_name(self, new_name)"
    assert update_func.mutates == ["self.name"]

    assert "User.Inner::process" in funcs
    process_func = funcs["User.Inner::process"]
    assert process_func.signature == "def process(self)"
    assert process_func.mutates == ["self.val"]

    assert "export_one" in funcs
    export_func = funcs["export_one"]
    assert export_func.signature == "def export_one(x)"
    assert export_func.mutates == ["global:count"]

    assert "_internal_helper" in funcs
