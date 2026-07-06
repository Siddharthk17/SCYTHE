import click
from pathlib import Path
from ctx_engine.commands import run_init, run_status

@click.group()
def main() -> None:
    """ctx — Codebase context engine CLI."""
    pass

@main.command(name="init")
@click.option(
    "--repo-root",
    default=".",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    help="Path to the repository root directory."
)
def init_cmd(repo_root: Path) -> None:
    """Initialize or update the codebase index database."""
    run_init(repo_root.resolve())

@main.command(name="status")
@click.option(
    "--repo-root",
    default=".",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    help="Path to the repository root directory."
)
def status_cmd(repo_root: Path) -> None:
    """Display statistics and status of the indexed repository."""
    try:
        run_status(repo_root.resolve())
    except FileNotFoundError as err:
        click.echo(f"Error: {err}", err=True)
        raise click.Abort()

@main.command(name="summarize")
@click.option(
    "--repo-root",
    default=".",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    help="Path to the repository root directory."
)
@click.option(
    "--batch-size",
    default=20,
    type=int,
    help="Number of files to process per LLM batch."
)
@click.option(
    "--force",
    is_flag=True,
    help="Force re-summarization of all files and functions."
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Preview the summarization scope and estimated cost without making API calls."
)
def summarize_cmd(repo_root: Path, batch_size: int, force: bool, dry_run: bool) -> None:
    """Bulk summarize files and functions needing updates."""
    from ctx_engine.commands.summarize import run_summarize
    try:
        run_summarize(repo_root.resolve(), batch_size, force, dry_run)
    except FileNotFoundError as err:
        click.echo(f"Error: {err}", err=True)
        raise click.Abort()

@main.command(name="update")
@click.argument("path", type=click.Path(exists=True))
@click.option(
    "--repo-root",
    default=".",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    help="Path to the repository root directory."
)
def update_cmd(path: Path, repo_root: Path) -> None:
    """Reindex and unconditionally summarize a single file."""
    from ctx_engine.commands.update import run_update
    try:
        run_update(repo_root.resolve(), str(path))
    except FileNotFoundError as err:
        click.echo(f"Error: {err}", err=True)
        raise click.Abort()

