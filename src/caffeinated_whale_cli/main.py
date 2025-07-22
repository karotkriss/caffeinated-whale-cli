# src/caffeinated_whale_cli/main.py

import typer
from caffeinated_whale_cli.commands import list as list_cmd

app = typer.Typer(help="Caffeinated Whale CLI – manage your whale ops.")

# Register command group under both 'list' and 'ls'
app.add_typer(list_cmd.app, name="list")
app.add_typer(list_cmd.app, name="ls")

if __name__ == "__main__":
    app()
