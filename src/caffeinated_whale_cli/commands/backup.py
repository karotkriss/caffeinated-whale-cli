from typing import NoReturn

import typer

from ..core import backup as core_backup
from ..core.envelope import Status
from ..core.errors import CwcliError
from ..utils import config_utils, db_utils
from ..utils.completion_utils import complete_project_names, complete_site_names
from ..utils.console import console, stderr_console
from ..utils.docker_utils import handle_docker_errors
from ..utils.tips import TipSpinner
from .utils import ensure_containers_running, resolve_bench_path


@handle_docker_errors
def backup(
    project_name: str = typer.Argument(
        ..., help="The Docker Compose project name.", autocompletion=complete_project_names
    ),
    site: str = typer.Option(
        None,
        "--site",
        "-s",
        help="Site name to backup. If not provided, uses the default site "
        "(from common_site_config.json's default_site or sites/currentsite.txt).",
        autocompletion=complete_site_names,
    ),
    bench: str = typer.Option(
        None,
        "--bench",
        help="Which bench to target: its numeric index or label (from 'cwcli inspect').",
    ),
    bench_path: str = typer.Option(
        None,
        "--path",
        "-p",
        help="Explicit bench directory inside the container (lower-level alternative to --bench).",
    ),
    with_files: bool = typer.Option(
        False,
        "--with-files",
        help="Include public and private files in the backup.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Auto-start stopped containers without prompting."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose output."),
):
    """
    Create a backup of a site's database and optionally files.

    This command runs 'bench backup' for the specified site. By default, it backs up
    only the database. Use --with-files to include public and private files.

    If --site is not provided, the default site is used, resolved from either
    common_site_config.json's `default_site` or sites/currentsite.txt.

    Examples:
        cwcli backup my-project
        cwcli backup my-project --site example.com
        cwcli backup my-project --with-files
    """
    # --- Interactive prologue (no spinner): resolve container/bench/site via the
    # CLI wrappers, so any prompt happens BEFORE the spinner (the known
    # spinner-over-questionary deadlock). The core.backup call below is then a
    # straight-through, non-prompting operation. ---

    # Ensure containers are running (auto-start with --yes; prompts otherwise).
    ensure_containers_running(project_name, require_running=True, verbose=verbose, auto_start=yes)

    # Resolve which bench to back up (--bench/--path, else the single bench, else
    # error on ambiguity). Falls back to the default path only when nothing is cached.
    resolved = resolve_bench_path(project_name, bench, bench_path, verbose=verbose)
    if resolved:
        bench_path = resolved
        if verbose:
            stderr_console.print(f"[dim]Using bench path: {bench_path}[/dim]")
    else:
        bench_path = "/workspace/frappe-bench"
        stderr_console.print(
            f"[yellow]Warning:[/yellow] No cached bench path found. Using default: {bench_path}"
        )

    # Get default site if not provided.
    if not site:
        try:
            default_site = db_utils.get_default_site(project_name, bench_path)
        except typer.Exit:
            raise
        except Exception as e:
            stderr_console.print(
                f"[bold red]Error:[/bold red] Failed to retrieve default site: {e}"
            )
            stderr_console.print(
                f"[dim]Tip: Specify --site explicitly or run 'cwcli inspect {project_name}' first.[/dim]"
            )
            raise typer.Exit(code=1) from e

        if default_site:
            site = default_site
            console.print(f"[dim]Using default site: {site}[/dim]")
        else:
            stderr_console.print(
                "[bold red]Error:[/bold red] No site specified and no default site found in config."
            )
            stderr_console.print(
                f"[dim]Tip: Run 'cwcli inspect {project_name}' first, or specify --site explicitly.[/dim]"
            )
            raise typer.Exit(code=1)

    # --- Core call inside the spinner. Everything is pre-resolved, so core.backup
    # runs straight through; the loop only re-invokes on the (rare) confirm_start
    # race, and that prompt runs after the spinner block exits. ---
    show_tips = config_utils.get_show_tips()
    console.print()
    while True:
        try:
            with TipSpinner(
                f"Creating backup for site '{site}'", console=stderr_console, enabled=show_tips
            ):
                result = core_backup.backup(
                    project_name, site=site, bench_path=bench_path, with_files=with_files
                )
        except CwcliError as e:
            _handle_backup_error(e, verbose)

        if (
            result.status is Status.NEEDS_CHOICE
            and result.choice is not None
            and result.choice.kind == "confirm_start"
        ):
            ensure_containers_running(
                project_name, require_running=True, verbose=verbose, auto_start=yes
            )
            continue
        break

    outcome = result.data
    assert outcome is not None  # OK/WARNING always carries a BackupOutcome
    # ponytail: verbose mode no longer echoes bench's own success chatter (the exec now
    # lives in core.backup, which returns a DTO, not raw output); the failure path still
    # surfaces bench output via CwcliError.detail. Re-add a capture channel only if the
    # success chatter is actually wanted.
    console.print()
    console.print(
        f"[bold green]✓[/bold green] Successfully created backup for site '{outcome.site}'"
    )
    if outcome.included_files:
        console.print("[dim]Backup includes database and files[/dim]")
    else:
        console.print("[dim]Backup includes database only[/dim]")
    console.print(
        f"[dim]Backup location: {outcome.bench_path}/sites/{outcome.site}/private/backups/[/dim]"
    )


def _handle_backup_error(e: CwcliError, verbose: bool) -> NoReturn:
    """Render a core backup failure with the historical CLI messages, then Exit(1)."""
    if e.code == "backup.failed":
        output = (e.detail or {}).get("output")
        if output:
            console.print()
            console.print("[dim]Backup output:[/dim]")
            console.print(output)
        console.print()
        stderr_console.print(f"[bold red]✗[/bold red] {e.message}")
        stderr_console.print()
        stderr_console.print("[bold]Common causes:[/bold]")
        stderr_console.print("  • Site not running or database connection issues")
        stderr_console.print("  • Insufficient disk space")
        stderr_console.print("  • Permission issues with backup directory")
        stderr_console.print("  • MariaDB/database service not accessible")
        stderr_console.print()
        stderr_console.print("[dim]Tip: Run with -v flag for detailed error output[/dim]")
        raise typer.Exit(code=1)

    if e.code == "backup_dir.failed":
        stderr_console.print(f"[bold red]Error:[/bold red] {e.message}")
        output = (e.detail or {}).get("output")
        if verbose and output:
            stderr_console.print(output)
        raise typer.Exit(code=1)

    stderr_console.print(f"[bold red]Error:[/bold red] {e.message}")
    raise typer.Exit(code=1)
