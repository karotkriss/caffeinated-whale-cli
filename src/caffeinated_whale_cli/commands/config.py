import typer
from rich.console import Console
from rich.table import Table
import questionary
from ..utils import db_utils, config_utils

app = typer.Typer(help="Manage CLI configuration and cache.")
cache_app = typer.Typer(help="Manage the cache.")
app.add_typer(cache_app, name="cache")

console = Console()


@app.command("path")
def config_path():
    """
    Display the path to the configuration file.
    """
    console.print(f"Config file is located at: [green]{config_utils.CONFIG_FILE}[/green]")


@app.command("add-path")
def add_path(
    path: str = typer.Argument(..., help="The absolute path to add to the custom search paths.")
):
    """
    Add a custom bench search path to the configuration.
    """
    if config_utils.add_custom_path(path):
        console.print(f"[green]Added '{path}' to custom search paths.[/green]")
    else:
        console.print(f"[yellow]'{path}' already exists in custom search paths.[/yellow]")


@app.command("remove-path")
def remove_path(
    path: str = typer.Argument(..., help="The path to remove from the custom search paths.")
):
    """
    Remove a custom bench search path from the configuration.
    """
    if config_utils.remove_custom_path(path):
        console.print(f"[green]Removed '{path}' from custom search paths.[/green]")
    else:
        console.print(f"[yellow]'{path}' not found in custom search paths.[/yellow]")


@cache_app.command("clear")
def clear_cache(
    project_name: str = typer.Argument(
        None, help="The name of the project to clear from the cache."
    ),
    all: bool = typer.Option(False, "--all", "-a", help="Clear the entire cache."),
):
    """
    Clear the cache for a specific project or the entire cache.
    """
    if all:
        confirmed = questionary.confirm("Are you sure you want to clear the entire cache?").ask()
        if confirmed:
            db_utils.clear_all_cache()
            console.print("[green]Entire cache has been cleared.[/green]")
        else:
            console.print("Operation cancelled.")
    elif project_name:
        if db_utils.clear_cache_for_project(project_name):
            console.print(f"Cache for project '[bold cyan]{project_name}[/bold cyan]' cleared.")
        else:
            console.print(
                f"[yellow]No cache found for project '[bold cyan]{project_name}[/bold cyan]'.[/yellow]"
            )
    else:
        console.print("Please specify a project name or use the --all flag.")


@cache_app.command("path")
def cache_path():
    """
    Display the path to the cache file.
    """
    console.print(f"Cache file is located at: [green]{db_utils.DB_PATH}[/green]")


@cache_app.command("list")
def list_cached_projects():
    """
    List all projects currently in the cache.
    """
    projects = db_utils.get_all_cached_projects()
    if not projects:
        console.print("[yellow]No projects found in the cache.[/yellow]")
        return

    table = Table(title="Cached Projects")
    table.add_column("Project Name", style="cyan")
    table.add_column("Last Updated", style="magenta")

    for project in projects:
        table.add_row(project.name, str(project.last_updated))

    console.print(table)
