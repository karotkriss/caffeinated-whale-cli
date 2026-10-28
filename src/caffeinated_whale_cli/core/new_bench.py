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

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import apps as core_apps
from . import docker as core_docker
from . import init as core_init
from . import resolvers
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
    after_init: Callable[[str], object] | None = None,
    on_event: OnEvent | None = None,
) -> Result[NewBenchReport]:
    """Add a bench to ``project``. ``after_init(bench_path)`` runs once the bench and
    its site exist (the job runner's cache refresh), before the supervisor launch
    and the app installs, which both resolve the bench by its path."""
    emit = on_event or (lambda _e: None)
    warnings: list[Message] = []
    bench_name = core_init.validate_bench_slug(bench_name)
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
        on_event=emit,
    )
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
                project, wanted, bench_path=bench_path, sites=[site_name], on_event=emit
            )
            warnings.extend(apps_result.warnings)
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
