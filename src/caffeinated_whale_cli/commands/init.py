"""
Enhanced initialization script for cwcli.

This version augments the original `init` command to support a more complete
setup workflow inspired by the article "How to Install ERPNext on Windows 11
Using Docker"【463985302350907†L129-L139】.  The enhancements include the ability to
clone the `frappe_docker` repository, copy example development container
configuration files, bring up Docker Compose services automatically and even
install the ERPNext application after your site has been created.  You can
trigger these steps via new command‑line flags.

Key additions:

* `--setup`:  Clone the `frappe_docker` repository from a configurable URL
  into a local directory, copy the example dev container and VS Code
  configuration files, and run `docker compose up` to start the services.
  These steps mirror the early setup phases described in the guide —
  cloning the repository【463985302350907†L129-L139】, copying configs【463985302350907†L146-L156】 and launching
  containers【463985302350907†L206-L220】.
* `--clone-from-project`:  If you already have a Compose stack running, you can
  pass the project name of the existing stack and this flag will launch a
  second instance using the same Compose file but with a new project name.
  This is a simple way to "clone" a containerised environment.
* `--install-erpnext`:  After creating your bench and site the script can
  automatically fetch and install the ERPNext application for you【463985302350907†L288-L299】.

Because these features rely on host system tools (`git`, `docker`, etc.) the
script uses Python's `subprocess.run` to execute them.  If a command fails it
prints an error message and exits with code 1.
"""

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass

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
    project_name: str | None, bench_name: str | None, site_name: str | None
) -> InitInputs:
    """Prompt the user for project, bench and site names if missing."""
    try:
        container_answer = questionary.text(
            "Docker project / container prefix (e.g. frappe-app)",
            default=project_name or "",
            validate=lambda text: bool(text.strip()),
        ).ask()
        if container_answer is None:
            raise typer.Exit(code=0)

        bench_answer = questionary.text(
            "App / Bench directory name (e.g. frappe-bench)",
            default=bench_name or "",
            validate=lambda text: bool(text.strip()),
        ).ask()
        if bench_answer is None:
            raise typer.Exit(code=0)

        site_answer = questionary.text(
            "Primary site name (must end with .localhost)",
            default=site_name or "development.localhost",
            validate=lambda text: bool(text.strip()) and text.strip().endswith(".localhost"),
        ).ask()
        if site_answer is None:
            raise typer.Exit(code=0)
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
    cmd: list[str], cwd: str | None = None, description: str | None = None
) -> None:
    """Run a command on the host system and raise if it fails."""
    if description:
        stderr_console.print(f"[bold cyan]➤[/bold cyan] {description}")
    result = subprocess.run(cmd, cwd=cwd)
    if result.returncode != 0:
        stderr_console.print(f"[bold red]Error:[/bold red] Host command failed: {' '.join(cmd)}")
        raise typer.Exit(code=1)


def _clone_frappe_repo(repo_url: str, dest_dir: str) -> None:
    """Clone the frappe_docker repository into dest_dir if it doesn't already exist."""
    if os.path.isdir(dest_dir) and os.listdir(dest_dir):
        # Directory exists and is non‑empty; assume repo already cloned
        return
    _run_host_command(
        ["git", "clone", repo_url, dest_dir], description=f"Cloning frappe_docker to {dest_dir}"
    )


def _copy_devcontainer_configs(repo_path: str) -> None:
    """Copy example dev container and VS Code config directories into place."""
    # Copy devcontainer example to .devcontainer
    src_devcontainer = os.path.join(repo_path, "devcontainer-example")
    dest_devcontainer = os.path.join(repo_path, ".devcontainer")
    if os.path.isdir(src_devcontainer) and not os.path.isdir(dest_devcontainer):
        stderr_console.print("[bold cyan]➤[/bold cyan] Copying devcontainer example configuration")
        shutil.copytree(src_devcontainer, dest_devcontainer)
    # Copy VS Code example to development/.vscode
    src_vscode = os.path.join(repo_path, "development", "vscode-example")
    dest_vscode = os.path.join(repo_path, "development", ".vscode")
    if os.path.isdir(src_vscode) and not os.path.isdir(dest_vscode):
        stderr_console.print("[bold cyan]➤[/bold cyan] Copying VS Code example configuration")
        shutil.copytree(src_vscode, dest_vscode)


