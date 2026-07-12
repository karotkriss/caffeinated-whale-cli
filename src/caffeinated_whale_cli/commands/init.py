"""
Simplified init command for cwcli.

The init command creates a complete Frappe development environment in a single step:
1. Creates a project directory structure in ~/.cwcli/projects/{project_name}/
2. Downloads docker-compose.yml from GitHub to conf/ subdirectory
3. Starts Docker Compose containers
4. Initializes a Frappe bench inside the container
5. Creates a new site with the specified configuration
6. Optionally installs ERPNext

Directory structure:
    ~/.cwcli/projects/{project_name}/
        conf/
            docker-compose.yml

All Docker volumes and persistence are scoped to the project directory, ensuring
complete isolation between projects.

This approach is lightweight and organized - no need to clone the entire frappe_docker
repository. All project files are stored in a dedicated projects directory.

Key features:
* Automatic project setup - no manual repository cloning needed
* Downloads only essential files from GitHub (< 10KB vs entire repo)
* All projects organized in ~/.cwcli/projects/
* Custom port selection with --port flag
* Optional ERPNext installation with `--install-erpnext`
* Interactive prompts for project/bench/site names if not provided

Example:
    cwcli init my-project
    # Creates ~/.cwcli/projects/my-project/conf/docker-compose.yml
    # Starts containers
    # Initializes bench and site
"""

import re
import secrets
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import questionary
import typer

from caffeinated_whale_cli.commands.config import add_path

from ..utils import config_utils, db_utils
from ..utils.completion_utils import complete_project_names
from ..utils.console import console, stderr_console
from ..utils.docker_utils import get_frappe_container, handle_docker_errors
from ..utils.port_utils import check_ports_in_use, format_port_list
from ..utils.tips import TipSpinner
from .utils import ensure_containers_running


@dataclass
class InitInputs:
    project_name: str
    bench_name: str
    site_name: str


