"""Tests for the Ruby language adapter (Week 9)."""
from ctx_engine.languages.ruby_adapter import RubyAdapter
from ctx_engine.languages.registry import get_parser


def _extract(src: str):
    parser = get_parser("ruby")
    source = src.encode("utf-8")
    tree = parser.parse(source)
    return RubyAdapter().extract(tree, source)


def test_ruby_instance_method():
    struct = _extract("class A\n  def initialize(name)\n    @name = name\n  end\nend\n")
    funcs = {f.name: f for f in struct.functions}
    assert "initialize" in funcs
    assert funcs["initialize"].class_name == "A"
    assert funcs["initialize"].name_separator == "#"


def test_ruby_singleton_method():
    struct = _extract("class A\n  def self.create(name)\n    new(name)\n  end\nend\n")
    funcs = {f.name: f for f in struct.functions}
    assert "create" in funcs
    assert funcs["create"].class_name == "A"
    assert funcs["create"].name_separator == "."


def test_ruby_singleton_class_body():
    struct = _extract(
        "class A\n  class << self\n    def build(x)\n      @built = x\n    end\n  end\nend\n"
    )
    funcs = {f.name: f for f in struct.functions}
    assert "build" in funcs
    assert funcs["build"].class_name == "A"
    assert "@built" in funcs["build"].mutates


def test_ruby_visibility_modifiers():
    src = (
        "class Service\n"
        "  def find(id)\n    id\n  end\n"
        "  private\n"
        "  def save(attrs)\n    attrs\n  end\n"
        "end\n"
    )
    struct = _extract(src)
    assert "find" in struct.exports
    assert "Service" in struct.exports
    assert "save" not in struct.exports
    # Private methods are still indexed, just not exported.
    assert "save" in {f.name for f in struct.functions}


def test_ruby_retroactive_private():
    src = (
        "class A\n"
        "  def foo\n  end\n"
        "  def bar\n  end\n"
        "  private :foo\n"
        "end\n"
    )
    struct = _extract(src)
    assert "bar" in struct.exports
    assert "foo" not in struct.exports


def test_ruby_instance_variable_mutation():
    struct = _extract("class A\n  def setup(v)\n    @instance_var = v\n  end\nend\n")
    funcs = {f.name: f for f in struct.functions}
    assert funcs["setup"].mutates == ["@instance_var"]


def test_ruby_class_and_global_variable_mutation():
    src = "def tune(v)\n  @@count = v\n  $debug = true\nend\n"
    struct = _extract(src)
    funcs = {f.name: f for f in struct.functions}
    assert "@@count" in funcs["tune"].mutates
    assert "$debug" in funcs["tune"].mutates


def test_ruby_require_relative_import():
    struct = _extract('require_relative "models/user"\n')
    assert len(struct.imports_raw) == 1
    assert struct.imports_raw[0].module == "models/user"
    assert struct.imports_raw[0].level == 1


def test_ruby_require_is_external_level():
    struct = _extract('require "json"\n')
    assert len(struct.imports_raw) == 1
    assert struct.imports_raw[0].module == "json"
    assert struct.imports_raw[0].level == 0


def test_ruby_block_not_indexed_calls_attributed():
    src = (
        "def each_item(list)\n"
        "  list.each do |x|\n"
        "    puts(x)\n"
        "  end\n"
        "end\n"
    )
    struct = _extract(src)
    assert [f.name for f in struct.functions] == ["each_item"]

    from ctx_engine.call_graph import collect_calls_in_subtree

    parser = get_parser("ruby")
    source = src.encode("utf-8")
    tree = parser.parse(source)
    again = RubyAdapter().extract(tree, source)
    calls = collect_calls_in_subtree(again.functions[0].node, "ruby")
    names = [c[0] for c in calls]
    assert "each" in names
    assert "puts" in names


def test_ruby_dotted_module():
    struct = _extract("module Concerns::Timestamps\n  def touch\n  end\nend\n")
    assert "Concerns::Timestamps" in struct.exports
    funcs = {f.name: f for f in struct.functions}
    assert "touch" in funcs
    assert funcs["touch"].class_name == "Concerns::Timestamps"


def test_ruby_alias():
    struct = _extract("class A\n  def save\n  end\n  alias alias_save save\nend\n")
    funcs = {f.name: f for f in struct.functions}
    assert "alias_save" in funcs
    assert funcs["alias_save"].signature == "alias alias_save save"


def test_ruby_require_resolution(tmp_path):
    """End-to-end: require_relative resolves, stdlib require does not."""
    from ctx_engine.imports_graph import resolve_file_imports

    files_set = {"service.rb", "models/user.rb"}
    struct = _extract('require_relative "models/user"\nrequire "json"\n')
    resolved = resolve_file_imports(
        "service.rb", "ruby", struct.imports_raw, files_set, {},
        None, repo_root=tmp_path,
    )
    assert resolved == ["models/user.rb"]


def test_ruby_signature_shape():
    struct = _extract(
        "class A\n  def create(name, age = nil)\n    name\n  end\nend\n"
    )
    funcs = {f.name: f for f in struct.functions}
    assert funcs["create"].signature == "def create(name, age = nil)"
