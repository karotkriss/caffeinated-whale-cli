"""
Simplified init command for cwcli.

The init command creates a complete Frappe development environment in a single step:
1. Creates a project directory in ~/.cwcli/projects/{project_name}/
2. Downloads essential files from GitHub (docker-compose.yml, .env)
3. Starts Docker Compose containers
4. Initializes a Frappe bench inside the container
5. Creates a new site with the specified configuration
6. Optionally installs ERPNext

This approach is lightweight and organized - no need to clone the entire frappe_docker
repository. All project files are stored in a dedicated projects directory.

Key features:
* Automatic project setup - no manual repository cloning needed
* Downloads only essential files from GitHub (< 10KB vs entire repo)
* All projects organized in ~/.cwcli/projects/
* Optional ERPNext installation with `--install-erpnext`
* Interactive prompts for project/bench/site names if not provided

Example:
    cwcli init my-project
    # Creates ~/.cwcli/projects/my-project/
    # Downloads compose files
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

from ..utils import db_utils
from ..utils.completion_utils import complete_project_names
from ..utils.console import console, stderr_console
from ..utils.docker_utils import get_frappe_container, handle_docker_errors
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


def _prompt_for_inputs(
    project_name: str | None, bench_name: str | None, site_name: str
) -> InitInputs:
    """Prompt the user for project, bench and site names if missing.

    Note: site_name now has a default value ('development.localhost'), so it's never None.
    Only project_name and bench_name will be prompted if not provided.
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

        # Only prompt for bench name if not provided
        if bench_name is None:
            bench_answer = questionary.text(
                "App / Bench directory name (e.g. frappe-bench)",
                default="",
                validate=lambda text: bool(text.strip()),
            ).ask()
            if bench_answer is None:
                raise typer.Exit(code=0)
        else:
            bench_answer = bench_name

        # Site name now has a default, so use it directly (no prompt needed)
        site_answer = site_name
    except KeyboardInterrupt:
        stderr_console.print("\n[yellow]Operation cancelled.[/yellow]")
        raise typer.Exit(code=0) from None

    return InitInputs(
        project_name=_validate_slug(container_answer, "Project name"),
        bench_name=_validate_slug(bench_answer, "Bench name"),
        site_name=_validate_site_name(site_answer),
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
    if description:
        stderr_console.print(f"[bold cyan]➤[/bold cyan] {description}")

    if verbose:
        stderr_console.print(f"[dim]$ {command}[/dim]")

    exec_id = container.client.api.exec_create(
        container.id,
        ["bash", "-lc", command],
    )["Id"]

    try:
        if stream_output:
            for stdout, stderr in container.client.api.exec_start(exec_id, stream=True, demux=True):
                if stdout:
                    console.print(stdout.decode("utf-8", errors="replace"), end="")
                if stderr:
                    stderr_console.print(stderr.decode("utf-8", errors="replace"), end="")
        else:
            output = container.client.api.exec_start(exec_id, stream=False)
            if output:
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
) -> None:
    """Run a command on the host system and raise if it fails."""
    if use_spinner and description:
        with stderr_console.status(f"[bold cyan]{description}...[/bold cyan]", spinner="dots"):
            result = subprocess.run(cmd, cwd=cwd, capture_output=True)
    else:
        if description:
            stderr_console.print(f"[bold cyan]➤[/bold cyan] {description}")
        result = subprocess.run(cmd, cwd=cwd)

    if result.returncode != 0:
        stderr_console.print(f"[bold red]Error:[/bold red] Host command failed: {' '.join(cmd)}")
        if use_spinner and result.stderr:
            stderr_console.print(result.stderr.decode("utf-8", errors="replace"))
        raise typer.Exit(code=1)


def _download_github_file(url: str, dest_path: Path, description: str | None = None) -> None:
    """Download a file from GitHub raw URL."""
    import urllib.request

    if description:
        stderr_console.print(f"[bold cyan]➤[/bold cyan] {description}")

    try:
        with urllib.request.urlopen(url) as response:
            content = response.read()
            dest_path.write_bytes(content)
    except Exception as e:
        stderr_console.print(f"[bold red]Error:[/bold red] Failed to download {url}: {e}")
        raise typer.Exit(code=1) from None


def _start_compose_project(project_name: str, project_dir: Path) -> None:
    """Run docker compose up -d in the project directory to start services."""
    cmd = ["docker", "compose", "-p", project_name, "-f", "docker-compose.yml", "up", "-d"]
    _run_host_command(
        cmd,
        cwd=str(project_dir),
        description=f"Starting Docker Compose project '{project_name}'",
        use_spinner=True,
    )


def _setup_project_directory(project_name: str, verbose: bool = False) -> Path:
    """
    Create project directory and download essential files from GitHub.

    Returns the path to the project directory.
    """
    from ..utils.config_utils import PROJECTS_DIR

    # Create project directory
    project_dir = PROJECTS_DIR / project_name
    project_dir.mkdir(parents=True, exist_ok=True)

    if verbose:
        stderr_console.print(f"[dim]Project directory: {project_dir}[/dim]")

    # GitHub raw URLs for frappe_docker development setup
    compose_url = (
        "https://raw.githubusercontent.com/frappe/frappe_docker/main/development/compose.yaml"
    )
    env_url = "https://raw.githubusercontent.com/frappe/frappe_docker/main/development/.env.example"

    # Download compose file
    compose_path = project_dir / "docker-compose.yml"
    if not compose_path.exists():
        _download_github_file(
            compose_url, compose_path, description="Downloading docker-compose.yml from GitHub"
        )

    # Download and rename .env.example to .env
    env_path = project_dir / ".env"
    if not env_path.exists():
        _download_github_file(
            env_url, env_path, description="Downloading .env configuration from GitHub"
        )

    return project_dir


