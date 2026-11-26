"""Utility functions for sendme binary management.

On a platform cwcli pins a sendme release for, it installs only that exact release
asset, checking the archive and the extracted binary against the SHA-256 it ships.
Every run copies the installed binary into a fresh private directory, hashing the
bytes as it copies them, and executes that copy only when its hash matches, so the
bytes that run are the bytes that were checked. On a platform with no pinned build,
cwcli uses the sendme on PATH as it is, unchecked, and never installs one.
"""

import hashlib
import os
import platform
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import requests
from rich.markup import escape
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


def get_sendme_install_dir(home: Path | None = None) -> Path:
    """Where cwcli installs sendme and looks for it first: ``~/.local/bin`` of the
    account cwcli runs as (``home`` names another account's home, read-only use)."""
    return (home or Path.home()) / ".local" / "bin"


def get_sendme_path() -> Path:
    """Get the expected path to the sendme binary."""
    install_dir = get_sendme_install_dir()
    if platform.system() == "Windows":
        return install_dir / "sendme.exe"
    return install_dir / "sendme"


def find_sendme() -> str | None:
    """The sendme cwcli would run: the install dir first, then PATH. A lookup only;
    ``sendme_command`` is what checks it is the pinned release."""
    path = get_sendme_path()
    if path.exists() and os.access(path, os.X_OK):
        return str(path)
    return shutil.which("sendme")


