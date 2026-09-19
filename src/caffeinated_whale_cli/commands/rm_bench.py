"""``cwcli rm-bench`` - the human frontend over ``core.remove_bench``.

A dedicated, noun-scoped verb (not a ``--bench`` flag on ``rm``): removing ONE
bench drops its sites and deletes its directory while every sibling bench and
container keeps running, whereas ``cwcli rm`` destroys the whole instance, and
hiding that difference behind a flag on the bigger command is a footgun. See
``core/rm_bench.py``'s module docstring for the full rationale, the "refuse while
running" gate, and the per-site backup-then-drop the deletion is built from.

This module only prompts, resolves the bench, streams each site drop's own
output, and renders the outcome; the resolution, the running gate, the
``bench drop-site`` fan-out, and the directory deletion live in the core.
"""

import sys

import typer

from ..core import rm_bench as core_rm_bench
from ..core.envelope import Status
from ..core.errors import CwcliError
from ..core.rm_bench import (
    BenchRmCommand,
    BenchRmNotice,
    BenchRmOutput,
    BenchRmStep,
    BenchRmWarning,
)
from ..utils import cache
from ..utils.completion_utils import complete_project_names
from ..utils.console import console, stderr_console
from ..utils.docker_utils import handle_docker_errors
from .utils import confirm_or_exit, ensure_containers_running, resolve_bench_path


def _make_renderer(*, verbose: bool):
    def on_event(event) -> None:
        if isinstance(event, BenchRmStep):
            if verbose:
                stderr_console.print(f"[dim]{event.label}[/dim]")
        elif isinstance(event, BenchRmCommand):
            if verbose:
                stderr_console.print(f"[dim]$ {event.command}[/dim]")
        elif isinstance(event, BenchRmOutput):
            sys.stdout.write(event.text)
            sys.stdout.flush()
        elif isinstance(event, BenchRmNotice):
            console.print(f"[dim]{event.text}[/dim]")
        elif isinstance(event, BenchRmWarning):
            stderr_console.print(f"[yellow]Warning:[/yellow] {event.text}")
            if event.hint:
                stderr_console.print(f"[dim]{event.hint}[/dim]")

    return on_event


def _exit_on_error(e: CwcliError):
    stderr_console.print(f"[bold red]Error:[/bold red] {e.message}")
    if e.hint:
        stderr_console.print(f"[dim]{e.hint}[/dim]")
    raise typer.Exit(code=1) from e


@handle_docker_errors
def rm_bench(
    project_name: str = typer.Argument(
        ..., help="The Docker Compose project name.", autocompletion=complete_project_names
    ),
    bench: str = typer.Option(
        None, "--bench", help="Which bench to remove: its numeric index or label."
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
        help="Skip the destructive confirmation and auto-start the container.",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose output."),
):
    """Permanently remove ONE bench from an instance (destructive).

    Backs up and drops every site on the target bench (deleting each site's
    database and files), then deletes the bench directory. Every OTHER bench and
    every container keep running - this never touches the instance's containers,
    named volumes, or the project directory (use 'cwcli rm' to remove the whole
    instance).

    A bench that is still running is refused: stop it first with
    'cwcli stop <project> --bench <selector>'. Each site's backup is copied out
    to ~/.cwcli/archive/<project>_dropped_sites/ (or $CWCLI_HOME/archive/... if
    that override is set) and verified on disk before the bench directory is
    deleted; if a backup cannot be copied out, the directory is kept so the
    backup is never destroyed, and this command exits non-zero.

    Examples:
        cwcli rm-bench my-project --bench 2
        cwcli rm-bench my-project --bench staging --yes
    """
    ensure_containers_running(project_name, require_running=True, verbose=verbose, auto_start=yes)

    resolved = resolve_bench_path(project_name, bench, bench_path, verbose=verbose)
    if resolved:
        bench_path = resolved
        if verbose:
            stderr_console.print(f"[dim]Using bench path: {bench_path}[/dim]")

    on_event = _make_renderer(verbose=verbose)

    def _remove(consent: bool):
        try:
            return core_rm_bench.remove_bench(
                project_name,
                bench_path=bench_path,
                consent=consent,
                db_root_password=db_root_password,
                on_event=on_event,
            )
        except CwcliError as e:
            _exit_on_error(e)

    result = _remove(False)

    if (
        result.status is Status.NEEDS_CHOICE
        and result.choice is not None
        and result.choice.kind == "confirm_remove_bench"
    ):
        confirm_or_exit(
            result.choice.prompt,
            assume_yes=yes,
            refuse_message=(
                "Removing a bench is destructive and no confirmation was given. "
                "Pass --yes to proceed."
            ),
        )
        console.print(f"[bold red]Removing bench '{bench_path}'...[/bold red]")
        result = _remove(True)

    if result.status is Status.NEEDS_CHOICE and result.choice is not None:
        # A resolver fork not handled above (e.g. select_bench, if the cache
        # changed under us): report rather than guess.
        stderr_console.print(f"[bold red]Error:[/bold red] {result.choice.prompt}")
        raise typer.Exit(code=1)

    outcome = result.data
    assert outcome is not None  # OK/WARNING always carries a BenchRemovalOutcome

    # Removing a bench changes the instance's bench set, so refresh the cache.
    # A failed recache is a warning, not a non-zero exit: the removal already
    # happened, and failing here would make a retry act on a bench that is gone.
    if not cache.recache_project(project_name, verbose=verbose):
        stderr_console.print(
            f"[yellow]Warning:[/yellow] bench removed, but re-caching '{project_name}' failed; "
            "run 'cwcli inspect --update' to refresh."
        )

    for warning in result.warnings:
        stderr_console.print(f"[yellow]Warning:[/yellow] {warning.text}")

    if outcome.dir_removed:
        dropped = len(outcome.sites_dropped)
        console.print(
            f"[bold green]✓[/bold green] Bench '{outcome.bench_path}' removed "
            f"({dropped} site(s) backed up and dropped)."
        )
        if outcome.archived_host_paths:
            console.print(
                "[dim]Each dropped site's backup archive was copied to "
                f"{outcome.archived_host_paths[0].rsplit('/', 1)[0]}/.[/dim]"
            )
    else:
        stderr_console.print(
            f"[yellow]Bench '{outcome.bench_path}' was not fully removed; "
            "see the warnings above.[/yellow]"
        )

    if not outcome.ok:
        raise typer.Exit(code=1)
