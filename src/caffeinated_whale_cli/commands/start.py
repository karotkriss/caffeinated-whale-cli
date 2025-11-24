import subprocess
import sys

import questionary
import typer

from ..utils import db_utils
from ..utils.completion_utils import complete_project_names
from ..utils.console import console, stderr_console
from ..utils.docker_utils import get_project_containers, handle_docker_errors
from ..utils.port_utils import (
    check_ports_in_use,
    find_project_using_ports,
    format_port_list,
    get_ports_in_use_with_processes,
    get_project_ports,
)

app = typer.Typer(help="Start a Frappe project's containers.")


def _check_port_conflicts(project_name: str, verbose: bool = False) -> bool:
    """
    Check for port conflicts before starting a project.

    Handles mixed scenarios where ports may be held by both Frappe projects
    and external processes. After stopping conflicting Frappe projects,
    re-checks all ports to ensure no external process conflicts remain.

    Args:
        project_name: The name of the docker-compose project.
        verbose: Enable verbose output.

    Returns:
        True if no conflicts or conflicts were resolved, False otherwise.

    Raises:
        typer.Exit: If port conflicts cannot be resolved.
    """
    project_ports = get_project_ports(project_name)

    if not project_ports:
        if verbose:
            stderr_console.print(
                f"[dim]VERBOSE: No ports configured for project '{project_name}'[/dim]"
            )
        return True

    if verbose:
        stderr_console.print(
            f"[dim]VERBOSE: Project '{project_name}' uses ports: {project_ports}[/dim]"
        )

    # Check which ports are in use
    ports_status = check_ports_in_use(project_ports, verbose=verbose)
    ports_in_use = [port for port, in_use in ports_status.items() if in_use]

    if not ports_in_use:
        if verbose:
            stderr_console.print("[dim]VERBOSE: All required ports are available[/dim]")
        return True

    if verbose:
        stderr_console.print(f"[dim]VERBOSE: Ports in use: {ports_in_use}[/dim]")

    # Find which Frappe projects are using these ports
    frappe_projects_on_ports = find_project_using_ports(ports_in_use, exclude_project=project_name)

    # Split ports into Frappe-owned vs non-Frappe-owned
    frappe_ports = set(frappe_projects_on_ports.keys())
    non_frappe_ports = set(ports_in_use) - frappe_ports

    if verbose:
        if frappe_ports:
            stderr_console.print(
                f"[dim]VERBOSE: Ports owned by Frappe projects: {sorted(frappe_ports)}[/dim]"
            )
        if non_frappe_ports:
            stderr_console.print(
                f"[dim]VERBOSE: Ports owned by non-Frappe processes: {sorted(non_frappe_ports)}[/dim]"
            )

    # Handle Frappe project conflicts first
    if frappe_projects_on_ports:
        conflicting_projects = set(frappe_projects_on_ports.values())

        # Group ports by project for better display
        project_to_ports = {}
        for port, proj in frappe_projects_on_ports.items():
            if proj not in project_to_ports:
                project_to_ports[proj] = []
            project_to_ports[proj].append(port)

        stderr_console.print(
            f"\n[yellow]Warning:[/yellow] Some ports needed by '{project_name}' are in use by other Frappe projects:"
        )

        for proj, ports in project_to_ports.items():
            formatted_ports = format_port_list(ports)
            stderr_console.print(f"  • Project '{proj}': {formatted_ports}")

        # Ask user if they want to stop conflicting projects
        try:
            for conflicting_project in conflicting_projects:
                answer = questionary.confirm(
                    f"Stop project '{conflicting_project}' to free up its ports?",
                    default=True,
                    auto_enter=False,
                ).ask()

                if answer:
                    # Stop the conflicting project
                    from .stop import _stop_project

                    stderr_console.print(
                        f"[yellow]Stopping project '{conflicting_project}'...[/yellow]"
                    )
                    with stderr_console.status(
                        f"[bold yellow]Stopping '{conflicting_project}'...[/bold yellow]",
                        spinner="dots",
                    ):
                        _stop_project(conflicting_project, verbose=verbose)
                    console.print(
                        f"[bold green]✓[/bold green] Stopped project '{conflicting_project}'"
                    )
                else:
                    stderr_console.print(
                        f"[bold red]Error:[/bold red] Cannot start '{project_name}' while '{conflicting_project}' is using required ports."
                    )
                    raise typer.Exit(code=1)
        except KeyboardInterrupt:
            stderr_console.print("\n[yellow]Operation cancelled.[/yellow]")
            raise typer.Exit(code=0) from None

        # After stopping Frappe projects, re-check ALL originally required ports
        # to catch any remaining conflicts from non-Frappe processes
        if verbose:
            stderr_console.print(
                "[dim]VERBOSE: Re-checking all ports after stopping Frappe projects...[/dim]"
            )

        ports_status = check_ports_in_use(project_ports, verbose=verbose)
        remaining_ports_in_use = [port for port, in_use in ports_status.items() if in_use]

        if remaining_ports_in_use:
            if verbose:
                stderr_console.print(
                    f"[dim]VERBOSE: Ports still in use: {remaining_ports_in_use}[/dim]"
                )

            # These must be non-Frappe processes since we just stopped all Frappe conflicts
            ports_with_processes = get_ports_in_use_with_processes(
                remaining_ports_in_use, verbose=verbose
            )

            # Group ports by process
            process_to_ports = {}
            for port in remaining_ports_in_use:
                process = ports_with_processes.get(port, "unknown")
                if process not in process_to_ports:
                    process_to_ports[process] = []
                process_to_ports[process].append(port)

            stderr_console.print(
                f"\n[bold red]Error:[/bold red] Cannot start '{project_name}'. Required ports are still in use by other processes:"
            )

            for process, ports in process_to_ports.items():
                formatted_ports = format_port_list(ports)
                if process == "unknown":
                    stderr_console.print(f"  • Ports {formatted_ports}: process unknown")
                else:
                    stderr_console.print(f"  • Ports {formatted_ports}: {process}")

            stderr_console.print(
                f"\n[dim]Please stop these processes before starting '{project_name}'.[/dim]"
            )
            raise typer.Exit(code=1)
        else:
            if verbose:
                stderr_console.print("[dim]VERBOSE: All ports are now available[/dim]")

    # Handle pure non-Frappe conflicts (no Frappe projects involved)
    elif non_frappe_ports:
        # Ports are in use by non-Frappe processes only
        ports_with_processes = get_ports_in_use_with_processes(
            list(non_frappe_ports), verbose=verbose
        )

        # Group ports by process
        process_to_ports = {}
        for port in non_frappe_ports:
            process = ports_with_processes.get(port, "unknown")
            if process not in process_to_ports:
                process_to_ports[process] = []
            process_to_ports[process].append(port)

        stderr_console.print(
            f"\n[bold red]Error:[/bold red] Cannot start '{project_name}'. Required ports are in use by other processes:"
        )

        for process, ports in process_to_ports.items():
            formatted_ports = format_port_list(sorted(ports))
            if process == "unknown":
                stderr_console.print(f"  • Ports {formatted_ports}: process unknown")
            else:
                stderr_console.print(f"  • Ports {formatted_ports}: {process}")

        stderr_console.print(
            f"\n[dim]Please stop these processes before starting '{project_name}'.[/dim]"
        )
        raise typer.Exit(code=1)

    return True


