import logging
import sqlite3
from pathlib import Path

import anyio
from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent, CallToolResult, ServerCapabilities, ToolsCapability

from ctx_engine.db.connection import connect
from ctx_engine.mcp_server.tools.read_tools import (
    handle_get_context,
    handle_get_function,
    handle_search,
    handle_get_dangers,
    handle_get_decisions,
    handle_get_callers,
    handle_get_tainted,
    handle_ctx_status,
)
from ctx_engine.mcp_server.tools.write_tools import (
    handle_add_danger,
    handle_remove_danger,
    handle_add_decision,
    handle_log_session,
    handle_log_change,
    handle_mark_tainted,
    handle_clear_taint,
    handle_update_summary,
    handle_update_file,
    handle_update_function,
    handle_plan,
)

logger = logging.getLogger("ctx.mcp")

TOOLS: list[Tool] = [
    Tool(
        name="get_context",
        description="Retrieve assembled context for a file: project info, directory tree, dangers, decisions, file summary, dependency summaries, and call graph. Returns a multi-zone document (Zone 3/0/1/2) pruned to fit an 8000-token budget.",
        inputSchema={
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Relative file path in the repo (e.g. 'src/main.py')"},
                "line": {"type": "integer", "description": "Optional target line number to prioritize nearby functions"},
            },
            "required": ["file"],
        },
    ),
    Tool(
        name="get_function",
        description="Retrieve the full record for a specific function by its ID, including signature, summary, danger flags, source code, and staleness warnings.",
        inputSchema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Function ID (e.g. 'src/main.py:main' or an auto-assigned ID)"},
            },
            "required": ["id"],
        },
    ),
    Tool(
        name="search",
        description="Full-text search across file purposes/summaries and function summaries/signatures using FTS5 (or LIKE fallback). Returns ranked matches.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query (FTS5 syntax or plain text)"},
                "limit": {"type": "integer", "description": "Max results (default 10, max 50)"},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="get_dangers",
        description="List all recorded danger zones, optionally filtered by file scope. Shows descriptions and reasons.",
        inputSchema={
            "type": "object",
            "properties": {
                "scope": {"type": "string", "description": "Optional file path to filter dangers by scope"},
            },
        },
    ),
    Tool(
        name="get_decisions",
        description="List recorded architectural decisions, optionally filtered by scope. Shows the decision, rejected alternatives, and rationale.",
        inputSchema={
            "type": "object",
            "properties": {
                "scope": {"type": "string", "description": "Optional scope to filter decisions"},
            },
        },
    ),
    Tool(
        name="get_callers",
        description="Find all functions that call a given function. Returns caller ID, signature, and file location.",
        inputSchema={
            "type": "object",
            "properties": {
                "function_id": {"type": "string", "description": "ID of the callee function"},
            },
            "required": ["function_id"],
        },
    ),
    Tool(
        name="get_tainted",
        description="List all functions currently marked as tainted (dependent on changed code). Optionally filter by file.",
        inputSchema={
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Optional file path to filter tainted functions"},
            },
        },
    ),
    Tool(
        name="ctx_status",
        description="Overview of the ctx index: file/function counts, staleness, taint queue, last recorded commit, hook status, and MCP configuration presence.",
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    Tool(
        name="update_file",
        description="Update record for an indexed file. Writes semantic metadata (purpose, summary, danger, last_change, system). Resets confidence to 1.0, clears stale flag. Does NOT reindex.",
        inputSchema={
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Relative file path in the repo"},
                "purpose": {"type": "string", "description": "File purpose description"},
                "summary": {"type": "string", "description": "Short summary of the file"},
                "danger": {"type": "string", "description": "Danger information about the file"},
                "last_change": {"type": "string", "description": "Description of the last change"},
                "system": {"type": "string", "description": "System/layer classification"},
            },
            "required": ["file"],
        },
    ),
    Tool(
        name="update_function",
        description="Update record for an indexed function. Writes summary, summary_long, danger, mutates. Clears taint and stale flags, resets confidence to 1.0. Does NOT reindex.",
        inputSchema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Function ID (e.g. 'src/main.py:main')"},
                "summary": {"type": "string", "description": "Short function summary"},
                "summary_long": {"type": "string", "description": "Extended function description"},
                "danger": {"type": "string", "description": "Danger information"},
                "mutates": {"type": "array", "items": {"type": "string"}, "description": "List of things this function mutates"},
            },
            "required": ["id"],
        },
    ),
    Tool(
        name="add_danger",
        description="Record a new danger zone. Writes to the dangers table with a deterministic ID. Does NOT reindex.",
        inputSchema={
            "type": "object",
            "properties": {
                "scope": {"type": "string", "description": "Scope: '*' (global, default) or a file path"},
                "description": {"type": "string", "description": "Description of the danger"},
                "reason": {"type": "string", "description": "Why this is dangerous"},
            },
            "required": ["description"],
        },
    ),
    Tool(
        name="remove_danger",
        description="Remove a danger zone by ID. Only model-added dangers can be removed via MCP. Human-added dangers require the CLI.",
        inputSchema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "ID of the danger zone to remove"},
            },
            "required": ["id"],
        },
    ),
    Tool(
        name="add_decision",
        description="Record an architectural decision. Writes to the decisions table with a deterministic ID. Does NOT reindex.",
        inputSchema={
            "type": "object",
            "properties": {
                "scope": {"type": "string", "description": "Scope: '*' (global, default) or a file path"},
                "decision": {"type": "string", "description": "What was decided"},
                "alternatives": {"type": "string", "description": "Alternatives that were rejected"},
                "reason": {"type": "string", "description": "Rationale for the decision"},
            },
            "required": ["decision"],
        },
    ),
    Tool(
        name="log_session",
        description="Log a session history entry to the session_log table. Enforces a rolling 10-entry cap. Does NOT reindex.",
        inputSchema={
            "type": "object",
            "properties": {
                "entry": {"type": "string", "description": "Session log entry text"},
                "files_touched": {"type": "array", "items": {"type": "string"}, "description": "List of files affected"},
            },
            "required": ["entry"],
        },
    ),
    Tool(
        name="log_change",
        description="Log a change record for a file to the changes table. Sets author='model'. Enforces a rolling 20-per-file cap. Does NOT reindex.",
        inputSchema={
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Relative file path that was changed"},
                "summary": {"type": "string", "description": "One-sentence description of the change"},
            },
            "required": ["file", "summary"],
        },
    ),
    Tool(
        name="mark_tainted",
        description="Mark a specific function as tainted and add it to the taint_queue. Does NOT reindex.",
        inputSchema={
            "type": "object",
            "properties": {
                "function_id": {"type": "string", "description": "ID of the function to mark as tainted"},
                "taint_source": {"type": "string", "description": "Description of what changed to cause the taint"},
            },
            "required": ["function_id", "taint_source"],
        },
    ),
    Tool(
        name="clear_taint",
        description="Clear the tainted flag from a function after review. Removes from taint_queue. Does NOT reindex.",
        inputSchema={
            "type": "object",
            "properties": {
                "function_id": {"type": "string", "description": "ID of the function to clear taint on"},
            },
            "required": ["function_id"],
        },
    ),
    Tool(
        name="update_summary",
        description="Update summary for a file or function. Resets confidence to 1.0, clears stale/taint. Does NOT reindex.",
        inputSchema={
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["file", "function"], "description": "Target type: 'file' (default) or 'function'"},
                "id": {"type": "string", "description": "File path or function ID to update"},
                "summary": {"type": "string", "description": "Short summary text"},
                "summary_long": {"type": "string", "description": "Optional extended description"},
            },
            "required": ["id", "summary"],
        },
    ),
    Tool(
        name="plan",
        description="Log a development plan to the session history. Does NOT reindex.",
        inputSchema={
            "type": "object",
            "properties": {
                "plan": {"type": "string", "description": "The development plan description"},
                "files_touched": {"type": "array", "items": {"type": "string"}, "description": "List of files to be affected"},
            },
            "required": ["plan"],
        },
    ),
]