def _validate_slug(value: str, field_label: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        stderr_console.print(f"[bold red]Error:[/bold red] {field_label} is required.")
        raise typer.Exit(code=1)

    allowed = "abcdefghijklmnopqrstuvwxyz0123456789-_"
    if not all(char in allowed for char in cleaned.lower()):
        stderr_console.print(
            f"[bold red]Error:[/bold red] {field_label} must contain only lowercase letters, "
            "numbers, dashes, or underscores."
        )
        raise typer.Exit(code=1)

    if cleaned[0] in "-_" or cleaned[-1] in "-_":
        stderr_console.print(
            f"[bold red]Error:[/bold red] {field_label} cannot start or end with '-' or '_'."
        )
        raise typer.Exit(code=1)

    return cleaned.lower()


def _validate_site_name(value: str) -> str:
    cleaned = value.strip().lower()
    if not cleaned:
        stderr_console.print("[bold red]Error:[/bold red] Site name is required.")
        raise typer.Exit(code=1)
    if not cleaned.endswith(".localhost"):
        stderr_console.print("[bold red]Error:[/bold red] Site name must end with '.localhost'.")
        raise typer.Exit(code=1)
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789-."
    if not all(char in allowed for char in cleaned):
        stderr_console.print(
            "[bold red]Error:[/bold red] Site name may only include lowercase letters, "
            "numbers, hyphens, and periods."
        )
        raise typer.Exit(code=1)
    return cleaned


def _prompt_for_inputs(project_name: str | None, bench_name: str, site_name: str) -> InitInputs:
    """Prompt the user for project name if missing.

    Note: bench_name and site_name now have default values, so they're never None.
    Only project_name will be prompted if not provided.
    """
    try:
        # Only prompt for project name if not provided
        if project_name is None:
            container_answer = questionary.text(
                "Docker project / container prefix (e.g. frappe-app)",
                default="",
                validate=lambda text: bool(text.strip()),
            ).ask()
            if container_answer is None:
                raise typer.Exit(code=0)
        else:
            container_answer = project_name
    except KeyboardInterrupt:
        stderr_console.print("\n[yellow]Operation cancelled.[/yellow]")
        raise typer.Exit(code=0) from None

    return InitInputs(
        project_name=_validate_slug(container_answer, "Project name"),
        bench_name=_validate_slug(bench_name, "Bench name"),
        site_name=_validate_site_name(site_name),
    )


def _exec_in_container(
    container,
    command: str,
    *,
    description: str | None = None,
    stream_output: bool = False,
    verbose: bool = False,
    environment: dict | None = None,
) -> None:
    """Execute a command inside a Docker container using the Docker API.

    ``environment`` is forwarded to ``exec_create`` so secrets can be referenced
    as unexpanded ``$VAR`` in ``command`` and expanded by the in-container shell
    at exec time - keeping them out of the verbose echo and off the ``bash -lc``
    wrapper argv / exec ``Cmd`` record, which mirrors ``restore.py``'s M5 pattern.
    (The leaf process still receives the expanded value on its own argv, an
    unavoidable consequence of a flag-only interface - see AGENTS.md.)
    """
    if verbose:
        stderr_console.print(f"[dim]$ {command}[/dim]")

    # Only show description when streaming output (verbose mode shows command instead)
    if description and stream_output and not verbose:
        stderr_console.print(f"{description}")

    exec_id = container.client.api.exec_create(
        container.id,
        ["bash", "-lc", command],
        environment=environment,
    )["Id"]

    try:
        if stream_output:
            for stdout, stderr in container.client.api.exec_start(exec_id, stream=True, demux=True):
                if stdout:
                    # Use raw sys.stdout.write to preserve carriage returns for progress bars
                    sys.stdout.write(stdout.decode("utf-8", errors="replace"))
                    sys.stdout.flush()
                if stderr:
                    sys.stderr.write(stderr.decode("utf-8", errors="replace"))
                    sys.stderr.flush()
        else:
            output = container.client.api.exec_start(exec_id, stream=False)
            # Only print output in verbose mode
            if output and verbose:
                console.print(output.decode("utf-8", errors="replace"))

        result = container.client.api.exec_inspect(exec_id)
    except Exception as exc:  # defensive against docker api errors
        stderr_console.print(f"[bold red]Error:[/bold red] Docker API error: {exc}")
        raise typer.Exit(code=1) from exc

    exit_code = result.get("ExitCode", 1)
    if exit_code != 0:
        # Check output for ENOSPC to give a more actionable error message
        raw_output = output if not stream_output else b""
        decoded = raw_output.decode("utf-8", errors="replace") if raw_output else ""
        if "ENOSPC" in decoded or "no space left on device" in decoded.lower():
            stderr_console.print(
                "[bold red]Error:[/bold red] No space left on device inside the container. "
                "Free up disk space and try again."
            )
        else:
            stderr_console.print(
                f"[bold red]Error:[/bold red] Command failed with exit code {exit_code}: {command}"
            )
        raise typer.Exit(code=1)


def _generate_admin_password() -> str:
    """Generate a strong, shell-safe admin password.

    ``token_urlsafe`` yields ``[A-Za-z0-9_-]`` only, so it never needs quoting.
    """
    return secrets.token_urlsafe(18)


def _is_interactive_session() -> bool:
    """True only for a real interactive terminal (both stdin and stdout TTYs).

    Gates whether a generated admin password may be shown to a human. If either
    stream is redirected the password would leak into a captured log, so init
    refuses instead and requires ``--admin-password``.
    """
    return sys.stdin.isatty() and sys.stdout.isatty()


def _directory_exists(container, path: str) -> bool:
    """Return True if a directory exists inside the container."""
    exit_code, _ = container.exec_run(["bash", "-lc", f"test -d {shlex.quote(path)}"])
    return bool(exit_code == 0)


def _ensure_directory(container, path: str) -> None:
    """Create a directory inside the container if it does not exist."""
    exit_code, output = container.exec_run(["bash", "-lc", f"mkdir -p {shlex.quote(path)}"])
    if exit_code != 0:
        message = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else output
        stderr_console.print(
            f"[bold red]Error:[/bold red] Failed to create directory '{path}': {message}"
        )
        raise typer.Exit(code=1)


def _bench_name_validation(value: str) -> bool | str:
    """Validate a replacement bench name typed at the reuse-decline prompt.

    Returns ``True`` when valid, or an error string so questionary re-prompts in
    place. A blank value is treated as valid so the caller's cancel path can
    handle it (leaving the prompt blank cancels). The non-blank rules match
    :func:`_validate_slug`.
    """
    cleaned = value.strip()
    if not cleaned:
        return True

    lowered = cleaned.lower()
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789-_"
    if not all(char in allowed for char in lowered):
        return "Bench name must contain only lowercase letters, numbers, dashes, or underscores."
    if lowered[0] in "-_" or lowered[-1] in "-_":
        return "Bench name cannot start or end with '-' or '_'."
    return True


def _resolve_bench_target(
    frappe_container,
    bench_parent_path: str,
    bench_name: str,
    reuse_bench: bool | None = None,
) -> tuple[str, str, bool]:
    """Resolve which bench to set up inside the container.

    Returns ``(bench_name, bench_full_path, bench_exists)``.

    ``reuse_bench`` pre-answers the existing-bench question so the command is
    drivable non-interactively (issue #41):

    - ``True`` (``--reuse-bench``): reuse the existing bench with no prompt
      (``bench init`` is skipped downstream, exactly like an interactive Yes).
    - ``False`` (``--no-reuse-bench``): refuse to reuse; error and exit 1 so the
      caller must pass a fresh ``--bench`` name (never enters the rename loop).
    - ``None`` (default): ask interactively on a TTY (the issue #20 loop below);
      on a non-TTY, refuse with an honest exit 1 instead of hanging on an idle
      pipe or crashing with ``EOFError`` inside questionary.

    When ``reuse_bench is None`` and the terminal is interactive, declining reuse
    does NOT abort: the user is prompted for a different bench name so site setup
    can continue on a fresh bench (issue #20). A blank name or a cancelled prompt
    exits cleanly (code 0, "No changes made.") without making changes.
    ``bench_exists`` is True when an existing bench will be reused (so
    ``bench init`` is skipped) and False when a fresh bench must be initialized.
    """
    bench_full_path = f"{bench_parent_path}/{bench_name}"
    bench_exists = _directory_exists(frappe_container, bench_full_path)

    # Non-interactive pre-answers for the existing-bench case. Each branch either
    # returns or exits, so the interactive loop below is only reached when
    # reuse_bench is None AND stdin is a TTY.
    if bench_exists:
        if reuse_bench is True:
            add_path(bench_full_path)
            return bench_name, bench_full_path, True
        if reuse_bench is False:
            stderr_console.print(
                f"[bold red]Error:[/bold red] Bench '{bench_full_path}' already exists and "
                "--no-reuse-bench was given. Pass a different --bench name to create a new bench."
            )
            raise typer.Exit(code=1)
        if not sys.stdin.isatty():
            stderr_console.print(
                f"[bold red]Error:[/bold red] Bench '{bench_full_path}' already exists and this is "
                "a non-interactive session. Pass --reuse-bench to reuse it, or --no-reuse-bench "
                "with a different --bench name to create a fresh bench."
            )
            raise typer.Exit(code=1)

    while bench_exists:
        console.print(f"[yellow]Bench '{bench_name}' already exists at {bench_full_path}.[/yellow]")
        reuse = questionary.confirm(
            f"Reuse the existing bench '{bench_name}' and continue with site setup?",
            default=True,
            auto_enter=False,
        ).ask()
        if reuse is None:
            # Prompt cancelled (e.g. Ctrl-C).
            console.print("[yellow]No changes made.[/yellow]")
            raise typer.Exit(code=0)
        if reuse:
            break

        # The user declined to reuse the existing bench. Let them name a
        # different bench and continue setup instead of dead-ending.
        new_name = questionary.text(
            "Enter a different bench name to create (leave blank to cancel):",
            default="",
            validate=_bench_name_validation,
        ).ask()
        if not new_name or not new_name.strip():
            console.print("[yellow]No changes made.[/yellow]")
            raise typer.Exit(code=0)

        bench_name = new_name.strip().lower()
        bench_full_path = f"{bench_parent_path}/{bench_name}"
        bench_exists = _directory_exists(frappe_container, bench_full_path)

    add_path(bench_full_path)
    return bench_name, bench_full_path, bench_exists


def _get_pyenv_python_version(container, prefix: str, verbose: bool = False) -> str | None:
    """Find a pyenv Python version matching the given prefix (e.g. '3.12') inside the container."""
    exit_code, output = container.exec_run(["bash", "-lc", "ls ~/.pyenv/versions"])
    if exit_code != 0:
        if verbose:
            stderr_console.print("[dim]Could not list pyenv versions[/dim]")
        return None

    versions: list[str] = output.decode("utf-8", errors="replace").split()
    for version in versions:
        if version.startswith(prefix + "."):
            if verbose:
                stderr_console.print(f"[dim]Found pyenv version: {version}[/dim]")
            return version

    if verbose:
        stderr_console.print(f"[dim]No pyenv version matching {prefix}.x found[/dim]")
    return None


def _install_pyenv_python(container, prefix: str, verbose: bool = False) -> str | None:
    """Install the latest Python version matching prefix (e.g. '3.12') via pyenv inside the container.

    Queries ``pyenv install --list`` for available versions, picks the latest
    matching ``<prefix>.<patch>`` release, installs it, and returns the version string.
    """
    import re

    # Find the latest available version matching the prefix
    exit_code, output = container.exec_run(["bash", "-lc", "pyenv install --list"])
    if exit_code != 0:
        if verbose:
            stderr_console.print("[dim]Could not list available pyenv versions[/dim]")
        return None

    pattern = re.compile(rf"^\s*({re.escape(prefix)}\.\d+)\s*$", re.MULTILINE)
    matches: list[str] = pattern.findall(output.decode("utf-8", errors="replace"))
    if not matches:
        if verbose:
            stderr_console.print(f"[dim]No available pyenv version matching {prefix}.x[/dim]")
        return None

    # Last match is the latest patch version
    target = matches[-1]
    if verbose:
        stderr_console.print(f"[dim]Installing Python {target} via pyenv...[/dim]")

    exit_code, install_output = container.exec_run(
        ["bash", "-lc", f"pyenv install {target}"],
        environment={"PYTHON_CONFIGURE_OPTS": "--enable-shared"},
    )
    if exit_code != 0:
        message = install_output.decode("utf-8", errors="replace") if install_output else ""
        stderr_console.print(
            f"[bold red]Error:[/bold red] Failed to install Python {target} via pyenv"
        )
        if verbose and message:
            stderr_console.print(f"[dim]{message}[/dim]")
        return None

    if verbose:
        stderr_console.print(f"[dim]Successfully installed Python {target}[/dim]")
    return target


def _get_nvm_node_version(container, major: str, verbose: bool = False) -> str | None:
    """Find an installed nvm Node.js version matching the given major (e.g. '16') inside the container."""
    exit_code, output = container.exec_run(["bash", "-lc", "ls ~/.nvm/versions/node/"])
    if exit_code != 0:
        if verbose:
            stderr_console.print("[dim]Could not list nvm Node.js versions[/dim]")
        return None

    versions: list[str] = output.decode("utf-8", errors="replace").split()
    for version in versions:
        # Entries look like v16.20.2, v22.22.0
        if version.startswith(f"v{major}."):
            if verbose:
                stderr_console.print(f"[dim]Found Node.js version: {version}[/dim]")
            return version

    if verbose:
        stderr_console.print(f"[dim]No Node.js version matching v{major}.x found[/dim]")
    return None


def _install_nvm_node(container, major: str, verbose: bool = False) -> str | None:
    """Install Node.js for the given major version via nvm inside the container."""
    if verbose:
        stderr_console.print(f"[dim]Installing Node.js {major} via nvm...[/dim]")

    exit_code, output = container.exec_run(
        ["bash", "-lc", f"source ~/.nvm/nvm.sh && nvm install {major}"]
    )
    if exit_code != 0:
        message = output.decode("utf-8", errors="replace") if output else ""
        stderr_console.print(
            f"[bold red]Error:[/bold red] Failed to install Node.js {major} via nvm"
        )
        if verbose and message:
            stderr_console.print(f"[dim]{message}[/dim]")
        return None

    # Retrieve the installed version
    installed = _get_nvm_node_version(container, major, verbose=False)
    if verbose and installed:
        stderr_console.print(f"[dim]Successfully installed Node.js {installed}[/dim]")
    return installed


def _build_cd_command(path: str, command: str) -> str:
    return f"cd {shlex.quote(path)} && {command}"


DEFAULT_FRAPPE_BRANCH = "version-16"

# Official SemVer 2.0.0 grammar (https://semver.org): MAJOR.MINOR.PATCH with an
# optional pre-release and build metadata. A full match resolves to a git tag;
# a bare integer major resolves to a version-N branch instead.
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)


