"""Utility functions for sendme binary management."""

import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path

import requests
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

from .console import console, stderr_console


def copy_to_clipboard(text: str) -> bool:
    """
    Copy text to the system clipboard.

    Args:
        text: The text to copy.

    Returns:
        True if successful, False otherwise.
    """
    system = platform.system()
    try:
        if system == "Darwin":  # macOS
            subprocess.run(["pbcopy"], input=text, text=True, check=True, capture_output=True)
            return True
        elif system == "Linux":
            if shutil.which("xclip"):
                subprocess.run(
                    ["xclip", "-selection", "clipboard"],
                    input=text,
                    text=True,
                    check=True,
                    capture_output=True,
                )
                return True
            elif shutil.which("xsel"):
                subprocess.run(
                    ["xsel", "--clipboard", "--input"],
                    input=text,
                    text=True,
                    check=True,
                    capture_output=True,
                )
                return True
        elif system == "Windows":
            subprocess.run(["clip"], input=text, text=True, check=True, capture_output=True)
            return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    return False


def get_sendme_install_dir() -> Path:
    """Get the directory where sendme should be installed."""
    return Path.home() / ".local" / "bin"


def get_sendme_path() -> Path:
    """Get the expected path to the sendme binary."""
    install_dir = get_sendme_install_dir()
    if platform.system() == "Windows":
        return install_dir / "sendme.exe"
    return install_dir / "sendme"


def is_sendme_installed() -> bool:
    """Check if sendme is installed and accessible."""
    # Check in our install directory first
    sendme_path = get_sendme_path()
    if sendme_path.exists() and os.access(sendme_path, os.X_OK):
        return True

    # Check if it's in PATH
    return shutil.which("sendme") is not None


def get_platform_target() -> str:
    """Determine the platform target for sendme download."""
    system = platform.system()
    machine = platform.machine().lower()

    if system == "Windows":
        return "windows-x86_64"
    elif system == "Darwin":  # macOS
        if machine in ("arm64", "aarch64"):
            return "darwin-aarch64"
        return "darwin-x86_64"
    elif system == "Linux":
        if machine in ("arm64", "aarch64"):
            return "linux-aarch64"
        return "linux-x86_64"
    else:
        # Default to Linux x86_64
        return "linux-x86_64"


