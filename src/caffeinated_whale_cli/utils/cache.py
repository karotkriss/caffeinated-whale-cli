"""
Cache management utilities for commands.

This module provides utilities for managing the project inspection cache,
including re-caching operations built on the core inspect slice.
"""

from . import config_utils, db_utils


def recache_project(
    project_name: str,
    verbose: bool = False,
    *,
    bench_path: str | None = None,
    warnings: list | None = None,
) -> bool:
    """
    Re-cache a project after a mutation: the whole project, or just one bench.

    This ensures the cache is fresh and trustworthy for operations that depend
    on accurate project state (e.g., checking for missing apps before restore).

    The recache is always non-interactive: it is invoked by callers (notably
    ``rm``) from inside a Rich ``console.status`` spinner, where an interactive
    prompt would be painted over and could never receive input. ``core.inspect``
    is therefore called with ``offer_choice=False`` so that a stopped project
    surfaces as a typed ``NOT_RUNNING`` error, degraded here to a clean ``False``
    return rather than deadlocking on a hidden "start the containers?" question.

    ``bench_path`` names the one bench a verb just changed. The recache then
    re-reads only that bench and splices it into the cached project
    (``core.inspect.refresh_bench``), falling back to the full inspect when the
    bench set changed elsewhere. A failed recache of either kind leaves the
    project's cache cleared, so the next read re-inspects rather than serving a
    cache the mutation made stale. A bench whose read fails inside a recache that
    otherwise succeeds keeps its cached row, and the recache's warnings (each
    naming the bench and the cause) are appended to ``warnings`` for the caller to
    show.

    Args:
        project_name: Name of the project to recache
        verbose: Unused; kept for signature compatibility with existing callers
            (the core populate emits no diagnostics on this path)
        bench_path: The bench the mutation touched, when it touched one
        warnings: Receives the recache's ``core.envelope.Message`` warnings

    Returns:
        True if recache succeeded, False otherwise (including when the project's
        containers are not running, since a stopped bench cannot be inspected).
    """
    # Caching off: there is no on-disk cache to refresh, so a post-mutation
    # recache would only run a full inspect into a store that is discarded.
    if config_utils.cache_disabled():
        return True

    from ..core import inspect as core_inspect

    try:
        if bench_path is not None:
            found = core_inspect.refresh_bench(project_name, bench_path).warnings
        else:
            # A full core inspect owns the cache write (an atomic rewrite).
            found = core_inspect.inspect(project_name, refresh="full", offer_choice=False).warnings
        if warnings is not None:
            warnings.extend(found)
        return True
    except Exception:
        # CwcliError(NOT_RUNNING) for a stopped project, and anything else the
        # populate raises, all degrade to the documented False.
        try:
            db_utils.clear_cache_for_project(project_name)
        except Exception:  # noqa: BLE001 - already reporting the failure
            pass
        return False
