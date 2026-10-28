"""``cwcli integrations``: list integrations, and add, update or remove community ones.

A community integration comes from a repository URL (GitHub, GitLab.com or a
self-hosted GitLab). ``add`` shows what the repository declares and the exact commit
before anything is installed, and installs only after you confirm (``--yes`` for a
script). Public repositories need no sign-in; a private one uses this machine's ``gh``
or ``glab`` sign-in. See ``core.community``. There is deliberately no ``cwcli axi``
twin: installing third-party code is a human decision.
"""

from __future__ import annotations

import getpass
import json
import os
from datetime import datetime

import typer

from ..core import community as core_community
from ..core import integrations as core_integrations
from ..core.errors import CwcliError
from ..utils import shared_home
from ..utils.console import console, stderr_console
from .utils import confirm_or_exit

app = typer.Typer(help="Integrations: list them; add, update or remove community ones.")

_WARNING = (
    "Community code runs with cwcli's own rights on this machine, and nobody has "
    "reviewed it. Install it only if you trust the publisher and this commit."
)


def _fail(e: CwcliError) -> None:
    stderr_console.print(f"[bold red]Error:[/bold red] {e.message}", soft_wrap=True)
    raise typer.Exit(code=1)


def _by() -> str:
    return f"cli:{os.environ.get('SUDO_USER') or getpass.getuser()}"


def _require_admin() -> None:
    """On a shared install only an Admin changes community integrations: root here,
    or an Admin in the web UI."""
    if shared_home.shared_mode() and hasattr(os, "geteuid") and os.geteuid() != 0:
        stderr_console.print(
            "[bold red]Error:[/bold red] on a shared install only an Admin adds, updates "
            "or removes community integrations: run this with sudo, or use Settings > "
            "Integrations in the web UI as an Admin.",
            soft_wrap=True,
        )
        raise typer.Exit(code=1)


def _show_declaration(decl: dict, url: str, ref: str | None, commit: str) -> None:
    console.print(f"[bold]{decl['name']}[/bold]  [yellow]Community, not reviewed[/yellow]")
    console.print(f"  publisher:   {decl['publisher']}", soft_wrap=True)
    console.print(f"  description: {decl['description']}", soft_wrap=True)
    console.print(f"  provides:    {', '.join(decl['provides']) or 'no hook points'}")
    console.print(f"  id:          {decl['id']}  (package {decl['project']})")
    console.print(f"  repository:  {url}", soft_wrap=True)
    console.print(f"  ref:         {ref or 'default branch'}")
    console.print(f"  commit:      {commit}")


def _read_with(signed_in_with: str | None, url: str) -> str:
    if signed_in_with is None:
        return "read anonymously (public)"
    from urllib.parse import urlsplit

    return f"read with this machine's {signed_in_with} sign-in for {urlsplit(url).hostname}"


@app.command("list")
def list_cmd(as_json: bool = typer.Option(False, "--json", help="Print JSON.")) -> None:
    """Every integration: official and community, with its state."""
    try:
        cards = core_integrations.view()
    except CwcliError as e:
        _fail(e)
    if as_json:
        console.print_json(json.dumps(cards))
        return
    for card in cards:
        badge = "Official" if card["publisher"] == "official" else "Community, not reviewed"
        console.print(f"[bold]{card['id']}[/bold]  {card['name']}  ({badge})  {card['state']}")
        community = card.get("community")
        if community:
            when = datetime.fromtimestamp(community["installed_at"]).strftime("%Y-%m-%d %H:%M")
            console.print(
                f"  {community['url']} @ {community['commit'][:12]} "
                f"({community.get('ref') or 'default branch'}) · by {community['publisher']} "
                f"· installed {when} by {community.get('installed_by') or '-'}",
                soft_wrap=True,
            )
            if community.get("load_error"):
                stderr_console.print(
                    f"  [red]does not load:[/red] {community['load_error']}", soft_wrap=True
                )


