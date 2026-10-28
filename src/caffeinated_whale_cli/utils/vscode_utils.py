import platform
import subprocess

import typer
from rich.console import Console

from ..core import editors
from ..core.errors import CwcliError
from . import config_utils
from .tips import TipSpinner

console_err = Console(stderr=True)

# Use shell=True on Windows to resolve .cmd files in PATH
IS_WINDOWS = platform.system() == "Windows"


# Cursor aliases the Microsoft Dev Containers extension id to its own
# publisher id, so `--list-extensions` on Cursor reports the extension under
# a different id than the one cwcli asks it to install.
DEV_CONTAINERS_EXTENSION_IDS = ("ms-vscode-remote.remote-containers", "anysphere.remote-containers")


def is_dev_containers_installed(vscode_command: str, verbose: bool = False) -> bool:
    """Check if Dev Containers extension is installed (under either its Microsoft or Cursor id)."""
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
        installed = result.stdout.lower()
        return any(ext_id in installed for ext_id in DEV_CONTAINERS_EXTENSION_IDS)
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


def install_extension(
    vscode_command: str, extension_id: str, extension_name: str, verbose: bool = False
) -> bool:
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

        # Some editor CLIs report "already installed" with a non-zero exit;
        # that is success, not a reason to fail the open.
        output = f"{result.stdout}\n{result.stderr}".lower()
        if "already installed" in output:
            console_err.print(f"✓ {extension_name} already installed.")
            return True

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
        vscode_command, "ms-vscode-remote.remote-containers", "Dev Containers extension", verbose
    )


def install_docker_extension(vscode_command: str, verbose: bool = False) -> bool:
    """Install Docker extension."""
    return install_extension(
        vscode_command, "ms-azuretools.vscode-docker", "Docker extension", verbose
    )


def container_name_to_hex(container_name: str) -> str:
    """Convert container name to hexadecimal (like xxd -p)."""
    return editors.container_name_to_hex(container_name)


def open_in_vscode(
    vscode_command: str, container_name: str, bench_path: str, verbose: bool = False
) -> None:
    """
    Open a dev container in VS Code, VS Code Insiders or Cursor: the CLI's spinner
    and exit-code wrapper over ``core.editors.launch``, which owns detection (WSL
    and macOS included), the extension preflight and the attach-URI handover.

    Args:
        vscode_command: Editor id ('code', 'code-insiders', or 'cursor')
        container_name: Docker container name
        bench_path: Path inside the container to open
        verbose: Enable verbose output
    """
    show_tips = config_utils.get_show_tips()
    editor_name = editors.EDITOR_NAMES.get(vscode_command, "VS Code")

    def log(line: str) -> None:
        console_err.print(("✓ " if "installed" in line and "Failed" not in line else "") + line)

    with TipSpinner(
        f"Preparing to open '{container_name}' in {editor_name}",
        console=console_err,
        enabled=show_tips,
    ) as status:
        status.update(f"Opening {editor_name}")
        try:
            uri = editors.launch(vscode_command, container_name, bench_path, log=log)
        except CwcliError as e:
            console_err.print(f"[bold red]✗ {e.message}[/bold red]")
            if e.hint:
                console_err.print(f"[dim]{e.hint}[/dim]")
            raise typer.Exit(code=1) from e
        if verbose:
            console_err.print(f"[dim]VERBOSE: Opened URI: {uri}[/dim]")

    console_err.print(f"[bold green]✓ Opened {container_name} in {editor_name}[/bold green]")
