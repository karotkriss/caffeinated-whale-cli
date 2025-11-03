import docker
import typer
import questionary
import socket
from docker.errors import DockerException
from typing import List, Optional, Dict, Tuple, Union
from ..utils.console import console, stderr_console


def _format_port_list(ports: List[int]) -> str:
    """
    Format a list of ports into a compact string with ranges.

    Args:
        ports: List of port numbers (as integers).

    Returns:
        Formatted string like "8000-8005, 9000-9005" or "8000, 8080, 9000"

    Examples:
        [8000, 8001, 8002, 9000] -> "8000-8002, 9000"
        [8000, 8080, 9000] -> "8000, 8080, 9000"
        [8000] -> "8000"
    """
    if not ports:
        return ""

    # Sort ports
    sorted_ports = sorted(ports)

    if len(sorted_ports) == 1:
        return str(sorted_ports[0])

    ranges = []
    start_of_range = sorted_ports[0]

    for i in range(1, len(sorted_ports)):
        # If the current port is not sequential, the previous range has ended
        if sorted_ports[i] != sorted_ports[i - 1] + 1:
            # Finalize the previous range
            if start_of_range == sorted_ports[i - 1]:
                ranges.append(str(start_of_range))
            else:
                ranges.append(f"{start_of_range}-{sorted_ports[i-1]}")
            # Start a new range
            start_of_range = sorted_ports[i]

    # After the loop, add the final range
    if start_of_range == sorted_ports[-1]:
        ranges.append(str(start_of_range))
    else:
        ranges.append(f"{start_of_range}-{sorted_ports[-1]}")

    return ", ".join(ranges)


def get_project_containers(project_name: str) -> List[docker.models.containers.Container] | None:
    """
    Finds all containers belonging to a specific Docker Compose project.

    Args:
        project_name: The name of the docker-compose project.

    Returns:
        A list of container objects, an empty list if not found,
        or None if there was a Docker connection error.
    """
    try:
        client = docker.from_env()
        client.ping()

        containers = client.containers.list(
            all=True,
            filters={"label": f"com.docker.compose.project={project_name}"}
        )
        return containers

    except DockerException:
        return None


def get_project_ports(project_name: str) -> List[int]:
    """
    Get all host ports used by a project's containers.

    Args:
        project_name: The name of the docker-compose project.

    Returns:
        List of port numbers (as integers) used by the project.
    """
    containers = get_project_containers(project_name)
    if not containers:
        return []

    ports = set()
    for container in containers:
        if container.ports:
            for _container_port, host_ports in container.ports.items():
                if host_ports:
                    for host_port_info in host_ports:
                        if host_port_info and "HostPort" in host_port_info:
                            try:
                                ports.add(int(host_port_info["HostPort"]))
                            except (ValueError, TypeError):
                                pass

        # Fallback to PortBindings if ports attribute is empty
        if container.attrs:
            port_bindings = container.attrs.get("HostConfig", {}).get("PortBindings")
            if port_bindings:
                for _container_port, bindings in port_bindings.items():
                    if bindings:
                        for binding in bindings:
                            if "HostPort" in binding and binding["HostPort"]:
                                try:
                                    ports.add(int(binding["HostPort"]))
                                except (ValueError, TypeError):
                                    pass

    return sorted(list(ports))


def find_project_using_ports(ports: Union[int, List[int]], exclude_project: str = None) -> Dict[int, str]:
    """
    Find which Frappe projects are using specific ports.

    Args:
        ports: Port number or list of port numbers to check.
        exclude_project: Optional project name to exclude from the search.

    Returns:
        Dictionary mapping port numbers to project names (only for ports used by Frappe projects).
    """
    import docker

    # Normalize input to list
    port_list = [ports] if isinstance(ports, int) else ports

    try:
        client = docker.from_env()
        # Get all Frappe containers
        containers = client.containers.list(
            all=True,
            filters={"label": "com.docker.compose.service=frappe"}
        )

        port_to_project = {}

        for container in containers:
            project_name = container.labels.get("com.docker.compose.project")
            if not project_name or (exclude_project and project_name == exclude_project):
                continue

            # Get all containers for this project
            project_containers = client.containers.list(
                all=True,
                filters={"label": f"com.docker.compose.project={project_name}"}
            )

            # Check if any of the project's containers use our target ports
            for proj_container in project_containers:
                if proj_container.ports:
                    for _container_port, host_ports in proj_container.ports.items():
                        if host_ports:
                            for host_port_info in host_ports:
                                if host_port_info and "HostPort" in host_port_info:
                                    try:
                                        host_port = int(host_port_info["HostPort"])
                                        if host_port in port_list:
                                            port_to_project[host_port] = project_name
                                    except (ValueError, TypeError):
                                        pass

        return port_to_project

    except Exception:
        return {}