@app.command("add")
def add_cmd(
    url: str = typer.Argument(..., help="The repository URL (https://github.com/owner/repo)."),
    ref: str | None = typer.Option(None, "--ref", help="A branch or tag (default branch)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Install without asking."),
) -> None:
    """Add a community integration from a repository: shows what it declares and the
    exact commit, then installs that commit after you confirm. It starts off."""
    _require_admin()
    try:
        found = core_community.preview(url, ref)
    except CwcliError as e:
        _fail(e)
    decl = found.as_dict()["declaration"]
    _show_declaration(decl, found.url, found.ref, found.commit)
    console.print(f"  sign-in:     {_read_with(found.signed_in_with, found.url)}")
    console.print(f"[yellow]{_WARNING}[/yellow]", soft_wrap=True)
    confirm_or_exit(
        f"Install {decl['name']} at {found.commit[:12]}?",
        assume_yes=yes,
        refuse_message="Installing community code needs confirmation; pass --yes to install "
        "the commit shown above.",
    )
    try:
        rec = core_community.install(found.url, found.ref, commit=found.commit, by=_by())
    except CwcliError as e:
        _fail(e)
    console.print(
        f"[green]✓[/green] {rec['name']} installed at {rec['commit'][:12]}. It is off; turn it "
        "on in Settings > Integrations.",
        soft_wrap=True,
    )


@app.command("update")
def update_cmd(
    integration_id: str = typer.Argument(..., help="The community integration's id."),
    check: bool = typer.Option(False, "--check", help="Only show whether an update exists."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Update without asking."),
) -> None:
    """Check a community integration for a newer commit on its branch or tag, and
    install it after you confirm. The update turns it off until you turn it on again."""
    if not check:
        _require_admin()
    try:
        found = core_community.check_update(integration_id)
    except CwcliError as e:
        _fail(e)
    console.print(f"  installed: {found['current']}")
    console.print(f"  latest:    {found['latest']}  ({found.get('ref') or 'default branch'})")
    if not found["update_available"]:
        console.print(f"[green]✓[/green] {integration_id} is up to date.")
        return
    _show_declaration(found["declaration"], found["url"], found.get("ref"), found["latest"])
    if check:
        console.print(f"An update is available: {found['current'][:12]} -> {found['latest'][:12]}.")
        return
    console.print(f"[yellow]{_WARNING}[/yellow]", soft_wrap=True)
    confirm_or_exit(
        f"Update {integration_id} from {found['current'][:12]} to {found['latest'][:12]}?",
        assume_yes=yes,
        refuse_message="Updating community code needs confirmation; pass --yes to install "
        "the commit shown above.",
    )
    try:
        done = core_community.update(integration_id, commit=found["latest"], by=_by())
    except CwcliError as e:
        _fail(e)
    console.print(
        f"[green]✓[/green] {integration_id} updated: {done['old'][:12]} -> {done['new'][:12]}."
        + (
            " It is off until you turn it on again (its test runs first)." if done["was_on"] else ""
        ),
        soft_wrap=True,
    )


@app.command("remove")
def remove_cmd(
    integration_id: str = typer.Argument(..., help="The community integration's id."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Remove without asking."),
) -> None:
    """Remove a community integration: its package, settings and secrets."""
    _require_admin()
    try:
        rec = core_community.record(integration_id)
    except CwcliError as e:
        _fail(e)
    console.print(f"{rec['name']} ({rec['url']} @ {rec['commit'][:12]})", soft_wrap=True)
    confirm_or_exit(
        f"Remove {integration_id} with its settings and secrets?",
        assume_yes=yes,
        refuse_message="Removing an integration needs confirmation; pass --yes.",
    )
    try:
        core_community.remove(integration_id, by=_by())
    except CwcliError as e:
        _fail(e)
    console.print(f"[green]✓[/green] {integration_id} removed, with its settings and secrets.")
