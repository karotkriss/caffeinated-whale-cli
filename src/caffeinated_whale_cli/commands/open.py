import sys

import typer
from rich.console import Console

from ..utils import db_utils, vscode_utils
from ..utils.completion_utils import complete_app_names, complete_project_names
from ..utils.docker_utils import exec_into_container, get_project_containers, handle_docker_errors
from .utils import ensure_containers_running, resolve_bench_path

stderr_console = Console(stderr=True)


@handle_docker_errors
def open_bench(
    project_name: str = typer.Argument(
        ..., help="The Docker Compose project name to open.", autocompletion=complete_project_names
    ),
    bench: str = typer.Option(
        None,
        "--bench",
        help="Which bench to open: its numeric index or label (from 'cwcli inspect').",
    ),
    bench_path: str | None = typer.Option(
        None,
        "--path",
        "-p",
        help="Explicit bench directory inside the container (lower-level alternative to --bench).",
    ),
    app: str = typer.Option(
        None,
        "--app",
        "-a",
        help="App name to open (opens the app's directory within the bench)",
        autocompletion=complete_app_names,
    ),
    code: bool = typer.Option(
        False,
        "--code",
        help="Open with VS Code (skips interactive prompt)",
    ),
    code_insiders: bool = typer.Option(
        False,
        "--code-insiders",
        help="Open with VS Code Insiders (skips interactive prompt)",
    ),
    cursor: bool = typer.Option(
        False,
        "--cursor",
        help="Open with Cursor (skips interactive prompt)",
    ),
    docker: bool = typer.Option(
        False,
        "--docker",
        help="Open with Docker exec (skips interactive prompt)",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Auto-start stopped containers without prompting."
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable verbose diagnostic output.",
    ),
):
    """
    Open a project's frappe container in VS Code/Cursor (with Dev Containers) or exec into it.
    """
    # Validate that only one editor flag is specified
    editor_flags = [code, code_insiders, cursor, docker]
    if sum(editor_flags) > 1:
        stderr_console.print(
            "[bold red]Error:[/bold red] Only one of --code, --code-insiders, --cursor, or --docker can be specified."
        )
        raise typer.Exit(code=1)

    # Ensure containers are running, prompt user if not (auto-start with --yes)
    ensure_containers_running(project_name, require_running=True, verbose=verbose, auto_start=yes)

    # Resolve which bench to open BEFORE the spinner so any selector error prints
    # cleanly. Returns None only when nothing is cached, in which case the
    # inspect-then-default fallback below runs; a multi-bench project with no
    # --bench/--path errors here with the bench list.
    bench_path = resolve_bench_path(project_name, bench, bench_path, verbose=verbose)

    # Single spinner that stays at the bottom and updates its message
    with stderr_console.status(
        f"[bold green]Preparing to open '{project_name}'...[/bold green]", spinner="dots"
    ) as status:
        # Find containers
        status.update(f"[bold green]Finding project '{project_name}'...[/bold green]")
        containers = get_project_containers(project_name)
        if not containers:
            stderr_console.print(f"[bold red]Error:[/bold red] Project '{project_name}' not found.")
            raise typer.Exit(code=1)

        # Find the frappe container
        frappe_container = next(
            (c for c in containers if c.labels.get("com.docker.compose.service") == "frappe"),
            None,
        )
        if not frappe_container:
            stderr_console.print(
                f"[bold red]Error:[/bold red] No 'frappe' service found for project '{project_name}'."
            )
            raise typer.Exit(code=1)

        if verbose:
            stderr_console.print(
                f"[dim]VERBOSE: Found frappe container: {frappe_container.name}[/dim]"
            )

        container_name = frappe_container.name

        # bench_path was resolved above (--bench/--path/single bench). If it is still
        # None here, nothing was cached, so the inspect fallback below populates it.

        # Detect VS Code installations
        status.update("[bold green]Detecting VS Code installations...[/bold green]")
        vscode_stable = vscode_utils.is_vscode_installed()
        vscode_insiders = vscode_utils.is_vscode_insiders_installed()
        cursor_installed = vscode_utils.is_cursor_installed()
        if verbose:
            stderr_console.print(
                f"[dim]VERBOSE: VS Code stable: {vscode_stable}, Insiders: {vscode_insiders}, Cursor: {cursor_installed}[/dim]"
            )

    # Handle inspect outside spinner context if needed
    if not bench_path:
        stderr_console.print("[yellow]No cached bench path found. Running inspect...[/yellow]")

        try:
            # Run inspect to populate cache (it has its own spinner)
            from .inspect import inspect as inspect_cmd_func

            # Call inspect directly with just the parameters it needs
            inspect_cmd_func(
                project_name=project_name,
                verbose=verbose,
                json_output=False,
                update=False,
                no_refresh=False,
                show_apps=False,
                interactive=False,
                yes=False,
            )

            # Re-resolve now that inspect has populated the cache. This applies the
            # same --bench/single/multi rules (so a freshly-inspected multi-bench
            # project still errors rather than silently picking the first bench).
            bench_path = resolve_bench_path(project_name, bench, None, verbose=verbose)
            if bench_path:
                if verbose:
                    stderr_console.print(
                        f"[dim]VERBOSE: Using cached bench path from inspect: {bench_path}[/dim]"
                    )
            else:
                # Still no cache, use default
                bench_path = "/workspace/frappe-bench"
                stderr_console.print(
                    f"[yellow]Warning: Could not detect bench path. Using default: {bench_path}[/yellow]"
                )
        except typer.Exit:
            raise
        except Exception as e:
            # Inspect failed, use default
            bench_path = "/workspace/frappe-bench"
            stderr_console.print(
                f"[yellow]Warning: Inspect failed. Using default bench path: {bench_path}[/yellow]"
            )
            if verbose:
                stderr_console.print(f"[dim]VERBOSE: Inspect error: {e}[/dim]")

        # Re-detect VS Code after inspect (if we ran it)
        vscode_stable = vscode_utils.is_vscode_installed()
        vscode_insiders = vscode_utils.is_vscode_insiders_installed()
        cursor_installed = vscode_utils.is_cursor_installed()
        if verbose:
            stderr_console.print(
                f"[dim]VERBOSE: VS Code stable: {vscode_stable}, Insiders: {vscode_insiders}, Cursor: {cursor_installed}[/dim]"
            )

    # Handle app option - verify app exists and update path
    if app:
        # Get cached data to check available apps
        cached_data = db_utils.get_cached_project_data(project_name)
        if not cached_data or not cached_data.get("bench_instances"):
            stderr_console.print(
                f"[bold red]Error:[/bold red] No cached bench data found. Run 'cwcli inspect {project_name}' first."
            )
            raise typer.Exit(code=1)

        # Select the cached bench the user is actually opening. bench_path may have
        # come from --path (a non-default bench), so anchoring to bench_instances[0]
        # would validate `--app` against the wrong bench and then open another one.
        # Match the cached bench by bench_path instead.
        bench_instance = next(
            (b for b in cached_data["bench_instances"] if b.get("path") == bench_path),
            None,
        )
        if bench_instance is None:
            stderr_console.print(
                f"[bold red]Error:[/bold red] Bench '{bench_path}' not found in cached data "
                f"for '{project_name}'. Run 'cwcli inspect {project_name}' first."
            )
            raise typer.Exit(code=1)
        available_apps = bench_instance.get("available_apps", [])

        # Run inspect's read-only T2 partial pass IN-MEMORY over the known benches so a
        # just-installed app is visible here without a manual `cwcli inspect -u`. The
        # containers were already ensured running above, so this is a cheap `ls` refresh
        # of available_apps. We deliberately do NOT persist it: leaving the cache untouched
        # lets the next plain `cwcli inspect` self-heal via escalate-on-drift (refreshing
        # the deep per-site installed lists too). Degrade to the cached list on any error
        # so a transient failure never blocks opening the app.
        from .inspect import partial_inspect_known_benches

        try:
            refreshed, _drift = partial_inspect_known_benches(
                frappe_container, cached_data["bench_instances"], verbose=verbose
            )
            # partial_inspect_known_benches drops any vanished bench, so `refreshed`
            # may be index-shifted relative to the cached list; match by path to the
            # SAME bench the membership check is about rather than indexing [0].
            match = next(
                (b for b in refreshed if b.get("path") == bench_instance["path"]),
                None,
            )
            if match and match.get("available_apps"):
                available_apps = match["available_apps"]
        except Exception as e:
            if verbose:
                stderr_console.print(f"[dim]VERBOSE: App-list refresh skipped: {e}[/dim]")

        if not available_apps:
            stderr_console.print(
                f"[bold red]Error:[/bold red] No apps found in cached data. Run 'cwcli inspect {project_name}' first."
            )
            raise typer.Exit(code=1)

        # Check if the requested app exists
        if app not in available_apps:
            stderr_console.print(
                f"[bold red]Error:[/bold red] App '{app}' not found in bench instance."
            )
            stderr_console.print(f"Available apps: {', '.join(available_apps)}")
            raise typer.Exit(code=1)

        # Update bench_path to point to the app directory
        bench_path = f"{bench_path}/apps/{app}"
        if verbose:
            stderr_console.print(f"[dim]VERBOSE: Opening app path: {bench_path}[/dim]")

    # Determine editor based on flags or interactive prompt
    editor = None

    # Check if a specific editor was requested via flags
    if code:
        if not vscode_stable:
            stderr_console.print(
                "[bold red]Error:[/bold red] VS Code is not installed. Install it from https://code.visualstudio.com/"
            )
            raise typer.Exit(code=1)
        editor = "code"
    elif code_insiders:
        if not vscode_insiders:
            stderr_console.print(
                "[bold red]Error:[/bold red] VS Code Insiders is not installed. Install it from https://code.visualstudio.com/insiders/"
            )
            raise typer.Exit(code=1)
        editor = "code-insiders"
    elif cursor:
        if not cursor_installed:
            stderr_console.print(
                "[bold red]Error:[/bold red] Cursor is not installed. Install it from https://cursor.sh/"
            )
            raise typer.Exit(code=1)
        editor = "cursor"
    elif docker:
        editor = "docker"

    # If no flag was specified, show interactive prompt
    if editor is None:
        # Build choices and prompt user (outside spinner)
        choices = []
        choice_map = {}

        if vscode_stable:
            choice_text = "VS Code - Open in development container"
            choices.append(choice_text)
            choice_map[choice_text] = "code"

        if vscode_insiders:
            choice_text = "VS Code Insiders - Open in development container"
            choices.append(choice_text)
            choice_map[choice_text] = "code-insiders"

        if cursor_installed:
            choice_text = "Cursor - Open in development container"
            choices.append(choice_text)
            choice_map[choice_text] = "cursor"

        docker_choice = "Docker - Execute interactive shell in container"
        choices.append(docker_choice)
        choice_map[docker_choice] = "docker"

        # Select editor
        if len(choices) == 1:
            editor = "docker"
        else:
            # Multiple editors and no explicit flag: interactive choice only. A
            # non-TTY cannot answer, so refuse (Exit 1) naming the flags rather than
            # hang on questionary or crash on EOF.
            if not sys.stdin.isatty():
                stderr_console.print(
                    "[bold red]Error:[/bold red] Multiple editors are available and no editor "
                    "flag was given. Re-run with one of --code, --code-insiders, --cursor, or "
                    "--docker to select non-interactively."
                )
                raise typer.Exit(code=1)

            import questionary
            from questionary import Style

            custom_style = Style(
                [
                    ("qmark", "fg:#00ff00 bold"),  # Bright green question mark
                    ("question", "fg:#00ffff bold"),  # Bright cyan question text
                    ("answer", "fg:#00ff00 bold"),  # Bright green answer
                    ("pointer", "fg:#ffff00 bold"),  # Bright yellow pointer
                    ("highlighted", "fg:#ffff00 bold"),  # Bright yellow highlighted option
                    ("selected", "fg:#00ff00"),  # Green for selected
                    ("separator", "fg:#666666"),  # Gray separator
                    ("instruction", "fg:#888888"),  # Gray instructions
                    ("text", "fg:#ffffff"),  # White text
                ]
            )

            choice = questionary.select(
                "How would you like to open this instance?",
                choices=choices,
                style=custom_style,
                pointer=">",
            ).ask()

            if choice is None:
                stderr_console.print("[yellow]Operation cancelled.[/yellow]")
                raise typer.Exit(code=1)

            editor = choice_map.get(choice, "docker")

    if verbose:
        stderr_console.print(f"[dim]VERBOSE: Selected editor: {editor}[/dim]")

    if editor == "docker":
        # Open with docker exec
        stderr_console.print(f"[bold green]Opening shell in {container_name}...[/bold green]")
        exec_into_container(container_name, working_dir=bench_path)
    else:
        # Open in VS Code/Cursor with Dev Containers
        vscode_utils.open_in_vscode(editor, container_name, bench_path, verbose=verbose)
