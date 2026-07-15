import shlex

import typer
from rich.console import Console

from ..utils.completion_utils import complete_project_names
from ..utils.docker_utils import (
    decode_exec_stream,
    get_project_containers,
    handle_docker_errors,
)
from .utils import ensure_containers_running, resolve_bench_path

stderr_console = Console(stderr=True)


@handle_docker_errors
def run(
    project_name: str = typer.Argument(
        ..., help="The Docker Compose project name.", autocompletion=complete_project_names
    ),
    bench_args: list[str] = typer.Argument(..., help="Bench command and arguments to run."),
    bench: str = typer.Option(
        None,
        "--bench",
        help="Which bench to target: its numeric index or label (from 'cwcli inspect').",
    ),
    bench_path: str = typer.Option(
        None,
        "--path",
        "-p",
        help="Explicit bench directory inside the container (lower-level alternative to --bench).",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Auto-start stopped containers without prompting."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose output."),
):
    """
    Execute 'bench <command>' inside the specified project's frappe container.
    """
    # Ensure containers are running, prompt user if not (auto-start with --yes)
    ensure_containers_running(project_name, require_running=True, verbose=verbose, auto_start=yes)

    # Resolve which bench to run against. Falls back to the historical default only
    # when there is no cached bench data to resolve against.
    bench_path = (
        resolve_bench_path(project_name, bench, bench_path, verbose=verbose)
        or "/workspace/frappe-bench"
    )

    containers = get_project_containers(project_name)
    if not containers:
        stderr_console.print(f"[bold red]Error:[/bold red] Project '{project_name}' not found.")
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

    # Build the bench command string
    cmd = "bench " + " ".join(shlex.quote(arg) for arg in bench_args)

    # Create and start a Docker exec instance for real-time streaming
    api = frappe_container.client.api
    exec_id = api.exec_create(frappe_container.id, cmd, workdir=bench_path)["Id"]
    for text in decode_exec_stream(api.exec_start(exec_id, stream=True)):
        typer.echo(text, nl=False)

    # Inspect exit code
    result = api.exec_inspect(exec_id)
    exit_code = result.get("ExitCode", 1)
    raise typer.Exit(code=exit_code)
