import typer
from rich.console import Console

from ..utils import vscode_utils, db_utils
from .utils import get_project_containers
from ..utils.docker_utils import handle_docker_errors

stderr_console = Console(stderr=True)


@handle_docker_errors
def open_bench(
    project_name: str = typer.Argument(..., help="The Docker Compose project name to open."),
    bench_path: str = typer.Option(
        None,
        "--path",
        "-p",
        help="Path inside the container to open (uses cached bench path from inspect if not specified)",
    ),
    app: str = typer.Option(
        None,
        "--app",
        "-a",
        help="App name to open (opens the app's directory within the bench)",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable verbose diagnostic output.",
    ),
):
    """
    Open a project's frappe container in VS Code (with Dev Containers) or exec into it.
    """
    # Single spinner that stays at the bottom and updates its message
    with stderr_console.status(f"[bold green]Preparing to open '{project_name}'...[/bold green]", spinner="dots") as status:
        # Find containers
        status.update(f"[bold green]Finding project '{project_name}'...[/bold green]")
        containers = get_project_containers(project_name)
        if not containers:
            stderr_console.print(
                f"[bold red]Error:[/bold red] Project '{project_name}' not found."
            )
            raise typer.Exit(code=1)

        # Find the frappe container
        frappe_container = next(
            (
                c
                for c in containers
                if c.labels.get("com.docker.compose.service") == "frappe"
            ),
            None,
        )
        if not frappe_container:
            stderr_console.print(
                f"[bold red]Error:[/bold red] No 'frappe' service found for project '{project_name}'."
            )
            raise typer.Exit(code=1)

        # Check if container is running
        if frappe_container.status != "running":
            stderr_console.print(
                f"[bold red]Error:[/bold red] Frappe container for project '{project_name}' is not running."
            )
            raise typer.Exit(code=1)

        if verbose:
            stderr_console.print(f"[dim]VERBOSE: Found frappe container: {frappe_container.name}[/dim]")

        container_name = frappe_container.name

        # Get bench path from cache if not provided
        if not bench_path:
            status.update("[bold green]Looking up bench path...[/bold green]")
            cached_data = db_utils.get_cached_project_data(project_name)
            if cached_data and cached_data.get("bench_instances"):
                # Use the first bench instance path
                bench_path = cached_data["bench_instances"][0]["path"]
                if verbose:
                    stderr_console.print(f"[dim]VERBOSE: Using cached bench path: {bench_path}[/dim]")
            else:
                # No cache found, need to run inspect
                # Exit the spinner context before running inspect (it has its own spinner)
                pass  # Will handle this outside the spinner context

        # Detect VS Code installations
        status.update("[bold green]Detecting VS Code installations...[/bold green]")
        vscode_stable = vscode_utils.is_vscode_installed()
        vscode_insiders = vscode_utils.is_vscode_insiders_installed()
        if verbose:
            stderr_console.print(f"[dim]VERBOSE: VS Code stable: {vscode_stable}, Insiders: {vscode_insiders}[/dim]")

    # Handle inspect outside spinner context if needed
    if not bench_path:
        stderr_console.print(f"[yellow]No cached bench path found. Running inspect...[/yellow]")

        try:
            # Run inspect to populate cache (it has its own spinner)
            from .inspect import inspect as inspect_cmd_func

            # Call inspect directly with just the parameters it needs
            inspect_cmd_func(
                project_name=project_name,
                verbose=verbose,
                json_output=False,
                update=False,
                show_apps=False,
                interactive=False
            )

            # Try to get cached data again
            cached_data = db_utils.get_cached_project_data(project_name)
            if cached_data and cached_data.get("bench_instances"):
                bench_path = cached_data["bench_instances"][0]["path"]
                if verbose:
                    stderr_console.print(f"[dim]VERBOSE: Using cached bench path from inspect: {bench_path}[/dim]")
            else:
                # Still no cache, use default
                bench_path = "/workspace/frappe-bench"
                stderr_console.print(
                    f"[yellow]Warning: Could not detect bench path. Using default: {bench_path}[/yellow]"
                )
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
        if verbose:
            stderr_console.print(f"[dim]VERBOSE: VS Code stable: {vscode_stable}, Insiders: {vscode_insiders}[/dim]")

    # Handle app option - verify app exists and update path
    if app:
        # Get cached data to check available apps
        cached_data = db_utils.get_cached_project_data(project_name)
        if not cached_data or not cached_data.get("bench_instances"):
            stderr_console.print(
                f"[bold red]Error:[/bold red] No cached bench data found. Run 'cwcli inspect {project_name}' first."
            )
            raise typer.Exit(code=1)

        # Get the list of apps from the first bench instance
        bench_instance = cached_data["bench_instances"][0]
        available_apps = bench_instance.get("available_apps", [])

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

    docker_choice = "Docker - Execute interactive shell in container"
    choices.append(docker_choice)
    choice_map[docker_choice] = "docker"

    # Select editor
    if len(choices) == 1:
        editor = "docker"
    else:
        import questionary
        from questionary import Style

        custom_style = Style([
            ('qmark', 'fg:#00ff00 bold'),           # Bright green question mark
            ('question', 'fg:#00ffff bold'),         # Bright cyan question text
            ('answer', 'fg:#00ff00 bold'),           # Bright green answer
            ('pointer', 'fg:#ffff00 bold'),          # Bright yellow pointer
            ('highlighted', 'fg:#ffff00 bold'),      # Bright yellow highlighted option
            ('selected', 'fg:#00ff00'),              # Green for selected
            ('separator', 'fg:#666666'),             # Gray separator
            ('instruction', 'fg:#888888'),           # Gray instructions
            ('text', 'fg:#ffffff'),                  # White text
        ])

        choice = questionary.select(
            "How would you like to open this instance?",
            choices=choices,
            style=custom_style,
            pointer=">"
        ).ask()

        if choice is None:
            stderr_console.print("[yellow]Operation cancelled.[/yellow]")
            raise typer.Exit(code=0)

        editor = choice_map.get(choice, "docker")

    if verbose:
        stderr_console.print(f"[dim]VERBOSE: Selected editor: {editor}[/dim]")

    if editor == "docker":
        # Open with docker exec
        stderr_console.print(f"[bold green]Opening shell in {container_name}...[/bold green]")
        vscode_utils.exec_into_container(container_name)
    else:
        # Open in VS Code with Dev Containers
        vscode_utils.open_in_vscode(editor, container_name, bench_path, verbose=verbose)