def ensure_containers_running(
    project_name: str,
    require_running: bool = False,
    verbose: bool = False,
    auto_start: bool = False
) -> bool:
    """
    Check if containers for a project are running and optionally prompt to start them.
    Also checks for port conflicts and handles them intelligently.

    Args:
        project_name: The name of the docker-compose project.
        require_running: If True, containers must be running for the operation to proceed.
        verbose: Enable verbose output.
        auto_start: If True, automatically start containers without prompting.

    Returns:
        True if containers are running (or were started), False otherwise.

    Raises:
        typer.Exit: If containers are not running and user chooses not to start them,
                   or if starting containers fails, or if port conflicts cannot be resolved.
    """
    if not require_running:
        return True

    containers = get_project_containers(project_name)

    if not containers:
        stderr_console.print(f"[bold red]Error:[/bold red] Project '{project_name}' not found.")
        raise typer.Exit(code=1)

    # Check if frappe container exists and is running
    frappe_container = next(
        (c for c in containers if c.labels.get("com.docker.compose.service") == "frappe"),
        None,
    )

    if not frappe_container:
        stderr_console.print(
            f"[bold red]Error:[/bold red] No 'frappe' service found for project '{project_name}'."
        )
        raise typer.Exit(code=1)

    # Reload container to get current status
    frappe_container.reload()

    if frappe_container.status == "running":
        if verbose:
            stderr_console.print(f"[dim]VERBOSE: Frappe container is running[/dim]")
        return True

    # Container is not running - ask user first if they want to start it
    user_wants_to_start = False

    if auto_start:
        if verbose:
            stderr_console.print(f"[dim]VERBOSE: Auto-starting containers for '{project_name}'[/dim]")
        user_wants_to_start = True
    else:
        # Prompt user to start containers
        stderr_console.print(
            f"[yellow]Warning:[/yellow] Frappe container for project '{project_name}' is not running."
        )

        try:
            answer = questionary.confirm(
                f"Would you like to start the containers for '{project_name}'?",
                default=True,
                auto_enter=False
            ).ask()

            if answer:
                user_wants_to_start = True
            else:
                stderr_console.print("[yellow]Operation cancelled.[/yellow]")
                stderr_console.print(f"[dim]Start containers with: cwcli start {project_name}[/dim]")
                raise typer.Exit(code=0)
        except KeyboardInterrupt:
            stderr_console.print("\n[yellow]Operation cancelled.[/yellow]")
            raise typer.Exit(code=0)

    # User wants to start - now check for port conflicts
    if user_wants_to_start:
        project_ports = get_project_ports(project_name)

        if verbose:
            stderr_console.print(f"[dim]VERBOSE: Project '{project_name}' uses ports: {project_ports}[/dim]")

        if project_ports:
            # Check which ports are in use
            ports_status = check_ports_in_use(project_ports, verbose=verbose)
            ports_in_use = [port for port, in_use in ports_status.items() if in_use]

            if ports_in_use:
                if verbose:
                    stderr_console.print(f"[dim]VERBOSE: Ports in use: {ports_in_use}[/dim]")

                # Find which Frappe projects are using these ports
                frappe_projects_on_ports = find_project_using_ports(ports_in_use, exclude_project=project_name)

                if frappe_projects_on_ports:
                    # Ports are used by other Frappe projects
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
                        # Format ports as ranges
                        formatted_ports = _format_port_list(ports)
                        stderr_console.print(f"  • Project '{proj}': {formatted_ports}")

                    # Ask user if they want to stop conflicting projects
                    try:
                        for conflicting_project in conflicting_projects:
                            answer = questionary.confirm(
                                f"Stop project '{conflicting_project}' to free up its ports?",
                                default=True,
                                auto_enter=False
                            ).ask()

                            if answer:
                                # Stop the conflicting project
                                from .stop import _stop_project
                                stderr_console.print(f"[yellow]Stopping project '{conflicting_project}'...[/yellow]")
                                with stderr_console.status(
                                    f"[bold yellow]Stopping '{conflicting_project}'...[/bold yellow]",
                                    spinner="dots"
                                ):
                                    _stop_project(conflicting_project, verbose=verbose)
                                console.print(f"[bold green]✓[/bold green] Stopped project '{conflicting_project}'")
                            else:
                                stderr_console.print(
                                    f"[bold red]Error:[/bold red] Cannot start '{project_name}' while '{conflicting_project}' is using required ports."
                                )
                                raise typer.Exit(code=1)
                    except KeyboardInterrupt:
                        stderr_console.print("\n[yellow]Operation cancelled.[/yellow]")
                        raise typer.Exit(code=0)

                else:
                    # Ports are in use by non-Frappe processes
                    ports_with_processes = get_ports_in_use_with_processes(ports_in_use, verbose=verbose)

                    # Group ports by process
                    process_to_ports = {}
                    for port in ports_in_use:
                        process = ports_with_processes.get(port, "unknown")
                        if process not in process_to_ports:
                            process_to_ports[process] = []
                        process_to_ports[process].append(port)

                    stderr_console.print(
                        f"\n[bold red]Error:[/bold red] Cannot start '{project_name}'. Required ports are in use by other processes:"
                    )

                    for process, ports in process_to_ports.items():
                        formatted_ports = _format_port_list(ports)
                        if process == "unknown":
                            stderr_console.print(f"  • Ports {formatted_ports}: process unknown")
                        else:
                            stderr_console.print(f"  • Ports {formatted_ports}: {process}")

                    stderr_console.print(
                        f"\n[dim]Please stop these processes before starting '{project_name}'.[/dim]"
                    )
                    raise typer.Exit(code=1)

        # All clear - start the containers
        _start_containers_for_command(project_name, verbose)
        return True


