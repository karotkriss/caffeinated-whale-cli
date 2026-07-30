"""``cwcli doctor`` - a system-wide, read-only environment preflight.

Thin CLI frontend over :func:`core.doctor.run_all`: it owns only the grouped
glyph-checklist rendering and the ``-v`` detail toggle. Every check, its
severity, and its suggested fix all live in the core so the human and ``axi``
surfaces render the exact same findings.

Exit codes (the maintainer's ruled contract, identical on both surfaces): a
WARN never blocks (exit 0), a FAIL exits non-zero - doctor is a chainable
preflight gate, and warnings are never a reason to stop a script.
"""

import typer
from rich.console import Console

from ..core.doctor import CheckResult, CheckStatus, run_all

console = Console()
stderr_console = Console(stderr=True)

_GLYPHS = {
    CheckStatus.PASS: "[bold green]✓[/bold green]",
    CheckStatus.WARN: "[bold yellow]![/bold yellow]",
    CheckStatus.FAIL: "[bold red]✗[/bold red]",
}


def _print_check(check: CheckResult, *, verbose: bool) -> None:
    # Rich reads a bare `[...]` as markup, so the literal check-id tag needs an
    # escaped bracket (`\\[`) or "[c1]" is silently swallowed as an unknown tag.
    tag = f"[dim]\\[{check.id}][/dim] " if verbose else ""
    name = f"{check.title:<32}"
    console.print(f"  {_GLYPHS[check.status]} {tag}{name}{check.detail}")
    if check.fix and check.status is not CheckStatus.PASS:
        console.print(f"      [dim]-> {check.fix}[/dim]")


def doctor(
    verbose: bool = typer.Option(
        False, "-v", "--verbose", help="Show each check's stable id alongside its title."
    ),
) -> None:
    """Check whether cwcli can operate on this machine: Docker, storage, tools.

    Strictly read-only: doctor never starts a container, installs anything, or
    writes a config file - it observes and suggests the fix command to run.
    Every check always runs (no tiers, no selection flags).
    """
    result = run_all()
    report = result.data
    assert report is not None  # doctor always returns data

    groups: dict[str, list[CheckResult]] = {}
    for check in report.checks:
        groups.setdefault(check.group, []).append(check)

    for group, checks in groups.items():
        console.print(f"\n[bold]{group}[/bold]")
        for check in checks:
            _print_check(check, verbose=verbose)

    console.print()
    summary = f"{report.passed} passed"
    if report.warned:
        summary += f", [yellow]{report.warned} warning(s)[/yellow]"
    if report.failed:
        summary += f", [bold red]{report.failed} failed[/bold red]"
    console.print(summary)

    raise typer.Exit(0 if report.ok else 1)
