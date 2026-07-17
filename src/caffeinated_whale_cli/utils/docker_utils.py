import functools
import os
import shutil

import docker
import typer
from docker.errors import DockerException
from rich.console import Console

console = Console()
stderr_console = Console(stderr=True)

# NOTE: the UI-free Docker primitives ``utf8_stream_decoder``,
# ``get_project_containers`` and ``get_project_volumes`` now live in
# ``core/docker.py`` so the UI-pure core never imports this module (which pulls
# ``typer``/``rich`` at load time). Import them from there, not from here.


def handle_docker_errors(func):
    """
    A decorator that handles Docker errors with clear distinction between:
    - Docker not installed
    - Docker daemon not running
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        # Check if Docker is installed (in PATH)
        if not shutil.which("docker"):
            stderr_console.print("[bold red]Error: Docker is not installed.[/bold red]")
            console.print("Please install Docker from https://www.docker.com/get-started")
            raise typer.Exit(code=1)

        try:
            # Check if Docker daemon is running
            client = docker.from_env()
            client.ping()
        except DockerException as e:
            # Check for specific Windows named pipe error
            if os.name == "nt" and "CreateFile" in str(e):
                stderr_console.print("[bold red]Error: Docker daemon is not running.[/bold red]")
                console.print("You may need to start Docker Desktop.")
            else:
                stderr_console.print(
                    "[bold red]Error: Could not connect to Docker daemon.[/bold red]"
                )
                console.print(str(e))
            raise typer.Exit(code=1) from None

        return func(*args, **kwargs)

    return wrapper


def get_frappe_container(project_name: str):
    """
    Get the frappe container for a project (CLI wrapper).

    Thin frontend over the core accessor ``core.docker.get_frappe_container``:
    it is the single resolution implementation; this wrapper only translates the
    typed :class:`CwcliError` it raises into the historical stderr message and
    ``typer.Exit(1)``, preserving the exact CLI behavior.

    Args:
        project_name: The name of the docker-compose project.

    Returns:
        The frappe container object.

    Raises:
        typer.Exit: If project not found, no frappe service exists, or the daemon
            is unreachable.
    """
    # Lazy import keeps this UI module free of a load-time dependency on the core.
    from ..core import docker as core_docker
    from ..core.errors import CwcliError

    try:
        return core_docker.get_frappe_container(project_name)
    except CwcliError as e:
        stderr_console.print(f"[bold red]Error:[/bold red] {e.message}")
        raise typer.Exit(code=1) from None


def exec_into_container(container_name: str, working_dir: str | None = None) -> None:
    """
    Execute into a Docker container using bash.

    IMPORTANT: This function uses os.execvp() which REPLACES the current process.
    The function DOES NOT RETURN. After this call:
    - The Python process is replaced by the docker exec process
    - No code after this function call will execute
    - No cleanup handlers in the calling code will run
    - The process ID (PID) remains unchanged
    - If execvp fails, OSError is raised (this is the only way the function "returns")

    This is intentional behavior for interactive shell sessions - the user's
    shell becomes the docker exec session, and when they exit, the entire
    Python process terminates.

    Args:
        container_name: Docker container name
        working_dir: Working directory to start in (optional)

    Raises:
        OSError: If os.execvp() fails to execute docker command
    """
    typer.echo(f"Opening shell in {container_name}...")

    if working_dir:
        # Use -w flag to set working directory
        os.execvp("docker", ["docker", "exec", "-it", "-w", working_dir, container_name, "bash"])
    else:
        os.execvp("docker", ["docker", "exec", "-it", container_name, "bash"])
