import typer
import importlib.metadata

from .commands import list as list_cmd
from .commands import start as start_cmd
from .commands import stop as stop_cmd
from .commands import restart as restart_cmd
from .commands.inspect import inspect as inspect_cmd_func
from .commands import config as config_cmd
from .commands.run import run as _run_cmd
from .commands.logs import logs as _logs_cmd
from .commands.update import update as _update_cmd
from .commands.unlock import unlock as _unlock_cmd

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
):
    # Initialize context object
    ctx.ensure_object(dict)

app.command("inspect")(inspect_cmd_func)

app.add_typer(list_cmd.app, name="ls")
app.add_typer(start_cmd.app, name="start")
app.add_typer(stop_cmd.app, name="stop")
app.add_typer(restart_cmd.app, name="restart")
app.add_typer(config_cmd.app, name="config")
from .commands.run import run as _run_cmd
app.command("run")(_run_cmd)
app.command("update")(_update_cmd)
from .commands.status import status as _status_cmd
app.command("status")(_status_cmd)
from .commands.open import open_bench as _open_cmd
app.command("open")(_open_cmd)
app.command("logs")(_logs_cmd)
app.command("unlock")(_unlock_cmd)

def cli():
    """
    The main entry point function for the CLI application.
    This is what `pyproject.toml` calls.
    """
    app()

if __name__ == "__main__":
    cli()
