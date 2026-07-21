"""``core.where`` - the read-only cached-instance search, lifted into the core.

Searches the SQLite cache for apps or sites whose name matches a string, and
returns typed, serializable :class:`WhereMatch` rows wrapped in a
:class:`WhereResult`. The ``--apps``/``--sites`` conflict is a
``CwcliError(USAGE)`` each frontend maps its own way. Table/JSON rendering stays
in ``commands/where.py``.

Every match carries a ``project_state`` token so a caller can always tell a
VERIFIED answer from a REMEMBERED one. The cache outlives the instances it
describes - a removed project's apps and sites stay in it - and ``where`` used
to present those rows identically to live ones, including while the Docker
daemon was unreachable. It now cross-checks each match's project against the
live compose-project listing (:func:`core.list.list_instances`) and reports
``present`` / ``absent`` / ``unverified``.

That is ONE Docker call per invocation regardless of match count - the same call
``cwcli axi ls`` makes, measured at ~75ms against a ~3ms cache read - and it
does NOT re-enter the expensive thing the cache exists to avoid: it never execs
into a bench to re-read apps or sites. ``verify=False`` is the escape hatch for
a caller that has already established liveness and wants the bare cache read.

Verification NEVER mutates: an ``absent`` row is reported, not pruned. Removing
a stale cache entry is ``cwcli inspect``'s job, and a read command that silently
deleted cached state would be the same class of defect in the other direction.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import peewee

from ..utils import db_utils
from .envelope import Message, Result, Status
from .errors import CwcliError, ErrorKind
from .list import list_instances

#: ``project_state`` tokens. ``unverified`` means the live check could not run -
#: it is deliberately distinct from ``present``, so an unreachable daemon can
#: never read as a confirmation.
PROJECT_PRESENT = "present"
PROJECT_ABSENT = "absent"
PROJECT_UNVERIFIED = "unverified"


@dataclass(frozen=True, slots=True, kw_only=True)
class WhereMatch:
    """One app or site match (serializable; site matches leave the app fields unset)."""

    type: str  # "app" | "site"
    project: str
    bench: str
    name: str
    version: str | None = None
    branch: str | None = None
    site: str | None = None
    installed: bool = False
    #: Whether this match's project still exists on the live Docker daemon:
    #: ``present`` | ``absent`` | ``unverified``. Every other field on this row
    #: is remembered, not observed - this one says whether to trust them.
    project_state: str = PROJECT_UNVERIFIED


@dataclass(frozen=True, slots=True, kw_only=True)
class WhereResult:
    """The typed outcome of a ``where`` search: matches sorted by project/type/name."""

    matches: list[WhereMatch] = field(default_factory=list)
    #: True only when the live compose-project listing actually ran and answered.
    verified: bool = False


def _search_apps(search_term: str) -> list[WhereMatch]:
    """Match available and installed apps by name (case-insensitive)."""
    db_utils.initialize_database()
    matches: list[WhereMatch] = []
    search_lower = search_term.lower()

    available_apps = (
        db_utils.AvailableApp.select(db_utils.AvailableApp, db_utils.Bench, db_utils.Project)
        .join(db_utils.Bench)
        .join(db_utils.Project)
    )
    for app_record in available_apps:
        if search_lower in app_record.name.lower():
            matches.append(
                WhereMatch(
                    type="app",
                    project=app_record.bench.project.name,
                    bench=app_record.bench.path,
                    name=app_record.name,
                )
            )

    installed_apps = (
        db_utils.InstalledAppDetail.select(
            db_utils.InstalledAppDetail, db_utils.Site, db_utils.Bench, db_utils.Project
        )
        .join(db_utils.Site)
        .join(db_utils.Bench)
        .join(db_utils.Project)
    )
    for app_record in installed_apps:
        if search_lower in app_record.name.lower():
            matches.append(
                WhereMatch(
                    type="app",
                    project=app_record.site.bench.project.name,
                    bench=app_record.site.bench.path,
                    name=app_record.name,
                    version=app_record.version or None,
                    branch=app_record.branch or None,
                    site=app_record.site.name,
                    installed=True,
                )
            )

    return matches


def _search_sites(search_term: str) -> list[WhereMatch]:
    """Match sites by name (case-insensitive)."""
    db_utils.initialize_database()
    matches: list[WhereMatch] = []
    search_lower = search_term.lower()

    sites = (
        db_utils.Site.select(db_utils.Site, db_utils.Bench, db_utils.Project)
        .join(db_utils.Bench)
        .join(db_utils.Project)
    )
    for site in sites:
        if search_lower in site.name.lower():
            matches.append(
                WhereMatch(
                    type="site",
                    project=site.bench.project.name,
                    bench=site.bench.path,
                    name=site.name,
                )
            )

    return matches


def _deduplicate_app_results(matches: list[WhereMatch]) -> list[WhereMatch]:
    """Prefer installed apps over available ones for the same (project, app)."""
    installed_keys = {(m.project, m.name) for m in matches if m.installed}

    deduplicated: list[WhereMatch] = []
    seen_installed: set[tuple] = set()
    for match in matches:
        if match.installed:
            # Keep every installed entry (different sites may host the same app once each).
            site_key = (match.project, match.name, match.site)
            if site_key not in seen_installed:
                seen_installed.add(site_key)
                deduplicated.append(match)
        elif (match.project, match.name) not in installed_keys:
            deduplicated.append(match)

    return deduplicated


def _live_project_names() -> set[str] | None:
    """The compose projects Docker currently knows about, or ``None`` if it could not say.

    Fail-HONEST, never fail-open: an unreachable daemon returns ``None``, which
    becomes ``unverified`` on every row. It must not degrade to an empty set,
    which would read as "every cached project is gone".
    """
    try:
        result = list_instances()
    except CwcliError:
        return None
    return {instance.project_name for instance in (result.data or [])}


def where(
    search: str,
    *,
    apps_only: bool = False,
    sites_only: bool = False,
    installed_only: bool = False,
    verify: bool = True,
) -> Result[WhereResult]:
    """Search the cache for apps/sites matching ``search``. See module docstring."""
    if apps_only and sites_only:
        raise CwcliError(
            ErrorKind.USAGE,
            "where.apps_sites_conflict",
            "Cannot use --apps and --sites together.",
        )

    matches: list[WhereMatch] = []

    # A corrupt/locked cache or a schema mismatch surfaces here as a raw peewee
    # error; keep it inside the core boundary as a typed CwcliError.
    try:
        if not sites_only:
            app_matches = _search_apps(search)
            if installed_only:
                app_matches = [m for m in app_matches if m.installed]
            matches.extend(_deduplicate_app_results(app_matches))

        if not apps_only:
            matches.extend(_search_sites(search))
    except peewee.PeeweeException as e:
        raise CwcliError(
            ErrorKind.INTERNAL,
            "cache.read_failed",
            "Failed to read the local cache.",
            detail={"output": str(e)},
        ) from e

    matches.sort(key=lambda m: (m.project, m.type, m.name))

    live = _live_project_names() if verify else None
    if live is None:
        # Not verified: either the caller opted out, or the daemon could not answer.
        # Rows stay `unverified` (the dataclass default), so nothing here vouches.
        warnings = (
            []
            if not verify
            else [
                Message(
                    "where.unverified",
                    "Could not reach the Docker daemon; these are cached results and "
                    "the instances may no longer exist.",
                )
            ]
        )
        return Result(
            status=Status.OK if not verify else Status.WARNING,
            data=WhereResult(matches=matches, verified=False),
            warnings=warnings,
        )

    matches = [
        replace(
            match,
            project_state=(PROJECT_PRESENT if match.project in live else PROJECT_ABSENT),
        )
        for match in matches
    ]
    stale = sorted({m.project for m in matches if m.project_state == PROJECT_ABSENT})
    warnings = (
        [
            Message(
                "where.stale_projects",
                f"Cached results reference {len(stale)} instance(s) that no longer exist: "
                f"{', '.join(stale)}. Run `cwcli inspect <project>` to refresh the cache.",
                detail={"projects": stale},
            )
        ]
        if stale
        else []
    )
    return Result(
        status=Status.WARNING if stale else Status.OK,
        data=WhereResult(matches=matches, verified=True),
        warnings=warnings,
    )
