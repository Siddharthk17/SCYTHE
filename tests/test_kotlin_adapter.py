"""Tests for the Kotlin language adapter (Week 8)."""
from ctx_engine.languages.kotlin_adapter import KotlinAdapter
from ctx_engine.languages.registry import get_parser


def _parse(src: str):
    parser = get_parser("kotlin")
    source = src.encode("utf-8")
    tree = parser.parse(source)
    return tree, source


def _extract(src: str):
    tree, source = _parse(src)
    return KotlinAdapter().extract(tree, source)


def test_kotlin_top_level_function():
    src = "fun main(args: Array<String>) {\n    println(args)\n}\n"
    struct = _extract(src)
    funcs = {f.name: f for f in struct.functions}
    assert "main" in funcs
    assert funcs["main"].class_name is None
    assert funcs["main"].signature == "fun main(args: Array<String>)"
    assert "main" in struct.exports


def test_kotlin_suspend_signature():
    src = (
        "class Api {\n"
        "    suspend fun fetchData(): Flow<String> {\n"
        "        return flow { }\n"
        "    }\n"
        "}\n"
    )
    struct = _extract(src)
    funcs = {f.name: f for f in struct.functions}
    assert "fetchData" in funcs
    # `suspend` must appear in the signature naturally.
    assert funcs["fetchData"].signature == "suspend fun fetchData(): Flow<String>"


def test_kotlin_visibility_filtering():
    """public (explicit and default) + internal are exported; private is not."""
    src = (
        "class Service {\n"
        "    public fun explicit() { }\n"
        "    fun implicit() { }\n"
        "    internal fun internalFn() { }\n"
        "    private fun hidden() { }\n"
        "    protected fun protectedFn() { }\n"
        "}\n"
    )
    struct = _extract(src)
    assert "explicit" in struct.exports
    assert "implicit" in struct.exports
    assert "internalFn" in struct.exports
    assert "hidden" not in struct.exports
    assert "protectedFn" not in struct.exports
    # Private/protected functions are not recorded at all (Kotlin acceptance).
    names = {f.name for f in struct.functions}
    assert "hidden" not in names
    assert "protectedFn" not in names
    assert {"explicit", "implicit", "internalFn"} <= names


def test_kotlin_companion_object():
    src = (
        "class MyClass {\n"
        "    companion object {\n"
        "        fun create(): MyClass = MyClass()\n"
        "    }\n"
        "}\n"
    )
    struct = _extract(src)
    funcs = {f"{f.class_name}::{f.name}": f for f in struct.functions}
    assert "MyClass.Companion::create" in funcs


def test_kotlin_data_class_no_generated_functions():
    """Data classes are exported; compiler-generated functions (copy,
    component1, ...) are not source-level and must not appear."""
    src = "data class User(val name: String, var age: Int)\n"
    struct = _extract(src)
    assert "User" in struct.exports
    names = {f.name for f in struct.functions}
    assert "copy" not in names
    assert "component1" not in names
    assert "User" not in names  # no primary-constructor pseudo-function


def test_kotlin_this_mutation():
    src = (
        "class Conn {\n"
        "    private var cache: Int = 0\n"
        "    fun setIt(v: Int) {\n"
        "        this.cache = v\n"
        "    }\n"
        "}\n"
    )
    struct = _extract(src)
    funcs = {f.name: f for f in struct.functions}
    assert "this.cache" in funcs["setIt"].mutates


def test_kotlin_extension_function():
    src = "fun String.myExt(): String = uppercase()\n"
    struct = _extract(src)
    funcs = {f.name: f for f in struct.functions}
    assert "myExt" in funcs
    assert funcs["myExt"].class_name is None
    # The receiver type is preserved in the signature.
    assert funcs["myExt"].signature == "fun String.myExt(): String"
    assert "myExt" in struct.exports


def test_kotlin_kts_same_as_kt():
    src = "fun scriptMain() {\n    println(\"kts\")\n}\n"
    # .kts files are parsed with the same grammar — the adapter is identical.
    struct = _extract(src)
    funcs = {f.name: f for f in struct.functions}
    assert "scriptMain" in funcs
    assert "scriptMain" in struct.exports


def test_kotlin_imports():
    src = (
        "import com.example.model.Foo\n"
        "import com.example.model.*\n"
        "import com.example.Foo as Bar\n"
    )
    struct = _extract(src)
    assert len(struct.imports_raw) == 3
    assert struct.imports_raw[0].module == "com.example.model.Foo"
    assert struct.imports_raw[1].names == ["*"]
    assert struct.imports_raw[2].alias == "Bar"


def test_kotlin_object_declaration():
    src = (
        "object Registry {\n"
        "    fun lookup(name: String): Int = 0\n"
        "}\n"
    )
    struct = _extract(src)
    assert "Registry" in struct.exports
    funcs = {f"{f.class_name}::{f.name}" for f in struct.functions}
    assert "Registry::lookup" in funcs


def test_kotlin_interface():
    src = (
        "interface Repo {\n"
        "    fun save(x: Int)\n"
        "}\n"
    )
    struct = _extract(src)
    assert "Repo" in struct.exports
    assert "save" in struct.exports
