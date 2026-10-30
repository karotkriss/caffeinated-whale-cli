"""``cwcli new-bench``: one more bench inside an EXISTING, running instance - the
same ``init_bench`` provisioning ``cwcli init`` runs for bench 0 (version gating,
pyenv/nvm for the Frappe major, ``bench init`` from upstream or a fork through the
credential bridge, the bench configs, the first site, optional ERPNext), then the
bench's supervisor launched like bench 0's (``core.start`` for that bench path),
then any extra apps fetched and installed on the first site (``core.apps``).

Composition only: no new provisioning step lives here. Python and Node versions
are not a choice - cwcli picks them from the Frappe major, as init does.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..utils import config_utils
from . import apps as core_apps
from . import db_password, resolvers
from . import docker as core_docker
from . import init as core_init
from . import start as core_start
from .envelope import Message, Result, Status
from .errors import CwcliError, ErrorKind

OnEvent = Callable[[Any], None]


@dataclass(frozen=True, slots=True, kw_only=True)
class NewBenchReport:
    project: str
    bench_name: str
    bench_path: str
    site: str
    frappe_ref: str
    erpnext_installed: bool
    started: bool
    apps_installed: list[str] = field(default_factory=list)
    apps_failed: list[str] = field(default_factory=list)


def new_bench(
    project: str,
    *,
    bench_name: str,
    site_name: str,
    admin_password: str,
    frappe_ref: str | None = None,
    version: str | None = None,
    frappe_url: str | None = None,
    install_erpnext: bool = False,
    erpnext_branch: str | None = None,
    apps: list[str] | None = None,
    bench_parent: str = "/workspace",
    db_root_password: str | None = None,
    with_deps: bool = False,
    dep_sources: dict[str, str] | None = None,
    ignore_frappe_range: bool = False,
    after_init: Callable[[str], object] | None = None,
    on_event: OnEvent | None = None,
) -> Result[NewBenchReport]:
    """Add a bench to ``project``. ``after_init(bench_path)`` runs once the bench and
    its site exist (the job runner's cache refresh), before the supervisor launch
    and the app installs, which both resolve the bench by its path.

    The extra ``apps`` go through ``core.apps.install_apps``' dependency plan against
    the bench just built: ``with_deps`` consents to their required apps, and a range
    that does not fit refuses them (the bench and its site stay; the apps are named
    as failed)."""
    emit = on_event or (lambda _e: None)
    warnings: list[Message] = []
    bench_name = core_init.validate_bench_slug(bench_name)
    site_name = core_init.validate_new_site_name(site_name)
    if not admin_password:
        raise CwcliError(
            ErrorKind.USAGE,
            "new_bench.admin_password_required",
            "An Administrator password is required.",
        )
    ref = frappe_ref or core_init.resolve_frappe_ref(version or "16")
    container = core_docker.get_frappe_container(project)
    state = resolvers.resolve_container_state(project, container, offer_choice=True)
    if state.status is Status.NEEDS_CHOICE:
        return Result(status=Status.NEEDS_CHOICE, choice=state.choice)

    planned_path = f"{bench_parent.rstrip('/') or '/workspace'}/{bench_name}"
    built = False

    def watch(event: Any) -> None:
        nonlocal built
        if isinstance(event, core_init.InitStepStart) and event.phase == "bench_init":
            built = True
        emit(event)

    try:
        init_result = core_init.init_bench(
            project,
            bench_name=bench_name,
            site_name=site_name,
            bench_parent=bench_parent,
            frappe_ref=ref,
            frappe_url=frappe_url or None,
            db_root_password=db_root_password,
            admin_password=admin_password,
            reuse_bench=False,  # an existing bench is a CONFLICT here, never silently reused
            install_erpnext=install_erpnext,
            erpnext_branch=erpnext_branch or ref,
            stream_output=True,
            on_event=watch,
        )
    except Exception as exc:
        # A bench this call's own `bench init` started is removed, so a retry with the
        # same name works (anything refused before that step, such as an existing
        # bench, was left untouched, and a lost exec may still be writing the
        # directory, so neither is removed).
        if not built or getattr(exc, "code", None) == "exec.stream_lost":
            raise
        try:
            removed = _remove_half_built(
                container, project, planned_path, site_name, db_root_password
            )
        except Exception:  # a failed cleanup must never hide why the build failed
            removed = False
        if removed and isinstance(exc, CwcliError):
            raise CwcliError(
                exc.kind,
                exc.code,
                f"{exc.message.rstrip().rstrip('.')}. cwcli removed the half-built bench "
                f"{planned_path}, "
                f"so a retry with the name '{bench_name}' works.",
                hint=exc.hint,
                detail=exc.detail,
            ) from exc
        raise
    if init_result.status is Status.NEEDS_CHOICE or init_result.data is None:
        return Result(
            status=Status.NEEDS_CHOICE, choice=init_result.choice, warnings=init_result.warnings
        )
    report = init_result.data
    warnings.extend(init_result.warnings)
    bench_path = report.bench_path
    if after_init is not None:
        after_init(bench_path)

    started = False
    try:
        start_result = core_start.start(project, bench_path=bench_path, on_event=emit)
        started = start_result.status in (Status.OK, Status.WARNING)
        warnings.extend(start_result.warnings)
    except CwcliError as e:
        warnings.append(
            Message(
                "new_bench.start_failed",
                f"The bench was created but its processes did not start: {e.message}",
            )
        )

    installed: list[str] = []
    failed: list[str] = []
    wanted = [a for a in (apps or []) if a]
    if wanted:
        try:
            apps_result = core_apps.install_apps(
                project,
                wanted,
                bench_path=bench_path,
                sites=[site_name],
                with_deps=with_deps,
                dep_sources=dep_sources,
                ignore_frappe_range=ignore_frappe_range,
                on_event=emit,
            )
            warnings.extend(apps_result.warnings)
            if apps_result.status is Status.NEEDS_CHOICE and apps_result.choice is not None:
                warnings.append(
                    Message(
                        "new_bench.apps_need_consent",
                        f"The extra apps were not installed: {apps_result.choice.prompt}",
                    )
                )
            for row in getattr(apps_result.data, "results", []) or []:
                (installed if getattr(row, "ok", False) else failed).append(
                    getattr(row, "app", "") or ""
                )
            if apps_result.data is None:
                failed.extend(wanted)
        except CwcliError as e:
            failed.extend(wanted)
            warnings.append(
                Message("new_bench.apps_failed", f"Fetching the extra apps failed: {e.message}")
            )

    return Result(
        status=Status.WARNING if warnings or failed else Status.OK,
        data=NewBenchReport(
            project=project,
            bench_name=bench_name,
            bench_path=bench_path,
            site=site_name,
            frappe_ref=ref,
            erpnext_installed=bool(getattr(report, "erpnext_installed", False)),
            started=started,
            apps_installed=installed,
            apps_failed=failed,
        ),
        warnings=warnings,
    )


def _remove_half_built(
    container, project: str, bench_path: str, site: str, db_root_password: str | None
) -> bool:
    """Best effort: drop the half-made site's database, delete the bench directory,
    and forget its search path. True when the directory is gone."""
    config_utils.remove_custom_path(bench_path)
    if not core_init._directory_exists(container, bench_path):
        return False
    # A failed `bench new-site` may already have created the site's database and user.
    if core_init._directory_exists(container, f"{bench_path}/sites/{site}"):
        container.exec_run(
            [
                "bash",
                "-lc",
                f"cd {shlex.quote(bench_path)} && bench drop-site {shlex.quote(site)} "
                '--force --no-backup --db-root-password "$CWCLI_DB_ROOT_PASSWORD"',
            ],
            environment={"CWCLI_DB_ROOT_PASSWORD": db_password.resolve(project, db_root_password)},
        )
    container.exec_run(["rm", "-rf", "--", bench_path])
    return not core_init._directory_exists(container, bench_path)
