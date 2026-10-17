from dataclasses import dataclass

import pytest

from ctx_engine.hashing import file_semantic_hash
from ctx_engine.languages.registry import LANGUAGES, get_parser
from ctx_engine.reindex import ADAPTERS, function_id_for


def parse_source_string(source: str, language: str):
    """Parse source text in memory without a temp file."""
    blob = source.encode("utf-8")
    tree = get_parser(language).parse(blob)
    return tree, blob


@dataclass
class ConformanceFixture:
    source: str
    exported_names: set[str]
    non_exported_names: set[str]
    expected_mutation_owner: str
    expected_mutation_contains: str


CONFORMANCE_FIXTURES: dict[str, ConformanceFixture] = {
    "python": ConformanceFixture(
        source=(
            "class Widget:\n"
            "    def public_method(self, x: int) -> str:\n"
            "        self.value = x\n"
            "        return str(x)\n"
            "    def _private_method(self):\n"
            "        return None\n"
        ),
        exported_names={"Widget"},
        non_exported_names={"_private_method"},
        expected_mutation_owner="Widget.public_method",
        expected_mutation_contains="self.value",
    ),
    "typescript": ConformanceFixture(
        source=(
            "export class Widget {\n"
            "    value: number = 0;\n"
            "    publicMethod(x: number): string {\n"
            "        this.value = x;\n"
            "        return String(x);\n"
            "    }\n"
            "    private privateMethod(): void {\n"
            "        return;\n"
            "    }\n"
            "}\n"
        ),
        exported_names={"Widget"},
        non_exported_names={"privateMethod"},
        expected_mutation_owner="Widget.publicMethod",
        expected_mutation_contains="this.value",
    ),
    "tsx": ConformanceFixture(
        source=(
            "export class Widget {\n"
            "    value: number = 0;\n"
            "    publicMethod(x: number): string {\n"
            "        this.value = x;\n"
            "        return String(x);\n"
            "    }\n"
            "    private privateMethod(): void {\n"
            "        return;\n"
            "    }\n"
            "}\n"
        ),
        exported_names={"Widget"},
        non_exported_names={"privateMethod"},
        expected_mutation_owner="Widget.publicMethod",
        expected_mutation_contains="this.value",
    ),
    "javascript": ConformanceFixture(
        source=(
            "export class Widget {\n"
            "    constructor() {\n"
            "        this.value = 0;\n"
            "    }\n"
            "    publicMethod(x) {\n"
            "        this.value = x;\n"
            "        return String(x);\n"
            "    }\n"
            "    _helper() {\n"
            "        return 1;\n"
            "    }\n"
            "}\n"
        ),
        exported_names={"Widget"},
        non_exported_names={"_helper"},
        expected_mutation_owner="Widget.publicMethod",
        expected_mutation_contains="this.value",
    ),
    "go": ConformanceFixture(
        source=(
            "package widget\n"
            "type Widget struct {\n"
            "    Value int\n"
            "}\n"
            "func (w *Widget) PublicMethod(x int) string {\n"
            '    w.Value = x\n'
            '    return ""\n'
            "}\n"
            "func (w *Widget) privateMethod() int {\n"
            "    return 1\n"
            "}\n"
        ),
        exported_names={"Widget"},
        non_exported_names={"privateMethod"},
        expected_mutation_owner="Widget.PublicMethod",
        expected_mutation_contains="w.Value",
    ),
    "rust": ConformanceFixture(
        source=(
            "pub struct Widget {\n"
            "    pub value: i32,\n"
            "}\n"
            "impl Widget {\n"
            "    pub fn public_method(&mut self, x: i32) -> String {\n"
            "        self.value = x;\n"
            "        String::new()\n"
            "    }\n"
            "    fn private_method(&self) -> i32 {\n"
            "        1\n"
            "    }\n"
            "}\n"
        ),
        exported_names={"Widget"},
        non_exported_names={"private_method"},
        expected_mutation_owner="Widget.public_method",
        expected_mutation_contains="self.value",
    ),
    "java": ConformanceFixture(
        source=(
            "public class Widget {\n"
            "    public int value;\n"
            "    public String publicMethod(int x) {\n"
            "        this.value = x;\n"
            "        return String.valueOf(x);\n"
            "    }\n"
            "    private void privateMethod() {\n"
            "        return;\n"
            "    }\n"
            "}\n"
        ),
        exported_names={"Widget", "publicMethod"},
        non_exported_names={"privateMethod"},
        expected_mutation_owner="Widget.publicMethod",
        expected_mutation_contains="this.value",
    ),
    "csharp": ConformanceFixture(
        source=(
            "public class Widget {\n"
            "    public int Value;\n"
            "    public string PublicMethod(int x) {\n"
            "        this.Value = x;\n"
            "        return x.ToString();\n"
            "    }\n"
            "    private void PrivateMethod() {\n"
            "    }\n"
            "}\n"
        ),
        exported_names={"Widget", "PublicMethod"},
        non_exported_names={"PrivateMethod"},
        expected_mutation_owner="Widget.PublicMethod",
        expected_mutation_contains="this.Value",
    ),
    "kotlin": ConformanceFixture(
        source=(
            "class Widget {\n"
            "    var value: Int = 0\n"
            "    fun publicMethod(x: Int): String {\n"
            "        this.value = x\n"
            "        return x.toString()\n"
            "    }\n"
            "}\n"
        ),
        exported_names={"Widget", "publicMethod"},
        non_exported_names={"privateMethod"},
        expected_mutation_owner="Widget.publicMethod",
        expected_mutation_contains="this.value",
    ),
    "ruby": ConformanceFixture(
        source=(
            "class Widget\n"
            "    def public_method(x)\n"
            "        @value = x\n"
            "        x.to_s\n"
            "    end\n"
            "    private\n"
            "    def private_method\n"
            "        nil\n"
            "    end\n"
            "end\n"
        ),
        exported_names={"Widget", "public_method"},
        non_exported_names={"private_method"},
        expected_mutation_owner="Widget.public_method",
        expected_mutation_contains="@value",
    ),
}


