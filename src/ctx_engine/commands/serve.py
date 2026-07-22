import anyio
from pathlib import Path


async def run_server_async(repo_root: Path) -> None:
    from ctx_engine.mcp_server.server import run_server
    await run_server(repo_root)


def run_serve(repo_root: Path) -> None:
    db_path = repo_root / ".ctx" / "index.db"
    if not db_path.exists():
        raise FileNotFoundError("Database not found. Please run 'ctx init' first.")

    anyio.run(run_server_async, repo_root)
