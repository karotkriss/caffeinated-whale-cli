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
    # Get containers for the project
    with stderr_console.status(f"[bold green]Finding project '{project_name}'...[/bold green]", spinner="dots"):
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

    # Get container name
    container_name = frappe_container.name

    # Get bench path from cache if not provided
    if not bench_path:
        with stderr_console.status("[bold green]Looking up bench path...[/bold green]", spinner="dots"):
            cached_data = db_utils.get_cached_project_data(project_name)
            if cached_data and cached_data.get("bench_instances"):
                # Use the first bench instance path
                bench_path = cached_data["bench_instances"][0]["path"]
                if verbose:
                    stderr_console.print(f"[dim]VERBOSE: Using cached bench path: {bench_path}[/dim]")
            else:
                # Fallback to default
                bench_path = "/workspace/frappe-bench"
                stderr_console.print(
                    f"[yellow]Warning: No cached bench path found. Using default: {bench_path}[/yellow]"
                )
                stderr_console.print(
                    f"[yellow]Run 'cwcli inspect {project_name}' first to cache the bench path.[/yellow]"
                )

    # Detect VS Code installations (don't prompt inside spinner)
    with stderr_console.status("[bold green]Detecting VS Code installations...[/bold green]", spinner="dots"):
        vscode_stable = vscode_utils.is_vscode_installed()
        vscode_insiders = vscode_utils.is_vscode_insiders_installed()
        if verbose:
            stderr_console.print(f"[dim]VERBOSE: VS Code stable: {vscode_stable}, Insiders: {vscode_insiders}[/dim]")

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
