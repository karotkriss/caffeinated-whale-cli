import typer
from rich.console import Console
from .utils import get_project_containers

app = typer.Typer(help="Stop a Frappe project's containers.")
console = Console()

def _stop_project(project_name: str):
    """The core logic for stopping containers."""
    containers = get_project_containers(project_name)

    if containers is None:
        console.print("[bold red]Error: Could not connect to Docker.[/bold red]")
        raise typer.Exit(code=1)

    if not containers:
        console.print(f"[bold red]Error: Project '{project_name}' not found.[/bold red]")
        raise typer.Exit(code=1)

    for container in containers:
        # Only try to stop containers that are currently running
        if container.status == "running":
            container.stop()

@app.callback(invoke_without_command=True)
def stop(
    project_name: str = typer.Argument(
        ...,
        help="The name of the Frappe project to stop."
    )
):
    """
    Stops all containers for a given project.
    """
    with console.status(f"[bold yellow]Stopping instance '{project_name}'...[/bold yellow]"):
        _stop_project(project_name)
    
    console.print(f"[bold yellow]Instance '{project_name}' stopped successfully.[/bold yellow]")