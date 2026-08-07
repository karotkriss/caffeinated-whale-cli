"""Nuitka ``--onefile`` entry point for the standalone cwcli Windows binary.

Build-time helper only. It is never shipped in the sdist or wheel: ``MANIFEST.in``
is a deny-all allow-list and re-includes only ``src/caffeinated_whale_cli``, so a
top-level ``packaging/`` file is excluded by construction.

Why a wrapper instead of compiling ``main.py`` directly: ``main.py`` uses package
relative imports (``from .commands import ...``). If Nuitka compiles it as the
top-level ``__main__`` there is no parent package for those to resolve against and
the build fails. Compiling this wrapper - which imports the package by its
absolute name - keeps the package a package. The release workflow pulls the whole
package into the binary with ``--include-package=caffeinated_whale_cli``.

The frozen binary carries no dist-info, so ``importlib.metadata`` cannot see the
distribution at runtime; ``core.version`` detects the frozen context and reads the
``__version__`` literal baked into the compiled ``__init__`` instead.
"""

from caffeinated_whale_cli.main import cli

if __name__ == "__main__":
    cli()