def _start_compose_project(project_name: str, repo_path: str, compose_file: str) -> None:
    """Run docker compose up -d in the given repository to start services."""
    cmd = ["docker", "compose", "-p", project_name]
    if compose_file:
        cmd += ["-f", compose_file]
    cmd += ["up", "-d"]
    _run_host_command(
        cmd, cwd=repo_path, description=f"Starting Docker Compose project '{project_name}'"
    )


def _clone_compose_project(
    _existing_project: str, new_project: str, repo_path: str, compose_file: str
) -> None:
    """Clone an existing Compose project by bringing up the same services under a new project name."""
    # We simply call docker compose with the new project name.  Docker Compose
    # will create a parallel set of containers, networks and volumes.  This
    # provides a lightweight "clone" of the running environment.
    # Note: _existing_project parameter is intentionally unused - it's for documentation only
    _start_compose_project(new_project, repo_path, compose_file)


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
    site_name: str | None = typer.Option(
        None,
        "--site",
        "-s",
        help="Primary site to create (must end with .localhost).",
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
    # New options below
    setup: bool = typer.Option(
        False,
        "--setup",
        help="Perform initial setup: clone frappe_docker, copy example configs and bring up containers.",
    ),
    clone_from_project: str | None = typer.Option(
        None,
        "--clone-from-project",
        help="Name of an existing docker compose project to clone.  If provided, the compose stack is duplicated under the new project name before bench initialization.",
    ),
    repo_url: str = typer.Option(
        "https://github.com/frappe/frappe_docker.git",
        "--repo-url",
        help="Git URL of the frappe_docker repository to clone when using --setup.",
    ),
    repo_path: str = typer.Option(
        "./frappe_docker",
        "--repo-path",
        help="Local directory where the frappe_docker repository will be cloned when using --setup.",
    ),
    compose_file: str = typer.Option(
        ".devcontainer/docker-compose.yml",
        "--compose-file",
        help="Docker compose file name relative to the repository path.  Defaults to 'compose.yaml'.",
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
    Initialize a new Frappe bench and site inside a running project's Frappe container.

    This enhanced version can optionally perform the initial setup steps of
    cloning the frappe_docker repository, copying example configuration files and
    starting the Docker Compose services【463985302350907†L129-L139】【463985302350907†L146-L156】.  It can also
    duplicate an existing Compose stack (via --clone-from-project) and
    install the ERPNext application on your new site【463985302350907†L288-L299】.
    """
    # Prompt for inputs (project, bench and site names) first
    inputs = _prompt_for_inputs(project_name, bench_name, site_name)

    # Perform optional setup: clone repo, copy configs, start containers
    if setup:
        # Ensure git and docker are available
        try:
            subprocess.run(["git", "--version"], check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["docker", "--version"], check=True, stdout=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            stderr_console.print(
                "[bold red]Error:[/bold red] 'git' and 'docker' commands must be installed "
                "and in your PATH for setup."
            )
            raise typer.Exit(code=1) from None
        # Clone repo if needed
        _clone_frappe_repo(repo_url, repo_path)
        # Copy example configurations into place
        _copy_devcontainer_configs(repo_path)
        # Start compose project with provided project name and compose file
        _start_compose_project(inputs.project_name, repo_path, compose_file)

    # Clone an existing compose project if requested
    if clone_from_project:
        if not os.path.isdir(repo_path):
            stderr_console.print(
                "[bold red]Error:[/bold red] --clone-from-project requires a valid "
                "--repo-path where compose files live."
            )
            raise typer.Exit(code=1)
        _clone_compose_project(clone_from_project, inputs.project_name, repo_path, compose_file)

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

    # Clear cached bench metadata
    db_utils.clear_cache_for_project(inputs.project_name)

    # Inform the user of success
    console.print(
        "[bold green]✓[/bold green] Bench initialization complete. "
        f"Bench path: [cyan]{bench_full_path}[/cyan]"
    )
    console.print(
        f"[dim]Next steps:[/dim] Run `cwcli open {inputs.project_name}` to start bench services."
    )
    if install_erpnext:
        console.print(
            f"[dim]ERPNext installed. Once services are running, open http://{inputs.site_name}:8000 in your browser.[/dim]"
        )
