import click
import sys
from pathlib import Path
from ctx_engine import __version__
from ctx_engine.commands import run_init, run_status, run_serve, run_generate_mcp_config

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
@click.option(
    "--full",
    is_flag=True,
    help="Show full verbose status including table counts, call graph, and mtime cache."
)
def status_cmd(repo_root: Path, full: bool) -> None:
    """Display statistics and status of the indexed repository."""
    try:
        run_status(repo_root.resolve(), full=full)
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

@main.command(name="serve")
@click.option(
    "--repo-root",
    default=".",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    help="Path to the repository root directory."
)
def serve_cmd(repo_root: Path) -> None:
    """Start the ctx MCP server (stdio transport)."""
    try:
        run_serve(repo_root.resolve())
    except FileNotFoundError as err:
        click.echo(f"Error: {err}", err=True)
        raise click.Abort()

@main.command(name="generate-mcp-config")
@click.option(
    "--repo-root",
    default=".",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    help="Path to the repository root directory."
)
def generate_mcp_config_cmd(repo_root: Path) -> None:
    """Generate a .mcp.json configuration file for Claude Desktop."""
    try:
        run_generate_mcp_config(repo_root.resolve())
    except (FileNotFoundError, FileExistsError) as err:
        click.echo(f"Error: {err}", err=True)
        raise click.Abort()

@main.group(name="watch", invoke_without_command=True)
@click.option(
    "--repo-root",
    default=".",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    help="Path to the repository root directory."
)
@click.option("--with-ollama", is_flag=True, help="Enable local LLM summarization via Ollama.")
@click.option("--daemon", is_flag=True, help="Run in the background as a daemon.")
@click.option("--log-file", default=None, help="Path to the watch log file.")
@click.pass_context
def watch_group(ctx: click.Context, repo_root: Path, with_ollama: bool, daemon: bool, log_file: str | None) -> None:
    """Watch files for changes and update the index automatically."""
    ctx.ensure_object(dict)
    ctx.obj["repo_root"] = repo_root.resolve()
    if ctx.invoked_subcommand is None:
        ctx.invoke(watch_start_cmd, with_ollama=with_ollama, daemon=daemon, log_file=log_file)

@watch_group.command(name="start")
@click.option("--with-ollama", is_flag=True, help="Enable local LLM summarization via Ollama.")
@click.option("--daemon", is_flag=True, help="Run in the background as a daemon.")
@click.option("--log-file", default=None, help="Path to the watch log file.")
@click.pass_context
def watch_start_cmd(ctx: click.Context, with_ollama: bool, daemon: bool, log_file: str | None) -> None:
    """Start the file watcher daemon."""
    from ctx_engine.commands.watch import run_watch
    try:
        run_watch(ctx.obj["repo_root"], with_ollama, daemon, log_file)
    except SystemExit:
        raise
    except Exception as err:
        click.echo(f"Error: {err}", err=True)
        raise click.Abort()

@watch_group.command(name="stop")
@click.pass_context
def watch_stop_cmd(ctx: click.Context) -> None:
    """Stop the file watcher daemon."""
    from ctx_engine.commands.watch import run_watch_stop
    run_watch_stop(ctx.obj["repo_root"])

@watch_group.command(name="status")
@click.pass_context
def watch_status_cmd(ctx: click.Context) -> None:
    """Show the file watcher daemon status."""
    from ctx_engine.commands.watch import run_watch_status
    run_watch_status(ctx.obj["repo_root"])


# ── Week 6 commands ─────────────────────────────────────────────────────────