def resolve_frappe_ref(version: str) -> str:
    """Resolve a ``--version`` value to a git ref for ``bench init``.

    - a bare integer ``N`` -> branch ``version-N`` (e.g. ``16`` -> ``version-16``)
    - a semantic version ``X.Y.Z`` -> tag ``vX.Y.Z`` (e.g. ``16.26.3`` -> ``v16.26.3``)

    The value must satisfy the SemVer 2.0.0 grammar to resolve to a tag; a
    malformed value (e.g. ``16.26`` or ``latest``) raises ``ValueError``.
    """
    value = version.strip()
    if re.fullmatch(r"\d+", value):
        return f"version-{value}"
    if _SEMVER_RE.match(value):
        return f"v{value}"
    raise ValueError(
        f"Invalid --version value {version!r}: expected a bare major version "
        f"(e.g. 16 -> version-16) or a full semantic version "
        f"(e.g. 16.26.3 -> v16.26.3)."
    )


def _resolve_frappe_branch(frappe_branch: str | None, version: str | None) -> str:
    """Resolve the effective Frappe git ref from the mutually-exclusive
    ``--frappe-branch`` / ``--version`` flags.

    Defaults to :data:`DEFAULT_FRAPPE_BRANCH` when neither is given. A malformed
    ``--version`` or passing both flags prints an error and exits non-zero.
    """
    if frappe_branch is not None and version is not None:
        stderr_console.print(
            "[bold red]Error:[/bold red] --frappe-branch and --version are "
            "mutually exclusive; pass only one."
        )
        raise typer.Exit(code=1)
    if version is not None:
        try:
            return resolve_frappe_ref(version)
        except ValueError as exc:
            stderr_console.print(f"[bold red]Error:[/bold red] {exc}")
            raise typer.Exit(code=1) from None
    if frappe_branch is not None:
        return frappe_branch
    return DEFAULT_FRAPPE_BRANCH