@handle_docker_errors
def init(
    project_name: str | None = typer.Argument(
        None,
        help="Docker Compose project name. If not provided, will prompt interactively.",
        autocompletion=complete_project_names,
    ),
    bench_name: str | None = typer.Option(
        None,
        "--bench",
        "-b",
        help="Bench directory to create inside the container.",
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

    If project_name, bench_name, or site_name are not provided, prompts interactively.

    Examples:
        cwcli init
        cwcli init my-project
        cwcli init my-project --bench my-bench --site mysite.localhost
        cwcli init my-project --frappe-branch version-15 --install-erpnext
        cwcli init my-project --db-root-password mypass --admin-password admin123
    """
    # Prompt for inputs (project, bench and site names) first
    inputs = _prompt_for_inputs(project_name, bench_name, site_name)

    # Setup project directory and download essential files
    project_dir = _setup_project_directory(inputs.project_name, verbose=verbose)

    # Start Docker Compose project
    _start_compose_project(inputs.project_name, project_dir)

    # Ensure containers are ready before interacting with them
    ensure_containers_running(inputs.project_name, require_running=True, auto_start=auto_start)

    # Get the frappe container
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
    else:
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
        _exec_in_container(
            frappe_container,
            bench_init_cmd,
            description=f"Initializing bench '{inputs.bench_name}' (this may take a while)...",
            stream_output=True,
            verbose=verbose,
        )

    # Configure bench hosts inside the container (db and redis services)【463985302350907†L232-L239】
    configs = [
        ("Setting MariaDB host", "bench set-config -g db_host mariadb"),
        ("Setting Redis cache", "bench set-config -g redis_cache redis://redis-cache:6379"),
        ("Setting Redis queue", "bench set-config -g redis_queue redis://redis-queue:6379"),
        (
            "Setting Redis socketio",
            "bench set-config -g redis_socketio redis://redis-queue:6379",
        ),
    ]
    for description, command in configs:
        _exec_in_container(
            frappe_container,
            _build_cd_command(bench_full_path, command),
            description=description,
            stream_output=verbose,
            verbose=verbose,
        )

    # Create the site if it doesn't already exist【463985302350907†L257-L271】
    site_path = f"{bench_full_path}/sites/{inputs.site_name}"
    site_exists = _directory_exists(frappe_container, site_path)
    if site_exists:
        console.print(
            f"[yellow]Site '{inputs.site_name}' already exists. Skipping new-site creation.[/yellow]"
        )
    else:
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
        _exec_in_container(
            frappe_container,
            new_site_cmd,
            description=f"Creating site '{inputs.site_name}'...",
            stream_output=True,
            verbose=verbose,
        )

    # Switch active site
    _exec_in_container(
        frappe_container,
        _build_cd_command(bench_full_path, f"bench use {shlex.quote(inputs.site_name)}"),
        description="Selecting active site",
        stream_output=verbose,
        verbose=verbose,
    )

    # Final configuration: enable developer mode and server script support【463985302350907†L273-L283】
    final_configs = [
        ("Enabling developer mode", "bench set-config developer_mode 1"),
        (
            "Enabling server script support",
            "bench set-config -g server_script_enabled 1",
        ),
    ]
    for description, command in final_configs:
        _exec_in_container(
            frappe_container,
            _build_cd_command(bench_full_path, command),
            description=description,
            stream_output=verbose,
            verbose=verbose,
        )

    # Optionally install ERPNext onto the site【463985302350907†L288-L299】
    if install_erpnext:
        erpnext_commands = [
            (
                "Fetching ERPNext app",
                f"bench get-app --branch {shlex.quote(erpnext_branch)} --resolve-deps erpnext",
            ),
            (
                "Installing ERPNext app",
                f"bench --site {shlex.quote(inputs.site_name)} install-app erpnext",
            ),
        ]
        for description, command in erpnext_commands:
            _exec_in_container(
                frappe_container,
                _build_cd_command(bench_full_path, command),
                description=description,
                stream_output=True,
                verbose=verbose,
            )

    # Clear any stale cached data for this project
    # Note: The 'inspect' command is responsible for populating detailed cache data.
    # Init just clears stale cache since it creates a new project.
    db_utils.clear_cache_for_project(inputs.project_name)

    # Inform the user of success
    console.print(
        f"[bold green]✓[/bold green] Successfully initialized bench '{inputs.bench_name}'"
    )
    console.print(f"[dim]Bench path: {bench_full_path}[/dim]")
    console.print(
        f"[dim]Next steps: Run `cwcli open {inputs.project_name}` to start bench services.[/dim]"
    )
    if install_erpnext:
        console.print(
            f"[dim]ERPNext installed. Once services are running, open http://{inputs.site_name}:8000 in your browser.[/dim]"
        )