@main.command(name="export")
@click.option(
    "--repo-root",
    default=".",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    help="Path to the repository root directory.",
)
@click.option("--claude", "targets", flag_value="claude", help="Only generate CLAUDE.md.")
@click.option("--copilot", "targets", flag_value="copilot", help="Only generate copilot-instructions.md.")
@click.option("--opencode", "targets", flag_value="opencode", help="Only generate .ctx/opencode.md.")
@click.option("--all", "all_targets", is_flag=True, default=True, help="Generate all output files (default).")
def export_cmd(repo_root: Path, targets: str | None, all_targets: bool) -> None:
    """Generate output context files (CLAUDE.md, copilot-instructions.md, opencode.md)."""
    from ctx_engine.db import connect
    from ctx_engine.commands.export_cmd import run_export

    db_path = repo_root.resolve() / ".ctx" / "index.db"
    if not db_path.exists():
        click.echo("Error: Database not found. Run 'ctx init' first.", err=True)
        raise click.Abort()

    conn = connect(db_path)
    conn.row_factory = __import__("sqlite3").Row

    if all_targets and targets is None:
        target_set = {"claude", "copilot", "opencode"}
    elif targets:
        target_set = {targets}
    else:
        target_set = {"claude", "copilot", "opencode"}

    try:
        report = run_export(conn, repo_root.resolve(), targets=target_set)
    finally:
        conn.close()

    if report.written:
        click.echo("ctx export")
        click.echo()
        click.echo("  Written:")
        for path in report.written:
            click.echo(f"    {path}")
        click.echo()
        click.echo("  (run 'ctx export' again after 'ctx sync' to keep them current)")
    else:
        click.echo("ctx export")
        click.echo()
        click.echo("  All output files are current (no changes since last export).")


@main.group(name="danger")
def danger_group() -> None:
    """Manage danger zones."""


@danger_group.command(name="add")
@click.argument("scope")
@click.argument("description")
@click.option("--reason", required=True, help="Why this is a danger zone.")
@click.option(
    "--repo-root",
    default=".",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
)
def danger_add_cmd(scope: str, description: str, reason: str, repo_root: Path) -> None:
    """Add a danger zone to the index."""
    from ctx_engine.db import connect
    from ctx_engine.commands.danger_cmd import danger_add

    db_path = repo_root.resolve() / ".ctx" / "index.db"
    if not db_path.exists():
        click.echo("Error: Database not found. Run 'ctx init' first.", err=True)
        raise click.Abort()

    conn = connect(db_path)
    try:
        danger_id = danger_add(conn, scope, description, reason)
    finally:
        conn.close()

    click.echo("ctx danger add")
    click.echo()
    click.echo(f"  Added danger zone: {danger_id}")
    click.echo(f"  Scope: {scope}")
    click.echo(f"  Description: {description}")
    click.echo(f"  Reason: {reason}")
    click.echo("  Added by: human")


@danger_group.command(name="remove")
@click.argument("danger_id")
@click.option("--confirm", is_flag=True, help="Confirm removal of a human-added danger.")
@click.option(
    "--repo-root",
    default=".",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
)
def danger_remove_cmd(danger_id: str, confirm: bool, repo_root: Path) -> None:
    """Remove a danger zone by id."""
    from ctx_engine.db import connect
    from ctx_engine.commands.danger_cmd import danger_remove

    db_path = repo_root.resolve() / ".ctx" / "index.db"
    if not db_path.exists():
        click.echo("Error: Database not found. Run 'ctx init' first.", err=True)
        raise click.Abort()

    conn = connect(db_path)
    try:
        result = danger_remove(conn, danger_id, confirmed=confirm)
    finally:
        conn.close()

    click.echo(f"ctx danger remove {danger_id}")
    click.echo()
    click.echo(f"  {result}")


@danger_group.command(name="list")
@click.option("--scope", default=None, help="Filter by scope.")
@click.option(
    "--repo-root",
    default=".",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
)
def danger_list_cmd(scope: str | None, repo_root: Path) -> None:
    """List all danger zones."""
    from ctx_engine.db import connect
    from ctx_engine.commands.danger_cmd import danger_list

    db_path = repo_root.resolve() / ".ctx" / "index.db"
    if not db_path.exists():
        click.echo("Error: Database not found. Run 'ctx init' first.", err=True)
        raise click.Abort()

    conn = connect(db_path)
    try:
        rows = danger_list(conn, scope=scope)
    finally:
        conn.close()

    if not rows:
        click.echo("No danger zones found.")
        return

    click.echo(f"ctx danger list")
    click.echo()
    click.echo(f"  DANGER ZONES ({len(rows)})")
    click.echo()

    for row in rows:
        if row["scope"] == "*":
            click.echo("  [GLOBAL]")
        else:
            click.echo(f"  [{row['scope']}]")
        tag = row["added_by"]
        click.echo(f"    id: {row['id']}")
        click.echo(f"    {row['description']} ({tag})")
        if row["reason"]:
            click.echo(f"    Reason: {row['reason']}")
        click.echo()


