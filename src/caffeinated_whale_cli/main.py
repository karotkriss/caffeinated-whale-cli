import typer
from typing import Optional
import importlib.metadata

from .commands import list as list_cmd
from .commands import start as start_cmd
from .commands import stop as stop_cmd
from .commands.inspect import inspect as inspect_cmd_func
from .commands import config as config_cmd
from .commands.run import run as _run_cmd

__version__ = importlib.metadata.version("caffeinated-whale-cli")

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
    ctx: typer.Context,
    version: bool = typer.Option(
        None,
        "--version",
        callback=version_callback,
        is_eager=True,
        help="Show the application's version and exit.",
    ),
    bench: Optional[str] = typer.Option(
        None,
        "--bench",
        "-b",
        help="Only operate on the specified bench alias",
        rich_help_panel="Global Options",
    ),
):
    # Initialize context object and store global bench filter
    ctx.ensure_object(dict)
    ctx.obj["bench"] = bench

app.command("inspect")(inspect_cmd_func)

app.add_typer(list_cmd.app, name="ls")
app.add_typer(start_cmd.app, name="start")
app.add_typer(stop_cmd.app, name="stop")
app.add_typer(config_cmd.app, name="config")
# Register apps subcommands for bench alias operations
from .commands.apps import list_apps as _list_apps_cmd
app.command("list-apps")(_list_apps_cmd)
app.command("run")(_run_cmd)
from .commands.status import status as _status_cmd
app.command("status")(_status_cmd)

def cli():
    """
    The main entry point function for the CLI application.
    This is what `pyproject.toml` calls.
    """
    app()

if __name__ == "__main__":
    cli()
