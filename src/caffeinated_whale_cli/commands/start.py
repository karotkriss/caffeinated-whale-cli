import typer
import sys
import subprocess
import shutil
from typing import List
from .utils import get_project_containers
from ..utils.docker_utils import handle_docker_errors
from ..utils import db_utils
from ..utils.console import console, stderr_console

app = typer.Typer(help="Start a Frappe project's containers.")


@handle_docker_errors
def _start_project(project_name: str, verbose: bool = False, status=None):
    """The core logic for starting a single project's containers."""
    containers = get_project_containers(project_name)

    if not containers:
        console.print(f"[bold red]Error: Project '{project_name}' not found.[/bold red]")
        # Continue to the next project instead of exiting the whole command
        return

    started_count = 0
    for container in containers:
        if container.status != "running":
            if verbose:
                stderr_console.print(f"[dim]VERBOSE: Starting container '{container.name}'[/dim]")
            if status:
                status.update(f"[bold green]Starting '{container.name}'...[/bold green]")
            container.start()
            started_count += 1

    # Find the frappe container
    frappe_container = next(
        (c for c in containers if c.labels.get("com.docker.compose.service") == "frappe"),
        None,
    )
    if not frappe_container:
        stderr_console.print(f"[yellow]Warning: No 'frappe' service found for project '{project_name}'. Skipping bench start.[/yellow]")
        return

    # Get bench path from cache
    cached_data = db_utils.get_cached_project_data(project_name)
    bench_path = None

    if cached_data and cached_data.get("bench_instances"):
        bench_path = cached_data["bench_instances"][0]["path"]
    else:
        # No cache found, run inspect
        if verbose:
            stderr_console.print(f"[dim]VERBOSE: No cached bench path found. Running inspect...[/dim]")

        # Exit spinner context to run inspect (it has its own spinner)
        if status:
            status.stop()

        stderr_console.print(f"[yellow]No cached bench path found. Running inspect...[/yellow]")

        try:
            from .inspect import inspect as inspect_cmd_func

            inspect_cmd_func(
                project_name=project_name,
                verbose=verbose,
                json_output=False,
                update=False,
                show_apps=False,
                interactive=False
            )

            # Try to get cached data again
            cached_data = db_utils.get_cached_project_data(project_name)
            if cached_data and cached_data.get("bench_instances"):
                bench_path = cached_data["bench_instances"][0]["path"]
                if verbose:
                    stderr_console.print(f"[dim]VERBOSE: Using cached bench path from inspect: {bench_path}[/dim]")
        except Exception as e:
            if verbose:
                stderr_console.print(f"[dim]VERBOSE: Inspect error: {e}[/dim]")

        # Resume spinner if it was active
        if status:
            status.start()

    # If we still don't have a bench path, skip bench start but continue with container start
    if not bench_path:
        stderr_console.print(f"[yellow]Warning: Could not detect bench path. Skipping bench start.[/yellow]")
        stderr_console.print(f"[dim]Containers started, but bench was not started automatically.[/dim]")
        return None

    container_name = frappe_container.name
    log_file = f"/tmp/bench-{project_name}.log"

    if verbose:
        stderr_console.print(f"[dim]VERBOSE: Starting bench in container '{container_name}'[/dim]")
        stderr_console.print(f"[dim]VERBOSE: Logs will be written to {log_file}[/dim]")
    if status:
        status.update(f"[bold green]Starting bench and logging to {log_file}...[/bold green]")

    # Start bench in background with output redirected to log file
    try:
        # Kill any existing bench processes
        if verbose:
            stderr_console.print(f"[dim]VERBOSE: Checking for existing bench processes[/dim]")
        kill_cmd = [
            "docker", "exec", container_name,
            "bash", "-c",
            f"pkill -f 'bench start' || true"
        ]
        subprocess.run(kill_cmd, check=False)

        # Start bench in background with nohup, redirecting all output to log file
        cmd = [
            "docker", "exec", "-d", container_name,
            "bash", "-c",
            f"cd {bench_path} && nohup bench start > {log_file} 2>&1 &"
        ]

        if verbose:
            stderr_console.print(f"[dim]VERBOSE: $ docker exec -d {container_name} bash -c \"cd {bench_path} && nohup bench start > {log_file} 2>&1 &\"[/dim]")

        subprocess.run(cmd, check=True)

        return log_file
    except subprocess.CalledProcessError as e:
        stderr_console.print(f"[bold red]Error:[/bold red] Failed to start bench: {e}")
        return None
    except Exception as e:
        stderr_console.print(f"[bold red]Error:[/bold red] {e}")
        return None

@app.callback(invoke_without_command=True)
def start(
    ctx: typer.Context,
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable verbose diagnostic output.",
    ),
    # Accept zero, one, or more project names. Default is None.
    project_name: List[str] = typer.Argument(
        None, help="The name(s) of the Frappe project(s) to start. Can be piped from stdin."
    ),
):
    """
    Starts all containers for a project and runs bench start in tmux.
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
        f"Attempting to start [bold cyan]{len(project_names_to_process)}[/bold cyan] project(s)..."
    )

    for name in project_names_to_process:
        with stderr_console.status(f"[bold green]Starting '{name}'...[/bold green]", spinner="dots") as status:
            log_file = _start_project(name, verbose=actual_verbose, status=status)

        # Print outside spinner context
        console.print(f"Instance '{name}' started.")
        if log_file:
            console.print(f"[bold green]✓ Started bench (logs: {log_file})[/bold green]")
            console.print(f"[dim]View logs with: cwcli logs {name}[/dim]")

    console.print("\n[bold green]Start command finished.[/bold green]")
