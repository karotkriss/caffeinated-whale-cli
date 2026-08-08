"""``core.config`` - settings, search paths, and cache inventory/clear, UI-pure.

The host-side half of the ``rework-config-dx`` migration (openspec, Decision 5).
Storage stays in ``utils/config_utils.py`` / ``utils/db_utils.py``; this module
owns the decisions: search-path validation and normalization live HERE, one
implementation for the ``paths`` verbs, the frozen ``add-path``/``remove-path``
aliases, and any future GUI. ``clear_cache`` takes destructive consent as a
PARAMETER and returns ``NEEDS_CHOICE`` without it - the fused-consent lesson
from ``apps uninstall`` applied from day one, so ``--yes`` stays one frontend's
UX spelling.

Touches no Docker, no benches, no resolvers - the zero-new-primitives claim of
this batch.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass

from ..utils import config_utils, db_utils
from . import auto_inspect as core_auto_inspect
from . import cred_bridge as core_cred_bridge
from .auto_inspect import AutoInspectState
from .cred_bridge import CredBridgeState
from .envelope import Choice, Result, Status
from .errors import CwcliError, ErrorKind


@dataclass(frozen=True, slots=True, kw_only=True)
class ConfigReport:
    """The one-shot effective config: every store `config show` renders."""

    config_file: str
    cache_db: str
    search_paths: list[str]
    auto_inspect: AutoInspectState
    cred_bridge: CredBridgeState
    show_tips: bool


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
            search_paths=list(config["search_paths"]["custom_bench_paths"]),
            auto_inspect=state,
            cred_bridge=bridge_state,
            show_tips=config_utils.get_show_tips(),
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
