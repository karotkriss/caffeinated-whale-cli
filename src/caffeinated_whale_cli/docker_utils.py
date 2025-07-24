import os
import shutil
import docker
import typer
import functools
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
                stderr_console.print("[bold red]Error: Could not connect to Docker daemon.[/bold red]")
                console.print(str(e))
            raise typer.Exit(code=1)

        return func(*args, **kwargs)

    return wrapper