READ_TOOL_NAMES = {
    "get_context", "get_function", "search", "get_dangers",
    "get_decisions", "get_callers", "get_tainted", "ctx_status",
}
WRITE_TOOL_NAMES = {
    "add_danger", "remove_danger", "add_decision", "log_session",
    "log_change", "mark_tainted", "clear_taint", "update_summary",
    "update_file", "update_function", "plan",
}

HANDLERS: dict[str, callable] = {
    "get_context": handle_get_context,
    "get_function": handle_get_function,
    "search": handle_search,
    "get_dangers": handle_get_dangers,
    "get_decisions": handle_get_decisions,
    "get_callers": handle_get_callers,
    "get_tainted": handle_get_tainted,
    "ctx_status": handle_ctx_status,
    "add_danger": handle_add_danger,
    "remove_danger": handle_remove_danger,
    "add_decision": handle_add_decision,
    "log_session": handle_log_session,
    "log_change": handle_log_change,
    "mark_tainted": handle_mark_tainted,
    "clear_taint": handle_clear_taint,
    "update_summary": handle_update_summary,
    "update_file": handle_update_file,
    "update_function": handle_update_function,
    "plan": handle_plan,
}


def create_server(repo_root: Path) -> Server:
    app = Server("ctx-mcp")

    db_path = repo_root / ".ctx" / "index.db"

    @app.list_tools()
    async def list_tools() -> list[Tool]:
        return TOOLS

    @app.call_tool()
    async def call_tool(name: str, arguments: dict | None) -> CallToolResult:
        if arguments is None:
            arguments = {}

        handler = HANDLERS.get(name)
        if handler is None:
            return CallToolResult(
                content=[TextContent(type="text", text=f"Unknown tool: {name}")],
                isError=True,
            )

        conn = connect(db_path)
        try:
            result_text = handler(conn, repo_root, arguments)
            return CallToolResult(content=[TextContent(type="text", text=result_text)])
        except Exception as err:
            if name in WRITE_TOOL_NAMES:
                raise
            logger.exception("Tool %s failed: %s", name, err)
            return CallToolResult(
                content=[TextContent(type="text", text=f"Error executing {name}: {err}")],
                isError=True,
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass

    return app


async def run_server(repo_root: Path) -> None:
    app = create_server(repo_root)
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="ctx-mcp",
                server_version="2.8.8.0",
                instructions="MCP server for ctx-codebase — query and update your indexed codebase context.",
                capabilities=ServerCapabilities(tools=ToolsCapability()),
            ),
        )