def _start_containers_for_command(project_name: str, verbose: bool = False):
    """
    Start containers for a project. Used by ensure_containers_running.

    Args:
        project_name: The name of the docker-compose project.
        verbose: Enable verbose output.

    Raises:
        typer.Exit: If starting containers fails.
    """
    from .start import _start_project

    try:
        with stderr_console.status(
            f"[bold green]Starting containers for '{project_name}'...[/bold green]",
            spinner="dots"
        ) as status:
            log_file = _start_project(project_name, verbose=verbose, status=status)

        console.print(f"[bold green]✓[/bold green] Containers started for '{project_name}'")
        if log_file:
            console.print(f"[dim]View logs with: cwcli logs {project_name}[/dim]")
    except Exception as e:
        stderr_console.print(f"[bold red]Error:[/bold red] Failed to start containers: {e}")
        raise typer.Exit(code=1)


def is_port_in_use(port: int, host: str = "0.0.0.0") -> bool:
    """
    Check if a specific port is in use on the host.

    Args:
        port: The port number to check.
        host: The host address to check (default: "0.0.0.0" for all interfaces).

    Returns:
        True if the port is in use, False otherwise.
    """
    try:
        # Try to bind to the port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            # SO_REUSEADDR allows binding to a port in TIME_WAIT state
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
            return False
    except (socket.error, OSError):
        return True


def check_ports_in_use(ports: Union[int, List[int]], host: str = "0.0.0.0", verbose: bool = False) -> Dict[int, bool]:
    """
    Check which ports from a list are currently in use.

    Args:
        ports: Port number or list of port numbers to check.
        host: The host address to check (default: "0.0.0.0" for all interfaces).
        verbose: Enable verbose output showing each port check.

    Returns:
        Dictionary mapping port numbers to their in-use status (True if in use, False if available).
    """
    # Normalize input to list
    port_list = [ports] if isinstance(ports, int) else ports

    results = {}

    for port in port_list:
        in_use = is_port_in_use(port, host)
        results[port] = in_use

        if verbose:
            status = "[red]IN USE[/red]" if in_use else "[green]AVAILABLE[/green]"
            stderr_console.print(f"[dim]Port {port}: {status}[/dim]")

    return results


