"""``core.config`` - settings, search paths, and cache inventory/clear, UI-pure.

The host-side half of the ``rework-config-dx`` migration (openspec, Decision 5).
Storage stays in ``utils/config_utils.py`` / ``utils/db_utils.py``; this module
owns the decisions: search-path validation and normalization live HERE, one
implementation for the ``paths`` verbs, the frozen ``add-path``/``remove-path``
aliases, and any future GUI. ``clear_cache`` takes destructive consent as a
PARAMETER and returns ``NEEDS_CHOICE`` without it - the fused-consent lesson
from ``apps uninstall`` applied from day one, so ``--yes`` stays one frontend's
UX spelling.

The one exception to "no Docker" is :func:`prune_search_paths` (issue #237): a
search path can only be shown dead by checking it against the LIVE instances it
was meant to help discover, so prune consults ``core.list``/``core.docker`` and
fails closed - a path present in any instance is kept, and if any instance
cannot be checked, nothing is pruned.
"""

from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass, field

from ..utils import config_utils, db_utils
from . import auto_inspect as core_auto_inspect
from . import cred_bridge as core_cred_bridge
from . import list as core_list
from .auto_inspect import AutoInspectState
from .cred_bridge import CredBridgeState
from .docker import get_project_containers
from .envelope import Choice, Result, Status
from .errors import CwcliError, ErrorKind


@dataclass(frozen=True, slots=True, kw_only=True)
class ConfigReport:
    """The one-shot effective config: every store `config show` renders."""

    config_file: str
    cache_db: str
    cache_enabled: bool
    cache_env_override: bool
    search_paths: list[str]
    auto_inspect: AutoInspectState
    cred_bridge: CredBridgeState
    show_tips: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class CacheState:
    """Whether the on-disk cache is on, and whether an env var is forcing it."""

    enabled: bool
    env_override: bool  # True when CWCLI_NO_CACHE is set (config key is inert)


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchPathChange:
    """A search-path add/remove: the normalized path and whether anything changed."""

    path: str
    changed: bool  # False = already present (add) / already absent (remove); still OK
    search_paths: list[str]  # the resulting list


@dataclass(frozen=True, slots=True, kw_only=True)
class TipsState:
    show_tips: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class CachedProjectDTO:
    name: str
    last_updated: str


@dataclass(frozen=True, slots=True, kw_only=True)
class CacheClearOutcome:
    """What a cache clear removed: one project (``found`` says whether it existed)
    or the whole cache."""

    scope: str  # "project" | "all"
    project: str | None
    found: bool


def _normalize(path: str) -> str:
    """Expanduser + normpath, so ``/a/b`` and ``/a/b/`` are one entry, not two (F9)."""
    return posixpath.normpath(posixpath.expanduser(path))


def _cache_env_override() -> bool:
    """True when CWCLI_NO_CACHE is set to a non-empty value (it wins over config)."""
    env = os.environ.get("CWCLI_NO_CACHE")
    return env is not None and env.strip() != ""


def show_config() -> Result[ConfigReport]:
    """The effective config as one report: paths, auto-inspect, tips, locations."""
    state = core_auto_inspect.status().data
    assert state is not None
    bridge_state = core_cred_bridge.status().data
    assert bridge_state is not None
    config = config_utils.load_config()
    return Result(
        status=Status.OK,
        data=ConfigReport(
            config_file=str(config_utils.CONFIG_FILE),
            cache_db=str(db_utils.DB_PATH),
            cache_enabled=not config_utils.cache_disabled(),
            cache_env_override=_cache_env_override(),
            search_paths=list(config["search_paths"]["custom_bench_paths"]),
            auto_inspect=state,
            cred_bridge=bridge_state,
            show_tips=config_utils.get_show_tips(),
        ),
    )


def set_cache(enabled: bool) -> Result[CacheState]:
    """Turn the on-disk cache on or off via the ``[cache] enabled`` config key.

    The cache DB is never deleted or altered here - it is left in place so
    re-enabling restores the previous behaviour. When ``CWCLI_NO_CACHE`` is set it
    overrides this key, so the returned :class:`CacheState` reports that override.
    """
    config_utils.set_cache_enabled(enabled)
    return Result(
        status=Status.OK,
        data=CacheState(
            enabled=not config_utils.cache_disabled(), env_override=_cache_env_override()
        ),
    )


