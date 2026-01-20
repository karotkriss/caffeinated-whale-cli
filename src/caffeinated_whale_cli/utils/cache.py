"""
Cache management utilities for commands.

This module provides utilities for managing the project inspection cache,
including re-caching operations that require calling other commands.
"""

from . import db_utils


def recache_project(project_name: str, verbose: bool = False) -> bool:
    """
    Re-cache a project by clearing its cache and running inspect.

    This ensures the cache is fresh and trustworthy for operations that depend
    on accurate project state (e.g., checking for missing apps before restore).

    Args:
        project_name: Name of the project to recache
        verbose: Enable verbose output

    Returns:
        True if recache succeeded, False otherwise
    """
    try:
        # Clear the cache for this project
        db_utils.clear_cache_for_project(project_name)

        # Re-run inspect to populate cache
        from ..commands.inspect import inspect as inspect_cmd_func

        inspect_cmd_func(
            project_name=project_name,
            verbose=verbose,
            json_output=False,
            update=False,
            show_apps=False,
            interactive=False,
        )
        return True
    except Exception:
        return False