def _frappe_major_version(ref: str) -> int | None:
    """Extract the major Frappe version from a resolved git ref.

    Handles both the branch form (``version-16`` -> 16) and the tag form
    (``v16.26.3`` -> 16). Returns ``None`` for refs with no leading numeric
    major (e.g. ``develop``), so version gating falls through to the modern
    defaults.
    """
    m = re.match(r"^(?:version-|v)(\d+)", ref)
    return int(m.group(1)) if m else None


def _select_mariadb_flag(frappe_branch: str) -> str:
    """Select the ``bench new-site`` MariaDB flag for the given Frappe ref.

    ``--mariadb-user-host-login-scope`` only exists in bench/Frappe 15+, so
    versions 14 and older must fall back to ``--no-mariadb-socket``. Works for
    both branch refs (``version-14``) and tag refs (``v14.80.0``).
    """
    major = _frappe_major_version(frappe_branch)
    if major is not None and major <= 14:
        return "--no-mariadb-socket"
    return "--mariadb-user-host-login-scope=%"


def _run_host_command(
    cmd: list[str],
    cwd: str | None = None,
    description: str | None = None,
    use_spinner: bool = False,
    capture_output: bool = True,
) -> None:
    """Run a command on the host system and raise if it fails."""
    if use_spinner and description:
        with stderr_console.status(f"[bold cyan]{description}...[/bold cyan]", spinner="dots"):
            result = subprocess.run(cmd, cwd=cwd, capture_output=True)
    else:
        result = subprocess.run(cmd, cwd=cwd, capture_output=capture_output)

    if result.returncode != 0:
        stderr_console.print(f"[bold red]Error:[/bold red] Host command failed: {' '.join(cmd)}")
        if capture_output and result.stderr:
            stderr_console.print(result.stderr.decode("utf-8", errors="replace"))
        raise typer.Exit(code=1)


