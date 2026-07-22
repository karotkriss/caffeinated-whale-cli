"""``cwcli rm-site`` - the human frontend over ``core.drop_site``.

A dedicated, noun-scoped verb (not a ``--site`` flag on ``rm``): dropping ONE
site leaves the instance running, while ``cwcli rm`` destroys the whole
instance, and hiding that difference behind a flag on the bigger command is a
footgun (forgetting the flag nukes the instance instead of one site). See
``core/rm_site.py``'s module docstring for the full rationale and the archive
handling this command discloses.

This module only prompts, resolves the bench, streams the command's own output,
and renders the outcome; the resolution, the ``bench drop-site`` exec, and the
archive relocation live in ``core.drop_site``.
"""

import sys

import typer

from ..core import rm_site as core_rm_site
from ..core.envelope import Status
from ..core.errors import CwcliError
from ..core.rm_site import DropSiteCommand, DropSiteNotice, DropSiteOutput
from ..utils import cache
from ..utils.completion_utils import complete_project_names, complete_site_names
from ..utils.console import console, stderr_console
from ..utils.docker_utils import handle_docker_errors
from .utils import confirm_or_exit, ensure_containers_running, resolve_bench_path


def _make_renderer(*, verbose: bool):
    def on_event(event) -> None:
        if isinstance(event, DropSiteCommand):
            if verbose:
                stderr_console.print(f"[dim]$ {event.command}[/dim]")
        elif isinstance(event, DropSiteOutput):
            sys.stdout.write(event.text)
            sys.stdout.flush()
        elif isinstance(event, DropSiteNotice):
            console.print(f"[dim]{event.text}[/dim]")

    return on_event


def _exit_on_exec_error(e: CwcliError):
    stderr_console.print(f"[bold red]Error:[/bold red] {e.message}")
    if e.hint:
        stderr_console.print(f"[dim]{e.hint}[/dim]")
    raise typer.Exit(code=1) from e


@handle_docker_errors
def rm_site(
    project_name: str = typer.Argument(
        ..., help="The Docker Compose project name.", autocompletion=complete_project_names
    ),
    site: str = typer.Argument(
        ..., help="The site to permanently drop.", autocompletion=complete_site_names
    ),
    bench: str = typer.Option(
        None, "--bench", help="Which bench to target: its numeric index or label."
    ),
    bench_path: str = typer.Option(
        None, "--path", "-p", help="Explicit bench directory (lower-level alternative to --bench)."
    ),
    db_root_password: str = typer.Option(
        "123", "--db-root-password", help="MariaDB root password used by 'bench drop-site'."
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the destructive confirmation and auto-start containers.",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose output."),
):
    """Permanently drop ONE site from a running bench (destructive).

    Runs 'bench drop-site --force', which deletes the site's database and its
    files. The instance and every OTHER site on it keep running - this never
    touches containers, volumes, or the project directory (use 'cwcli rm' to
    remove the whole instance).

    'bench drop-site' archives the dropped site's full directory - including
    site_config.json (its database credentials and, if set, its encryption
    key) - inside the container's own archived/sites/ folder before this
    command ever sees it.
    Left there it would grow, unpruned, forever. So this command immediately
    copies that one archive out to ~/.cwcli/archive/{project}_dropped_sites/ (or
    $CWCLI_HOME/archive/... if that override is set) - the same managed location
    'cwcli rm' already uses - and, only once that copy is verified on disk,
    deletes the in-container copy. If the copy cannot be verified, the
    in-container archive is left in place rather than deleted unbacked, and this
    command exits non-zero so the leftover credentials are never silently missed.

    Examples:
        cwcli rm-site my-project task-42.localhost
        cwcli rm-site my-project task-42.localhost --bench 1 --yes
    """
    ensure_containers_running(project_name, require_running=True, verbose=verbose, auto_start=yes)

    resolved = resolve_bench_path(project_name, bench, bench_path, verbose=verbose)
    if resolved:
        bench_path = resolved
        if verbose:
            stderr_console.print(f"[dim]Using bench path: {bench_path}[/dim]")

    on_event = _make_renderer(verbose=verbose)

    def _drop(consent: bool):
        try:
            return core_rm_site.drop_site(
                project_name,
                site,
                bench_path=bench_path,
                consent=consent,
                db_root_password=db_root_password,
                on_event=on_event,
            )
        except CwcliError as e:
            _exit_on_exec_error(e)

    started = False
    result = _drop(False)
    while (
        result.status is Status.NEEDS_CHOICE
        and result.choice is not None
        and result.choice.kind == "confirm_start"
    ):
        # Re-invoke at most ONCE after an attempted start (the `unlock` race
        # backstop): a second confirm_start after ensure_containers_running
        # already claimed success means the start didn't take.
        if started:
            stderr_console.print(
                f"[bold red]Error:[/bold red] Frappe container for project "
                f"'{project_name}' failed to start."
            )
            raise typer.Exit(code=1)
        ensure_containers_running(
            project_name, require_running=True, verbose=verbose, auto_start=yes
        )
        started = True
        result = _drop(False)

    if (
        result.status is Status.NEEDS_CHOICE
        and result.choice is not None
        and result.choice.kind == "confirm_drop_site"
    ):
        confirm_or_exit(
            result.choice.prompt,
            assume_yes=yes,
            refuse_message=(
                "Dropping a site is destructive and no confirmation was given. "
                "Pass --yes to proceed."
            ),
        )
        console.print(f"[bold red]Dropping site '{site}'...[/bold red]")
        result = _drop(True)

    if result.status is Status.NEEDS_CHOICE and result.choice is not None:
        # A resolver fork not handled above (e.g. select_bench, if the cache
        # changed under us): report rather than guess.
        stderr_console.print(f"[bold red]Error:[/bold red] {result.choice.prompt}")
        raise typer.Exit(code=1)

    outcome = result.data
    assert outcome is not None  # OK/WARNING always carries a DropSiteOutcome

    if not cache.recache_project(project_name, verbose=verbose):
        stderr_console.print(
            f"[yellow]Warning:[/yellow] site dropped, but re-caching '{project_name}' failed; "
            "run 'cwcli inspect --update' to refresh."
        )

    for warning in result.warnings:
        stderr_console.print(f"[yellow]Warning:[/yellow] {warning.text}")

    console.print(f"[bold green]✓[/bold green] Site '{outcome.site}' dropped.")
    if outcome.archived_host_path and outcome.archive_pruned_in_container:
        console.print(
            f"[dim]Its archive (site_config.json and any credentials it holds) was "
            f"copied to {outcome.archived_host_path} and removed from the container.[/dim]"
        )
    elif outcome.archived_host_path:
        console.print(
            f"[yellow]Its archive was copied to {outcome.archived_host_path}, but the "
            "in-container copy could not be removed; see the warning above.[/yellow]"
        )
    else:
        console.print(
            "[yellow]Its archive could not be safely copied out of the container; see the "
            "warning above.[/yellow]"
        )

    if not outcome.ok:
        raise typer.Exit(code=1)
