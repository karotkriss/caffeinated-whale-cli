import functools
import os
import shutil

import docker
import typer
from docker.errors import DockerException
from rich.console import Console

console = Console()
stderr_console = Console(stderr=True)


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


def get_project_containers(
    project_name: str,
) -> list[docker.models.containers.Container] | None:
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
            all=True, filters={"label": f"com.docker.compose.project={project_name}"}
        )
        return containers

    except DockerException:
        return None


def get_frappe_container(project_name: str):
    """
    Get the frappe container for a project.

    Args:
        project_name: The name of the docker-compose project.

    Returns:
        The frappe container object.

    Raises:
        typer.Exit: If project not found or no frappe service exists.
    """
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

    return frappe_container


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
