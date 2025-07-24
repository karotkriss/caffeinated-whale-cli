import typer
import toml
from pathlib import Path

from .commands import list as list_cmd
from .commands import start as start_cmd
from .commands import stop as stop_cmd
from .commands.inspect import inspect as inspect_cmd_func
from .commands import config as config_cmd

__version__ = toml.load(Path(__file__).parent.parent.parent / "pyproject.toml")["project"]["version"]

app = typer.Typer(
    help="""
    A command-line tool to help you create, manage, and back up
    your Frappe and ERPNext Docker instances.
    """,
    rich_markup_mode="markdown",
)

def version_callback(value: bool):
    if value:
        print(f"Caffeinated Whale CLI Version: {__version__}")
        raise typer.Exit()

@app.callback()
def main(
    version: bool = typer.Option(
        None, "--version", callback=version_callback, is_eager=True, help="Show the application's version and exit."
    )
):
    pass

app.command("inspect")(inspect_cmd_func)

app.add_typer(list_cmd.app, name="ls")
app.add_typer(start_cmd.app, name="start")
app.add_typer(stop_cmd.app, name="stop")
app.add_typer(config_cmd.app, name="config")

def cli():
    """
    The main entry point function for the CLI application.
    This is what `pyproject.toml` calls.
    """
    app()

if __name__ == "__main__":
    cli()