def download_file_with_progress(url: str, destination: Path) -> bool:
    """
    Download a file with a progress bar.

    Args:
        url: URL to download from
        destination: Path to save the file

    Returns:
        True if successful, False otherwise
    """
    try:
        response = requests.get(url, stream=True, timeout=30)
        response.raise_for_status()

        total_size = int(response.headers.get("content-length", 0))

        with Progress(
            TextColumn("[bold blue]{task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Downloading sendme", total=total_size)

            with open(destination, "wb") as file:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        file.write(chunk)
                        progress.update(task, advance=len(chunk))

        return True
    except Exception as e:
        stderr_console.print(f"[bold red]Error:[/bold red] Failed to download: {e}")
        return False


def install_sendme(verbose: bool = False) -> bool:
    """
    Install sendme binary to ~/.local/bin.

    Args:
        verbose: Enable verbose output

    Returns:
        True if installation successful, False otherwise
    """
    console.print("[bold cyan]Installing sendme...[/bold cyan]")

    # Create install directory
    install_dir = get_sendme_install_dir()
    install_dir.mkdir(parents=True, exist_ok=True)

    # Get platform target
    target = get_platform_target()
    if verbose:
        stderr_console.print(f"[dim]Platform target: {target}[/dim]")

    # Fetch latest release info
    repo = "n0-computer/sendme"
    release_url = f"https://api.github.com/repos/{repo}/releases/latest"

    try:
        if verbose:
            stderr_console.print(f"[dim]Fetching release info from {release_url}[/dim]")

        response = requests.get(release_url, timeout=10)
        response.raise_for_status()
        release_data = response.json()

        # Find the download URL for our platform
        download_url = None
        for asset in release_data.get("assets", []):
            if target in asset.get("browser_download_url", ""):
                download_url = asset["browser_download_url"]
                break

        if not download_url:
            stderr_console.print(
                f"[bold red]Error:[/bold red] No release found for platform: {target}"
            )
            return False

        if verbose:
            stderr_console.print(f"[dim]Download URL: {download_url}[/dim]")

        # Download to temporary directory
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            # Determine if it's a zip or tar.gz
            if download_url.endswith(".zip"):
                archive_path = temp_path / "sendme.zip"
            else:
                archive_path = temp_path / "sendme.tar.gz"

            # Download with progress bar
            if not download_file_with_progress(download_url, archive_path):
                return False

            console.print("[bold green]✓[/bold green] Download complete")

            # Extract archive
            console.print("[dim]Extracting...[/dim]")
            if archive_path.suffix == ".zip":
                import zipfile

                with zipfile.ZipFile(archive_path, "r") as zip_ref:
                    zip_ref.extractall(temp_path)
            else:
                import tarfile

                with tarfile.open(archive_path, "r:gz") as tar_ref:
                    tar_ref.extractall(temp_path)

            # Find the sendme binary
            binary_name = "sendme.exe" if platform.system() == "Windows" else "sendme"
            sendme_binary = None

            for file in temp_path.rglob(binary_name):
                sendme_binary = file
                break

            if not sendme_binary or not sendme_binary.exists():
                stderr_console.print(
                    f"[bold red]Error:[/bold red] Could not find {binary_name} in archive"
                )
                return False

            # Copy to install directory
            dest_path = install_dir / binary_name
            shutil.copy2(sendme_binary, dest_path)

            # Make executable on Unix systems
            if platform.system() != "Windows":
                os.chmod(dest_path, 0o755)

            console.print(f"[bold green]✓[/bold green] Installed to {dest_path}")

            # Setup PATH
            setup_path(install_dir, verbose)

            return True

    except requests.RequestException as e:
        stderr_console.print(f"[bold red]Error:[/bold red] Network error: {e}")
        return False
    except Exception as e:
        stderr_console.print(f"[bold red]Error:[/bold red] Installation failed: {e}")
        if verbose:
            import traceback

            stderr_console.print(traceback.format_exc())
        return False


def setup_path(install_dir: Path, verbose: bool = False):
    """
    Setup PATH in shell RC files.

    Args:
        install_dir: Directory containing sendme binary
        verbose: Enable verbose output
    """
    install_dir_str = str(install_dir)

    # Check if already in PATH
    path_env = os.environ.get("PATH", "")
    if install_dir_str in path_env:
        if verbose:
            stderr_console.print("[dim]Install directory already in PATH[/dim]")
        return

    console.print("[dim]Setting up PATH...[/dim]")

    # Detect shell and RC file
    shell_configs = []

    if platform.system() == "Windows":
        # Windows: Update user PATH via PowerShell
        try:
            ps_command = f"""
$path = [Environment]::GetEnvironmentVariable('Path', 'User')
if ($path -notlike '*{install_dir_str}*') {{
    [Environment]::SetEnvironmentVariable('Path', "$path;{install_dir_str}", 'User')
    Write-Output 'Added to PATH'
}} else {{
    Write-Output 'Already in PATH'
}}
"""
            result = subprocess.run(
                ["powershell", "-Command", ps_command],
                capture_output=True,
                text=True,
                check=False,
            )
            if verbose:
                stderr_console.print(f"[dim]PowerShell output: {result.stdout.strip()}[/dim]")

            console.print(
                "[bold green]✓[/bold green] Added to PATH (restart terminal to take effect)"
            )
        except Exception as e:
            stderr_console.print(f"[yellow]Warning:[/yellow] Could not update PATH: {e}")
    else:
        # Unix: Add to shell RC files
        home = Path.home()

        # Detect available shells
        possible_configs = [
            home / ".bashrc",
            home / ".bash_profile",
            home / ".zshrc",
            home / ".profile",
            home / ".config" / "fish" / "config.fish",
        ]

        for config in possible_configs:
            if config.exists():
                shell_configs.append(config)

        if not shell_configs:
            # Create .bashrc if no config exists
            bashrc = home / ".bashrc"
            bashrc.touch()
            shell_configs.append(bashrc)

        # Add PATH export to each config
        path_export = f'\n# Added by cwcli for sendme\nexport PATH="$PATH:{install_dir_str}"\n'

        for config in shell_configs:
            try:
                # Check if already added
                content = config.read_text()
                if install_dir_str in content:
                    if verbose:
                        stderr_console.print(f"[dim]Already in {config.name}[/dim]")
                    continue

                # Append PATH export
                with open(config, "a") as f:
                    f.write(path_export)

                if verbose:
                    stderr_console.print(f"[dim]Updated {config.name}[/dim]")

            except Exception as e:
                stderr_console.print(
                    f"[yellow]Warning:[/yellow] Could not update {config.name}: {e}"
                )

        if shell_configs:
            console.print(
                "[bold green]✓[/bold green] Added to PATH (restart terminal or run: source ~/.bashrc)"
            )


def ensure_sendme_installed(verbose: bool = False) -> bool:
    """
    Ensure sendme is installed, installing if necessary.

    Args:
        verbose: Enable verbose output

    Returns:
        True if sendme is available, False otherwise
    """
    if is_sendme_installed():
        if verbose:
            stderr_console.print("[dim]sendme is already installed[/dim]")
        return True

    console.print()
    console.print("[yellow]sendme is not installed.[/yellow]")
    console.print("[dim]sendme is required for remote backup transfers.[/dim]")
    console.print()

    # Auto-install
    console.print("[bold cyan]Installing sendme automatically...[/bold cyan]")
    success = install_sendme(verbose)

    if success:
        console.print()
        console.print("[bold green]✓[/bold green] sendme installed successfully!")
        console.print(
            "[dim]You may need to restart your terminal for PATH changes to take effect.[/dim]"
        )
        console.print()
        return True
    else:
        console.print()
        stderr_console.print("[bold red]✗[/bold red] Failed to install sendme")
        stderr_console.print()
        stderr_console.print("[bold]Manual installation:[/bold]")
        stderr_console.print("  Visit: https://github.com/n0-computer/sendme")
        stderr_console.print("  Or run: curl -fsSL https://iroh.computer/sendme.sh | sh")
        console.print()
        return False


def get_sendme_command() -> str:
    """
    Get the sendme command to use.

    Returns:
        Full path to sendme binary or 'sendme' if in PATH
    """
    # Check our install directory first
    sendme_path = get_sendme_path()
    if sendme_path.exists() and os.access(sendme_path, os.X_OK):
        return str(sendme_path)

    # Check PATH
    sendme_in_path = shutil.which("sendme")
    if sendme_in_path:
        return sendme_in_path

    return "sendme"  # Fallback, will likely fail