def add_search_path(path: str) -> Result[SearchPathChange]:
    """Add a bench search path: refuse non-absolute, normalize before dedup.

    An already-present path is a no-op SUCCESS (``changed=False``), idempotent
    per the AXI standard.
    """
    expanded = posixpath.expanduser(path)
    if not posixpath.isabs(expanded):
        raise CwcliError(
            ErrorKind.USAGE,
            "path.not_absolute",
            f"'{path}' is not an absolute path.",
            hint="pass an absolute path, e.g. /home/you/benches",
        )
    normalized = posixpath.normpath(expanded)

    config = config_utils.load_config()
    paths = config["search_paths"]["custom_bench_paths"]
    if any(_normalize(p) == normalized for p in paths):
        return Result(
            status=Status.OK,
            data=SearchPathChange(path=normalized, changed=False, search_paths=list(paths)),
        )
    paths.append(normalized)
    config_utils.save_config(config)
    return Result(
        status=Status.OK,
        data=SearchPathChange(path=normalized, changed=True, search_paths=list(paths)),
    )


def remove_search_path(path: str) -> Result[SearchPathChange]:
    """Remove a bench search path, matching on the NORMALIZED form.

    No absolute-path refusal here, deliberately: a legacy config may hold a
    relative entry the old unvalidated ``add-path`` stored, and remove must be
    able to clean it out. An absent path is a no-op success (``changed=False``).
    """
    normalized = _normalize(path)
    config = config_utils.load_config()
    paths = config["search_paths"]["custom_bench_paths"]
    kept = [p for p in paths if _normalize(p) != normalized]
    if len(kept) == len(paths):
        return Result(
            status=Status.OK,
            data=SearchPathChange(path=normalized, changed=False, search_paths=list(paths)),
        )
    config["search_paths"]["custom_bench_paths"] = kept
    config_utils.save_config(config)
    return Result(
        status=Status.OK,
        data=SearchPathChange(path=normalized, changed=True, search_paths=kept),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchPathStatus:
    """One custom search path's live verdict.

    ``present`` is True when the path exists as a directory in at least one
    checked instance (so it is still referenced and must be kept). ``prunable``
    is True ONLY when the path was verified absent from EVERY instance AND every
    instance was checkable - so an unverifiable (stopped/unreachable) instance
    keeps every not-present path off the prune list.
    """

    path: str
    present: bool
    prunable: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class PrunePlan:
    """The outcome of a search-path prune (dry-run or applied).

    ``pruned`` is empty on a dry-run; ``applied`` says whether the config was
    written. ``unchecked_instances`` are instances that could not be inspected
    (stopped, or their frappe container unreachable) - while any exist, no path
    is prunable, and the frontend explains why nothing was dropped.
    """

    statuses: list[SearchPathStatus] = field(default_factory=list)
    unchecked_instances: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    applied: bool = False
    search_paths: list[str] = field(default_factory=list)


def _existing_dirs(container, paths: list[str]) -> set[str] | None:
    """The subset of ``paths`` that exist as directories inside ``container``.

    One exec for the whole list. Returns None if the check could not be trusted
    (exec failed, or the script did not run to completion) so the caller treats
    that instance as unchecked rather than "every path absent" - the same
    fail-honest rule ``core.where`` uses for an unreachable daemon.
    """
    # A trailing sentinel makes success detectable: a bare `for` loop's exit code
    # is that of its LAST `[ -d ]` test (1 when the last path is absent), so the
    # exit code cannot distinguish "ran, found nothing" from "exec broke".
    script = (
        'for p in "$@"; do [ -d "$p" ] && printf "%s\\n" "$p"; done; '
        'printf "__CWCLI_PRUNE_END__\\n"'
    )
    try:
        _code, output = container.exec_run(["sh", "-c", script, "sh", *paths])
    except Exception:
        return None
    try:
        text = output.decode("utf-8")
    except (UnicodeDecodeError, AttributeError):
        return None
    if "__CWCLI_PRUNE_END__" not in text:
        return None
    found = {line for line in text.splitlines() if line and line != "__CWCLI_PRUNE_END__"}
    return found & set(paths)


def _running_frappe_container(project_name: str):
    """The project's RUNNING frappe container, or None (stopped/absent/error)."""
    containers = get_project_containers(project_name)
    if not containers:
        return None
    for container in containers:
        if container.labels.get("com.docker.compose.service") == "frappe":
            return container if container.status == "running" else None
    return None


def prune_search_paths(*, apply: bool = False) -> Result[PrunePlan]:
    """Drop custom search paths that no live instance references (issue #237).

    ``search_paths`` accumulate a graveyard: a path added for an instance that
    was later removed lingers forever, scanned on every ``inspect``. This finds
    the dead ones by checking each path LIVE against every instance's frappe
    container: a path present in any instance is REFERENCED and kept; a path
    verified absent from all instances is a prune candidate.

    Fail-closed, because dropping a still-referenced path would re-hide the very
    hand-made benches issue #237 asks to surface: if the Docker daemon is
    unreachable the whole operation raises ``CwcliError(DOCKER)``, and if any
    single instance cannot be checked (stopped, or its container unreachable) NO
    path is prunable - it is reported ``unchecked`` so the frontend can say why.

    ``apply=False`` (the default) is a dry-run that computes the plan without
    touching the config; ``apply=True`` removes the prunable paths and persists.
    """
    config = config_utils.load_config()
    stored = list(config["search_paths"]["custom_bench_paths"])
    if not stored:
        return Result(status=Status.OK, data=PrunePlan(applied=apply, search_paths=[]))

    # Raises CwcliError(DOCKER) when the daemon is unreachable - can't verify, so
    # refuse the whole prune rather than guess every path is dead.
    instances = core_list.list_instances().data
    assert instances is not None

    present: set[str] = set()
    unchecked: list[str] = []
    for instance in instances:
        container = _running_frappe_container(instance.project_name)
        if container is None:
            unchecked.append(instance.project_name)
            continue
        found = _existing_dirs(container, stored)
        if found is None:
            unchecked.append(instance.project_name)
            continue
        present |= found

    statuses: list[SearchPathStatus] = []
    prunable: list[str] = []
    for path in stored:
        is_present = path in present
        # Prunable only when verified absent everywhere AND nothing was unchecked.
        can_prune = (not is_present) and not unchecked
        statuses.append(SearchPathStatus(path=path, present=is_present, prunable=can_prune))
        if can_prune:
            prunable.append(path)

    pruned: list[str] = []
    remaining = stored
    if apply and prunable:
        drop = set(prunable)
        remaining = [p for p in stored if p not in drop]
        config["search_paths"]["custom_bench_paths"] = remaining
        config_utils.save_config(config)
        pruned = prunable

    return Result(
        status=Status.OK,
        data=PrunePlan(
            statuses=statuses,
            unchecked_instances=unchecked,
            pruned=pruned,
            applied=apply,
            search_paths=remaining,
        ),
    )


def set_tips(enabled: bool) -> Result[TipsState]:
    config_utils.set_show_tips(enabled)
    return Result(status=Status.OK, data=TipsState(show_tips=enabled))


def cached_projects() -> Result[list[CachedProjectDTO]]:
    """The cache inventory, read-only."""
    projects = db_utils.get_all_cached_projects()
    return Result(
        status=Status.OK,
        data=[CachedProjectDTO(name=p.name, last_updated=str(p.last_updated)) for p in projects],
    )


def clear_cache(
    project: str | None = None, *, all_projects: bool = False, consent: bool = False
) -> Result[CacheClearOutcome]:
    """Clear one project's cache, or (with consent) the whole cache.

    A project name AND ``all_projects`` is contradictory and a USAGE error -
    never resolved toward the more destructive reading (F4). Neither target is
    likewise USAGE (F10: a usage error, so frontends map it to exit 2).
    ``all_projects`` without ``consent`` returns ``NEEDS_CHOICE``
    (``confirm_clear``); the CLI renders it as the confirm prompt / non-TTY
    refusal, so the core never decides destruction on its own.
    """
    if project and all_projects:
        raise CwcliError(
            ErrorKind.USAGE,
            "cache.conflicting_target",
            "Pass a project name OR --all, not both.",
        )
    if not project and not all_projects:
        raise CwcliError(
            ErrorKind.USAGE,
            "cache.no_target",
            "Pass a project name to clear one project, or --all to clear everything.",
        )

    if all_projects:
        if not consent:
            return Result(
                status=Status.NEEDS_CHOICE,
                choice=Choice(
                    kind="confirm_clear",
                    param="consent",
                    prompt="Are you sure you want to clear the entire cache?",
                ),
            )
        db_utils.clear_all_cache()
        return Result(
            status=Status.OK,
            data=CacheClearOutcome(scope="all", project=None, found=True),
        )

    assert project is not None  # narrowed by the target guards above
    found = db_utils.clear_cache_for_project(project)
    return Result(
        status=Status.OK,
        data=CacheClearOutcome(scope="project", project=project, found=found),
    )
