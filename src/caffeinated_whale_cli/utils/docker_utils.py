import functools
import os
import platform
import shutil
import subprocess
import sys

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


def _is_wsl() -> bool:
    """Same idiom as ``commands/serve.py``'s WSL-forwarding hint - do not fork it."""
    return "microsoft" in platform.uname().release.lower()


def handle_docker_errors(func):
    """
    A decorator that handles Docker errors with clear distinction between:
    - Docker not installed
    - Docker Desktop stopped on WSL
    - Docker daemon not running
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        # Check whether the Docker CLI resolves in PATH.
        if not shutil.which("docker"):
            if _is_wsl():
                # /usr/bin/docker is a symlink into the Docker Desktop WSL
                # mount (/mnt/wsl/docker-desktop/...), which vanishes while
                # Desktop is stopped - an unresolvable binary here means
                # "stopped", not "never installed".
                stderr_console.print(
                    "[bold red]Error: Docker Desktop appears to be stopped.[/bold red]"
                )
                console.print("Start Docker Desktop on Windows, then re-run this command.")
            else:
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
    Execute into a Docker container using an interactive bash shell.

    Handover is platform-split because ``os.exec*`` semantics differ by OS:

    - POSIX (``os.name != "nt"``): ``os.execvp`` REPLACES the current process.
      The function DOES NOT RETURN - the Python process becomes the docker exec
      session, keeping the same PID and the shell's signal/process-tree behavior
      intact, and when the user exits, the whole process terminates. If execvp
      fails, ``OSError`` is raised (the only way the POSIX path "returns").

    - Windows (``os.name == "nt"``): ``os.exec*`` does NOT replace the caller.
      It spawns a new process via ``CreateProcess`` and terminates Python without
      the launching shell (PowerShell/cmd) waiting on the child, so both the shell
      and the docker-exec bash end up reading the same console and keystrokes
      interleave. Instead we run docker exec as a WAITED child that inherits the
      console and ``sys.exit`` with its return code, so exactly one process owns
      the console at a time and the shell resumes only after the session exits.

    Args:
        container_name: Docker container name
        working_dir: Working directory to start in (optional)

    Raises:
        OSError: If ``os.execvp`` fails to execute docker on the POSIX path.
    """
    typer.echo(f"Opening shell in {container_name}...")

    cmd = ["docker", "exec", "-it"]
    if working_dir:
        cmd += ["-w", working_dir]
    cmd += [container_name, "bash"]

    if os.name == "nt":
        # Windows: no process replacement; run as a waited child owning the console.
        proc = subprocess.run(cmd)
        sys.exit(proc.returncode)

    os.execvp("docker", cmd)
