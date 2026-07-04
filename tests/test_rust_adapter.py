from ctx_engine.languages.registry import get_parser
from ctx_engine.languages.rust_adapter import RustAdapter

def test_rust_adapter_extraction():
    src = """
use std::collections::HashMap;
use crate::utils::{self, helper};

pub struct Db<T> {
    connections: HashMap<String, T>,
}

impl<T> Db<T> {
    pub fn new() -> Self {
        Db { connections: HashMap::new() }
    }
    
    pub fn add_connection(&mut self, name: String, conn: T) {
        self.connections.insert(name, conn);
    }
}

pub fn run_app() {
    let mut db = Db::new();
}

fn internal_func() {}
"""
    parser = get_parser("rust")
    source_bytes = src.encode("utf-8")
    tree = parser.parse(source_bytes)

    adapter = RustAdapter()
    struct = adapter.extract(tree, source_bytes)

    # 1. Assert exports
    assert set(struct.exports) == {"Db", "run_app"}

    # 2. Assert raw imports
    # use std::collections::HashMap -> module std::collections, name HashMap
    # use crate::utils::{self, helper} -> crate::utils::self and crate::utils::helper
    # Let's inspect the extracted raw imports
    imps = [(imp.module, tuple(imp.names)) for imp in struct.imports_raw]
    assert len(imps) >= 2
    assert ("std::collections", ("HashMap",)) in imps

    # 3. Assert functions and mutations
    funcs = {f"{f.class_name}::{f.name}" if f.class_name else f.name: f for f in struct.functions}

    assert "Db::new" in funcs
    new_func = funcs["Db::new"]
    assert new_func.signature == "pub fn new() -> Self"

    assert "Db::add_connection" in funcs
    add_conn_func = funcs["Db::add_connection"]
    assert add_conn_func.signature == "pub fn add_connection(&mut self, name: String, conn: T)"
    # connections is mutated, but it is a field on self.
    # self.connections.insert(...) is a method call on field connections, which is not direct assignment to self.connections itself,
    # but wait: let's verify if Rust mutations captures direct assignment like `self.field = value`
    # In this case there's no direct assignment to `self.connections` (it's a method call `.insert()`).
    # Direct assignment mutation would be: `self.connections = ...` which would be captured.
    assert add_conn_func.mutates == []

    assert "run_app" in funcs
    run_app_func = funcs["run_app"]
    assert run_app_func.signature == "pub fn run_app()"

    assert "internal_func" in funcs
