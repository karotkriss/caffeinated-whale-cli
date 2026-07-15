import codecs
import functools
import os
import shutil
from collections.abc import Iterable, Iterator

import docker
import typer
from docker.errors import DockerException
from rich.console import Console

console = Console()
stderr_console = Console(stderr=True)


def utf8_stream_decoder() -> codecs.IncrementalDecoder:
    """A UTF-8 decoder that carries a partial character ACROSS exec-stream chunks.

    Docker frames an exec socket at 32KB and ``bench`` (CPython, talking to a pipe)
    flushes in 8KB blocks, so a chunk boundary lands at an arbitrary BYTE offset -
    routinely mid-character, since Frappe emits unicode (``✓ ✗ → ─ │ └ ⚠ ✅``)
    routinely. Decoding a chunk in isolation is therefore wrong: strict decoding
    raises on the split, and ``errors="replace"`` corrupts the character to U+FFFD.
    One incremental decoder held across the whole stream buffers those leftover
    bytes until the next chunk completes the character.

    ``replace`` still applies to input that is genuinely not UTF-8 - streaming
    another program's output must never kill the CLI - but a mere split is no
    longer mistaken for invalid input.

    Each stream needs its OWN decoder: a demuxed exec (``init``) must not feed
    stderr's bytes into stdout's pending character.
    """
    return codecs.getincrementaldecoder("utf-8")("replace")


def decode_exec_stream(chunks: Iterable[bytes | bytearray | str]) -> Iterator[str]:
    """Decode a docker exec stream into text, tolerating mid-character chunk splits.

    Wraps a non-demuxed ``exec_start(..., stream=True)`` iterator. A trailing
    incomplete character (a truncated stream) is flushed as U+FFFD rather than
    silently dropped, so corruption is visible instead of swallowed.
    """
    decoder = utf8_stream_decoder()
    for chunk in chunks:
        if isinstance(chunk, (bytes, bytearray)):
            text = decoder.decode(bytes(chunk))
        else:
            text = str(chunk)
        if text:
            yield text
    tail = decoder.decode(b"", final=True)
    if tail:
        yield tail


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

        containers: list = client.containers.list(
            all=True, filters={"label": f"com.docker.compose.project={project_name}"}
        )
        return containers

    except DockerException:
        return None


def get_project_volumes(project_name: str):
    """
    Finds all named volumes belonging to a specific Docker Compose project.

    These are the volumes Docker Compose creates and labels (e.g. ``sites``,
    ``db-data``). They are distinct from anonymous container volumes, which
    ``Container.remove(v=True)`` already handles.

    Args:
        project_name: The name of the docker-compose project.

    Returns:
        A list of volume objects, an empty list if none are found,
        or None if there was a Docker connection error.
    """
    try:
        client = docker.from_env()
        client.ping()

        volumes = client.volumes.list(
            filters={"label": f"com.docker.compose.project={project_name}"}
        )
        return volumes

    except DockerException:
        return None


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
    # Lazy import breaks the docker_utils <-> core.docker cycle (core.docker imports
    # get_project_containers from this module at load time).
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
