import shlex
from dataclasses import dataclass
from typing import Optional

import questionary
import typer
from rich.console import Console

from ..utils import db_utils
from ..utils.console import console, stderr_console
from ..utils.docker_utils import get_project_containers, handle_docker_errors
from .utils import ensure_containers_running

_INFO_CONSOLE = Console()


class InitCommandError(Exception):
    """Raised when an initialization step fails."""


@dataclass
class InitInputs:
    project_name: str
    bench_name: str
    site_name: str


def _validate_slug(value: str, field_label: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise InitCommandError(f"{field_label} is required.")

    allowed = "abcdefghijklmnopqrstuvwxyz0123456789-_"
    if not all(char in allowed for char in cleaned.lower()):
        raise InitCommandError(
            f"{field_label} must contain only lowercase letters, numbers, dashes, or underscores."
        )

    if cleaned[0] in "-_" or cleaned[-1] in "-_":
        raise InitCommandError(f"{field_label} cannot start or end with '-' or '_'.")

    return cleaned.lower()


def _validate_site_name(value: str) -> str:
    cleaned = value.strip().lower()
    if not cleaned:
        raise InitCommandError("Site name is required.")
    if not cleaned.endswith(".localhost"):
        raise InitCommandError("Site name must end with '.localhost'.")
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789-."
    if not all(char in allowed for char in cleaned):
        raise InitCommandError(
            "Site name may only include lowercase letters, numbers, hyphens, and periods."
        )
    return cleaned


def _prompt_for_inputs(
    project_name: Optional[str], bench_name: Optional[str], site_name: Optional[str]
) -> InitInputs:
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
    container, command: str, *, description: Optional[str] = None, stream_output: bool = False
) -> None:
    if description:
        _INFO_CONSOLE.print(f"[bold cyan]➤[/bold cyan] {description}")

    exec_id = container.client.api.exec_create(
        container.id,
        ["bash", "-lc", command],
    )["Id"]

    try:
        if stream_output:
            for stdout, stderr in container.client.api.exec_start(
                exec_id, stream=True, demux=True
            ):
                if stdout:
                    console.print(stdout.decode("utf-8", errors="replace"), end="")
                if stderr:
                    stderr_console.print(stderr.decode("utf-8", errors="replace"), end="")
        else:
            output = container.client.api.exec_start(exec_id, stream=False)
            if output:
                console.print(output.decode("utf-8", errors="replace"))

        result = container.client.api.exec_inspect(exec_id)
    except Exception as exc:  # pragma: no cover - defensive against docker api errors
        raise InitCommandError(str(exc)) from exc

    exit_code = result.get("ExitCode", 1)
    if exit_code != 0:
        raise InitCommandError(f"Command failed with exit code {exit_code}: {command}")


def _directory_exists(container, path: str) -> bool:
    exit_code, _ = container.exec_run(["bash", "-lc", f"test -d {shlex.quote(path)}"])
    return exit_code == 0


def _ensure_directory(container, path: str) -> None:
    exit_code, output = container.exec_run(["bash", "-lc", f"mkdir -p {shlex.quote(path)}"])
    if exit_code != 0:
        message = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else output
        raise InitCommandError(f"Failed to create directory '{path}': {message}")


def _build_cd_command(path: str, command: str) -> str:
    return f"cd {shlex.quote(path)} && {command}"


@handle_docker_errors
def init(
    project_name: Optional[str] = typer.Option(
        None,
        "--project",
        "-p",
        help="Docker compose project name (container prefix).",
    ),
    bench_name: Optional[str] = typer.Option(
        None,
        "--bench",
        "-b",
        help="Bench directory to create inside the container.",
    ),
    site_name: Optional[str] = typer.Option(
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
) -> None:
    """
    Initialize a new Frappe bench and site inside a running project's Frappe container.
    """
    inputs = _prompt_for_inputs(project_name, bench_name, site_name)

    # Ensure containers are ready
    ensure_containers_running(inputs.project_name, require_running=True, auto_start=auto_start)
    containers = get_project_containers(inputs.project_name)

    if not containers:
        raise typer.Exit(code=1)

    frappe_container = next(
        (c for c in containers if c.labels.get("com.docker.compose.service") == "frappe"),
        None,
    )
    if not frappe_container:
        stderr_console.print(
            f"[bold red]Error:[/bold red] No 'frappe' service found for project '{inputs.project_name}'."
        )
        raise typer.Exit(code=1)

    bench_parent_path = bench_parent.rstrip("/")
    if not bench_parent_path:
        bench_parent_path = "/workspace"

    bench_full_path = f"{bench_parent_path}/{inputs.bench_name}"

    try:
        _ensure_directory(frappe_container, bench_parent_path)

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
            )

        # Common bench configuration
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
            )

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
            )

        _exec_in_container(
            frappe_container,
            _build_cd_command(bench_full_path, f"bench use {shlex.quote(inputs.site_name)}"),
            description="Selecting active site",
            stream_output=verbose,
        )

        final_configs = [
            ("Enabling developer mode", "bench set-config developer_mode 1"),
            ("Enabling server script support", "bench set-config -g server_script_enabled 1"),
        ]

        for description, command in final_configs:
            _exec_in_container(
                frappe_container,
                _build_cd_command(bench_full_path, command),
                description=description,
                stream_output=verbose,
            )

        db_utils.clear_cache_for_project(inputs.project_name)

        console.print(
            "[bold green]✓[/bold green] Bench initialization complete. "
            f"Bench path: [cyan]{bench_full_path}[/cyan]"
        )
        console.print(
            f"[dim]Next steps:[/dim] Run `cwcli start {inputs.project_name}` to start bench services."
        )
    except InitCommandError as exc:
        stderr_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
