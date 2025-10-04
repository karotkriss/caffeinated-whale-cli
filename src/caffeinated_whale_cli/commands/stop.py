import typer
import sys
from typing import List
from .utils import get_project_containers
from ..utils.docker_utils import handle_docker_errors
from ..utils.console import console, stderr_console

app = typer.Typer(help="Stop a Frappe project's containers.")


@handle_docker_errors
def _stop_project(project_name: str, verbose: bool = False, status=None):
    """The core logic for stopping a single project's containers."""
    containers = get_project_containers(project_name)

    if not containers:
        console.print(f"[bold red]Error: Project '{project_name}' not found.[/bold red]")
        return 0

    # Check which containers are running
    running_containers = [c for c in containers if c.status == "running"]

    if verbose:
        stderr_console.print(f"[dim]VERBOSE: Found {len(containers)} total container(s), {len(running_containers)} running[/dim]")

    if not running_containers:
        if verbose:
            stderr_console.print(f"[dim]VERBOSE: No running containers to stop for '{project_name}'[/dim]")
        console.print(f"Instance '{project_name}' is already stopped.")
        return 0

    for container in running_containers:
        if verbose:
            stderr_console.print(f"[dim]VERBOSE: Stopping container '{container.name}'[/dim]")
        if status:
            status.update(f"[bold yellow]Stopping '{container.name}'...[/bold yellow]")
        container.stop()

    return len(running_containers)


@app.callback(invoke_without_command=True)
def stop(
    ctx: typer.Context,
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable verbose diagnostic output.",
    ),
    project_name: List[str] = typer.Argument(
        None, help="The name(s) of the Frappe project(s) to stop. Can be piped from stdin."
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

    for name in project_names_to_process:
        with stderr_console.status(f"[bold yellow]Stopping '{name}'...[/bold yellow]", spinner="dots") as status:
            result = _stop_project(name, verbose=actual_verbose, status=status)

        # Print outside spinner context
        if result > 0:
            console.print(f"Instance '{name}' stopped.")
        # If result is 0, the "already stopped" message was already printed

    console.print("\n[bold yellow]Stop command finished.[/bold yellow]")
