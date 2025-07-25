import typer
import docker
import json
from docker.errors import DockerException
from typing import List, Dict, Set
from rich.console import Console
from rich.table import Table
from ..utils.docker_utils import handle_docker_errors

app = typer.Typer(
    name="list",
    help="""
    Scans Docker for Frappe/ERPNext projects and displays their status and ports.
    """,
)

console = Console()


def _format_ports_as_ranges(ports: List[str]) -> str:
    """
    Condenses a sorted list of ports into ranges.
    Example: ['8000', '8001', '8002', '9000'] -> "8000-8002, 9000"
    """
    if not ports:
        return "N/A"

    # Convert string ports to integers for numerical operations
    int_ports = [int(p) for p in ports]

    ranges = []
    start_of_range = int_ports[0]

    for i in range(1, len(int_ports)):
        # If the current port is not sequential, the previous range has ended
        if int_ports[i] != int_ports[i - 1] + 1:
            # Finalize the previous range
            if start_of_range == int_ports[i - 1]:
                ranges.append(str(start_of_range))
            else:
                ranges.append(f"{start_of_range}-{int_ports[i-1]}")
            # Start a new range
            start_of_range = int_ports[i]

    # After the loop, add the final range
    if start_of_range == int_ports[-1]:
        ranges.append(str(start_of_range))
    else:
        ranges.append(f"{start_of_range}-{int_ports[-1]}")

    return ", ".join(ranges)


def _get_container_ports(container) -> Set[str]:
    ports = set()
    if container.ports:
        for _container_port, host_ports in container.ports.items():
            if host_ports:
                for host_port_info in host_ports:
                    if host_port_info and "HostPort" in host_port_info:
                        ports.add(host_port_info["HostPort"])
    if not ports and container.attrs:
        port_bindings = container.attrs.get("HostConfig", {}).get("PortBindings")
        if port_bindings:
            for _container_port, bindings in port_bindings.items():
                if bindings:
                    for binding in bindings:
                        if "HostPort" in binding and binding["HostPort"]:
                            ports.add(binding["HostPort"])
    return ports


@handle_docker_errors
def _list_instances(service_name: str = "frappe") -> List[Dict]:
    client = docker.from_env()
    containers = client.containers.list(
        all=True, filters={"label": f"com.docker.compose.service={service_name}"}
    )

    projects = {}
    for container in containers:
        project_name = container.labels.get("com.docker.compose.project")
        if not project_name:
            continue
        if project_name not in projects:
            projects[project_name] = {"status": container.status, "ports": set()}
        ports = _get_container_ports(container)
        projects[project_name]["ports"].update(ports)

    return [
        {"projectName": name, "ports": sorted(list(data["ports"])), "status": data["status"]}
        for name, data in projects.items()
    ]


@app.callback(invoke_without_command=True)
def default(
    ctx: typer.Context,
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Display all ports individually, without condensing them into ranges.",
        rich_help_panel="Output Formatting",  # Changed panel name for clarity
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        "-q",
        help="Only display project names, one per line. Useful for scripting.",
        rich_help_panel="Output Formatting",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Output the list of instances as a raw JSON string.",
        rich_help_panel="Output Formatting",
    ),
):
    """
    List all Frappe instances managed by Docker Compose.
    """
    if ctx.invoked_subcommand is not None:
        return

    # In quiet or json mode, we don't want the spinner.
    if not quiet and not json_output:
        with console.status(
            "[bold green]Connecting to Docker and fetching instances...[/bold green]"
        ):
            instances = _list_instances()
    else:
        instances = _list_instances()

    if not instances:
        if not quiet and not json_output:
            console.print("[yellow]No Frappe instances found.[/yellow]")
        raise typer.Exit()

    # --- UPDATED LOGIC FOR OUTPUT MODES ---

    if quiet:
        for instance in instances:
            typer.echo(instance["projectName"])
        raise typer.Exit()

    if json_output:
        # Dump the instances list to a formatted JSON string
        json_string = json.dumps(instances, indent=4)
        typer.echo(json_string)
        raise typer.Exit()

    table = Table(title="Caffeinated Whale Instances")
    table.add_column("Project Name", style="cyan", no_wrap=True)
    table.add_column("Status", style="magenta")
    table.add_column("Ports", style="green")

    for instance in instances:
        status = instance["status"]
        if "exited" in status or "dead" in status:
            status_style = f"[red]{status}[/red]"
        elif "running" in status or "healthy" in status:
            status_style = f"[green]{status}[/green]"
        else:
            status_style = f"[yellow]{status}[/yellow]"

        if verbose:
            ports_str = ", ".join(instance["ports"]) if instance["ports"] else "N/A"
        else:
            ports_str = _format_ports_as_ranges(instance["ports"])

        table.add_row(instance["projectName"], status_style, ports_str)

    console.print(table)
