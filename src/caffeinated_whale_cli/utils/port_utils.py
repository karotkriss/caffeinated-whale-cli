"""
Port management utilities for detecting, checking, and reporting port conflicts.

This module provides functions for:
- Checking if ports are in use
- Finding which projects are using specific ports
- Identifying processes using ports
- Formatting port lists for display
"""

import platform
import socket
import subprocess

from .console import stderr_console


def format_port_list(ports: list[int]) -> str:
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


def get_project_ports(project_name: str) -> list[int]:
    """
    Get all host ports used by a project's containers.

    Args:
        project_name: The name of the docker-compose project.

    Returns:
        List of port numbers (as integers) used by the project.
    """
    from .docker_utils import get_project_containers

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


def find_project_using_ports(ports: int | list[int], exclude_project: str = None) -> dict[int, str]:
    """
    Find which Frappe projects are using specific ports.

    Args:
        ports: Port number or list of port numbers to check.
        exclude_project: Optional project name to exclude from the search.

    Returns:
        Dictionary mapping port numbers to project names (only for ports used by Frappe projects).
    """
    import docker

    from .docker_utils import get_project_containers

    # Normalize input to list
    port_list = [ports] if isinstance(ports, int) else ports

    try:
        client = docker.from_env()
        # Get all Frappe containers
        containers = client.containers.list(
            all=True, filters={"label": "com.docker.compose.service=frappe"}
        )

        port_to_project = {}

        for container in containers:
            project_name = container.labels.get("com.docker.compose.project")
            if not project_name or (exclude_project and project_name == exclude_project):
                continue

            # Get all containers for this project
            project_containers = get_project_containers(project_name)
            if not project_containers:
                continue

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
    except OSError:
        return True


def check_ports_in_use(
    ports: int | list[int], host: str = "0.0.0.0", verbose: bool = False
) -> dict[int, bool]:
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


def get_ports_in_use_with_processes(
    ports: int | list[int], verbose: bool = False
) -> dict[int, str | None]:
    """
    Check which ports are in use and try to identify the process using them.

    Args:
        ports: Port number or list of port numbers to check.
        verbose: Enable verbose output.

    Returns:
        Dictionary mapping port numbers to process information (None if port is available).
        Process info format: "PID/program_name" or "unknown" if cannot be determined.
    """
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
                    pid = result.stdout.strip().split("\n")[0]

                    # Try to get process name
                    try:
                        cmd_name = ["ps", "-p", pid, "-o", "comm="]
                        name_result = subprocess.run(
                            cmd_name, capture_output=True, text=True, timeout=2
                        )
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
                    for line in result.stdout.split("\n"):
                        if f":{port}" in line and "LISTENING" in line:
                            parts = line.split()
                            if parts:
                                pid = parts[-1]

                                # Try to get process name using tasklist
                                try:
                                    cmd_name = [
                                        "tasklist",
                                        "/FI",
                                        f"PID eq {pid}",
                                        "/FO",
                                        "CSV",
                                        "/NH",
                                    ]
                                    name_result = subprocess.run(
                                        cmd_name, capture_output=True, text=True, timeout=2
                                    )
                                    if name_result.returncode == 0 and name_result.stdout.strip():
                                        # Parse CSV output: "name.exe","PID","Session","Mem"
                                        process_name = name_result.stdout.split(",")[0].strip('"')
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


def report_port_conflicts(
    ports: int | list[int], verbose: bool = False
) -> tuple[list[int], dict[int, str | None]]:
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
        stderr_console.print("\n[yellow]Warning:[/yellow] The following ports are already in use:")
        for port in ports_in_use:
            process = ports_with_processes[port]
            stderr_console.print(f"  • Port {port}: {process}")
        stderr_console.print()

    return ports_in_use, ports_with_processes
