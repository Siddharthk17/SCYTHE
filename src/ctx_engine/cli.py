import click
from pathlib import Path
from ctx_engine import __version__
from ctx_engine.commands import run_init, run_status

@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(version=__version__, prog_name="ctx")
def main() -> None:
    """ctx — Auto-updating codebase context engine."""

@main.command(name="init")
@click.option(
    "--repo-root",
    default=".",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    help="Path to the repository root directory."
)
def init_cmd(repo_root: Path) -> None:
    """Initialize or update the codebase index database."""
    try:
        run_init(repo_root.resolve())
    except (FileNotFoundError, ValueError) as err:
        click.echo(f"Error: {err}", err=True)
        raise click.Abort()

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

@main.command(name="validate")
@click.option(
    "--repo-root",
    default=".",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    help="Path to the repository root directory."
)
@click.option(
    "--files",
    multiple=True,
    default=None,
    help="Explicit paths to validate (skip automatic staged-file detection)."
)
def validate_cmd(repo_root: Path, files: tuple[str, ...] | None) -> None:
    """Validate staged file content against the index."""
    from ctx_engine.commands.validate import run_validate
    try:
        file_list = list(files) if files else None
        run_validate(repo_root.resolve(), file_list)
    except (FileNotFoundError, ValueError) as err:
        click.echo(f"Error: {err}", err=True)
        raise click.Abort()

@main.command(name="log-commit")
@click.option(
    "--repo-root",
    default=".",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    help="Path to the repository root directory."
)
@click.option(
    "--hash",
    default="HEAD",
    help="Commit hash to log (default: HEAD)."
)
def log_commit_cmd(repo_root: Path, hash: str) -> None:
    """Record commit metadata in the changes table."""
    from ctx_engine.commands.log_commit import run_log_commit
    try:
        run_log_commit(repo_root.resolve(), hash)
    except (FileNotFoundError, ValueError) as err:
        click.echo(f"Error: {err}", err=True)
        raise click.Abort()

@main.command(name="install-hooks")
@click.option(
    "--repo-root",
    default=".",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    help="Path to the repository root directory."
)
def install_hooks_cmd(repo_root: Path) -> None:
    """Install git hooks for commit-time validation and logging."""
    from ctx_engine.commands.install_hooks import run_install_hooks
    try:
        run_install_hooks(repo_root.resolve())
    except ValueError as err:
        click.echo(f"Error: {err}", err=True)
        raise click.Abort()

@main.command(name="sync")
@click.option(
    "--repo-root",
    default=".",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    help="Path to the repository root directory."
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Preview the sync scope without making API calls."
)
def sync_cmd(repo_root: Path, dry_run: bool) -> None:
    """Reindex and re-summarize — make everything fresh."""
    from ctx_engine.commands.sync import run_sync
    try:
        run_sync(repo_root.resolve(), dry_run)
    except FileNotFoundError as err:
        click.echo(f"Error: {err}", err=True)
        raise click.Abort()

