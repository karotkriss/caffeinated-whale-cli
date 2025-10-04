import typer
import sys
from typing import List
from .utils import get_project_containers
from ..utils.docker_utils import handle_docker_errors
from .stop import _stop_project
from .start import _start_project
from ..utils.console import console, stderr_console

app = typer.Typer(help="Restart a Frappe project's containers.")


@handle_docker_errors
def _restart_project(project_name: str, verbose: bool = False, status=None):
    """The core logic for restarting a single project's containers."""
    containers = get_project_containers(project_name)

    if not containers:
        console.print(f"[bold red]Error: Project '{project_name}' not found.[/bold red]")
        return None, 0

    # Check if any containers are running
    running_containers = [c for c in containers if c.status == "running"]

    if running_containers:
        if verbose:
            stderr_console.print(f"[dim]VERBOSE: Found {len(running_containers)} running container(s) for '{project_name}'[/dim]")
    else:
        if verbose:
            stderr_console.print(f"[dim]VERBOSE: No running containers found for '{project_name}'[/dim]")

    # Stop then start
    stopped = _stop_project(project_name, verbose=verbose, status=status)
    log_file = _start_project(project_name, verbose=verbose, status=status)

    return log_file, stopped


@app.callback(invoke_without_command=True)
def restart(
    ctx: typer.Context,
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable verbose diagnostic output.",
    ),
    project_name: List[str] = typer.Argument(
        None, help="The name(s) of the Frappe project(s) to restart. Can be piped from stdin."
    ),
):
    """
    Restarts all containers for a project and runs bench start in tmux.
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
        f"Attempting to restart [bold cyan]{len(project_names_to_process)}[/bold cyan] project(s)..."
    )

    for name in project_names_to_process:
        with stderr_console.status(f"[bold cyan]Restarting '{name}'...[/bold cyan]", spinner="dots") as status:
            log_file, stopped = _restart_project(name, verbose=actual_verbose, status=status)

        # Print outside spinner context
        if stopped > 0:
            console.print(f"Instance '{name}' stopped.")
        console.print(f"Instance '{name}' started.")
        if log_file:
            console.print(f"[bold green]✓ Started bench (logs: {log_file})[/bold green]")
            console.print(f"[dim]View logs with: cwcli logs {name}[/dim]")

    console.print("\n[bold green]Restart command finished.[/bold green]")
