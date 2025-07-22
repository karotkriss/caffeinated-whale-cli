# src/caffeinated_whale_cli/commands/start.py

import typer
from rich.console import Console
from .utils import get_project_containers

app = typer.Typer(help="Start a Frappe project's containers.")
console = Console()

def _start_project(project_name: str):
    """The core logic for starting containers."""
    containers = get_project_containers(project_name)

    if containers is None:
        console.print("[bold red]Error: Could not connect to Docker.[/bold red]")
        raise typer.Exit(code=1)

    if not containers:
        console.print(f"[bold red]Error: Project '{project_name}' not found.[/bold red]")
        raise typer.Exit(code=1)

    for container in containers:
        # Only try to start containers that are not already running
        if container.status != "running":
            container.start()

@app.callback(invoke_without_command=True)
def start(
    project_name: str = typer.Argument(
        ...,
        help="The name of the Frappe project to start."
    )
):
    """
    Starts all containers for a given project.
    """
    with console.status(f"[bold green]Starting instance '{project_name}'...[/bold green]"):
        _start_project(project_name)
    
    console.print(f"[bold green]Instance '{project_name}' started successfully.[/bold green]")