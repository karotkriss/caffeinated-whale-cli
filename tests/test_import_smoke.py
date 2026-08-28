"""Import smoke + private-API guard.

A P1 crash shipped because ``commands/axi.py`` imported ``typer._click.exceptions``
(a PRIVATE, non-API module) at module load: a Typer upgrade reshuffled that module
(0.27 dropped ``Abort`` from it), so the attribute lookup raised at import time and
made EVERY ``cwcli`` invocation crash. These tests fail CI on that class of breakage:
one imports every command module (a private-API break at load surfaces here), the
other statically forbids reaching into Typer/Click private submodules in ``src/``.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

import caffeinated_whale_cli.commands as commands_pkg

SRC = Path(__file__).resolve().parent.parent / "src" / "caffeinated_whale_cli"


def test_every_command_module_imports():
    """Importing each command module must not raise - a private-API breakage (the
    ``typer._click.exceptions.Abort`` incident) fails right here, before any verb runs."""
    names = [m.name for m in pkgutil.iter_modules(commands_pkg.__path__)]
    assert "axi" in names  # the module that carried the bug is actually covered
    for name in names:
        importlib.import_module(f"caffeinated_whale_cli.commands.{name}")


def test_no_private_typer_or_click_imports_in_src():
    """No shipped module may reach into Typer/Click PRIVATE internals: their layout is
    not API and shifts across releases. Only public homes (``typer.Abort``,
    ``click.exceptions``, ...) are allowed, so a Typer/Click upgrade cannot break the CLI."""
    offenders = []
    for path in SRC.rglob("*.py"):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue  # a comment naming the private module (to explain WHY it's avoided) is fine
            if "typer._click" in line or 'import_module("typer._' in line:
                offenders.append(f"{path.relative_to(SRC)}:{lineno}: {stripped}")
    assert not offenders, "private Typer internals referenced:\n" + "\n".join(offenders)
