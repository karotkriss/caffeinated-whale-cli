import shutil
import subprocess
import platform
from typing import Literal
import typer
import questionary
from rich.console import Console

console_err = Console(stderr=True)

# Use shell=True on Windows to resolve .cmd files in PATH
IS_WINDOWS = platform.system() == "Windows"


def is_vscode_installed() -> bool:
    """Check if VS Code (stable) is installed."""
    return shutil.which("code") is not None


def is_vscode_insiders_installed() -> bool:
    """Check if VS Code Insiders is installed."""
    return shutil.which("code-insiders") is not None


def select_vscode_editor() -> Literal["code", "code-insiders", "docker"]:
    """
    Detect available VS Code installations and prompt user to choose.
    Always includes Docker exec as an option.

    Returns:
        str: The selected command ('code', 'code-insiders', or 'docker')
    """
    vscode_stable = is_vscode_installed()
    vscode_insiders = is_vscode_insiders_installed()

    # Build choices based on what's available
    choices = []
    if vscode_stable:
        choices.append("VS Code")
    if vscode_insiders:
        choices.append("VS Code Insiders")

    # Always include Docker as an option
    choices.append("Docker (exec into container)")

    # If only Docker is available, use it directly
    if len(choices) == 1:
        return "docker"

    # Let user choose
    choice = questionary.select("Select editor:", choices=choices).ask()

    if choice == "VS Code":
        return "code"
    elif choice == "VS Code Insiders":
        return "code-insiders"
    else:
        return "docker"


def is_dev_containers_installed(vscode_command: str, verbose: bool = False) -> bool:
    """Check if Dev Containers extension is installed."""
    try:
        cmd = [vscode_command, "--list-extensions"]
        if verbose:
            console_err.print(f"[dim]$ {' '.join(cmd)}[/dim]")
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=5,
            shell=IS_WINDOWS,
        )
        return "ms-vscode-remote.remote-containers" in result.stdout.lower()
    except (subprocess.TimeoutExpired, FileNotFoundError, subprocess.SubprocessError):
        return False


def is_docker_extension_installed(vscode_command: str, verbose: bool = False) -> bool:
    """Check if Docker extension is installed."""
    try:
        cmd = [vscode_command, "--list-extensions"]
        if verbose:
            console_err.print(f"[dim]$ {' '.join(cmd)}[/dim]")
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=5,
            shell=IS_WINDOWS,
        )
        return "ms-azuretools.vscode-docker" in result.stdout.lower()
    except (subprocess.TimeoutExpired, FileNotFoundError, subprocess.SubprocessError):
        return False


def install_extension(vscode_command: str, extension_id: str, extension_name: str, verbose: bool = False) -> bool:
    """Install a VS Code extension."""
    try:
        cmd = [vscode_command, "--install-extension", extension_id]
        if verbose:
            console_err.print(f"[dim]$ {' '.join(cmd)}[/dim]")
        console_err.print(f"Installing {extension_name}...")
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            shell=IS_WINDOWS,
        )

        if result.returncode == 0:
            console_err.print(f"✓ {extension_name} installed successfully.")
            return True
        else:
            console_err.print(
                f"✗ Failed to install {extension_name}: {result.stderr}",
            )
            return False
    except subprocess.TimeoutExpired:
        console_err.print("✗ Installation timed out.")
        return False
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        console_err.print(f"✗ Installation failed: {e}")
        return False


def install_dev_containers_extension(vscode_command: str, verbose: bool = False) -> bool:
    """Install Dev Containers extension."""
    return install_extension(
        vscode_command,
        "ms-vscode-remote.remote-containers",
        "Dev Containers extension",
        verbose
    )


def install_docker_extension(vscode_command: str, verbose: bool = False) -> bool:
    """Install Docker extension."""
    return install_extension(
        vscode_command,
        "ms-azuretools.vscode-docker",
        "Docker extension",
        verbose
    )


def container_name_to_hex(container_name: str) -> str:
    """Convert container name to hexadecimal (like xxd -p)."""
    return container_name.encode().hex()


def open_in_vscode(vscode_command: str, container_name: str, bench_path: str, verbose: bool = False) -> None:
    """
    Open a dev container in VS Code.

    Args:
        vscode_command: VS Code command ('code' or 'code-insiders')
        container_name: Docker container name
        bench_path: Path inside the container to open
        verbose: Enable verbose output
    """
    with console_err.status(f"[bold green]Preparing to open '{container_name}' in VS Code...[/bold green]", spinner="dots") as status:
        # Verify container exists
        status.update("[bold green]Verifying container exists...[/bold green]")
        try:
            cmd = ["docker", "inspect", container_name]
            if verbose:
                console_err.print(f"[dim]$ {' '.join(cmd)}[/dim]")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                console_err.print(f"[bold red]✗ Container '{container_name}' does not exist.[/bold red]")
                raise typer.Exit(code=1)
            if verbose:
                console_err.print(f"[dim]VERBOSE: Container '{container_name}' verified[/dim]")
        except subprocess.TimeoutExpired:
            console_err.print("[bold red]✗ Docker inspect timed out.[/bold red]")
            raise typer.Exit(code=1)
        except FileNotFoundError:
            console_err.print("[bold red]✗ Docker command not found.[/bold red]")
            raise typer.Exit(code=1)

        # Check for Docker extension
        status.update("[bold green]Checking Docker extension...[/bold green]")
        if not is_docker_extension_installed(vscode_command, verbose):
            if verbose:
                console_err.print("[dim]VERBOSE: Docker extension not found, installing...[/dim]")
            if not install_docker_extension(vscode_command, verbose):
                console_err.print("[bold red]✗ Cannot open without Docker extension.[/bold red]")
                raise typer.Exit(code=1)
        elif verbose:
            console_err.print("[dim]VERBOSE: Docker extension already installed[/dim]")

        # Check for Dev Containers extension
        status.update("[bold green]Checking Dev Containers extension...[/bold green]")
        if not is_dev_containers_installed(vscode_command, verbose):
            if verbose:
                console_err.print("[dim]VERBOSE: Dev Containers extension not found, installing...[/dim]")
            if not install_dev_containers_extension(vscode_command, verbose):
                console_err.print("[bold red]✗ Cannot open without Dev Containers extension.[/bold red]")
                raise typer.Exit(code=1)
        elif verbose:
            console_err.print("[dim]VERBOSE: Dev Containers extension already installed[/dim]")

        # Build the VS Code remote URI
        container_hex = container_name_to_hex(container_name)
        uri = f"vscode-remote://attached-container+{container_hex}{bench_path}"

        if verbose:
            console_err.print(f"[dim]VERBOSE: Opening URI: {uri}[/dim]")

        # Open VS Code
        status.update("[bold green]Opening VS Code...[/bold green]")
        try:
            cmd = [vscode_command, "--folder-uri", uri]
            if verbose:
                console_err.print(f"[dim]$ {' '.join(cmd)}[/dim]")
            subprocess.run(cmd, check=True, shell=IS_WINDOWS)
        except subprocess.CalledProcessError as e:
            console_err.print(f"[bold red]✗ Failed to open VS Code: {e}[/bold red]")
            raise typer.Exit(code=1)

    console_err.print(f"[bold green]✓ Opened {container_name} in VS Code[/bold green]")


def exec_into_container(container_name: str) -> None:
    """
    Execute into a Docker container using bash.

    Args:
        container_name: Docker container name
    """
    import os

    typer.echo(f"Opening shell in {container_name}...")
    os.execvp("docker", ["docker", "exec", "-it", container_name, "bash"])
