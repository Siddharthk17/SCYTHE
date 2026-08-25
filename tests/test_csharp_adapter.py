"""Tests for the C# language adapter."""
import pytest

from ctx_engine.languages.csharp_adapter import CSharpAdapter
from ctx_engine.languages.registry import get_parser


def _parse(src: str):
    parser = get_parser("csharp")
    source = src.encode("utf-8")
    tree = parser.parse(source)
    return tree, source


def test_csharp_namespace_class_extraction():
    src = """
namespace MyApp.Services {
    public class UserService {
        public string GetName() {
            return "x";
        }
    }
}
"""
    tree, source = _parse(src)
    struct = CSharpAdapter().extract(tree, source)
    assert "UserService" in struct.exports
    assert "GetName" in struct.exports
    funcs = {f"{f.class_name}::{f.name}": f for f in struct.functions}
    assert "MyApp.Services.UserService::GetName" in funcs


def test_csharp_file_scoped_namespace():
    src = """
namespace MyApp.Solo;

public class Person {
    public string Name { get; set; }
}
"""
    tree, source = _parse(src)
    struct = CSharpAdapter().extract(tree, source)
    assert "Person" in struct.exports
    funcs = {f"{f.class_name}::{f.name}": f for f in struct.functions}
    assert "MyApp.Solo.Person::Name" in funcs


def test_csharp_visibility_filtering():
    """public + internal are exported, private is not."""
    src = """
public class Service {
    public void PublicMethod() { }
    internal void InternalMethod() { }
    private void HiddenMethod() { }
}
"""
    tree, source = _parse(src)
    struct = CSharpAdapter().extract(tree, source)
    assert "PublicMethod" in struct.exports
    assert "InternalMethod" in struct.exports
    assert "HiddenMethod" not in struct.exports


def test_csharp_property_declaration():
    src = """
public class User {
    public string Name { get; set; }
    public int Age { get; private set; }
}
"""
    tree, source = _parse(src)
    struct = CSharpAdapter().extract(tree, source)
    funcs = {f.name: f for f in struct.functions}
    assert "Name" in funcs
    assert funcs["Name"].signature == "public string Name { get; set; }"


def test_csharp_mutation_tracking():
    src = """
public class Connection {
    private string conn;

    public void Configure(string c) {
        this.conn = c;
        this.conn = "default";
    }
}
"""
    tree, source = _parse(src)
    struct = CSharpAdapter().extract(tree, source)
    funcs = {f.name: f for f in struct.functions}
    assert "this.conn" in funcs["Configure"].mutates


def test_csharp_static_mutation_tracking():
    src = """
public class Counter {
    public void Reset() {
        Counter.Total = 0;
    }
}
"""
    tree, source = _parse(src)
    struct = CSharpAdapter().extract(tree, source)
    funcs = {f.name: f for f in struct.functions}
    assert "static:Counter.Total" in funcs["Reset"].mutates


def test_csharp_partial_class_two_files():
    """Two .cs files with the same partial class name both produce records."""
    src_a = """
namespace MyApp;
public partial class Widget {
    public void Render() { }
}
"""
    src_b = """
namespace MyApp;
public partial class Widget {
    public void Layout() { }
}
"""
    # First file
    tree_a, source_a = _parse(src_a)
    struct_a = CSharpAdapter().extract(tree_a, source_a)
    funcs_a = {f"{f.class_name}::{f.name}": f for f in struct_a.functions}

    # Second file
    tree_b, source_b = _parse(src_b)
    struct_b = CSharpAdapter().extract(tree_b, source_b)
    funcs_b = {f"{f.class_name}::{f.name}": f for f in struct_b.functions}

    # Both produce function records with class_name = 'MyApp.Widget'
    assert any("Widget::Render" in k for k in funcs_a.keys())
    assert any("Widget::Layout" in k for k in funcs_b.keys())


def test_csharp_using_directive():
    src = """
using System;
using System.Collections.Generic;
using Alias = System.Text;
"""
    tree, source = _parse(src)
    struct = CSharpAdapter().extract(tree, source)
    assert len(struct.imports_raw) == 3
    assert struct.imports_raw[0].module == "System"
    assert struct.imports_raw[1].module == "System.Collections.Generic"
    assert struct.imports_raw[2].alias == "Alias"
    assert struct.imports_raw[2].module == "System.Text"


def test_csharp_constructor_extraction():
    src = """
public class Service {
    public Service(string config) {
    }
}
"""
    tree, source = _parse(src)
    struct = CSharpAdapter().extract(tree, source)
    funcs = {f.name: f for f in struct.functions}
    assert "Service" in funcs
    # Constructor name == class name
    assert "Service" in struct.exports


def test_csharp_record_declaration():
    src = """
public record Person(string Name, int Age);
"""
    tree, source = _parse(src)
    struct = CSharpAdapter().extract(tree, source)
    assert "Person" in struct.exports


def test_csharp_struct_declaration():
    src = """
public struct Point {
    public int X { get; set; }
    public int Y { get; set; }
}
"""
    tree, source = _parse(src)
    struct = CSharpAdapter().extract(tree, source)
    assert "Point" in struct.exports
