import typer
from rich.console import Console
from .. import config_utils

app = typer.Typer(help="Manage custom configuration for caffeinated-whale.")
console = Console()


@app.command("add-path")
def add_path(path: str = typer.Argument(..., help="The absolute path to a custom bench directory.")):
    if config_utils.add_custom_path(path):
        console.print(f"[green]Successfully added path:[/green] {path}")
    else:
        console.print(f"[yellow]Path already exists:[/yellow] {path}")

@app.command("remove-path")
def remove_path(path: str = typer.Argument(..., help="The custom bench directory path to remove.")):
    if config_utils.remove_custom_path(path):
        console.print(f"[green]Successfully removed path:[/green] {path}")
    else:
        console.print(f"[yellow]Path not found in configuration.[/yellow]")

@app.command("list-paths")
def list_paths():
    """Lists all saved custom bench directory search paths."""
    config = config_utils.load_config()
    paths = config.get("search_paths", {}).get("custom_bench_paths", [])
    
    if not paths:
        console.print("[yellow]No custom search paths are configured.[/yellow]")
        return
        
    console.print("[bold cyan]Custom Bench Search Paths:[/bold cyan]")
    for path in paths:
        console.print(f"- {path}")

@app.command("path")
def config_path():
    """Prints the path to the configuration file."""
    console.print(f"[bold cyan]Configuration file location:[/bold cyan]")
    console.print(str(config_utils.CONFIG_FILE))