"""``cwcli admin``: the web UI's administrator account, from the host.

``reset-password`` is the recovery path: back to ``admin`` / ``admin`` with the
password change forced again on the next sign-in, and every session of the
account signed out. See ``core.accounts``.
"""

from __future__ import annotations

import typer

from ..core import accounts as core_accounts
from ..core.errors import CwcliError
from ..utils.console import console, stderr_console

app = typer.Typer(help="The web UI's administrator account.")


@app.command("reset-password")
def reset_password(
    username: str = typer.Option(
        core_accounts.DEFAULT_USERNAME, "--username", "-u", help="The account to reset."
    ),
) -> None:
    """Reset the web UI account to admin / admin; the next sign-in must choose a new password."""
    try:
        core_accounts.reset_password(username)
    except CwcliError as e:
        stderr_console.print(f"[bold red]Error:[/bold red] {e.message}")
        raise typer.Exit(code=1) from e
    console.print(
        f"[green]✓[/green] '{username}' is back to the default password "
        f"('{core_accounts.DEFAULT_PASSWORD}'); the next sign-in must choose a new one. "
        "Every signed-in browser was signed out."
    )
