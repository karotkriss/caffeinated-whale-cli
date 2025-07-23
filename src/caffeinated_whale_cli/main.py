import typer
# Import command modules
from .commands import list as list_cmd
from .commands import start as start_cmd
from .commands import stop as stop_cmd
# --- IMPORT THE 'inspect' FUNCTION DIRECTLY ---
from .commands.inspect import inspect as inspect_cmd_func

app = typer.Typer(
    help="""
    [bold cyan]Caffeinated Whale CLI[/bold cyan] 🐳

    A friendly command-line tool to help you create, manage, and back up
    your Frappe and ERPNext Docker instances.
    """,
    rich_markup_mode="markdown",
    epilog="Try `caffeinated-whale <COMMAND> --help` for more details on a command."
)

# --- REGISTER THE 'inspect' FUNCTION AS A COMMAND ---
# This tells Typer that 'inspect' is a single command, not a group.
app.command("inspect")(inspect_cmd_func)


# Register other commands that ARE groups
app.add_typer(list_cmd.app, name="list")
app.add_typer(list_cmd.app, name="ls")
app.add_typer(start_cmd.app, name="start")
app.add_typer(stop_cmd.app, name="stop")

def cli():
    """The main entry point function for the CLI application."""
    app()

if __name__ == "__main__":
    cli()