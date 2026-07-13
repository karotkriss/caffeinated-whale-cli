"""``core.where`` - the read-only cached-instance search, lifted into the core.

Searches the SQLite cache for apps or sites whose name matches a string, and
returns typed, serializable :class:`WhereMatch` rows wrapped in a
:class:`WhereResult`. It only reads (the cache DB); no live Docker object is
touched. The ``--apps``/``--sites`` conflict is a ``CwcliError(USAGE)`` each
frontend maps its own way. Table/JSON rendering stays in ``commands/where.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..utils import db_utils
from .envelope import Result, Status
from .errors import CwcliError, ErrorKind


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


@dataclass(frozen=True, slots=True, kw_only=True)
class WhereResult:
    """The typed outcome of a ``where`` search: matches sorted by project/type/name."""

    matches: list[WhereMatch] = field(default_factory=list)


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


def where(
    search: str,
    *,
    apps_only: bool = False,
    sites_only: bool = False,
    installed_only: bool = False,
) -> Result[WhereResult]:
    """Search the cache for apps/sites matching ``search``. See module docstring."""
    if apps_only and sites_only:
        raise CwcliError(
            ErrorKind.USAGE,
            "where.apps_sites_conflict",
            "Cannot use --apps and --sites together.",
        )

    matches: list[WhereMatch] = []

    if not sites_only:
        app_matches = _search_apps(search)
        if installed_only:
            app_matches = [m for m in app_matches if m.installed]
        matches.extend(_deduplicate_app_results(app_matches))

    if not apps_only:
        matches.extend(_search_sites(search))

    matches.sort(key=lambda m: (m.project, m.type, m.name))
    return Result(status=Status.OK, data=WhereResult(matches=matches))
