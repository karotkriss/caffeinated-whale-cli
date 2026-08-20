"""Passive "a newer cwcli is available" notice, checked against PyPI at most once/day.

Thin frontend over :func:`core.version.passive_notice` (the cache-only,
non-blocking, fail-open gate). Rendered to STDERR only, and only when stderr is
a real TTY, so it NEVER corrupts a piped / ``--json`` / agent stdout - above all
the ``cwcli axi`` TOON contract, which an agent parses as a single document from
stdout - and never appears in scripts, CI, or captured output. Suppressible
entirely with ``CWCLI_NO_UPDATE_CHECK=1``.

Wired once into the root Typer callback (``main.py``), which runs for every
``cwcli ...`` and ``cwcli axi ...`` invocation, so both surfaces are covered
without repeating the check per command.
"""

from __future__ import annotations

import os
import sys

_ENV_DISABLE = "CWCLI_NO_UPDATE_CHECK"


def _stderr_is_tty() -> bool:
    return sys.stderr.isatty()


def notify_if_outdated() -> None:
    """Print a one-line update notice to stderr iff a human is watching and an
    upgrade is known-available. Never raises, never blocks, never touches stdout.
    """
    try:
        if os.environ.get(_ENV_DISABLE):
            return
        if not _stderr_is_tty():
            return
        from .core import version as core_version

        info = core_version.passive_notice()
        if info is None or not info.upgrade_command:
            return
        _print_notice(info.current, info.latest, " ".join(info.upgrade_command))
    except Exception:
        pass  # fail-open: a notice must never break a command


def _print_notice(current: str, latest: str | None, command: str) -> None:
    from rich.console import Console

    Console(stderr=True, highlight=False).print(
        f"[yellow]cwcli {latest} is available (you have {current}). Upgrade with:[/yellow] "
        f"[cyan]{command}[/cyan]",
        soft_wrap=True,  # never hard-wrap the upgrade command mid-token
    )
