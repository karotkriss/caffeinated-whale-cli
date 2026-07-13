import json

import typer
from rich.console import Console
from rich.table import Table

from ..core import list as core_list
from ..core.errors import CwcliError
from ..utils.docker_utils import handle_docker_errors, stderr_console

app = typer.Typer(
    name="list",
    help="""
    Scans Docker for Frappe/ERPNext projects and displays their status and ports.
    """,
)

console = Console()


def _format_ports_as_ranges(ports: list[str]) -> str:
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


@app.callback(invoke_without_command=True)
@handle_docker_errors
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
    try:
        if not quiet and not json_output:
            with console.status(
                "[bold green]Connecting to Docker and fetching instances...[/bold green]"
            ):
                instances = core_list.list_instances().data
        else:
            instances = core_list.list_instances().data
    except CwcliError as e:
        # @handle_docker_errors pings first, so a daemon-down is caught above; this
        # only guards the rare race where the daemon dies between ping and list.
        stderr_console.print(f"[bold red]Error:[/bold red] {e.message}")
        raise typer.Exit(code=1) from None

    assert instances is not None  # Status.OK always carries the list (possibly empty)

    # JSON first, so an empty instance set emits a definitive `[]` rather than nothing.
    if json_output:
        rows = [
            {"projectName": i.project_name, "ports": i.ports, "status": i.status} for i in instances
        ]
        typer.echo(json.dumps(rows, indent=4))
        raise typer.Exit()

    if quiet:
        for instance in instances:
            typer.echo(instance.project_name)
        raise typer.Exit()

    if not instances:
        console.print("[yellow]No Frappe instances found.[/yellow]")
        raise typer.Exit()

    table = Table(title="Caffeinated Whale Instances")
    table.add_column("Project Name", style="cyan", no_wrap=True)
    table.add_column("Status", style="magenta")
    table.add_column("Ports", style="green")

    for instance in instances:
        status = instance.status
        if "exited" in status or "dead" in status:
            status_style = f"[red]{status}[/red]"
        elif "running" in status or "healthy" in status:
            status_style = f"[green]{status}[/green]"
        else:
            status_style = f"[yellow]{status}[/yellow]"

        if verbose:
            ports_str = ", ".join(instance.ports) if instance.ports else "N/A"
        else:
            ports_str = _format_ports_as_ranges(instance.ports)

        table.add_row(instance.project_name, status_style, ports_str)

    console.print(table)
