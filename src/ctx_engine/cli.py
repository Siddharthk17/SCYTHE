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
