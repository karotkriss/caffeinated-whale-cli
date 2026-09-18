"""``cwcli setup`` - privileged, opt-in shared/multi-user provisioning.

Thin renderer over :mod:`core.setup`. Two subcommands, both root-only and
POSIX-only:

* ``cwcli setup shared`` provisions the shared state tree (the ``cwcli`` group +
  service account, ``/var/lib/cwcli`` with setgid group-writable subdirs, the
  ``/etc/cwcli/shared.toml`` marker, and optionally the machine-wide credential-
  bridge system unit).
* ``cwcli setup migrate`` consolidates existing per-user ``~/.cwcli`` homes into
  the shared tree.

This is distinct from ``cwcli axi setup`` (the agent SessionStart-hook installer).
Nothing here runs for a default per-user install.
"""

from typing import NoReturn

import typer
from rich.console import Console

from ..core import setup as core_setup
from ..core.errors import CwcliError

app = typer.Typer(
    help="Set up the opt-in shared/multi-user cwcli install (Linux/UNIX; requires sudo)."
)

console = Console()
stderr_console = Console(stderr=True)


def _fail(e: CwcliError) -> NoReturn:
    stderr_console.print(f"[bold red]Error:[/bold red] {e.message}")
    if e.hint:
        stderr_console.print(f"[dim]-> {e.hint}[/dim]")
    raise typer.Exit(code=1)


@app.command("shared")
def shared(
    group: str = typer.Option(
        core_setup.shared_home.DEFAULT_GROUP, "--group", help="Dedicated group name."
    ),
    state_dir: str = typer.Option(
        None,
        "--state-dir",
        help="Shared state tree root (default /var/lib/cwcli).",
    ),
    users: str = typer.Option(
        None,
        "--users",
        help="Comma-separated existing users to add to the group.",
    ),
    cred_bridge: bool = typer.Option(
        False,
        "--cred-bridge/--no-cred-bridge",
        help="Also install + enable the machine-wide credential-bridge system unit.",
    ),
) -> None:
    """Provision the shared state tree (idempotent; requires sudo).

    Creates the cwcli group and service account, the setgid group-writable state
    tree, and the machine marker that turns shared mode on. SECURITY: membership
    of the cwcli group is the trust boundary - anyone in it can read every
    instance's cache and backups, edit the shared config, and (with --cred-bridge)
    reach the machine-wide credential bridge. Keep membership deliberate.
    """
    from pathlib import Path

    try:
        result = core_setup.provision(
            group=group,
            state_dir=Path(state_dir) if state_dir else None,
            users=[u.strip() for u in users.split(",") if u.strip()] if users else None,
            cred_bridge=cred_bridge,
        )
    except CwcliError as e:
        _fail(e)

    outcome = result.data
    assert outcome is not None
    console.print("[bold green]✓[/bold green] Shared mode provisioned.")
    console.print(f"  state dir : {outcome.state_dir}")
    console.print(f"  group     : {outcome.group}")
    console.print(f"  service   : {outcome.service_user}")
    console.print(f"  marker    : {outcome.marker}")
    for action in outcome.actions:
        console.print(f"  [dim]- {action}[/dim]")
    console.print(
        "\n[dim]Add users with: sudo usermod -aG "
        f"{outcome.group} <user>  (they must re-login for the group to apply).[/dim]"
    )


@app.command("migrate")
def migrate(
    from_paths: list[str] = typer.Option(
        None,
        "--from",
        help="A per-user cwcli home to consolidate (repeatable).",
    ),
    discover: bool = typer.Option(
        False,
        "--discover",
        help="Also consolidate every /home/*/.cwcli and /root/.cwcli found.",
    ),
) -> None:
    """Consolidate per-user cwcli homes into the shared tree (idempotent; requires sudo).

    Copies each source's project state into the shared tree with the correct group
    ownership and setgid modes. It NEVER deletes a source and REFUSES to overwrite
    an existing shared project (reported as a conflict) rather than clobber it, so
    a re-run and a genuine name clash are both safe. The per-user SQLite cache is
    regenerable (via `cwcli inspect`) and deliberately not merged.
    """
    from pathlib import Path

    try:
        result = core_setup.consolidate(
            sources=[Path(p) for p in from_paths] if from_paths else None,
            discover=discover,
        )
    except CwcliError as e:
        _fail(e)

    outcome = result.data
    assert outcome is not None
    console.print("[bold green]✓[/bold green] Consolidation complete.")
    console.print(f"  sources merged  : {len(outcome.sources)}")
    console.print(f"  projects merged : {len(outcome.merged_projects)}")
    for proj in outcome.merged_projects:
        console.print(f"    [dim]+ {proj}[/dim]")
    if outcome.conflicts:
        stderr_console.print(
            f"\n[bold yellow]![/bold yellow] {len(outcome.conflicts)} project(s) skipped "
            "(already present in the shared tree; not overwritten):"
        )
        for conflict in outcome.conflicts:
            stderr_console.print(f"    [yellow]- {conflict}[/yellow]")
        stderr_console.print(
            "[dim]Resolve by renaming the source project or removing the shared one, "
            "then re-run.[/dim]"
        )
        raise typer.Exit(code=1)
