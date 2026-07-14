import sys

import typer

from ..utils import db_utils
from ..utils.completion_utils import complete_project_names, complete_site_names
from ..utils.console import console, stderr_console
from ..utils.docker_utils import get_project_containers, handle_docker_errors
from .utils import ensure_containers_running, resolve_bench_path


@handle_docker_errors
def unlock(
    project_name: str = typer.Argument(
        ..., help="The Docker Compose project name.", autocompletion=complete_project_names
    ),
    site: str = typer.Option(
        None,
        "--site",
        "-s",
        help="Site name to unlock. If not provided, uses the default site "
        "(from common_site_config.json's default_site or sites/currentsite.txt).",
        autocompletion=complete_site_names,
    ),
    bench: str = typer.Option(
        None,
        "--bench",
        help="Which bench to target: its numeric index or label (from 'cwcli inspect').",
    ),
    bench_path: str = typer.Option(
        None,
        "--path",
        "-p",
        help="Explicit bench directory inside the container (lower-level alternative to --bench).",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Auto-start stopped containers without prompting."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose output."),
):
    """
    Remove the locks folder for a specified site to unlock it.

    This command removes the {bench_path}/sites/{site_name}/locks directory,
    which can help resolve issues when a site is stuck in a locked state.

    If --site is not provided, the default site is used, resolved from either
    common_site_config.json's `default_site` or sites/currentsite.txt.

    Examples:
        cwcli unlock my-project --site example.com
        cwcli unlock my-project  # Uses default site
    """
    # Ensure containers are running, prompt user if not (auto-start with --yes)
    ensure_containers_running(project_name, require_running=True, verbose=verbose, auto_start=yes)

    containers = get_project_containers(project_name)
    if not containers:
        stderr_console.print(f"[bold red]Error:[/bold red] Project '{project_name}' not found.")
        raise typer.Exit(code=1)

    frappe_container = next(
        (c for c in containers if c.labels.get("com.docker.compose.service") == "frappe"),
        None,
    )
    if not frappe_container:
        stderr_console.print(
            f"[bold red]Error:[/bold red] No 'frappe' service found for project '{project_name}'."
        )
        raise typer.Exit(code=1)

    # Resolve which bench to unlock (--bench/--path, else the single bench, else
    # error on ambiguity). Falls back to the default path only when nothing is cached.
    resolved = resolve_bench_path(project_name, bench, bench_path, verbose=verbose)
    if resolved:
        bench_path = resolved
        if verbose:
            stderr_console.print(f"[dim]Using bench path: {bench_path}[/dim]")
    else:
        bench_path = "/workspace/frappe-bench"
        stderr_console.print(
            f"[yellow]Warning:[/yellow] No cached bench path found. Using default: {bench_path}"
        )

    # Get default site if not provided
    if not site:
        try:
            default_site = db_utils.get_default_site(project_name, bench_path)
        except typer.Exit:
            # Re-raise typer.Exit without catching
            raise
        except Exception as e:
            stderr_console.print(
                f"[bold red]Error:[/bold red] Failed to retrieve default site: {e}"
            )
            stderr_console.print(
                f"[dim]Tip: Specify --site explicitly or run 'cwcli inspect {project_name}' first.[/dim]"
            )
            raise typer.Exit(code=1) from e

        if default_site:
            site = default_site
            console.print(f"[dim]Using default site: {site}[/dim]")
        else:
            stderr_console.print(
                "[bold red]Error:[/bold red] No site specified and no default site found in config."
            )
            stderr_console.print(
                f"[dim]Tip: Run 'cwcli inspect {project_name}' first, or specify --site explicitly.[/dim]"
            )
            raise typer.Exit(code=1)

    # Validate site name to prevent command injection
    if not site or not site.strip():
        stderr_console.print("[bold red]Error:[/bold red] Site name cannot be empty.")
        raise typer.Exit(code=1)

    # Basic validation: site names should not contain shell metacharacters
    invalid_chars = [";", "&", "|", "$", "`", "(", ")", "<", ">", "\n", "\r", "\\"]
    if any(char in site for char in invalid_chars):
        stderr_console.print(
            f"[bold red]Error:[/bold red] Invalid site name '{site}'. "
            "Site names cannot contain special shell characters."
        )
        raise typer.Exit(code=1)

    # Validate bench_path to prevent command injection
    if any(char in bench_path for char in invalid_chars):
        stderr_console.print(
            f"[bold red]Error:[/bold red] Invalid bench path '{bench_path}'. "
            "Paths cannot contain special shell characters."
        )
        raise typer.Exit(code=1)

    # Verify bench path exists. Run via an argv list (no shell): none of these
    # container commands need shell features, and argv is immune to metacharacter
    # interpolation regardless of the input validation above.
    exit_code, _ = frappe_container.exec_run(["test", "-d", f"{bench_path}/sites"])
    if verbose:
        stderr_console.print(f"[dim]$ test -d {bench_path}/sites[/dim]")
        stderr_console.print(f"[dim]Exit code: {exit_code}[/dim]")

    if exit_code != 0:
        stderr_console.print(
            f"[bold red]Error:[/bold red] Bench directory not found at {bench_path}"
        )
        raise typer.Exit(code=1)

    # Check if site exists
    site_path = f"{bench_path}/sites/{site}"
    exit_code, _ = frappe_container.exec_run(["test", "-d", site_path])
    if verbose:
        stderr_console.print(f"[dim]$ test -d {site_path}[/dim]")
        stderr_console.print(f"[dim]Exit code: {exit_code}[/dim]")

    if exit_code != 0:
        stderr_console.print(f"[bold red]Error:[/bold red] Site '{site}' not found at {site_path}")
        raise typer.Exit(code=1)

    # Remove locks folder. argv list (no shell), -v for streamed per-file output.
    locks_path = f"{site_path}/locks"
    cmd = ["rm", "-rfv", locks_path]

    if verbose:
        stderr_console.print(f"[dim]$ {' '.join(cmd)}[/dim]")

        # Stream output in verbose mode
        api = frappe_container.client.api
        exec_id = api.exec_create(frappe_container.id, cmd, workdir=bench_path, tty=False)["Id"]

        console.print(f"[bold green]Unlocking site '{site}'...[/bold green]")

        for chunk in api.exec_start(exec_id, stream=True):
            if isinstance(chunk, (bytes, bytearray)):
                # Write directly to stdout to preserve output
                sys.stdout.write(chunk.decode("utf-8"))
                sys.stdout.flush()
            else:
                sys.stdout.write(str(chunk))
                sys.stdout.flush()

        result = api.exec_inspect(exec_id)
        exit_code = result.get("ExitCode", 1)
    else:
        # Non-verbose mode: use spinner
        with console.status(f"[bold green]Unlocking site '{site}'...[/bold green]", spinner="dots"):
            exit_code, _ = frappe_container.exec_run(cmd, workdir=bench_path)

    if exit_code == 0:
        console.print(f"[bold green]✓[/bold green] Successfully unlocked site '{site}'")
        console.print(f"[dim]Removed locks folder: {locks_path}[/dim]")
    else:
        stderr_console.print(f"[bold red]✗[/bold red] Failed to unlock site '{site}'")
        raise typer.Exit(code=1)
