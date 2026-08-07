"""``cwcli self-update`` - upgrade cwcli itself, install-method aware.

Thin human-CLI frontend over :func:`core.version.check`. It owns only the flag
parsing, the rendering, the subprocess run, and the honest exit codes; all
detection and lookup live in the UI-pure core. Named ``self-update`` because the
top-level ``update`` is the (deprecated) Frappe-app updater.

Exit codes (honest, per the AXI idempotency norm):
  0  already up to date (incl. the dev-ahead case), upgraded OK, or a
     dev/uvx/standalone no-op; also a ``--check`` that reached PyPI and found no
     update, or a ``--check`` whose network lookup failed open.
  1  the upgrade subprocess failed, a network failure blocked an actual upgrade,
     or ``--check`` found that an update IS available (so scripts/CI can gate).
"""

import subprocess

import typer
from rich.console import Console

from ..core import version as core_version

console = Console()
stderr_console = Console(stderr=True)


def self_update(
    check: bool = typer.Option(
        False,
        "--check",
        help="Dry run: report current vs latest and print the upgrade command; do not execute. "
        "Exits non-zero if an update is available.",
    ),
    no_cache: bool = typer.Option(
        False,
        "--no-cache",
        help="Force a fresh PyPI check, ignoring the shared ≤1-day version cache "
        "(use right after publishing a release).",
    ),
):
    """
    Upgrade cwcli itself to the latest version published on PyPI.

    Detects how cwcli was installed and runs the matching upgrade
    (``uv tool upgrade`` or ``pip install --upgrade``). A dev/editable checkout
    is always a no-op - upgrade it with ``git pull``.

    Examples:

        cwcli self-update           # upgrade if a newer version exists

        cwcli self-update --check   # report only; exit 1 if an update is available
    """
    result = core_version.check(use_cache=not no_cache)
    info = result.data
    assert info is not None  # core.version.check always returns data

    # Dev/editable checkout: never self-modify a source tree.
    if info.is_dev:
        where = f" at {info.dev_path}" if info.dev_path else ""
        console.print(
            f"[yellow]Running from a dev/editable checkout{where}.[/yellow]\n"
            "Upgrade it with [cyan]git pull[/cyan], not self-update."
        )
        raise typer.Exit(0)

    # Ephemeral `uvx --from ... cwcli` run: nothing persistent to upgrade.
    if info.method == "uvx":
        console.print(
            "[yellow]Running via an ephemeral uvx invocation - nothing to update.[/yellow]\n"
            f"Install it persistently with [cyan]uv tool install {core_version.DIST}[/cyan]."
        )
        raise typer.Exit(0)

    # Frozen standalone binary (Nuitka onefile / winget portable): self-update
    # cannot replace the running executable in place.
    if info.method == "standalone":
        console.print(
            "[yellow]Running from a standalone binary - self-update can't replace it "
            "in place.[/yellow]\n"
            "Upgrade with [cyan]winget upgrade caffeinated-whale-cli[/cyan] (if you "
            "installed via winget), or download the latest signed .exe from:\n"
            "    [cyan]https://github.com/karotkriss/caffeinated-whale-cli/releases/latest[/cyan]"
        )
        raise typer.Exit(0)

    network_failed = info.latest is None

    if check:
        _report(info, network_failed)
        if network_failed:
            raise typer.Exit(0)  # a read-only check must not punish a flaky network
        raise typer.Exit(1 if info.is_outdated else 0)

    # Default: run the upgrade.
    if network_failed:
        stderr_console.print(
            "[bold red]Error:[/bold red] Could not reach PyPI to check for updates."
        )
        raise typer.Exit(1)

    if not info.is_outdated:
        console.print(
            f"[green]cwcli is up to date[/green] (current {info.current}, latest {info.latest})."
        )
        raise typer.Exit(0)

    assert info.upgrade_command is not None  # uv/pip always carry a command
    console.print(
        f"[cyan]Upgrading cwcli {info.current} -> {info.latest}[/cyan] "
        f"via [dim]{' '.join(info.upgrade_command)}[/dim]"
    )
    try:
        completed = subprocess.run(info.upgrade_command)
    except FileNotFoundError as exc:
        stderr_console.print(
            f"[bold red]Error:[/bold red] '{info.upgrade_command[0]}' not found on PATH."
        )
        raise typer.Exit(1) from exc
    if completed.returncode != 0:
        stderr_console.print(
            f"[bold red]Error:[/bold red] Upgrade failed (exit {completed.returncode})."
        )
        raise typer.Exit(1)
    console.print(f"[green]Upgraded cwcli to {info.latest}.[/green]")
    raise typer.Exit(0)


def _report(info: core_version.VersionInfo, network_failed: bool) -> None:
    """``--check`` output: current/latest plus the upgrade command to run."""
    console.print(f"Current: [cyan]{info.current}[/cyan]")
    if network_failed:
        console.print("[yellow]Latest:  unknown (could not reach PyPI).[/yellow]")
        return
    console.print(f"Latest:  [cyan]{info.latest}[/cyan]")
    if info.is_outdated and info.upgrade_command:
        console.print(
            f"[yellow]An update is available.[/yellow] Upgrade with:\n"
            f"    [cyan]{' '.join(info.upgrade_command)}[/cyan]"
        )
    else:
        console.print("[green]cwcli is up to date.[/green]")