def get_platform_target() -> str | None:
    """This platform's pinned sendme target, or None when cwcli pins no build for it
    (never another platform's)."""
    system = {"Windows": "windows", "Darwin": "darwin", "Linux": "linux"}.get(platform.system())
    machine = platform.machine().lower()
    arch = {"x86_64": "x86_64", "amd64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}
    if system is None or machine not in arch:
        return None
    target = f"{system}-{arch[machine]}"
    return target if target in _PINNED else None


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


class SendmeInstallError(Exception):
    """No sendme matching the pinned release could be installed; ``str(error)`` says why."""


PINNED_VERSION = "v0.37.1"
SENDME_DOWNLOAD_URL = f"https://github.com/n0-computer/sendme/releases/download/{PINNED_VERSION}"
# target: (release asset, its sha256, the sha256 of the sendme binary inside it)
_PINNED = {
    "linux-x86_64": (
        "sendme-v0.37.1-linux-x86_64.tar.gz",
        "6250f28ccf082a2f77a5bb2244c821783c8746b6cff4539be32cbcc03ef7d0a0",
        "5203634d8688495e972c7f8a988ff5a371619ace78d93d7eb9dcb062f269be71",
    ),
    "linux-aarch64": (
        "sendme-v0.37.1-linux-aarch64.tar.gz",
        "8dcf195435ca1465b801f4f08923faf34a7f98e7700280d870f1a83093173f64",
        "af1f668857cb37fe35d21dcaeb6a18c3ffe916be4ed124c1dcecabed1f591d3d",
    ),
    "darwin-x86_64": (
        "sendme-v0.37.1-darwin-x86_64.tar.gz",
        "955c653546cf2b939528fe89ea24c2ea2021777774f58b18f47954a88169e3ee",
        "66f500ce717a994e5ecda70edc3f9568b3f9afe09a2303ba80a92e012917ad5a",
    ),
    "darwin-aarch64": (
        "sendme-v0.37.1-darwin-aarch64.tar.gz",
        "3c2502860b87cd2500a3295dbad6fbff3355318122e17f1a1f9638086e4a2bcd",
        "422eda74d65e70bdf4c772c1a3a2abcfdf19f477a3e88c7ba92d652e745ad2b4",
    ),
    "windows-x86_64": (
        "sendme-v0.37.1-windows-x86_64.zip",
        "fcf37ac1ebfff164f0a8e0cd0bd592531a028dac364ef6b05bff277b3d11faf5",
        "f83055bcbc6e6ac35731385e7126cd5653375cc498b571018bfeae38096af50d",
    ),
}


def pinned_release() -> tuple[str, str, str] | None:
    """This platform's pinned release: (asset name, archive sha256, binary sha256), or
    None when cwcli pins no build for this platform."""
    target = get_platform_target()
    return _PINNED[target] if target is not None else None


def _no_pinned_build() -> str:
    return (
        f"there is no pinned sendme build for this platform ({platform.system()} "
        f"{platform.machine()}); install sendme by hand and put it on PATH"
    )


def sha256_file(path: Path | str) -> str:
    """The file's SHA-256 as lowercase hex; ``OSError`` when it cannot be read."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def is_pinned_sendme(path: Path | str) -> bool:
    """Whether ``path`` is the pinned sendme binary for this platform."""
    release = pinned_release()
    try:
        return release is not None and sha256_file(path) == release[2]
    except OSError:
        return False


def manual_install_command() -> str:
    """The exact command that installs the pinned sendme by hand where cwcli looks for
    it (``get_sendme_install_dir()`` of the user running cwcli), checking its sha256."""
    install_dir = get_sendme_install_dir()
    release = pinned_release()
    if release is None:
        return "install sendme from https://github.com/n0-computer/sendme and put it on PATH"
    asset, archive_sha, _binary_sha = release
    url = f"{SENDME_DOWNLOAD_URL}/{asset}"
    if platform.system() == "Windows":
        return (
            f"download {url}, check that (Get-FileHash {asset}).Hash is "
            f"{archive_sha.upper()}, and extract sendme.exe into {install_dir}"
        )
    check = "shasum -a 256 -c -" if platform.system() == "Darwin" else "sha256sum -c -"
    quoted = shlex.quote(str(install_dir))
    return (
        f"mkdir -p {quoted} && cd {quoted} && curl -fsSLO {url} && "
        f"echo '{archive_sha}  {asset}' | {check} && tar -xzf {asset} sendme && rm {asset}"
    )


def _download_quietly(url: str, destination: Path) -> bool:
    """``download_file_with_progress`` without the progress bar or any output; a
    failure raises (``requests.RequestException``/``OSError``) so the caller has the reason."""
    response = requests.get(url, stream=True, timeout=30)
    response.raise_for_status()
    with open(destination, "wb") as file:
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                file.write(chunk)
    return True


def fetch_sendme(*, progress: bool = True, verbose: bool = False) -> Path:
    """Download the pinned sendme release into ``get_sendme_install_dir()`` and return
    the installed binary's path. The archive's sha256 is checked before it is
    extracted and the binary's before it is installed. Raises ``SendmeInstallError``
    naming why it failed.

    ``progress=False`` prints nothing at all, for callers that own stdout (the web UI's
    jobs, ``--json`` output); it neither shows a progress bar nor edits shell RC files.
    """
    install_dir = get_sendme_install_dir()
    release = pinned_release()
    if release is None:
        raise SendmeInstallError(_no_pinned_build())
    asset, archive_sha, binary_sha = release
    download_url = f"{SENDME_DOWNLOAD_URL}/{asset}"
    if verbose and progress:
        stderr_console.print(f"[dim]Download URL: {download_url}[/dim]")

    try:
        install_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            archive_path = temp_path / asset

            if progress:
                if not download_file_with_progress(download_url, archive_path):
                    raise SendmeInstallError(f"Failed to download {download_url}")
                console.print("[bold green]✓[/bold green] Download complete")
                console.print("[dim]Extracting...[/dim]")
            else:
                _download_quietly(download_url, archive_path)

            if sha256_file(archive_path) != archive_sha:
                raise SendmeInstallError(
                    f"the downloaded {asset} does not match its pinned sha256; not installing it"
                )

            extracted = temp_path / "extracted"
            if asset.endswith(".zip"):
                import zipfile

                with zipfile.ZipFile(archive_path, "r") as zip_ref:
                    zip_ref.extractall(extracted)
            else:
                import tarfile

                with tarfile.open(archive_path, "r:gz") as tar_ref:
                    tar_ref.extractall(extracted)

            binary_name = "sendme.exe" if platform.system() == "Windows" else "sendme"
            sendme_binary = next(extracted.rglob(binary_name), None)
            if sendme_binary is None or not sendme_binary.is_file():
                raise SendmeInstallError(f"Could not find {binary_name} in {asset}")
            if sha256_file(sendme_binary) != binary_sha:
                raise SendmeInstallError(
                    f"{binary_name} in {asset} does not match its pinned sha256; not installing it"
                )

            dest_path = install_dir / binary_name
            fd, staged = tempfile.mkstemp(prefix=f".{binary_name}.", dir=install_dir)
            os.close(fd)
            try:
                shutil.copy2(sendme_binary, staged)
                if platform.system() != "Windows":
                    os.chmod(staged, 0o755)
                os.replace(staged, dest_path)
            except BaseException:
                Path(staged).unlink(missing_ok=True)
                raise

            return dest_path

    except SendmeInstallError:
        raise
    except requests.RequestException as e:
        raise SendmeInstallError(f"Network error: {e}") from e
    except Exception as e:
        raise SendmeInstallError(f"Installation failed: {e}") from e


def _copy_if_pinned(source: str, dest: Path, pinned_sha256: str) -> bool:
    """Copy ``source`` to ``dest`` (0700), hashing the bytes as they are written; keep
    the copy only when that hash is the pinned one. An unreadable source is a miss."""
    digest = hashlib.sha256()
    try:
        with open(source, "rb") as src:
            fd = os.open(
                dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o700
            )
            with os.fdopen(fd, "wb") as out:
                for block in iter(lambda: src.read(1 << 20), b""):
                    digest.update(block)
                    out.write(block)
    except OSError:
        dest.unlink(missing_ok=True)
        return False
    if digest.hexdigest() != pinned_sha256:
        dest.unlink()
        return False
    os.chmod(dest, 0o700)
    return True


@contextmanager
def sendme_command(
    *,
    progress: bool = True,
    verbose: bool = False,
    on_install: Callable[[str], None] | None = None,
) -> Iterator[str]:
    """The sendme to run, for the length of the ``with`` block.

    With a pinned build for this platform it is a private copy of the installed
    binary, in a fresh 0700 directory removed when the block ends (on error and on
    interrupt too), whose bytes were hashed as they were copied and match the pin. A
    missing or mismatched install is replaced by the pinned release once (``on_install``
    hears why first) and copied again. Without a pinned build it is the sendme found on
    PATH, unchecked. Raises ``SendmeInstallError`` naming the problem when there is no
    sendme it may run."""
    release = pinned_release()
    if release is None:
        found = find_sendme()
        if found is None:
            raise SendmeInstallError(_no_pinned_build())
        yield found
        return
    private = Path(tempfile.mkdtemp(prefix="cwcli-sendme-"))
    try:
        copy = private / get_sendme_path().name
        found = find_sendme()
        if found is None or not _copy_if_pinned(found, copy, release[2]):
            problem = (
                "sendme is not installed"
                if found is None
                else f"{found} is not the pinned sendme {PINNED_VERSION} (its sha256 differs)"
            )
            if on_install is not None:
                on_install(problem)
            try:
                fetch_sendme(progress=progress, verbose=verbose)
            except SendmeInstallError as e:
                raise SendmeInstallError(f"{problem}, and installing it failed: {e}") from e
            found = find_sendme()
            if found is None or not _copy_if_pinned(found, copy, release[2]):
                raise SendmeInstallError(
                    f"{problem}, and after installing the pinned release "
                    f"{found or get_sendme_path()} still does not match its sha256"
                )
        yield str(copy)
    finally:
        shutil.rmtree(private, ignore_errors=True)


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
    Ensure a sendme cwcli may run is available: the pinned release (installing it, or
    replacing a copy that does not match its sha256, if necessary), or, on a platform
    with no pinned build, a sendme on PATH.

    Args:
        verbose: Enable verbose output

    Returns:
        True if sendme is available, False otherwise
    """
    installing: list[str] = []

    def announce(problem: str) -> None:
        installing.append(problem)
        console.print()
        console.print(f"[yellow]{problem}.[/yellow]")
        console.print("[dim]sendme is required for remote backup transfers.[/dim]")
        console.print()
        console.print(f"[bold cyan]Installing sendme {PINNED_VERSION}...[/bold cyan]")

    try:
        with sendme_command(verbose=verbose, on_install=announce):
            pass
    except SendmeInstallError as e:
        console.print()
        stderr_console.print(f"[bold red]✗[/bold red] {escape(str(e))}", highlight=False)
        stderr_console.print()
        stderr_console.print("[bold]Install it by hand:[/bold]")
        stderr_console.print(f"  {manual_install_command()}", markup=False, highlight=False)
        console.print()
        return False

    if not installing:
        if verbose:
            stderr_console.print("[dim]sendme is already installed[/dim]")
        return True
    path = get_sendme_path()
    console.print(f"[bold green]✓[/bold green] Installed to {path}")
    setup_path(path.parent, verbose)
    console.print()
    console.print("[bold green]✓[/bold green] sendme installed successfully!")
    console.print(
        "[dim]You may need to restart your terminal for PATH changes to take effect.[/dim]"
    )
    console.print()
    return True