def _get_adapter(language: str):
    return ADAPTERS[language]


def build_function_id(path: str, fn) -> str:
    return function_id_for(path, fn)


@pytest.mark.parametrize("language", sorted(CONFORMANCE_FIXTURES.keys()))
def test_adapter_extracts_without_raising(language):
    fixture = CONFORMANCE_FIXTURES[language]
    tree, source = parse_source_string(fixture.source, language)
    adapter = _get_adapter(language)
    structure = adapter.extract(tree, source)
    assert structure.functions, f"{language}: no functions extracted from fixture"


@pytest.mark.parametrize("language", sorted(CONFORMANCE_FIXTURES.keys()))
def test_exports_match_visibility_rules(language):
    fixture = CONFORMANCE_FIXTURES[language]
    tree, source = parse_source_string(fixture.source, language)
    structure = _get_adapter(language).extract(tree, source)
    for name in fixture.exported_names:
        assert name in structure.exports, f"{language}: expected '{name}' in exports"
    for name in fixture.non_exported_names:
        assert name not in structure.exports, f"{language}: '{name}' should not be exported"


@pytest.mark.parametrize("language", sorted(CONFORMANCE_FIXTURES.keys()))
def test_signature_has_no_trailing_body_token(language):
    fixture = CONFORMANCE_FIXTURES[language]
    tree, source = parse_source_string(fixture.source, language)
    structure = _get_adapter(language).extract(tree, source)
    for fn in structure.functions:
        sig = fn.signature.rstrip()
        assert not sig.endswith((":", "{", "=>")), (
            f"{language}: signature '{sig}' has a trailing body token — "
            f"the byte-slice extraction algorithm should never include this"
        )


