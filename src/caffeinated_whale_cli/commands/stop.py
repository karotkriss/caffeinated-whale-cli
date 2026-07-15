import sys

import typer

from ..core import stop as core_stop
from ..core.errors import CwcliError, ErrorKind
from ..utils.completion_utils import complete_project_names
from ..utils.console import console, stderr_console
from ..utils.docker_utils import handle_docker_errors

app = typer.Typer(help="Stop a Frappe project's containers.")


@app.callback(invoke_without_command=True)
@handle_docker_errors
def stop(
    ctx: typer.Context,
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable verbose diagnostic output.",
    ),
    project_name: list[str] = typer.Argument(
        None,
        help="The name(s) of the Frappe project(s) to stop. Can be piped from stdin.",
        autocompletion=complete_project_names,
    ),
):
    """
    Stops all containers for a given project or for all projects piped from stdin.
    """
    project_names_to_process = []

    # Handle -v or --verbose in remaining args
    actual_verbose = verbose
    filtered_project_names = []

    if project_name:
        for name in project_name:
            if name in ("-v", "--verbose"):
                actual_verbose = True
            else:
                filtered_project_names.append(name)
        project_names_to_process.extend(filtered_project_names)

    if not sys.stdin.isatty():
        piped_input = [line.strip() for line in sys.stdin]
        project_names_to_process.extend([name for name in piped_input if name])

    if not project_names_to_process:
        console.print(
            "[bold red]Error:[/bold red] Please provide at least one project name or pipe a list of names."
        )
        raise typer.Exit(code=1)

    console.print(
        f"Attempting to stop [bold yellow]{len(project_names_to_process)}[/bold yellow] project(s)..."
    )

    had_failure = False

    for name in project_names_to_process:
        try:
            with stderr_console.status(
                f"[bold yellow]Stopping '{name}'...[/bold yellow]", spinner="dots"
            ) as status:
                if actual_verbose:
                    stderr_console.print(f"[dim]VERBOSE: Stopping project '{name}'[/dim]")
                status.update(f"[bold yellow]Stopping '{name}'...[/bold yellow]")
                result = core_stop.stop(name)
        except CwcliError as e:
            # A missing project (or an unreachable daemon) is a per-project failure:
            # keep processing the rest, then exit non-zero, rather than falsely
            # reporting success.
            console.print(f"[bold red]Error: {e.message}[/bold red]")
            had_failure = True
            continue

        outcome = result.data
        assert outcome is not None  # OK always carries a StopOutcome

        if actual_verbose:
            stderr_console.print(
                f"[dim]VERBOSE: Stopped {outcome.stopped} container(s) for '{name}'[/dim]"
            )

        # Print outside the spinner context.
        if outcome.already_stopped:
            console.print(f"Instance '{name}' is already stopped.")
        else:
            console.print(f"Instance '{name}' stopped.")

    console.print("\n[bold yellow]Stop command finished.[/bold yellow]")

    if had_failure:
        raise typer.Exit(code=1)


def stop_project_best_effort(project_name: str, verbose: bool = False) -> int | None:
    """Stop a project for another CLI frontend, returning the count stopped, or None
    if the project does not exist.

    A frontend-side rendering adapter, NOT logic: `core.stop` owns the behavior and
    this only maps its typed `NOT_FOUND` back to the `None` the two remaining
    callers (`restart`'s stop-then-start, `start`'s port-conflict resolution) were
    written against. Unlike the `_stop_project` it replaces, everything it prints
    goes to STDERR, so no caller can corrupt a machine-readable stdout. `axi` and
    `rm` do not use it: `axi` calls `core.stop` directly (its errors are TOON), and
    `rm`'s return-to-stopped already treats any exception as a best-effort warning.
    """
    try:
        result = core_stop.stop(project_name)
    except CwcliError as e:
        if e.kind is ErrorKind.NOT_FOUND:
            stderr_console.print(f"[bold red]Error: {e.message}[/bold red]")
            return None
        raise

    outcome = result.data
    assert outcome is not None
    if verbose:
        stderr_console.print(
            f"[dim]VERBOSE: Stopped {outcome.stopped} container(s) for '{project_name}'[/dim]"
        )
    return outcome.stopped