@danger_group.command(name="detect")
@click.option(
    "--repo-root",
    default=".",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
)
@click.option("--dry-run", is_flag=True, help="Preview detected dangers without writing.")
def danger_detect_cmd(repo_root: Path, dry_run: bool) -> None:
    """Auto-detect danger zones from code heuristics."""
    from ctx_engine.db import connect
    from ctx_engine.commands.danger_cmd import danger_detect

    db_path = repo_root.resolve() / ".ctx" / "index.db"
    if not db_path.exists():
        click.echo("Error: Database not found. Run 'ctx init' first.", err=True)
        raise click.Abort()

    conn = connect(db_path)
    try:
        result = danger_detect(conn, repo_root.resolve(), dry_run=dry_run)
    finally:
        conn.close()

    if dry_run:
        click.echo("ctx danger detect --dry-run")
        click.echo()
        click.echo(f"  Would add {len(result['added'])} new danger zones (auto-detected):")
        click.echo(f"  Total detected on current code: {len(result['detected'])}")
        click.echo()
        for d in result["detected"]:
            click.echo(f"  [{d.scope}]")
            click.echo(f"    \"{d.description}\"")
            click.echo()
        click.echo(f"  Would remove {len(result['removed'])} stale auto-detections.")
        click.echo()
        click.echo("  Run without --dry-run to apply.")
    else:
        click.echo("ctx danger detect")
        click.echo()
        click.echo(f"  {len(result['added'])} danger zones added (auto)")
        click.echo(f"  {len(result['removed'])} stale danger zones removed (auto)")
        click.echo()
        click.echo(f"  Total auto-detected: {len(result['detected'])}")


@main.group(name="decision")
def decision_group() -> None:
    """Manage architectural decisions."""


@decision_group.command(name="add")
@click.argument("decision")
@click.option("--scope", default=None, help="Scope of the decision (file or module).")
@click.option("--alternatives", default=None, help="Alternatives rejected.")
@click.option("--reason", required=True, help="Why this decision was made.")
@click.option(
    "--repo-root",
    default=".",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
)
def decision_add_cmd(decision: str, scope: str | None, alternatives: str | None, reason: str, repo_root: Path) -> None:
    """Record an architectural decision."""
    from ctx_engine.db import connect
    from ctx_engine.commands.decision_cmd import decision_add

    db_path = repo_root.resolve() / ".ctx" / "index.db"
    if not db_path.exists():
        click.echo("Error: Database not found. Run 'ctx init' first.", err=True)
        raise click.Abort()

    conn = connect(db_path)
    try:
        decision_id = decision_add(conn, scope, decision, alternatives, reason)
    finally:
        conn.close()

    click.echo("ctx decision add")
    click.echo()
    click.echo(f"  Decision recorded: {decision_id}")
    click.echo(f"  Scope: {scope or 'Global'}")
    click.echo(f"  Decision: {decision}")
    if alternatives:
        click.echo(f"  Alternatives rejected: {alternatives}")
    click.echo(f"  Reason: {reason}")
    click.echo("  Added by: human")


@decision_group.command(name="remove")
@click.argument("decision_id")
@click.option("--confirm", is_flag=True, help="Confirm removal of a human-added decision.")
@click.option(
    "--repo-root",
    default=".",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
)
def decision_remove_cmd(decision_id: str, confirm: bool, repo_root: Path) -> None:
    """Remove a decision by id."""
    from ctx_engine.db import connect
    from ctx_engine.commands.decision_cmd import decision_remove

    db_path = repo_root.resolve() / ".ctx" / "index.db"
    if not db_path.exists():
        click.echo("Error: Database not found. Run 'ctx init' first.", err=True)
        raise click.Abort()

    conn = connect(db_path)
    try:
        result = decision_remove(conn, decision_id, confirmed=confirm)
    finally:
        conn.close()

    click.echo(f"ctx decision remove {decision_id}")
    click.echo()
    click.echo(f"  {result}")