@pytest.mark.parametrize("language", sorted(CONFORMANCE_FIXTURES.keys()))
def test_mutation_detected(language):
    fixture = CONFORMANCE_FIXTURES[language]
    tree, source = parse_source_string(fixture.source, language)
    structure = _get_adapter(language).extract(tree, source)
    owner_fn = next(
        (
            fn
            for fn in structure.functions
            if f"{fn.class_name}.{fn.name}" == fixture.expected_mutation_owner
            or fn.name == fixture.expected_mutation_owner
        ),
        None,
    )
    assert owner_fn is not None, f"{language}: could not find {fixture.expected_mutation_owner}"
    assert any(
        fixture.expected_mutation_contains in m for m in owner_fn.mutates
    ), f"{language}: expected a mutation containing '{fixture.expected_mutation_contains}'"


@pytest.mark.parametrize("language", sorted(CONFORMANCE_FIXTURES.keys()))
def test_reformatting_preserves_semantic_hash(language):
    fixture = CONFORMANCE_FIXTURES[language]
    original_hash = file_semantic_hash(
        *parse_source_string(fixture.source, language), language
    )
    reformatted = "\n\n".join(
        "  " + line if line.strip() else line
        for line in fixture.source.splitlines()
    )
    reformatted_hash = file_semantic_hash(
        *parse_source_string(reformatted, language), language
    )
    assert original_hash == reformatted_hash, (
        f"{language}: reformatting changed the semantic hash — "
        f"this violates the core Week 1 hashing guarantee"
    )


@pytest.mark.parametrize("language", sorted(CONFORMANCE_FIXTURES.keys()))
def test_function_id_format_is_well_formed(language):
    fixture = CONFORMANCE_FIXTURES[language]
    tree, source = parse_source_string(fixture.source, language)
    structure = _get_adapter(language).extract(tree, source)
    for fn in structure.functions:
        fn_id = build_function_id(f"fixture.{language}", fn)
        assert "::" in fn_id, f"{language}: malformed function id '{fn_id}' — missing '::'"
        assert not fn_id.endswith("::"), f"{language}: malformed function id '{fn_id}'"


def test_all_registered_languages_have_fixtures():
    assert set(CONFORMANCE_FIXTURES.keys()) == set(LANGUAGES.keys()), (
        f"fixture languages {sorted(CONFORMANCE_FIXTURES.keys())} != "
        f"registered {sorted(LANGUAGES.keys())}"
    )
    assert len(CONFORMANCE_FIXTURES) == 10


def test_fault_injection_signature_check_discriminates():
    """Break one adapter's signature slice; only that language must fail."""
    import copy

    from ctx_engine.languages.base import FileStructure

    fixture = CONFORMANCE_FIXTURES["python"]
    tree, source = parse_source_string(fixture.source, "python")
    real_struct = ADAPTERS["python"].extract(tree, source)

    broken_funcs = []
    for fn in real_struct.functions:
        broken_funcs.append(
            type(fn)(
                name=fn.name,
                class_name=fn.class_name,
                signature=fn.signature + " {",
                line_start=fn.line_start,
                line_end=fn.line_end,
                node=fn.node,
                body_node=fn.body_node,
                mutates=list(fn.mutates),
                name_separator=fn.name_separator,
            )
        )
    broken = FileStructure(
        exports=list(real_struct.exports),
        imports_raw=list(real_struct.imports_raw),
        functions=broken_funcs,
        class_superclasses=dict(real_struct.class_superclasses),
    )

    def _is_bad(struct):
        return any(fn.signature.rstrip().endswith((":", "{", "=>")) for fn in struct.functions)

    assert _is_bad(broken) is True

    # All other languages' real fixtures remain clean.
    for language in sorted(CONFORMANCE_FIXTURES.keys()):
        if language == "python":
            continue
        fix = CONFORMANCE_FIXTURES[language]
        t, s = parse_source_string(fix.source, language)
        struct = ADAPTERS[language].extract(t, s)
        assert not any(
            fn.signature.rstrip().endswith((":", "{", "=>")) for fn in struct.functions
        ), f"{language} unexpectedly has a trailing body token"
