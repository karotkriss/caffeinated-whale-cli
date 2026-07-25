"""The ``label`` command: assign, clear, or list per-bench user labels.

Thin human-CLI frontend over ``core.label``. It owns only the typer signature,
the ``rich`` rendering, and the honest exit codes; the bench resolution, the
label rules, the container gate, and the two-store write all live in the UI-pure
core. See ``core/label.py`` for the marker-before-cache ordering that makes
``--clear`` safe, and ``utils/bench_labels.py`` for the label model and marker
format.
"""

from typing import NoReturn

import typer

from ..core import label as core_label
from ..core import resolvers
from ..core.envelope import Status
from ..core.errors import CwcliError
from ..utils.completion_utils import complete_project_names
from ..utils.console import console, stderr_console
from ..utils.docker_utils import handle_docker_errors


def _state_part(state: str) -> str:
    """The per-row existence marker. ``present`` is unmarked - the normal case is
    the quiet one, and only a row that cannot be trusted earns ink."""
    if state == resolvers.BENCH_ABSENT:
        return "  [bold red](GONE - directory no longer exists)[/bold red]"
    if state == resolvers.BENCH_UNVERIFIED:
        return "  [yellow](cached, not verified)[/yellow]"
    return ""


def _bench_line(bench) -> str:
    label_part = (
        f" [magenta]'{bench.label}'[/magenta]" if bench.label else " [dim](no label)[/dim]"
    )
    return (
        f"  [cyan]\\[{bench.index}][/cyan]{label_part}  {bench.path}"
        f"{_state_part(bench.state)}"
    )


def _print_bench_list(project_name: str, benches: list) -> None:
    console.print(f"Benches in project [bold cyan]{project_name}[/bold cyan]:")
    for bench in benches:
        console.print(_bench_line(bench))


def _handle_label_error(e: CwcliError, project_name: str) -> NoReturn:
    """Render a core label failure with the historical CLI messages, then Exit(1).

    Message and hint render on ONE line, which is what reproduces the pre-migration
    text verbatim (e.g. "No cached benches for project 'x'. Run 'cwcli inspect x'
    first.") now that the remedy half travels as the error's ``hint``.
    """
    text = f"[bold red]Error:[/bold red] {e.message}"
    if e.hint:
        text += f" {e.hint}"
    stderr_console.print(text)

    if e.code == "bench.not_found":
        # The available-benches list is presentation, so the core does not carry it
        # on the error; fetch it here. A project with no cached benches raises
        # before this point, so the list is non-empty.
        try:
            listing = core_label.list_benches(project_name)
        except CwcliError:  # pragma: no cover - cache vanished mid-command
            raise typer.Exit(code=1) from None
        assert listing.data is not None
        stderr_console.print("Available benches (address by index or label):")
        for bench in listing.data.benches:
            stderr_console.print(_bench_line(bench))

    raise typer.Exit(code=1)


@handle_docker_errors
def label(
    project_name: str = typer.Argument(
        ..., help="The Docker Compose project name.", autocompletion=complete_project_names
    ),
    bench_selector: str = typer.Argument(
        None,
        help="Which bench to label: its numeric index or an existing label. Omit to list benches.",
    ),
    new_label: str = typer.Argument(
        None,
        help="The new label to assign. Omit and pass --clear to remove the label.",
    ),
    clear: bool = typer.Option(
        False, "--clear", help="Remove the selected bench's user label (revert to numeric index)."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose output."),
):
    """
    Assign, clear, or list per-bench labels for a project.

    Examples:

        cwcli label my-project                 # list benches with their indices/labels

        cwcli label my-project 1 staging       # label bench index 1 as 'staging'

        cwcli label my-project staging prod     # rename label 'staging' to 'prod'

        cwcli label my-project 1 --clear       # remove bench 1's label
    """
    # No selector -> list mode (read-only, no container needed). Not a NEEDS_CHOICE:
    # omitting the selector is a legitimate request, not an ambiguity.
    if bench_selector is None:
        try:
            listing = core_label.list_benches(project_name)
        except CwcliError as e:
            _handle_label_error(e, project_name)
        assert listing.data is not None
        _print_bench_list(project_name, listing.data.benches)
        for warning in listing.warnings:
            stderr_console.print(f"[yellow]{warning.text}[/yellow]")
        return

    try:
        # verify=False: this call is a "has this project been inspected" gate, not a
        # report, and the row it resolves is about to be used against the live
        # container anyway - which is where a stale path fails honestly.
        core_label.list_benches(project_name, verify=False)
        resolvers.resolve_bench(project_name, bench_selector, None)
    except CwcliError as e:
        _handle_label_error(e, project_name)

    if not clear and new_label is None:
        stderr_console.print(
            "[bold red]Error:[/bold red] Provide a new label, or pass --clear to remove one."
        )
        raise typer.Exit(code=1)

    try:
        if clear:
            result = core_label.clear_label(project_name, bench=bench_selector)
        else:
            result = core_label.set_label(project_name, bench=bench_selector, label=new_label)
    except CwcliError as e:
        _handle_label_error(e, project_name)

    if result.status is Status.NEEDS_CHOICE and result.choice is not None:
        # select_bench: unreachable from this frontend (an explicit selector is
        # required to get here), so reaching it means the cache changed under us.
        # Report rather than guess a bench.
        stderr_console.print(f"[bold red]Error:[/bold red] {result.choice.prompt}")
        raise typer.Exit(code=1)

    outcome = result.data
    assert outcome is not None  # OK/WARNING always carries a LabelOutcome

    if verbose:
        for warning in result.warnings:
            stderr_console.print(f"[dim]{warning.text}[/dim]")
        stderr_console.print(f"[dim]Marker file: {outcome.marker_path}[/dim]")

    if outcome.cleared:
        console.print(
            f"[bold green]✓[/bold green] Cleared label for bench at {outcome.bench_path}."
        )
    else:
        console.print(
            f"[bold green]✓[/bold green] Labeled bench at {outcome.bench_path} as "
            f"[magenta]'{outcome.label}'[/magenta]."
        )

    # Show the refreshed list so the user sees the result.
    refreshed = core_label.list_benches(project_name)
    assert refreshed.data is not None
    _print_bench_list(project_name, refreshed.data.benches)