@decision_group.command(name="list")
@click.option("--scope", default=None, help="Filter by scope.")
@click.option(
    "--repo-root",
    default=".",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
)
def decision_list_cmd(scope: str | None, repo_root: Path) -> None:
    """List all architectural decisions."""
    from ctx_engine.db import connect
    from ctx_engine.commands.decision_cmd import decision_list

    db_path = repo_root.resolve() / ".ctx" / "index.db"
    if not db_path.exists():
        click.echo("Error: Database not found. Run 'ctx init' first.", err=True)
        raise click.Abort()

    conn = connect(db_path)
    try:
        rows = decision_list(conn, scope=scope)
    finally:
        conn.close()

    if not rows:
        click.echo("No architectural decisions recorded.")
        return

    click.echo(f"ctx decision list")
    click.echo()
    click.echo(f"  ARCHITECTURAL DECISIONS ({len(rows)})")
    click.echo()

    for row in rows:
        scope_str = row["scope"] or "Global"
        tag = row["added_by"]
        click.echo(f"  [{scope_str}]")
        click.echo(f"    id: {row['id']}")
        click.echo(f"    {row['decision']} ({tag})")
        if row["alternatives"]:
            click.echo(f"    Rejected: {row['alternatives']}")
        click.echo(f"    Because: {row['reason']}")
        click.echo()


@main.command(name="quickstart")
@click.option(
    "--repo-root",
    default=".",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
)
def quickstart_cmd(repo_root: Path) -> None:
    """Print the 5-minute setup guide."""
    from ctx_engine.commands.quickstart import run_quickstart
    run_quickstart(repo_root.resolve())


# ── Week 7 commands ───────────────────────────────────────────────────────────


@main.command(name="doctor")
@click.option(
    "--repo-root",
    default=".",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
)
@click.option("--fix", "apply_fix", is_flag=True, help="Apply auto-fixes for failing checks.")
@click.option("--json", "json_output", is_flag=True, help="Emit a machine-readable JSON report.")
def doctor_cmd(repo_root: Path, apply_fix: bool, json_output: bool) -> None:
    """Run a comprehensive health check of the index, hooks, and environment."""
    from ctx_engine.commands.doctor import run_doctor
    try:
        exit_code = run_doctor(repo_root.resolve(), apply_fix=apply_fix, json_output=json_output)
    except Exception as err:
        click.echo(f"Error: {err}", err=True)
        raise click.Abort()
    # Use sys.exit so the exit code is preserved without click.Abort() noise.
    import sys
    sys.exit(exit_code)


@main.command(name="diff")
@click.option(
    "--repo-root",
    default=".",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
)
@click.argument("commit1", required=False)
@click.argument("commit2", required=False)
def diff_cmd(repo_root: Path, commit1: str | None, commit2: str | None) -> None:
    """Show what changed in the index.

    With no arguments: compares on-disk code against the indexed state.
    With two commits: shows what the changes table records between them.
    """
    from ctx_engine.commands.diff_cmd import run_diff
    try:
        run_diff(repo_root.resolve(), commit1, commit2)
    except (FileNotFoundError, ValueError) as err:
        click.echo(f"Error: {err}", err=True)
        raise click.Abort()


@main.command(name="ci")
@click.option(
    "--repo-root",
    default=".",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
)
@click.option("--json", "json_output", is_flag=True, help="Emit a machine-readable JSON report.")
@click.option(
    "--output-workflow", is_flag=True,
    help="Write a reusable .github/workflows/ctx-validate.yml file.",
)
def ci_cmd(repo_root: Path, json_output: bool, output_workflow: bool) -> None:
    """CI-mode validation: check that changed files are indexed and logged."""
    from ctx_engine.commands.ci_cmd import run_ci
    try:
        exit_code = run_ci(repo_root.resolve(), json_output=json_output, output_workflow=output_workflow)
    except (FileNotFoundError, ValueError) as err:
        click.echo(f"Error: {err}", err=True)
        raise click.Abort()
    if exit_code != 0 and not json_output:
        raise click.Abort()


@main.command(name="explain")
@click.argument("target")
@click.option(
    "--repo-root",
    default=".",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
)
@click.option(
    "--depth", type=click.Choice(["function", "file", "system"]),
    default=None,
    help="Force a specific explanation depth. Defaults are inferred from the target.",
)
def explain_cmd(target: str, repo_root: Path, depth: str | None) -> None:
    """Generate a deep, structured explanation of a file, function, or system."""
    from ctx_engine.commands.explain_cmd import run_explain
    try:
        run_explain(repo_root.resolve(), target, depth=depth)
    except (FileNotFoundError, ValueError) as err:
        click.echo(f"Error: {err}", err=True)
        raise click.Abort()

