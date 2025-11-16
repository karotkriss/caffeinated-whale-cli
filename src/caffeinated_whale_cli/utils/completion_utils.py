"""
Tab completion utilities for CLI commands.

Provides autocompletion functions for projects, apps, and sites using Docker
and cached data from the database. Includes performance optimizations with
caching to reduce latency.
"""

import time

import docker
import typer

from . import db_utils

# Cache configuration
_CACHE_TTL = 2.0  # Cache results for 2 seconds (enough for tab completion session)
_docker_client: docker.DockerClient | None = None
_cache: dict[str, tuple[float, list[str]]] = {}


def _get_docker_client() -> docker.DockerClient | None:
    """
    Get or create a cached Docker client.

    Returns:
        Docker client instance or None if Docker is unavailable.
    """
    global _docker_client
    if _docker_client is None:
        try:
            _docker_client = docker.from_env()
        except Exception:
            return None
    return _docker_client


def _get_cached(key: str, ttl: float = _CACHE_TTL) -> list[str] | None:
    """
    Get cached completion results if still valid.

    Args:
        key: Cache key.
        ttl: Time-to-live in seconds.

    Returns:
        Cached list or None if expired/missing.
    """
    if key in _cache:
        cached_time, cached_value = _cache[key]
        if time.time() - cached_time < ttl:
            return cached_value
    return None


def _set_cached(key: str, value: list[str]) -> None:
    """
    Store completion results in cache.

    Args:
        key: Cache key.
        value: List of completion values.
    """
    _cache[key] = (time.time(), value)


def complete_project_names(
    ctx: typer.Context = None, args: list[str] = None, incomplete: str = ""
) -> list[str]:
    """
    Complete Frappe project names from Docker containers.

    Queries Docker for all containers with the frappe service label and
    extracts unique project names. Results are cached for 2 seconds to
    improve performance during tab completion sessions.

    Args:
        ctx: Typer context (unused but required by Typer's autocompletion).
        args: List of arguments (unused but required by Typer's autocompletion).
        incomplete: Partial string being completed (unused but required by Typer's autocompletion).

    Returns:
        Sorted list of project names. Empty list if Docker is unavailable.
    """
    # Check cache first
    cache_key = "projects"
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached

    try:
        client = _get_docker_client()
        if client is None:
            return []

        # Query with filters to reduce data transfer
        # Note: Cannot use sparse=True because we need access to labels
        containers = client.containers.list(
            all=True,
            filters={"label": "com.docker.compose.service=frappe"},
        )

        projects: set[str] = set()
        for container in containers:
            project_name = container.labels.get("com.docker.compose.project")
            if project_name:
                projects.add(project_name)

        result = sorted(projects)
        _set_cached(cache_key, result)
        return result
    except Exception:
        # Fail gracefully - Docker might not be running or accessible
        return []


def complete_app_names(
    ctx: typer.Context = None, args: list[str] = None, incomplete: str = ""
) -> list[str]:
    """
    Complete app names for a given project.

    Uses cached project data to get available apps. Requires the project_name
    parameter to be set in the context. Results are cached per project.

    Args:
        ctx: Typer context containing command parameters.
        args: List of arguments (unused).
        incomplete: Partial string being completed (unused).

    Returns:
        Sorted list of app names. Empty list if no project or cache data available.
    """
    # Get project name from context parameters
    if not ctx:
        return []
    project_name = ctx.params.get("project_name")
    if not project_name:
        return []

    # Check cache first
    cache_key = f"apps:{project_name}"
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached

    try:
        cached_data = db_utils.get_cached_project_data(project_name)
        if not cached_data or not cached_data.get("bench_instances"):
            return []

        apps: set[str] = set()
        for bench in cached_data["bench_instances"]:
            # Get available apps from each bench instance
            available_apps = bench.get("available_apps", [])
            apps.update(available_apps)

        result = sorted(apps)
        _set_cached(cache_key, result)
        return result
    except Exception:
        return []


def complete_site_names(
    ctx: typer.Context = None, args: list[str] = None, incomplete: str = ""
) -> list[str]:
    """
    Complete site names for a given project.

    Uses cached project data to get sites. Requires the project_name
    parameter to be set in the context. Results are cached per project.

    Args:
        ctx: Typer context containing command parameters.
        args: List of arguments (unused).
        incomplete: Partial string being completed (unused).

    Returns:
        Sorted list of site names. Empty list if no project or cache data available.
    """
    # Get project name from context parameters
    if not ctx:
        return []
    project_name = ctx.params.get("project_name")
    if not project_name:
        return []

    # Check cache first
    cache_key = f"sites:{project_name}"
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached

    try:
        cached_data = db_utils.get_cached_project_data(project_name)
        if not cached_data or not cached_data.get("bench_instances"):
            return []

        sites: set[str] = set()
        for bench in cached_data["bench_instances"]:
            # Get sites from each bench instance
            for site in bench.get("sites", []):
                site_name = site.get("name")
                if site_name:
                    sites.add(site_name)

        result = sorted(sites)
        _set_cached(cache_key, result)
        return result
    except Exception:
        return []
