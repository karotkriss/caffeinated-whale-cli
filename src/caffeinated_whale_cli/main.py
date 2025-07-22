import typer

from .commands import list as list_cmd

app = typer.Typer(
    help="""
    [bold cyan]Caffeinated Whale CLI[/bold cyan] 🐳

    A friendly command-line tool to help you create, manage, and back up
    your Frappe and ERPNext Docker instances.
    """,
    rich_markup_mode="markdown",
)

app.add_typer(list_cmd.app, name="ls")


def cli():
    """The main entry point function for the CLI application."""
    app()


if __name__ == "__main__":
    cli()
