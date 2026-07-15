from typing import NoReturn

import typer

from ..core import unlock as core_unlock
from ..core.envelope import Status
from ..core.errors import CwcliError
from ..utils.completion_utils import complete_project_names, complete_site_names
from ..utils.console import console, stderr_console
from ..utils.docker_utils import handle_docker_errors
from .utils import ensure_containers_running, resolve_bench_path


@handle_docker_errors
def unlock(
    project_name: str = typer.Argument(
        ..., help="The Docker Compose project name.", autocompletion=complete_project_names
    ),
    site: str = typer.Option(
        None,
        "--site",
        "-s",
        help="Site name to unlock. If not provided, uses the default site "
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
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Auto-start stopped containers without prompting."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose output."),
):
    """
    Remove the locks folder for a specified site to unlock it.

    This command removes the {bench_path}/sites/{site_name}/locks directory,
    which can help resolve issues when a site is stuck in a locked state.

    If --site is not provided, the default site is used, resolved from either
    common_site_config.json's `default_site` or sites/currentsite.txt.

    Examples:
        cwcli unlock my-project --site example.com
        cwcli unlock my-project  # Uses default site
    """
    # Pre-resolve the interactive forks BEFORE the spinner, mirroring `backup`: a
    # questionary prompt under a rich spinner deadlocks.
    ensure_containers_running(project_name, require_running=True, verbose=verbose, auto_start=yes)

    resolved = resolve_bench_path(project_name, bench, bench_path, verbose=verbose)
    if resolved:
        bench_path = resolved
        if verbose:
            stderr_console.print(f"[dim]Using bench path: {bench_path}[/dim]")

    started = False
    while True:
        try:
            with console.status(
                f"[bold green]Unlocking site '{site or 'default'}'...[/bold green]", spinner="dots"
            ):
                result = core_unlock.unlock(
                    project_name, site=site, bench=bench, bench_path=bench_path
                )
        except CwcliError as e:
            _handle_unlock_error(e, verbose)

        if (
            result.status is Status.NEEDS_CHOICE
            and result.choice is not None
            and result.choice.kind == "confirm_start"
        ):
            # Re-invoke at most ONCE after an attempted start. A second confirm_start
            # after ensure_containers_running already claimed success means the start
            # didn't take (crash-loop / teardown race) - fail closed, don't spin.
            if started:
                stderr_console.print(
                    "[bold red]Error:[/bold red] Frappe container for project "
                    f"'{project_name}' failed to start."
                )
                raise typer.Exit(code=1)
            ensure_containers_running(
                project_name, require_running=True, verbose=verbose, auto_start=yes
            )
            started = True
            continue
        break

    if result.status is Status.NEEDS_CHOICE and result.choice is not None:
        # select_bench: resolve_bench_path above already errors on a multi-bench
        # project with no selector, so reaching here means the cache changed under
        # us. Report rather than guess a bench.
        stderr_console.print(f"[bold red]Error:[/bold red] {result.choice.prompt}")
        raise typer.Exit(code=1)

    outcome = result.data
    assert outcome is not None  # OK/WARNING always carries an UnlockOutcome

    for warning in result.warnings:
        if warning.code == "bench.default_used":
            stderr_console.print(f"[yellow]Warning:[/yellow] {warning.text}")
        elif warning.code == "default_site.resolved":
            console.print(f"[dim]{warning.text}[/dim]")

    if verbose:
        stderr_console.print(f"[dim]$ rm -rfv {outcome.locks_path}[/dim]")
        # Plain stdout, not `console.print`: these are rm's own output lines, and a
        # rich console would wrap a long locks path at the terminal width and
        # highlight it. The streamed implementation wrote them raw to stdout, so
        # this keeps them byte-comparable - only the timing changed (at completion,
        # from the structured `removed` list, rather than chunk by chunk).
        for path in outcome.removed:
            typer.echo(f"removed '{path}'")

    if outcome.already_unlocked:
        console.print(f"[bold green]✓[/bold green] Site '{outcome.site}' is already unlocked")
        console.print(f"[dim]No locks folder at: {outcome.locks_path}[/dim]")
    else:
        console.print(f"[bold green]✓[/bold green] Successfully unlocked site '{outcome.site}'")
        console.print(f"[dim]Removed locks folder: {outcome.locks_path}[/dim]")


def _handle_unlock_error(e: CwcliError, verbose: bool) -> NoReturn:
    """Render a core unlock failure with the historical CLI messages, then Exit(1)."""
    if e.code == "unlock.failed":
        stderr_console.print(f"[bold red]✗[/bold red] {e.message}")
        output = (e.detail or {}).get("output")
        if verbose and output:
            stderr_console.print(output)
        raise typer.Exit(code=1)

    if e.code in ("site.no_default", "default_site.error"):
        stderr_console.print(f"[bold red]Error:[/bold red] {e.message}")
        stderr_console.print(
            "[dim]Tip: Specify --site explicitly, or run 'cwcli inspect' first.[/dim]"
        )
        raise typer.Exit(code=1)

    stderr_console.print(f"[bold red]Error:[/bold red] {e.message}")
    raise typer.Exit(code=1)
