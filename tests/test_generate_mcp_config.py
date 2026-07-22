import json
import pytest
from pathlib import Path
from ctx_engine.commands.generate_mcp_config import (
    run_generate_mcp_config,
    MCP_CONFIG_TEMPLATE,
    SYSTEM_PROMPT_CONTENT,
)


@pytest.fixture
def fresh_repo(tmp_path: Path) -> Path:
    (tmp_path / ".ctx").mkdir()
    return tmp_path


def test_generates_mcp_config(fresh_repo: Path, capsys):
    run_generate_mcp_config(fresh_repo)
    config_path = fresh_repo / ".mcp.json"
    assert config_path.exists()
    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert "mcpServers" in data
    assert "ctx" in data["mcpServers"]
    assert data["mcpServers"]["ctx"]["command"] == "ctx"
    assert data["mcpServers"]["ctx"]["args"] == ["serve"]


def test_already_configured(fresh_repo: Path, capsys):
    run_generate_mcp_config(fresh_repo)
    captured = capsys.readouterr()
    assert "Written" in captured.out

    run_generate_mcp_config(fresh_repo)
    captured = capsys.readouterr()
    assert "already configured" in captured.out

    config_path = fresh_repo / ".mcp.json"
    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert "ctx" in data["mcpServers"]


def test_merge_with_existing_other_server(fresh_repo: Path, capsys):
    config_path = fresh_repo / ".mcp.json"
    other_config = {
        "mcpServers": {
            "other": {
                "command": "other-tool",
                "args": [],
                "env": {},
            }
        }
    }
    config_path.write_text(json.dumps(other_config, indent=2), encoding="utf-8")

    run_generate_mcp_config(fresh_repo)
    captured = capsys.readouterr()
    assert "Added ctx server" in captured.out

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert "ctx" in data["mcpServers"]
    assert "other" in data["mcpServers"]
    assert data["mcpServers"]["other"]["command"] == "other-tool"


def test_invalid_json_not_touched(fresh_repo: Path, capsys):
    config_path = fresh_repo / ".mcp.json"
    config_path.write_text("not valid json", encoding="utf-8")

    run_generate_mcp_config(fresh_repo)
    captured = capsys.readouterr()
    assert "invalid JSON" in captured.out

    assert config_path.read_text(encoding="utf-8") == "not valid json"


def test_writes_system_prompt(fresh_repo: Path, capsys):
    run_generate_mcp_config(fresh_repo)
    prompt_path = fresh_repo / ".ctx" / "system-prompt.md"
    assert prompt_path.exists()
    content = prompt_path.read_text(encoding="utf-8")
    assert "get_context" in content
    assert "update_file" in content
    assert "update_function" in content
    assert "log_session" in content


def test_update_existing_ctx_config_with_new_values(fresh_repo: Path, capsys):
    config_path = fresh_repo / ".mcp.json"
    existing = {
        "mcpServers": {
            "ctx": {
                "command": "ctx",
                "args": ["serve", "--extra"],
                "env": {"OLD": "val"},
            }
        }
    }
    config_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")

    run_generate_mcp_config(fresh_repo)
    captured = capsys.readouterr()
    assert "Updated ctx server" in captured.out

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["mcpServers"]["ctx"]["args"] == ["serve"]