def get_ports_in_use_with_processes(ports: Union[int, List[int]], verbose: bool = False) -> Dict[int, Optional[str]]:
    """
    Check which ports are in use and try to identify the process using them.

    Args:
        ports: Port number or list of port numbers to check.
        verbose: Enable verbose output.

    Returns:
        Dictionary mapping port numbers to process information (None if port is available).
        Process info format: "PID/program_name" or "unknown" if cannot be determined.
    """
    import subprocess
    import platform

    # Normalize input to list
    port_list = [ports] if isinstance(ports, int) else ports

    results = {}
    system = platform.system()

    for port in port_list:
        if not is_port_in_use(port):
            results[port] = None
            if verbose:
                stderr_console.print(f"[dim]Port {port}: [green]AVAILABLE[/green][/dim]")
            continue

        # Port is in use, try to find the process
        process_info = "unknown"

        try:
            if system == "Linux" or system == "Darwin":  # macOS is Darwin
                # Use lsof to find the process
                cmd = ["lsof", "-i", f":{port}", "-sTCP:LISTEN", "-t"]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=2)

                if result.returncode == 0 and result.stdout.strip():
                    pid = result.stdout.strip().split('\n')[0]

                    # Try to get process name
                    try:
                        cmd_name = ["ps", "-p", pid, "-o", "comm="]
                        name_result = subprocess.run(cmd_name, capture_output=True, text=True, timeout=2)
                        if name_result.returncode == 0:
                            process_name = name_result.stdout.strip()
                            process_info = f"{pid}/{process_name}"
                        else:
                            process_info = f"{pid}"
                    except (subprocess.TimeoutExpired, subprocess.SubprocessError):
                        process_info = f"{pid}"

            elif system == "Windows":
                # Use netstat on Windows
                cmd = ["netstat", "-ano"]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=2)

                if result.returncode == 0:
                    for line in result.stdout.split('\n'):
                        if f":{port}" in line and "LISTENING" in line:
                            parts = line.split()
                            if parts:
                                pid = parts[-1]

                                # Try to get process name using tasklist
                                try:
                                    cmd_name = ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"]
                                    name_result = subprocess.run(cmd_name, capture_output=True, text=True, timeout=2)
                                    if name_result.returncode == 0 and name_result.stdout.strip():
                                        # Parse CSV output: "name.exe","PID","Session","Mem"
                                        process_name = name_result.stdout.split(',')[0].strip('"')
                                        process_info = f"{pid}/{process_name}"
                                    else:
                                        process_info = f"{pid}"
                                except (subprocess.TimeoutExpired, subprocess.SubprocessError):
                                    process_info = f"{pid}"
                                break

        except (subprocess.TimeoutExpired, subprocess.SubprocessError, FileNotFoundError):
            # Command not available or failed
            process_info = "unknown"

        results[port] = process_info

        if verbose:
            stderr_console.print(f"[dim]Port {port}: [red]IN USE[/red] by {process_info}[/dim]")

    return results


def report_port_conflicts(ports: Union[int, List[int]], verbose: bool = False) -> Tuple[List[int], Dict[int, Optional[str]]]:
    """
    Check for port conflicts and report them in a user-friendly format.

    Args:
        ports: Port number or list of port numbers to check.
        verbose: Enable verbose output.

    Returns:
        Tuple of (list of ports in use, dict mapping ports to process info).
    """
    ports_with_processes = get_ports_in_use_with_processes(ports, verbose=verbose)

    ports_in_use = [port for port, process in ports_with_processes.items() if process is not None]

    if ports_in_use:
        stderr_console.print(f"\n[yellow]Warning:[/yellow] The following ports are already in use:")
        for port in ports_in_use:
            process = ports_with_processes[port]
            stderr_console.print(f"  • Port {port}: {process}")
        stderr_console.print()

    return ports_in_use, ports_with_processes