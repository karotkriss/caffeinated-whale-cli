"""``cwcli self-update`` - upgrade cwcli itself, install-method aware.

Thin human-CLI frontend over :func:`core.version.check` and :mod:`core.self_update`
(which upgrades the install cwcli runs from, then restarts every cwcli service that
was running). It owns only the flag parsing, the rendering and the honest exit codes. Named ``self-update`` because the
top-level ``update`` is the (deprecated) Frappe-app updater.

Exit codes (honest, per the AXI idempotency norm):
  0  already up to date (incl. the dev-ahead case), upgraded OK, or a
     dev/uvx/standalone no-op; also a ``--check`` that reached PyPI and found no
     update, or a ``--check`` whose network lookup failed open.
  1  the upgrade failed or left the install unchanged (nothing is restarted then),
     a service did not come back on the new version, a network failure blocked an
     actual upgrade, or ``--check`` found that an update IS available.
"""

import typer
from rich.console import Console

from ..core import self_update as core_self_update
from ..core import version as core_version
from ..core.errors import CwcliError
from ..utils import shared_home

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
    (``uv tool upgrade`` or ``pip install --upgrade``) against that install - in
    shared mode run it with sudo. Then restarts every cwcli service that was
    running (scheduler, credential bridge, auto-inspect, web UI daemons) and
    reports which came back. A dev/editable checkout is always a no-op - upgrade
    it with ``git pull``.

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
    which = "the shared cwcli install" if shared_home.shared_mode() else "cwcli"
    console.print(
        f"[cyan]Upgrading {which} {info.current} -> {info.latest}[/cyan] "
        f"in [dim]{core_version.install_root()}[/dim] "
        f"via [dim]{' '.join(info.upgrade_command)}[/dim]",
        soft_wrap=True,
    )
    # Read what is running BEFORE the upgrade replaces the code under it.
    services = core_self_update.running_services()
    try:
        installed = core_self_update.upgrade(
            info,
            on_output=lambda line: console.print(
                line.rstrip("\n"), markup=False, highlight=False, soft_wrap=True
            ),
        )
    except CwcliError as e:
        stderr_console.print(f"[bold red]Error:[/bold red] {e.message}", soft_wrap=True)
        if e.hint:
            stderr_console.print(f"[dim]-> {e.hint}[/dim]")
        raise typer.Exit(1) from e
    console.print(f"[green]Upgraded cwcli to {installed}.[/green]")
    if not services:
        console.print("No cwcli services were running, so none needed a restart.")
        raise typer.Exit(0)
    console.print(f"Restarting {len(services)} running cwcli service(s) on {installed}:")
    results = core_self_update.restart(services, installed)
    for r in results:
        mark = "[green]✓[/green]" if r.ok else "[red]✗[/red]"
        console.print(f"  {mark} {r.name}: {r.detail}", soft_wrap=True)
    failed = [r for r in results if not r.ok]
    if failed:
        stderr_console.print(
            f"[bold red]Error:[/bold red] {len(failed)} service(s) did not come back on "
            f"{installed}; restart them by hand (see above)."
        )
        raise typer.Exit(1)
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
