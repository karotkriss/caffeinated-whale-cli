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

import shlex
import subprocess
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
) -> None:
    """Execute a command inside a Docker container using the Docker API."""
    if verbose:
        stderr_console.print(f"[dim]$ {command}[/dim]")

    # Only show description when streaming output (verbose mode shows command instead)
    if description and stream_output and not verbose:
        stderr_console.print(f"{description}")

    exec_id = container.client.api.exec_create(
        container.id,
        ["bash", "-lc", command],
    )["Id"]

    try:
        if stream_output:
            import sys

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
        stderr_console.print(
            f"[bold red]Error:[/bold red] Command failed with exit code {exit_code}: {command}"
        )
        raise typer.Exit(code=1)


def _directory_exists(container, path: str) -> bool:
    """Return True if a directory exists inside the container."""
    exit_code, _ = container.exec_run(["bash", "-lc", f"test -d {shlex.quote(path)}"])
    return exit_code == 0


def _ensure_directory(container, path: str) -> None:
    """Create a directory inside the container if it does not exist."""
    exit_code, output = container.exec_run(["bash", "-lc", f"mkdir -p {shlex.quote(path)}"])
    if exit_code != 0:
        message = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else output
        stderr_console.print(
            f"[bold red]Error:[/bold red] Failed to create directory '{path}': {message}"
        )
        raise typer.Exit(code=1)


def _build_cd_command(path: str, command: str) -> str:
    return f"cd {shlex.quote(path)} && {command}"


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
    compose_path: Path, port: int, frappe_branch: str, verbose: bool = False, spinner=None
) -> None:
    """
    Customize the port mappings and frappe image tag in docker-compose.yml.

    Args:
        compose_path: Path to the docker-compose.yml file
        port: Starting port number (e.g., 8000)
        frappe_branch: Frappe branch being used (determines image tag)
        verbose: Print detailed information
        spinner: Optional spinner to update status

    The function replaces:
    - Web server ports: 8000-8005 → {port}-{port+5}
    - SocketIO ports: 9000-9005 → {port+1000}-{port+1005}
    - Frappe image tag: latest → v5.26.0 (only for version-15 branch)
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

    # Replace frappe image tag (only for version-15 to ensure stability)
    if frappe_branch == "version-15":
        content = content.replace("docker.io/frappe/bench:latest", "docker.io/frappe/bench:v5.26.0")

    compose_path.write_text(content)


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
    frappe_branch: str = typer.Option(
        "version-15",
        "--frappe-branch",
        help="Frappe branch to use for bench init.",
    ),
    db_root_password: str = typer.Option(
        "123",
        "--db-root-password",
        help="MariaDB root password used for bench new-site.",
    ),
    admin_password: str = typer.Option(
        "admin",
        "--admin-password",
        help="Administrator password for the new site.",
    ),
    auto_start: bool = typer.Option(
        False,
        "--auto-start",
        help="Automatically start containers if they are not running.",
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
        "version-15",
        "--erpnext-branch",
        help="ERPNext branch to use when fetching the app (used with --install-erpnext).",
    ),
) -> None:
    """
    Initialize a new Frappe project with bench and site.

    Creates project directory, downloads compose files, starts containers,
    initializes bench, and creates a site. Optionally installs ERPNext.

    If project_name is not provided, prompts interactively.

    Examples:
        cwcli init
        cwcli init my-project
        cwcli init my-project --bench my-bench --site mysite.localhost
        cwcli init my-project --frappe-branch version-15 --install-erpnext
        cwcli init my-project --db-root-password mypass --admin-password admin123
    """
    import time

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
        _customize_compose_ports(compose_path, port, frappe_branch, verbose=verbose, spinner=None)
        stderr_console.print("[dim]Pulling Docker images...[/dim]")
        _pull_compose_images(inputs.project_name, conf_dir, verbose=verbose)
        _start_compose_project(inputs.project_name, conf_dir, verbose=verbose)
        ensure_containers_running(inputs.project_name, require_running=True, auto_start=auto_start)
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
            _customize_compose_ports(
                compose_path, port, frappe_branch, verbose=verbose, spinner=spinner
            )

            # Pull Docker images (can take a while)
            spinner.update("Pulling Docker images")
            _pull_compose_images(inputs.project_name, conf_dir)

            # Start containers
            spinner.update("Starting Docker Compose containers")
            _start_compose_project(inputs.project_name, conf_dir, verbose=verbose)

            spinner.update("Waiting for containers to be ready")
            ensure_containers_running(
                inputs.project_name, require_running=True, auto_start=auto_start
            )

            # Get the frappe container
            spinner.update("Getting frappe container")
            frappe_container = get_frappe_container(inputs.project_name)

    # Prepare bench paths inside the container
    bench_parent_path = bench_parent.rstrip("/") or "/workspace"
    bench_full_path = f"{bench_parent_path}/{inputs.bench_name}"

    # Add path to config for open to work
    add_path(bench_full_path)

    # Create parent directory inside the container
    _ensure_directory(frappe_container, bench_parent_path)

    # Bench initialization
    bench_exists = _directory_exists(frappe_container, bench_full_path)

    # Exit spinner before interactive prompt
    if bench_exists:
        console.print(
            f"[yellow]Bench '{inputs.bench_name}' already exists at {bench_full_path}.[/yellow]"
        )
        reuse = questionary.confirm(
            "Reuse the existing bench and continue with site setup?",
            default=True,
            auto_enter=False,
        ).ask()
        if not reuse:
            console.print("[yellow]No changes made.[/yellow]")
            raise typer.Exit(code=0)

    # Initialize bench if it doesn't exist
    if not bench_exists:
        bench_init_cmd = _build_cd_command(
            bench_parent_path,
            " ".join(
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
        new_site_cmd = _build_cd_command(
            bench_full_path,
            " ".join(
                [
                    "bench",
                    "new-site",
                    "--db-root-password",
                    shlex.quote(db_root_password),
                    "--admin-password",
                    shlex.quote(admin_password),
                    "--mariadb-user-host-login-scope=%",
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
                )

    # Final configuration in a spinner
    with TipSpinner(
        f"Finalizing setup for '{inputs.site_name}'",
        console=stderr_console,
        enabled=show_tips,
    ) as spinner:
        # Switch active site
        spinner.update(f"Setting '{inputs.site_name}' as active site")
        _exec_in_container(
            frappe_container,
            _build_cd_command(bench_full_path, f"bench use {shlex.quote(inputs.site_name)}"),
            stream_output=False,
            verbose=False,
        )

        # Final configuration: enable developer mode and server script support
        spinner.update("Enabling developer mode and server scripts")
        final_configs = [
            ("bench set-config developer_mode 1"),
            ("bench set-config -g server_script_enabled 1"),
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
