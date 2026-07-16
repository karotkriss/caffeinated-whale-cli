"""
Cache management utilities for commands.

This module provides utilities for managing the project inspection cache,
including re-caching operations built on the core inspect slice.
"""

from . import db_utils


def recache_project(project_name: str, verbose: bool = False) -> bool:
    """
    Re-cache a project by clearing its cache and running a full core inspect.

    This ensures the cache is fresh and trustworthy for operations that depend
    on accurate project state (e.g., checking for missing apps before restore).

    The recache is always non-interactive: it is invoked by callers (notably
    ``rm``) from inside a Rich ``console.status`` spinner, where an interactive
    prompt would be painted over and could never receive input. ``core.inspect``
    is therefore called with ``offer_choice=False`` so that a stopped project
    surfaces as a typed ``NOT_RUNNING`` error, degraded here to a clean ``False``
    return rather than deadlocking on a hidden "start the containers?" question.

    Args:
        project_name: Name of the project to recache
        verbose: Unused; kept for signature compatibility with existing callers
            (the core populate emits no diagnostics on this path)

    Returns:
        True if recache succeeded, False otherwise (including when the project's
        containers are not running, since a stopped bench cannot be inspected).
    """
    try:
        # Clear the cache for this project
        db_utils.clear_cache_for_project(project_name)

        # Re-populate it with a full core inspect (owns the cache write).
        from ..core import inspect as core_inspect

        core_inspect.inspect(project_name, refresh="full", offer_choice=False)
        return True
    except Exception:
        # CwcliError(NOT_RUNNING) for a stopped project, and anything else the
        # populate raises, all degrade to the documented False.
        return False
