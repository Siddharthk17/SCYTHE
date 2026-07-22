import json
from pathlib import Path

MCP_CONFIG_TEMPLATE = {
    "$schema": "https://raw.githubusercontent.com/anthropics/claude-desktop/main/schemas/mcp.json",
    "mcpServers": {
        "ctx": {
            "command": "ctx",
            "args": ["serve"],
            "env": {},
        }
    },
}

SYSTEM_PROMPT_CONTENT = """You are working in a codebase managed by ctx.
The file .ctx/index.db contains the complete structural and semantic index.

MANDATORY WORKFLOW:
Before editing any file: Call get_context(file=...) to load current metadata.
After editing any file:
  Call update_file(file=..., purpose=..., last_change=...)
  For each modified function: update_function(id=..., summary=..., ...)
  Call log_session(entry="What I did. What's next.")
"""


def _detect_mcp_json_format(path: Path) -> str | None:
    """Detect which format the existing .mcp.json uses: 'claude' or 'cursor'."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            if "mcpServers" in data:
                return "claude"
            if "servers" in data:
                return "cursor"
    except (json.JSONDecodeError, OSError):
        pass
    return None


def run_generate_mcp_config(repo_root: Path) -> None:
    config_path = repo_root / ".mcp.json"
    system_prompt_path = repo_root / ".ctx" / "system-prompt.md"

    if config_path.exists():
        try:
            existing = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(
                f"Error: {config_path} contains invalid JSON.\n"
                "The file should look like:\n"
                f"{json.dumps(MCP_CONFIG_TEMPLATE, indent=2)}\n"
                "Fix or remove the file and try again."
            )
            return

        mcp_servers = existing.get("mcpServers", {})
        if "ctx" in mcp_servers:
            if mcp_servers["ctx"] == MCP_CONFIG_TEMPLATE["mcpServers"]["ctx"]:
                print("already configured.")
            else:
                mcp_servers["ctx"] = MCP_CONFIG_TEMPLATE["mcpServers"]["ctx"]
                existing["mcpServers"] = mcp_servers
                config_path.write_text(
                    json.dumps(existing, indent=2) + "\n", encoding="utf-8"
                )
                print(
                    f"Updated ctx server config in {config_path}.\n"
                    "Existing MCP config preserved."
                )
        else:
            mcp_servers["ctx"] = MCP_CONFIG_TEMPLATE["mcpServers"]["ctx"]
            existing["mcpServers"] = mcp_servers
            config_path.write_text(
                json.dumps(existing, indent=2) + "\n", encoding="utf-8"
            )
            print(f"Added ctx server to existing {config_path}.")
    else:
        config_path.write_text(
            json.dumps(MCP_CONFIG_TEMPLATE, indent=2) + "\n", encoding="utf-8"
        )
        print(f"Written: {config_path}")

    gitignore_path = repo_root / ".gitignore"
    mcp_entry = ".mcp.json"
    if gitignore_path.exists():
        existing_gitignore = gitignore_path.read_text(encoding="utf-8")
        if mcp_entry not in existing_gitignore:
            with open(gitignore_path, "a") as f:
                f.write(f"\n# MCP config — generated per-workspace, user-specific\n{mcp_entry}\n")
            print(f"Added '{mcp_entry}' to {gitignore_path}")
    else:
        gitignore_path.write_text(f"# MCP config — generated per-workspace, user-specific\n{mcp_entry}\n", encoding="utf-8")
        print(f"Created {gitignore_path} with '{mcp_entry}' entry")

    system_prompt_path.parent.mkdir(parents=True, exist_ok=True)
    system_prompt_path.write_text(SYSTEM_PROMPT_CONTENT, encoding="utf-8")
    print(f"Written: {system_prompt_path}")
