from ctx_engine.languages.registry import get_parser
from ctx_engine.languages.go_adapter import GoAdapter

def test_go_adapter_extraction():
    src = """
package main

import (
    "fmt"
    log "github.com/sirupsen/logrus"
)

type Config struct {
    Host string
    Port int
}

var ExportedVar = "hello"
const exportedConst = 42

func NewConfig(host string, port int) *Config {
    return &Config{Host: host, Port: port}
}

func (c *Config) UpdateHost(newHost string) {
    c.Host = newHost
}

func (c Config) GetPort() int {
    return c.Port
}

func unexportedFunc() {}
"""
    parser = get_parser("go")
    source_bytes = src.encode("utf-8")
    tree = parser.parse(source_bytes)

    adapter = GoAdapter()
    struct = adapter.extract(tree, source_bytes)

    # 1. Assert exports
    assert set(struct.exports) == {"Config", "ExportedVar", "NewConfig"}

    # 2. Assert raw imports
    assert len(struct.imports_raw) == 2
    assert struct.imports_raw[0].module == "fmt"
    assert struct.imports_raw[0].alias is None
    assert struct.imports_raw[1].module == "github.com/sirupsen/logrus"
    assert struct.imports_raw[1].alias == "log"

    # 3. Assert functions and mutations
    funcs = {f"{f.class_name}::{f.name}" if f.class_name else f.name: f for f in struct.functions}

    assert "NewConfig" in funcs
    new_config_func = funcs["NewConfig"]
    assert new_config_func.signature == "func NewConfig(host string, port int) *Config"

    assert "Config::UpdateHost" in funcs
    update_host_func = funcs["Config::UpdateHost"]
    assert update_host_func.signature == "func (c *Config) UpdateHost(newHost string)"
    assert update_host_func.mutates == ["c.Host"]

    assert "Config::GetPort" in funcs
    get_port_func = funcs["Config::GetPort"]
    assert get_port_func.signature == "func (c Config) GetPort() int"
    assert get_port_func.mutates == []

    assert "unexportedFunc" in funcs
