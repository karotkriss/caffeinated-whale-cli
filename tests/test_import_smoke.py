"""Command-module import smoke tests.

A P1 crash shipped because ``commands/axi.py`` imported ``typer._click.exceptions``
(a PRIVATE, non-API module) at module load: a Typer upgrade reshuffled that module
(0.27 dropped ``Abort`` from it), so the attribute lookup raised at import time and
made EVERY ``cwcli`` invocation crash. Importing every command module reproduces
that class of failure at the same executable boundary.
"""

from __future__ import annotations

import importlib
import pkgutil

import caffeinated_whale_cli.commands as commands_pkg


def test_every_command_module_imports():
    """Importing each command module must not raise - a private-API breakage (the
    ``typer._click.exceptions.Abort`` incident) fails right here, before any verb runs."""
    names = [m.name for m in pkgutil.iter_modules(commands_pkg.__path__)]
    assert "axi" in names  # the module that carried the bug is actually covered
    for name in names:
        importlib.import_module(f"caffeinated_whale_cli.commands.{name}")
