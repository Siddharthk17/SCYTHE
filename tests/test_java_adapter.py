"""Tests for the Java language adapter."""
import pytest

from ctx_engine.languages.java_adapter import JavaAdapter
from ctx_engine.languages.registry import get_parser


def _parse(src: str):
    parser = get_parser("java")
    source = src.encode("utf-8")
    tree = parser.parse(source)
    return tree, source


def test_java_public_class_with_mixed_methods():
    src = """
package com.example;

public class UserService {
    private String dbUrl;

    public UserService(String url) {
        this.dbUrl = url;
    }

    public String findAll(int limit) {
        return dbUrl;
    }

    private void helper() {
        return;
    }
}
"""
    tree, source = _parse(src)
    struct = JavaAdapter().extract(tree, source)

    assert "UserService" in struct.exports
    assert "findAll" in struct.exports
    assert "UserService" in struct.exports
    # private method should NOT be in exports
    assert "helper" not in struct.exports


def test_java_import_extraction():
    src = """
package com.example;
import java.util.List;
import com.example.service.UserService;
import com.example.model.*;
"""
    tree, source = _parse(src)
    struct = JavaAdapter().extract(tree, source)
    assert len(struct.imports_raw) == 3
    assert struct.imports_raw[0].module == "java.util.List"
    assert struct.imports_raw[1].module == "com.example.service.UserService"
    assert struct.imports_raw[2].module == "com.example.model"
    assert struct.imports_raw[2].names == ["*"]


def test_java_method_signature_no_trailing_brace():
    src = """
public class Calc {
    public List<String> findAll(Pageable pageable) {
        return null;
    }
}
"""
    tree, source = _parse(src)
    struct = JavaAdapter().extract(tree, source)
    funcs = {f.name: f for f in struct.functions}
    assert "findAll" in funcs
    sig = funcs["findAll"].signature
    assert "{" not in sig
    assert sig.endswith(")")


def test_java_constructor_extraction():
    src = """
public class UserRepository {
    private DataSource dataSource;

    public UserRepository(DataSource ds) {
        this.dataSource = ds;
    }
}
"""
    tree, source = _parse(src)
    struct = JavaAdapter().extract(tree, source)
    funcs = {f"{f.class_name}::{f.name}": f for f in struct.functions}
    assert "UserRepository::UserRepository" in funcs
    ctor = funcs["UserRepository::UserRepository"]
    assert ctor.class_name == "UserRepository"
    assert ctor.name == "UserRepository"


def test_java_constructor_mutation_tracking():
    src = """
public class UserRepository {
    private DataSource dataSource;

    public UserRepository(DataSource ds) {
        this.dataSource = ds;
        this.dataSource = null;
    }
}
"""
    tree, source = _parse(src)
    struct = JavaAdapter().extract(tree, source)
    funcs = {f.name: f for f in struct.functions}
    assert "this.dataSource" in funcs["UserRepository"].mutates


def test_java_static_mutation_tracking():
    src = """
public class Counter {
    public void reset() {
        Counter.total = 0;
    }
}
"""
    tree, source = _parse(src)
    struct = JavaAdapter().extract(tree, source)
    funcs = {f.name: f for f in struct.functions}
    assert "static:Counter.total" in funcs["reset"].mutates


def test_java_annotation_decorated_method():
    src = """
public class Greeter {
    @Override
    public String toString() {
        return "hello";
    }
}
"""
    tree, source = _parse(src)
    struct = JavaAdapter().extract(tree, source)
    funcs = {f.name: f for f in struct.functions}
    assert "toString" in funcs
    # The signature starts with public (the modifier), may include the annotation
    assert "public" in funcs["toString"].signature
    assert "String" in funcs["toString"].signature


def test_java_interface_methods():
    src = """
public interface MyInterface {
    void doIt();
    String getName(int id);
}
"""
    tree, source = _parse(src)
    struct = JavaAdapter().extract(tree, source)
    funcs = {f.name: f for f in struct.functions}
    assert "doIt" in funcs
    assert "getName" in funcs
    # Interface methods have no modifiers and no body \u2014 not exported (no public modifier)
    # (interfaces declare them as implicitly public, but the AST puts no 'public' child
    # for the no-modifier case; only those with explicit 'public' are exported)
    assert "doIt" not in struct.exports  # no explicit public modifier


def test_java_enum_declaration():
    src = """
public enum Color {
    RED, GREEN, BLUE;
}
"""
    tree, source = _parse(src)
    struct = JavaAdapter().extract(tree, source)
    assert "Color" in struct.exports


def test_java_package_private_excluded():
    src = """
class Internal {
    public void doStuff() {
    }
}
"""
    tree, source = _parse(src)
    struct = JavaAdapter().extract(tree, source)
    # class with no public modifier: not exported
    assert "Internal" not in struct.exports