def _get_latest_bench_tag(verbose: bool = False) -> str:
    """Query Docker Hub for the latest semver tag of frappe/bench.

    Returns the most recently updated tag matching ``v<major>.<minor>.<patch>``.
    Falls back to a known-good version if the API call fails.
    """
    import json
    import re
    import urllib.request

    fallback = "v5.29.1"
    api_url = "https://hub.docker.com/v2/repositories/frappe/bench/tags/?page_size=25&ordering=last_updated"

    try:
        req = urllib.request.Request(api_url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        semver_re = re.compile(r"^v\d+\.\d+\.\d+$")
        for result in data.get("results", []):
            tag: str = result.get("name", "")
            if semver_re.match(tag):
                if verbose:
                    stderr_console.print(f"[dim]Resolved latest bench image tag: {tag}[/dim]")
                return tag
    except Exception:
        if verbose:
            stderr_console.print(
                f"[dim]Could not fetch latest bench tag, using fallback: {fallback}[/dim]"
            )

    return fallback


def _download_github_file(url: str, dest_path: Path) -> None:
    """Download a file from GitHub raw URL."""
    import urllib.request

    try:
        urllib.request.urlretrieve(url, dest_path)
    except Exception as e:
        stderr_console.print(f"[bold red]Error:[/bold red] Failed to download {url}: {e}")
        raise typer.Exit(code=1) from None


def _pull_compose_images(project_name: str, project_dir: Path, verbose: bool = False) -> None:
    """Pull Docker images for the compose project."""
    cmd = ["docker", "compose", "-p", project_name, "-f", "docker-compose.yml", "pull"]
    if not verbose:
        cmd.append("--quiet")
    _run_host_command(
        cmd,
        cwd=str(project_dir),
        description=None,  # Description shown by caller
        use_spinner=False,
        capture_output=not verbose,  # Show output in verbose mode
    )


def _start_compose_project(project_name: str, project_dir: Path, verbose: bool = False) -> None:
    """Run docker compose up -d in the project directory to start services."""
    cmd = ["docker", "compose", "-p", project_name, "-f", "docker-compose.yml", "up", "-d"]
    _run_host_command(
        cmd,
        cwd=str(project_dir),
        description=None,  # Description shown by caller
        use_spinner=False,
        capture_output=not verbose,  # Show output only in verbose mode
    )


def _setup_project_directory(project_name: str, verbose: bool = False) -> Path:
    """
    Create project directory structure and download essential files from GitHub.

    Structure:
        ~/.cwcli/projects/{project_name}/
            conf/
                docker-compose.yml

    Returns the path to the conf directory (where docker-compose.yml is located).
    """
    from ..utils.config_utils import PROJECTS_DIR

    # Create project directory structure
    project_dir = PROJECTS_DIR / project_name
    conf_dir = project_dir / "conf"
    conf_dir.mkdir(parents=True, exist_ok=True)

    if verbose:
        stderr_console.print(f"[dim]Project directory: {project_dir}[/dim]")
        stderr_console.print(f"[dim]Config directory: {conf_dir}[/dim]")

    # GitHub raw URLs for frappe_docker devcontainer setup
    compose_url = "https://raw.githubusercontent.com/frappe/frappe_docker/refs/heads/main/devcontainer-example/docker-compose.yml"

    # Download compose file to conf directory (progress bar will be shown by _download_github_file)
    compose_path = conf_dir / "docker-compose.yml"
    if not compose_path.exists():
        _download_github_file(compose_url, compose_path)

    return conf_dir


def _customize_compose_ports(
    compose_path: Path, port: int, verbose: bool = False, spinner=None
) -> None:
    """
    Customize the port mappings and frappe image tag in docker-compose.yml.

    Args:
        compose_path: Path to the docker-compose.yml file
        port: Starting port number (e.g., 8000)
        verbose: Print detailed information
        spinner: Optional spinner to update status

    The function replaces:
    - Web server ports: 8000-8005 → {port}-{port+5}
    - SocketIO ports: 9000-9005 → {port+1000}-{port+1005}
    - Frappe bench image tag: latest → latest stable semver from Docker Hub
    """
    if spinner:
        spinner.update(
            f"Customizing ports: {port}-{port+5} (web), {port+1000}-{port+1005} (socketio)"
        )
    elif verbose:
        stderr_console.print(
            f"[dim]Customizing ports: {port}-{port+5} (web), {port+1000}-{port+1005} (socketio)[/dim]"
        )

    content = compose_path.read_text()

    # Replace web server port range
    content = content.replace("8000-8005:8000-8005", f"{port}-{port+5}:8000-8005")

    # Replace socketio port range
    socketio_start = port + 1000
    content = content.replace(
        "9000-9005:9000-9005", f"{socketio_start}-{socketio_start+5}:9000-9005"
    )

    # Always pin the bench image to the latest stable semver tag (never use :latest)
    if spinner:
        spinner.update("Resolving latest bench image tag from Docker Hub")
    elif verbose:
        stderr_console.print("[dim]Resolving latest bench image tag from Docker Hub...[/dim]")
    bench_tag = _get_latest_bench_tag(verbose=verbose)
    if verbose:
        stderr_console.print(f"[dim]Pinning bench image: docker.io/frappe/bench:{bench_tag}[/dim]")
    content = content.replace(
        "docker.io/frappe/bench:latest", f"docker.io/frappe/bench:{bench_tag}"
    )

    compose_path.write_text(content)


def _wait_for_containers_running(
    project_name: str,
    *,
    verbose: bool = False,
    attempts: int = 10,
    delay: float = 0.5,
) -> bool:
    """Poll for the frappe container to reach 'running' after ``compose up -d``.

    Right after ``docker compose up -d`` a container briefly reports
    ``created``/``starting``; a single status check therefore mis-reads a normal
    slow start as "not running". This bounded, silent poll (``prompt=False``,
    ``auto_start=False`` so it never prompts and never starts anything) re-checks
    status a few times over ``attempts * delay`` seconds. It is safe to call
    under a live spinner precisely because it never prompts; the caller runs the
    interactive prompt/refusal OUTSIDE the spinner only if this returns False.
    """
    for attempt in range(attempts):
        if ensure_containers_running(
            project_name,
            require_running=True,
            auto_start=False,
            prompt=False,
            verbose=verbose,
        ):
            return True
        if attempt < attempts - 1:
            time.sleep(delay)
    return False


@handle_docker_errors
def init(
    project_name: str | None = typer.Argument(
        None,
        help="Docker Compose project name. If not provided, will prompt interactively.",
        autocompletion=complete_project_names,
    ),
    port: int = typer.Option(
        8000,
        "--port",
        "-P",
        help="Starting port for the project. Creates ports {port}-{port+5} for web servers and {port+1000}-{port+1005} for socketio.",
    ),
    bench_name: str = typer.Option(
        "frappe-bench",
        "--bench",
        "-b",
        help="Bench directory to create inside the container. Defaults to 'frappe-bench'.",
    ),
    site_name: str = typer.Option(
        "development.localhost",
        "--site",
        "-s",
        help="Primary site to create (must end with .localhost). Defaults to 'development.localhost'.",
    ),
    bench_parent: str = typer.Option(
        "/workspace",
        "--bench-parent",
        help="Directory inside the container where the bench will be created.",
    ),
    frappe_branch: str | None = typer.Option(
        None,
        "--frappe-branch",
        help=(
            "Frappe branch or tag for bench init (e.g. version-16 or v16.26.3). "
            "Mutually exclusive with --version. Default: version-16."
        ),
    ),
    version: str | None = typer.Option(
        None,
        "--version",
        help=(
            "Frappe version for bench init, resolved by shape: a bare major "
            "(16 -> version-16 branch) or a full semantic version "
            "(16.26.3 -> v16.26.3 tag). Mutually exclusive with --frappe-branch."
        ),
    ),
    db_root_password: str = typer.Option(
        "123",
        "--db-root-password",
        help="MariaDB root password used for bench new-site.",
    ),
    admin_password: str | None = typer.Option(
        None,
        "--admin-password",
        help=(
            "Administrator password for the new site, used verbatim (no strength "
            "check). If omitted, a strong password is generated and printed once "
            "in an interactive run; a non-interactive run must supply this flag."
        ),
    ),
    auto_start: bool = typer.Option(
        False,
        "--auto-start",
        help="Automatically start containers if they are not running.",
    ),
    reuse_bench: bool | None = typer.Option(
        None,
        "--reuse-bench/--no-reuse-bench",
        help=(
            "When the target bench directory already exists: --reuse-bench reuses it "
            "(skips bench init), --no-reuse-bench requires a fresh --bench name and errors "
            "if it exists. Default: ask interactively (refuse on a non-TTY). Distinct from "
            "--auto-start, which controls container startup."
        ),
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Show verbose docker exec output for quick commands.",
    ),
    install_erpnext: bool = typer.Option(
        False,
        "--install-erpnext",
        help="Install the ERPNext application onto the created site after initialization.",
    ),
    erpnext_branch: str = typer.Option(
        "version-16",
        "--erpnext-branch",
        help="ERPNext branch to use when fetching the app (used with --install-erpnext).",
    ),
) -> None:
    """
    Initialize a new Frappe project with bench and site.

    Creates project directory, downloads compose files, starts containers,
    initializes bench, and creates a site. Optionally installs ERPNext.

    If project_name is not provided, prompts interactively.

    The site's administrator password is generated and printed once when
    --admin-password is omitted in an interactive run; a non-interactive run
    must pass --admin-password. A supplied value is used verbatim.

    Examples:
        cwcli init
        cwcli init my-project
        cwcli init my-project --bench my-bench --site mysite.localhost
        cwcli init my-project --version 16 --install-erpnext
        cwcli init my-project --version 16.26.3
        cwcli init my-project --frappe-branch version-16 --admin-password mypass
    """
    # Resolve the Frappe git ref first so a malformed --version fails fast,
    # before any project dir / container work.
    frappe_branch = _resolve_frappe_branch(frappe_branch, version)

    # Resolve the admin password before any container work so a missing one
    # fails fast. A supplied value is used verbatim (bench is the only backstop);
    # when omitted, an interactive run gets a generated one (printed once at the
    # end), while a non-interactive run refuses rather than leak a generated
    # secret into a captured log.
    admin_password_generated = False
    if admin_password is None:
        if _is_interactive_session():
            admin_password = _generate_admin_password()
            admin_password_generated = True
        else:
            stderr_console.print(
                "[bold red]Error:[/bold red] No --admin-password supplied and this is a "
                "non-interactive session. Pass --admin-password to set the site's "
                "administrator password."
            )
            raise typer.Exit(code=1)

    start_time = time.time()

    # Prompt for project name if not provided
    inputs = _prompt_for_inputs(project_name, bench_name, site_name)

    # Check for port conflicts before proceeding
    web_ports = list(range(port, port + 6))  # e.g., 8000-8005
    socketio_ports = list(range(port + 1000, port + 1006))  # e.g., 9000-9005
    all_ports = web_ports + socketio_ports

    port_status = check_ports_in_use(all_ports)
    ports_in_use = [p for p, in_use in port_status.items() if in_use]

    if ports_in_use:
        stderr_console.print(
            f"[bold red]Error:[/bold red] The following ports are already in use: {format_port_list(ports_in_use)}"
        )
        stderr_console.print(
            "\n[yellow]Tip:[/yellow] Use the [cyan]--port[/cyan] flag to select a different starting port."
        )
        stderr_console.print(f"[dim]Example: cwcli init {inputs.project_name} --port 10000[/dim]")
        raise typer.Exit(code=1)

    # Get tips configuration
    show_tips = config_utils.get_show_tips()

    if verbose:
        # Verbose mode: no spinner, show all output
        console.print()
        conf_dir = _setup_project_directory(inputs.project_name, verbose=verbose)
        compose_path = conf_dir / "docker-compose.yml"
        _customize_compose_ports(compose_path, port, verbose=verbose, spinner=None)
        stderr_console.print("[dim]Pulling Docker images...[/dim]")
        _pull_compose_images(inputs.project_name, conf_dir, verbose=verbose)
        _start_compose_project(inputs.project_name, conf_dir, verbose=verbose)
        # Bounded silent poll first (a container is often 200ms from ready right
        # after 'compose up -d'); only prompt/refuse if it is genuinely not up.
        if not _wait_for_containers_running(inputs.project_name, verbose=verbose):
            ensure_containers_running(
                inputs.project_name, require_running=True, auto_start=auto_start
            )
        frappe_container = get_frappe_container(inputs.project_name)
    else:
        # Non-verbose mode: use spinners for user feedback
        with TipSpinner(
            f"Setting up project '{inputs.project_name}'",
            console=stderr_console,
            enabled=show_tips,
        ) as spinner:
            spinner.update("Creating project directory")
            conf_dir = _setup_project_directory(inputs.project_name, verbose=verbose)
            compose_path = conf_dir / "docker-compose.yml"
            _customize_compose_ports(compose_path, port, verbose=verbose, spinner=spinner)

            # Pull Docker images (can take a while)
            spinner.update("Pulling Docker images")
            _pull_compose_images(inputs.project_name, conf_dir)

            # Start containers
            spinner.update("Starting Docker Compose containers")
            _start_compose_project(inputs.project_name, conf_dir, verbose=verbose)

            spinner.update("Waiting for containers to be ready")
            # Silent bounded poll (prompt=False) is safe under the spinner; the
            # interactive prompt/refusal below runs only AFTER the spinner exits,
            # so a slow container start never paints a confirm under the spinner
            # (the repo's known deadlock pattern).
            containers_running = _wait_for_containers_running(inputs.project_name, verbose=verbose)

        # Spinner has exited. If the containers are still not up, prompt/refuse
        # OUTSIDE the spinner, then fetch the frappe container.
        if not containers_running:
            ensure_containers_running(
                inputs.project_name, require_running=True, auto_start=auto_start
            )
        frappe_container = get_frappe_container(inputs.project_name)

    # Prepare bench paths inside the container
    bench_parent_path = bench_parent.rstrip("/") or "/workspace"

    # Create parent directory inside the container
    _ensure_directory(frappe_container, bench_parent_path)

    # Resolve which bench to set up (the spinner has already exited, so the
    # prompts below own the terminal). If the bench already exists, the user is
    # asked whether to reuse it; declining lets them pick a different bench name
    # and continue site setup instead of aborting (issue #20).
    inputs.bench_name, bench_full_path, bench_exists = _resolve_bench_target(
        frappe_container, bench_parent_path, inputs.bench_name, reuse_bench=reuse_bench
    )

    # Initialize bench if it doesn't exist
    if not bench_exists:
        # Determine Python and Node.js requirements per branch
        env_prefix = ""
        nvm_prefix = ""
        branch_python = {15: "3.12", 14: "3.10", 13: "3.9"}
        branch_node = {14: "16", 13: "14"}
        frappe_major = _frappe_major_version(frappe_branch)

        python_prefix = branch_python.get(frappe_major) if frappe_major is not None else None
        if python_prefix:
            py_version = _get_pyenv_python_version(frappe_container, python_prefix, verbose=verbose)
            if not py_version:
                if not verbose:
                    stderr_console.print(
                        f"[yellow]Python {python_prefix} not found, installing via pyenv...[/yellow]"
                    )
                py_version = _install_pyenv_python(frappe_container, python_prefix, verbose=verbose)
            if py_version:
                env_prefix = f"PYENV_VERSION={py_version} "
                if verbose:
                    stderr_console.print(
                        f"[dim]Using PYENV_VERSION={py_version} for {frappe_branch}[/dim]"
                    )

        node_major = branch_node.get(frappe_major) if frappe_major is not None else None
        if node_major:
            node_version = _get_nvm_node_version(frappe_container, node_major, verbose=verbose)
            if not node_version:
                if not verbose:
                    stderr_console.print(
                        f"[yellow]Node.js {node_major} not found, installing via nvm...[/yellow]"
                    )
                node_version = _install_nvm_node(frappe_container, node_major, verbose=verbose)
            if node_version:
                nvm_prefix = f"source ~/.nvm/nvm.sh && nvm use {node_version} && "
                if verbose:
                    stderr_console.print(
                        f"[dim]Using Node.js {node_version} for {frappe_branch}[/dim]"
                    )
                if verbose:
                    stderr_console.print(
                        f"[dim]Installing yarn globally for Node.js {node_version}...[/dim]"
                    )
                yarn_exit_code, _ = frappe_container.exec_run(
                    [
                        "bash",
                        "-lc",
                        f"source ~/.nvm/nvm.sh && nvm use {node_version} && npm install -g yarn",
                    ]
                )
                if yarn_exit_code != 0:
                    stderr_console.print(
                        "[yellow]Warning: Failed to install yarn globally.[/yellow]"
                    )
                elif verbose:
                    stderr_console.print("[dim]yarn installed successfully.[/dim]")

        bench_init_cmd = _build_cd_command(
            bench_parent_path,
            nvm_prefix
            + env_prefix
            + " ".join(
                [
                    "bench",
                    "init",
                    "--skip-redis-config-generation",
                    "--frappe-branch",
                    shlex.quote(frappe_branch),
                    shlex.quote(inputs.bench_name),
                    "--verbose",
                ]
            ),
        )

        if verbose:
            # Verbose mode: stream output without spinner
            console.print()
            _exec_in_container(
                frappe_container,
                bench_init_cmd,
                description=f"Initializing bench '{inputs.bench_name}' (this may take a while)",
                stream_output=True,
                verbose=verbose,
            )
        else:
            # Non-verbose mode: use spinner
            with TipSpinner(
                f"Initializing bench '{inputs.bench_name}'",
                console=stderr_console,
                enabled=show_tips,
            ):
                _exec_in_container(
                    frappe_container,
                    bench_init_cmd,
                    stream_output=False,
                    verbose=False,
                )

    # Pin setuptools<82 for version-13 to retain pkg_resources
    if _frappe_major_version(frappe_branch) == 13:
        pin_cmd = _build_cd_command(bench_full_path, "./env/bin/pip install 'setuptools<82'")
        if verbose:
            stderr_console.print("[dim]Pinning setuptools<82 for version-13...[/dim]")
        pin_exit_code, pin_output = frappe_container.exec_run(["bash", "-lc", pin_cmd])
        if pin_exit_code != 0:
            msg = pin_output.decode("utf-8", errors="replace") if pin_output else ""
            stderr_console.print("[yellow]Warning: Failed to pin setuptools<82.[/yellow]")
            if verbose and msg:
                stderr_console.print(f"[dim]{msg}[/dim]")
        elif verbose:
            stderr_console.print("[dim]setuptools pinned successfully.[/dim]")

    # Continue with bench/site configuration in a spinner
    with TipSpinner(
        f"Configuring bench '{inputs.bench_name}'",
        console=stderr_console,
        enabled=show_tips,
    ) as spinner:
        # Configure bench hosts inside the container (db and redis services)
        spinner.update("Configuring bench database and Redis connections")
        configs = [
            ("bench set-config -g db_host mariadb"),
            ("bench set-config -g redis_cache redis://redis-cache:6379"),
            ("bench set-config -g redis_queue redis://redis-queue:6379"),
            ("bench set-config -g redis_socketio redis://redis-queue:6379"),
        ]
        for command in configs:
            _exec_in_container(
                frappe_container,
                _build_cd_command(bench_full_path, command),
                stream_output=False,
                verbose=False,
            )

        # Check if site exists
        site_path = f"{bench_full_path}/sites/{inputs.site_name}"
        site_exists = _directory_exists(frappe_container, site_path)

    # Create site if it doesn't exist
    if not site_exists:
        mariadb_flag = _select_mariadb_flag(frappe_branch)
        # Both passwords ride in the environment and are referenced as unexpanded
        # $VARs, so they stay out of the verbose echo and off the bash -lc wrapper
        # argv - the printed command is identical to the executed one. Mirrors
        # restore.py's M5 pattern. The db-root default "123" is coupled to the
        # compose's MYSQL_ROOT_PASSWORD, so it is not randomized, only shielded.
        new_site_env = {
            "CWCLI_DB_ROOT_PASSWORD": db_root_password,
            "CWCLI_ADMIN_PASSWORD": admin_password,
        }
        new_site_cmd = _build_cd_command(
            bench_full_path,
            " ".join(
                [
                    "bench",
                    "new-site",
                    "--db-root-password",
                    '"$CWCLI_DB_ROOT_PASSWORD"',
                    "--admin-password",
                    '"$CWCLI_ADMIN_PASSWORD"',
                    mariadb_flag,
                    shlex.quote(inputs.site_name),
                    "--verbose",
                ]
            ),
        )

        if verbose:
            # Verbose mode: stream output without spinner
            console.print()
            _exec_in_container(
                frappe_container,
                new_site_cmd,
                description=f"Creating site '{inputs.site_name}'",
                stream_output=True,
                verbose=verbose,
                environment=new_site_env,
            )
        else:
            # Non-verbose mode: use spinner
            with TipSpinner(
                f"Creating site '{inputs.site_name}'",
                console=stderr_console,
                enabled=show_tips,
            ):
                _exec_in_container(
                    frappe_container,
                    new_site_cmd,
                    stream_output=False,
                    verbose=False,
                    environment=new_site_env,
                )

    # Final configuration in a spinner
    with TipSpinner(
        f"Finalizing setup for '{inputs.site_name}'",
        console=stderr_console,
        enabled=show_tips,
    ) as spinner:
        # Final configuration: enable developer mode and server script support
        spinner.update("Enabling developer mode and server scripts")
        final_configs = [
            f"bench --site {shlex.quote(inputs.site_name)} set-config developer_mode 1",
            "bench set-config -g server_script_enabled 1",
        ]
        for command in final_configs:
            _exec_in_container(
                frappe_container,
                _build_cd_command(bench_full_path, command),
                stream_output=False,
                verbose=False,
            )

    # Optionally install ERPNext onto the site
    if install_erpnext:
        if verbose:
            # Verbose mode: stream output without spinner
            console.print()
            _exec_in_container(
                frappe_container,
                _build_cd_command(
                    bench_full_path,
                    f"bench get-app --branch {shlex.quote(erpnext_branch)} --resolve-deps erpnext",
                ),
                description=f"Fetching ERPNext app (branch: {erpnext_branch})",
                stream_output=True,
                verbose=verbose,
            )

            console.print()
            _exec_in_container(
                frappe_container,
                _build_cd_command(
                    bench_full_path,
                    f"bench --site {shlex.quote(inputs.site_name)} install-app erpnext",
                ),
                description=f"Installing ERPNext on site '{inputs.site_name}'",
                stream_output=True,
                verbose=verbose,
            )
        else:
            # Non-verbose mode: use spinner
            with TipSpinner(
                "Installing ERPNext",
                console=stderr_console,
                enabled=show_tips,
            ) as spinner:
                spinner.update(f"Fetching ERPNext app (branch: {erpnext_branch})")
                _exec_in_container(
                    frappe_container,
                    _build_cd_command(
                        bench_full_path,
                        f"bench get-app --branch {shlex.quote(erpnext_branch)} --resolve-deps erpnext",
                    ),
                    stream_output=False,
                    verbose=False,
                )

                spinner.update(f"Installing ERPNext on site '{inputs.site_name}'")
                _exec_in_container(
                    frappe_container,
                    _build_cd_command(
                        bench_full_path,
                        f"bench --site {shlex.quote(inputs.site_name)} install-app erpnext",
                    ),
                    stream_output=False,
                    verbose=False,
                )

    # Clear any stale cached data for this project
    # Note: The 'inspect' command is responsible for populating detailed cache data.
    # Init just clears stale cache since it creates a new project.
    db_utils.clear_cache_for_project(inputs.project_name)

    # Calculate elapsed time
    elapsed_time = time.time() - start_time
    minutes = int(elapsed_time // 60)
    seconds = int(elapsed_time % 60)
    time_str = f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"

    # Inform the user of success
    console.print()
    console.print(
        f"[bold green]✓[/bold green] Successfully initialized bench '{inputs.bench_name}' in {time_str}"
    )
    console.print(f"[dim]Bench path: {bench_full_path}[/dim]")
    console.print(
        f"[dim]Next steps: Run `cwcli open {inputs.project_name}` to open the project in vscode or exec with docker.[/dim]"
    )
    if install_erpnext:
        console.print(
            f"[dim]ERPNext installed. Once services are running, open http://{inputs.site_name}:8000 in your browser.[/dim]"
        )

    # Show the generated admin password once - and only when a site was actually
    # created this run. On an idempotent re-run bench new-site is skipped
    # (site_exists), so no password was set; printing a fresh one would be a lie.
    if admin_password_generated and not site_exists:
        console.print()
        console.print(
            f"[bold yellow]Administrator password (generated):[/bold yellow] {admin_password}"
        )
        console.print(
            "[dim]Shown once and not stored anywhere. To change it later, run "
            f"`bench --site {inputs.site_name} set-admin-password <new-password>`.[/dim]"
        )
