import typer
import sys
from rich.console import Console
from typing import List
from .utils import get_project_containers
from ..utils.docker_utils import handle_docker_errors

app = typer.Typer(help="Stop a Frappe project's containers.")
console = Console()


@handle_docker_errors
def _stop_project(project_name: str):
    """The core logic for stopping a single project's containers."""
    containers = get_project_containers(project_name)

    if not containers:
        console.print(f"[bold red]Error: Project '{project_name}' not found.[/bold red]")
        return

    for container in containers:
        if container.status == "running":
            container.stop()

    console.print(f"Instance '{project_name}' stopped.")


@app.callback(invoke_without_command=True)
def stop(
    project_name: List[str] = typer.Argument(
        None, help="The name(s) of the Frappe project(s) to stop. Can be piped from stdin."
    )
):
    """
    Stops all containers for a given project or for all projects piped from stdin.
    """
    project_names_to_process = []

    if project_name:
        project_names_to_process.extend(project_name)

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
        with console.status(f"[bold yellow]Stopping '{name}'...[/bold yellow]"):
            _stop_project(name)

    console.print("\n[bold yellow]Stop command finished.[/bold yellow]")
