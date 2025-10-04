import typer
import subprocess
from .utils import get_project_containers
from ..utils.docker_utils import handle_docker_errors
from ..utils.console import console, stderr_console


@handle_docker_errors
def logs(
    project_name: str = typer.Argument(..., help="The name of the Frappe project to view logs for."),
    follow: bool = typer.Option(
        True,
        "--follow/--no-follow",
        "-f",
        help="Follow log output in real-time.",
    ),
    lines: int = typer.Option(
        100,
        "--lines",
        "-n",
        help="Number of lines to show from the end of the logs.",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable verbose diagnostic output.",
    ),
):
    """
    View bench logs in real-time from the log file.
    """
    containers = get_project_containers(project_name)

    if not containers:
        console.print(f"[bold red]Error: Project '{project_name}' not found.[/bold red]")
        raise typer.Exit(code=1)

    # Find the frappe container
    frappe_container = next(
        (c for c in containers if c.labels.get("com.docker.compose.service") == "frappe"),
        None,
    )
    if not frappe_container:
        stderr_console.print(f"[bold red]Error: No 'frappe' service found for project '{project_name}'.[/bold red]")
        raise typer.Exit(code=1)

    # Check if container is running
    if frappe_container.status != "running":
        stderr_console.print(f"[bold red]Error: Frappe container for project '{project_name}' is not running.[/bold red]")
        stderr_console.print(f"[dim]Start it with: cwcli start {project_name}[/dim]")
        raise typer.Exit(code=1)

    container_name = frappe_container.name
    log_file = f"/tmp/bench-{project_name}.log"

    if verbose:
        stderr_console.print(f"[dim]VERBOSE: Checking for log file '{log_file}' in container '{container_name}'[/dim]")

    # Check if log file exists in container
    check_cmd = ["docker", "exec", container_name, "test", "-f", log_file]
    result = subprocess.run(check_cmd, capture_output=True)

    if result.returncode != 0:
        stderr_console.print(f"[bold red]Error: No log file '{log_file}' found in container.[/bold red]")
        stderr_console.print(f"[dim]The bench may not be running. Start it with: cwcli start {project_name}[/dim]")
        raise typer.Exit(code=1)

    if verbose:
        stderr_console.print(f"[dim]VERBOSE: Tailing log file '{log_file}'...[/dim]")

    # Tail the log file
    console.print(f"[bold green]Viewing bench logs for '{project_name}'...[/bold green]")
    console.print(f"[dim]Press Ctrl+c to exit[/dim]\n")

    if follow:
        tail_cmd = ["docker", "exec", "-it", container_name, "tail", "-f", "-n", str(lines), log_file]
    else:
        tail_cmd = ["docker", "exec", "-it", container_name, "tail", "-n", str(lines), log_file]

    if verbose:
        stderr_console.print(f"[dim]VERBOSE: $ {' '.join(tail_cmd)}[/dim]")

    try:
        subprocess.run(tail_cmd)
    except subprocess.CalledProcessError as e:
        stderr_console.print(f"[bold red]Error:[/bold red] Failed to tail log file: {e}")
        raise typer.Exit(code=1)
    except KeyboardInterrupt:
        # User pressed Ctrl+C, which is normal
        console.print("\n[yellow]Stopped viewing logs.[/yellow]")
