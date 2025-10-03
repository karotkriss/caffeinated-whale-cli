import shlex
import typer

from rich.console import Console

from .utils import get_project_containers
from ..utils.docker_utils import handle_docker_errors
from docker.errors import APIError, NotFound

stderr_console = Console(stderr=True)


@handle_docker_errors
def status(
    project_name: str = typer.Argument(..., help="The Docker Compose project name to check."),
    verbose: bool = typer.Option(
        False,
        "--verbose", "-v",
        help="Show the health-check command, raw curl output, and explain the reported status.",
    ),
):
    """
    Check the health status of a Frappe project instance.
    """
    containers = get_project_containers(project_name)
    if not containers:
        typer.echo("offline")
        raise typer.Exit(code=0)

    frappe_container = next(
        (c for c in containers if c.labels.get("com.docker.compose.service") == "frappe"),
        None,
    )
    if not frappe_container:
        typer.echo("offline")
        raise typer.Exit(code=0)

    # Refresh status info
    try:
        frappe_container.reload()
    except (APIError, NotFound) as e:
        if verbose:
            stderr_console.print(f"[dim]Failed to reload container: {e}[/dim]")
        typer.echo("offline")
        raise typer.Exit(code=0) from e

    if frappe_container.status != "running":
        typer.echo("offline")
        raise typer.Exit(code=0)

    # Execute curl inside container
    cmd = shlex.split('curl -s -o /dev/null -w "%{http_code}" http://localhost:8000')
    if verbose:
        stderr_console.print(f"[dim]$ {' '.join(shlex.quote(arg) for arg in cmd)}[/dim]")
    exit_code, output = frappe_container.exec_run(cmd)
    if verbose:
        stderr_console.print(f"[yellow]VERBOSE: Exit Code:[/yellow] [cyan]{exit_code}[/cyan]")
        decoded = (output or b"").decode("utf-8", errors="replace").strip()
        stderr_console.print(f"[yellow]VERBOSE: Output:[/yellow] [cyan]{decoded}[/cyan]")

    # Decide and explain status: 'running' if HTTP probe succeeded, otherwise 'online'.
    http_code = (output or b"").decode("utf-8").strip()
    if exit_code == 0 and http_code and http_code != "000":
        if verbose:
            stderr_console.print(
                "[bold green]VERBOSE: bench is started and running.[/bold green]"
            )
        typer.echo("running")
    else:
        if verbose:
            stderr_console.print(
                "[bold yellow]VERBOSE: Bench is not running but containers are online.[/bold yellow]"
            )
        typer.echo("online")
    raise typer.Exit(code=0)