@handle_docker_errors
def _start_project(project_name: str, verbose: bool = False, status=None):
    """
    The core logic for starting a single project's containers.

    Note: Port conflict checks should be performed by the caller before
    calling this function. This function only handles container startup.

    Args:
        project_name: The name of the docker-compose project.
        verbose: Enable verbose output.
        status: Optional rich status object for progress updates.
    """
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
        stderr_console.print(
            f"[yellow]Warning: No 'frappe' service found for project '{project_name}'. Skipping bench start.[/yellow]"
        )
        return

    # Get bench path from cache
    cached_data = db_utils.get_cached_project_data(project_name)
    bench_path = None

    if cached_data and cached_data.get("bench_instances"):
        bench_path = cached_data["bench_instances"][0]["path"]
    else:
        # No cache found, run inspect
        if verbose:
            stderr_console.print(
                "[dim]VERBOSE: No cached bench path found. Running inspect...[/dim]"
            )

        # Exit spinner context to run inspect (it has its own spinner)
        if status:
            status.stop()

        stderr_console.print("[yellow]No cached bench path found. Running inspect...[/yellow]")

        try:
            from .inspect import inspect as inspect_cmd_func

            inspect_cmd_func(
                project_name=project_name,
                verbose=verbose,
                json_output=False,
                update=False,
                show_apps=False,
                interactive=False,
            )

            # Try to get cached data again
            cached_data = db_utils.get_cached_project_data(project_name)
            if cached_data and cached_data.get("bench_instances"):
                bench_path = cached_data["bench_instances"][0]["path"]
                if verbose:
                    stderr_console.print(
                        f"[dim]VERBOSE: Using cached bench path from inspect: {bench_path}[/dim]"
                    )
        except Exception as e:
            if verbose:
                stderr_console.print(f"[dim]VERBOSE: Inspect error: {e}[/dim]")

        # Resume spinner if it was active
        if status:
            status.start()

    # If we still don't have a bench path, skip bench start but continue with container start
    if not bench_path:
        stderr_console.print(
            "[yellow]Warning: Could not detect bench path. Skipping bench start.[/yellow]"
        )
        stderr_console.print(
            "[dim]Containers started, but bench was not started automatically.[/dim]"
        )
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
            stderr_console.print("[dim]VERBOSE: Checking for existing bench processes[/dim]")
        kill_cmd = [
            "docker",
            "exec",
            container_name,
            "bash",
            "-c",
            "pkill -f 'bench start' || true",
        ]
        subprocess.run(kill_cmd, check=False)

        # Start bench in background with nohup, redirecting all output to log file
        cmd = [
            "docker",
            "exec",
            "-d",
            container_name,
            "bash",
            "-c",
            f"cd {bench_path} && nohup bench start > {log_file} 2>&1 &",
        ]

        if verbose:
            stderr_console.print(
                f'[dim]VERBOSE: $ docker exec -d {container_name} bash -c "cd {bench_path} && nohup bench start > {log_file} 2>&1 &"[/dim]'
            )

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
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable verbose diagnostic output.",
    ),
    # Accept zero, one, or more project names. Default is None.
    project_name: list[str] = typer.Argument(
        None,
        help="The name(s) of the Frappe project(s) to start. Can be piped from stdin.",
        autocompletion=complete_project_names,
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
        # Check for port conflicts BEFORE starting containers
        try:
            _check_port_conflicts(name, verbose=actual_verbose)
        except typer.Exit as e:
            # Exit code 0 = user cancelled (Ctrl+C), should exit entire operation
            # Exit code 1 = port conflict couldn't be resolved, skip this project
            if e.exit_code == 0:
                # User cancelled, propagate the exit to cancel entire operation
                raise
            else:
                # Port conflict couldn't be resolved, skip this project and continue
                console.print(f"[yellow]Skipping project '{name}' due to port conflicts.[/yellow]")
                continue

        with stderr_console.status(
            f"[bold green]Starting '{name}'...[/bold green]", spinner="dots"
        ) as status:
            log_file = _start_project(name, verbose=actual_verbose, status=status)

        # Print outside spinner context
        console.print(f"Instance '{name}' started.")
        if log_file:
            console.print(f"[bold green]✓ Started bench (logs: {log_file})[/bold green]")
            console.print(f"[dim]View logs with: cwcli logs {name}[/dim]")

    console.print("\n[bold green]Start command finished.[/bold green]")
