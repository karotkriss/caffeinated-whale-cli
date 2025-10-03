import shlex
import typer
from typing import List

from rich.console import Console

from .utils import get_project_containers
from ..utils.docker_utils import handle_docker_errors

stderr_console = Console(stderr=True)


@handle_docker_errors
def run(
    project_name: str = typer.Argument(..., help="The Docker Compose project name."),
    bench_args: List[str] = typer.Argument(
        ..., help="Bench command and arguments to run."
    ),
    bench_path: str = typer.Option(
        "/workspace/frappe-bench",
        "--path",
        "-p",
        help="Path to the bench directory inside the container (default: /workspace/frappe-bench)",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Enable verbose output."
    ),
):
    """
    Execute 'bench <command>' inside the specified project's frappe container.
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

    # Build the bench command string
    cmd = "bench " + " ".join(shlex.quote(arg) for arg in bench_args)

    # Create and start a Docker exec instance for real-time streaming
    api = frappe_container.client.api
    exec_id = api.exec_create(frappe_container.id, cmd, workdir=bench_path)["Id"]
    for chunk in api.exec_start(exec_id, stream=True):
        if isinstance(chunk, (bytes, bytearray)):
            typer.echo(chunk.decode("utf-8"), nl=False)
        else:
            typer.echo(str(chunk), nl=False)

    # Inspect exit code
    result = api.exec_inspect(exec_id)
    exit_code = result.get("ExitCode", 1)
    raise typer.Exit(code=exit_code)
