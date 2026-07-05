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

    The recache is always non-interactive: it is invoked by callers (notably
    ``rm``) from inside a Rich ``console.status`` spinner, where an interactive
    prompt would be painted over and could never receive input. ``inspect`` is
    therefore called with ``prompt_to_start=False`` so that a stopped project
    degrades to a clean ``False`` return rather than deadlocking on a hidden
    "start the containers?" question.

    Args:
        project_name: Name of the project to recache
        verbose: Enable verbose output

    Returns:
        True if recache succeeded, False otherwise (including when the project's
        containers are not running, since a stopped bench cannot be inspected).
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
            no_refresh=False,
            show_apps=False,
            interactive=False,
            yes=False,
            prompt_to_start=False,
        )
        return True
    except Exception:
        return False
