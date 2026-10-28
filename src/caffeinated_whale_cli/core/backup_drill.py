"""Restore drills (backups spec phase 3): prove a site's latest good set restores, by
restoring it into a throwaway site on the same bench, running the post-checks, dropping
the throwaway site, and recording the verdict on the set (``drill``) so the site's
Backups section can say "last drill: ok, 2h ago". The throwaway is
``<first label>-drill.localhost``; it is dropped through the ordinary rm-site path
(its own gate archive lands in the store under reason before-removal and is pruned by
the policy like any safety set), and it is never the source site.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import backup_store
from . import new_site as core_new_site
from . import rm_site as core_rm_site
from .backup_store import StoreNotice, StoreStep
from .envelope import Result, Status
from .errors import CwcliError, ErrorKind

OnEvent = Callable[[Any], None]


def _noop(_e: Any) -> None:
    return None


@dataclass
class DrillReport:
    project: str
    site: str
    set_key: str | None
    drill_site: str
    created: bool = False
    restored: bool = False
    checks: list[dict[str, Any]] = field(default_factory=list)
    dropped: bool = False
    ok: bool = False
    detail: str = ""


def drill_site_name(site: str) -> str:
    first, _, rest = site.partition(".")
    return f"{first}-drill.{rest or 'localhost'}"


def run_drill(
    project: str,
    *,
    site: str,
    bench: str | None = None,
    mariadb_root_password: str,
    set_key: str | None = None,
    on_event: OnEvent | None = None,
) -> Result[DrillReport]:
    emit = on_event or _noop
    bset = backup_store.find_set(set_key) if set_key else backup_store.last_good(project, site)
    drill_site = drill_site_name(site)
    report = DrillReport(project, site, bset.key if bset else None, drill_site)
    if bset is None:
        report.detail = "no verified backup to drill from"
        emit(StoreNotice(report.detail, "error"))
        return Result(status=Status.WARNING, data=report)
    if drill_site == site:
        raise CwcliError(
            ErrorKind.USAGE, "drill.same_site", "The drill site cannot be the source site."
        )
    emit(StoreStep("drill", f"Restoring {bset.key} into the throwaway site {drill_site}"))
    try:
        made = core_new_site.new_site(
            project,
            site=drill_site,
            admin_password=secrets.token_urlsafe(18),
            bench=bench,
            apps=[a for a in bset.apps if a != "frappe"],
            db_root_password=mariadb_root_password,
            on_event=emit,
        )
        report.created = made.data is not None
        restored = backup_store.restore_set(
            bset.key,
            project=project,
            site=drill_site,
            bench=bench,
            mariadb_root_password=mariadb_root_password,
            consent=True,
            config_mode="carry",
            on_event=emit,
        )
        rep = restored.data
        report.restored = bool(rep and rep.restored)
        report.checks = [
            {"name": c.name, "ok": c.ok, "detail": c.detail}
            for c in (rep.post_checks if rep else [])
        ]
        migrate_ok = not (rep and rep.migrate_ran and not rep.migrate_ok)
        report.ok = report.restored and migrate_ok and all(c["ok"] for c in report.checks)
        report.detail = (
            "restored, migrated and answered every check"
            if report.ok
            else "; ".join(
                [
                    *(["migrate failed"] if not migrate_ok else []),
                    *(f"{c['name']} failed ({c['detail']})" for c in report.checks if not c["ok"]),
                ]
            )
            or "the restore did not complete"
        )
    except CwcliError as exc:
        report.detail = exc.message
        emit(StoreNotice(f"drill failed: {exc.message}", "error"))
    finally:
        if report.created:
            emit(StoreStep("drill", f"Dropping the throwaway site {drill_site}"))
            try:
                dropped = core_rm_site.drop_site(
                    project,
                    drill_site,
                    bench=bench,
                    consent=True,
                    db_root_password=mariadb_root_password,
                    on_event=emit,
                )
                report.dropped = dropped.data is not None
            except CwcliError as exc:
                emit(
                    StoreNotice(
                        f"the throwaway site {drill_site} could not be dropped: {exc.message}",
                        "error",
                    )
                )
        backup_store.record_drill(
            bset,
            {
                "at": backup_store._now(),
                "ok": report.ok,
                "detail": report.detail,
                "drill_site": drill_site,
                "dropped": report.dropped,
            },
        )
    emit(
        StoreNotice(
            f"drill of {bset.key}: {'OK' if report.ok else 'FAILED'} - {report.detail}",
            "info" if report.ok else "error",
        )
    )
    return Result(status=Status.OK if report.ok else Status.WARNING, data=report)
