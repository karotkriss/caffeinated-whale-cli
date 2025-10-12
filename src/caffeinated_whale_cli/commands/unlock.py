import sys
import typer
from typing import List

from .utils import get_project_containers
from ..utils.docker_utils import handle_docker_errors
from ..utils import db_utils
from ..utils.console import console, stderr_console


@handle_docker_errors
def unlock(
    project_name: str = typer.Argument(..., help="The Docker Compose project name."),
    site: str = typer.Option(
        ...,
        "--site",
        "-s",
        help="Site name to unlock (removes the locks folder).",
    ),
    bench_path: str = typer.Option(
        None,
        "--path",
        "-p",
        help="Path to the bench directory inside the container (uses cached path from inspect if not specified).",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Enable verbose output."
    ),
):
    """
    Remove the locks folder for a specified site to unlock it.

    This command removes the {bench_path}/sites/{site_name}/locks directory,
    which can help resolve issues when a site is stuck in a locked state.

    Example:
        cwcli unlock my-project --site example.com
    """
    containers = get_project_containers(project_name)
    if not containers:
        stderr_console.print(
            f"[bold red]Error:[/bold red] Project '{project_name}' not found."
        )
        raise typer.Exit(code=1)

    frappe_container = next(
        (c for c in containers if c.labels.get("com.docker.compose.service") == "frappe"),
        None,
    )
    if not frappe_container:
        stderr_console.print(
            f"[bold red]Error:[/bold red] No 'frappe' service found for project '{project_name}'."
        )
        raise typer.Exit(code=1)

    if frappe_container.status != "running":
        stderr_console.print(
            f"[bold red]Error:[/bold red] Frappe container for project '{project_name}' is not running."
        )
        raise typer.Exit(code=1)

    # Get bench path from cache or use provided path
    if not bench_path:
        cached_data = db_utils.get_cached_project_data(project_name)
        if cached_data and cached_data.get("bench_instances"):
            bench_path = cached_data["bench_instances"][0]["path"]
            if verbose:
                stderr_console.print(f"[dim]Using cached bench path: {bench_path}[/dim]")
        else:
            # No cache found, use default
            bench_path = "/workspace/frappe-bench"
            stderr_console.print(
                f"[yellow]Warning:[/yellow] No cached bench path found. Using default: {bench_path}"
            )

    # Verify bench path exists
    exit_code, _ = frappe_container.exec_run(
        f'sh -c "test -d {bench_path}/sites"'
    )
    if verbose:
        stderr_console.print(f'[dim]$ sh -c "test -d {bench_path}/sites"[/dim]')
        stderr_console.print(f"[dim]Exit code: {exit_code}[/dim]")

    if exit_code != 0:
        stderr_console.print(f"[bold red]Error:[/bold red] Bench directory not found at {bench_path}")
        raise typer.Exit(code=1)

    # Check if site exists
    site_path = f"{bench_path}/sites/{site}"
    exit_code, _ = frappe_container.exec_run(f'sh -c "test -d {site_path}"')
    if verbose:
        stderr_console.print(f'[dim]$ sh -c "test -d {site_path}"[/dim]')
        stderr_console.print(f"[dim]Exit code: {exit_code}[/dim]")

    if exit_code != 0:
        stderr_console.print(f"[bold red]Error:[/bold red] Site '{site}' not found at {site_path}")
        raise typer.Exit(code=1)

    # Remove locks folder
    locks_path = f"{site_path}/locks"
    cmd = f"rm -rfv {locks_path}"  # Use -v for verbose output

    if verbose:
        stderr_console.print(f"[dim]$ {cmd}[/dim]")

        # Stream output in verbose mode
        api = frappe_container.client.api
        exec_id = api.exec_create(frappe_container.id, cmd, workdir=bench_path, tty=False)["Id"]

        console.print(f"[bold green]Unlocking site '{site}'...[/bold green]")

        for chunk in api.exec_start(exec_id, stream=True):
            if isinstance(chunk, (bytes, bytearray)):
                # Write directly to stdout to preserve output
                sys.stdout.write(chunk.decode("utf-8"))
                sys.stdout.flush()
            else:
                sys.stdout.write(str(chunk))
                sys.stdout.flush()

        result = api.exec_inspect(exec_id)
        exit_code = result.get("ExitCode", 1)
    else:
        # Non-verbose mode: use spinner
        with console.status(f"[bold green]Unlocking site '{site}'...[/bold green]", spinner="dots"):
            exit_code, output = frappe_container.exec_run(cmd, workdir=bench_path)

    if exit_code == 0:
        console.print(f"[bold green]✓[/bold green] Successfully unlocked site '{site}'")
        console.print(f"[dim]Removed locks folder: {locks_path}[/dim]")
    else:
        stderr_console.print(f"[bold red]✗[/bold red] Failed to unlock site '{site}'")
        raise typer.Exit(code=1)
