from ctx_engine.languages.registry import get_parser
from ctx_engine.languages.javascript_adapter import JavaScriptAdapter

def test_javascript_adapter_extraction():
    src = """
import defaultExport, { named1, named2 as alias } from "./module";
import * as namespace from "../namespace";

export class Button {
    constructor(label) {
        this.label = label;
        this.clicked = false;
    }
    
    click() {
        this.clicked = true;
    }
}

export function helper() {
    return 42;
}

const arrowHelper = (x) => {
    return x * 2;
};

export default arrowHelper;
"""
    parser = get_parser("javascript")
    source_bytes = src.encode("utf-8")
    tree = parser.parse(source_bytes)

    adapter = JavaScriptAdapter()
    struct = adapter.extract(tree, source_bytes)

    # 1. Assert exports
    assert set(struct.exports) == {"Button", "helper", "default"}

    # 2. Assert raw imports
    assert len(struct.imports_raw) == 2
    assert struct.imports_raw[0].module == "./module"
    assert set(struct.imports_raw[0].names) == {"defaultExport", "named1", "named2"}
    assert struct.imports_raw[1].module == "../namespace"
    assert struct.imports_raw[1].names == ["*"]

    # 3. Assert functions and mutations
    funcs = {f"{f.class_name}::{f.name}" if f.class_name else f.name: f for f in struct.functions}

    assert "Button::constructor" in funcs
    constructor_func = funcs["Button::constructor"]
    assert constructor_func.signature == "constructor(label)"
    assert set(constructor_func.mutates) == {"this.label", "this.clicked"}

    assert "Button::click" in funcs
    click_func = funcs["Button::click"]
    assert click_func.signature == "click()"
    assert click_func.mutates == ["this.clicked"]

    assert "helper" in funcs
    helper_func = funcs["helper"]
    assert helper_func.signature == "function helper()"

    assert "arrowHelper" in funcs
    arrow_func = funcs["arrowHelper"]
    assert arrow_func.signature == "(x) =>"
