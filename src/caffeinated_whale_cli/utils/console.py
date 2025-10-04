"""Shared console instances for consistent output across all commands."""
from rich.console import Console

# Shared console instances
console = Console()
stderr_console = Console(stderr=True)
