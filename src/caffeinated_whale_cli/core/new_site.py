"""``cwcli new-site``: one more site on an EXISTING bench - the same ``bench
new-site`` that ``init`` runs for a fresh bench, then ``install-app`` per
requested app, ``bench use`` when it is to be the default, and developer mode
when asked. UI-pure; the job runner, the CLI and ``axi`` are renderers.

Secrets ride exactly as in ``core.init``: both passwords in the exec
environment, referenced as ``$CWCLI_*`` in the command string, so no event,
report or trace carries a value. The MariaDB root password resolves as init's
does (given, else the one remembered at init, else the compose default).
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import db_password, resolvers
from . import docker as core_docker
from .envelope import Message, Result, Status
from .errors import CwcliError, ErrorKind
from .init import (
    InitStepEnd,
    InitStepStart,
    _build_cd_command,
    _directory_exists,
    _run_exec,
    _select_mariadb_flag,
)

OnEvent = Callable[[Any], None]
SITE_NAME_RE = re.compile(
    r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$"
)
APP_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True, kw_only=True)
class NewSiteReport:
    project: str
    bench_path: str
    site: str
    apps_installed: list[str] = field(default_factory=list)
    default_set: bool = False
    developer_mode: bool = False


def check_site_name(site: str) -> None:
    """A hostname-shaped, lowercase site name (what Frappe and the browser both want)."""
    resolvers.validate_site_name(site)
    if not SITE_NAME_RE.match(site):
        raise CwcliError(
            ErrorKind.USAGE,
            "site.name_invalid",
            f"'{site}' is not a valid site name.",
            hint="Use lowercase letters, digits, dots and dashes, like mysite.localhost or erp.example.com.",
        )


def frappe_major(container, bench_path: str) -> int | None:
    """The bench's Frappe major from apps/frappe/frappe/__init__.py (``__version__``)."""
    code, out = container.exec_run(
        [
            "sh",
            "-c",
            'grep -m1 -E "^__version__" "$1/apps/frappe/frappe/__init__.py" 2>/dev/null',
            "sh",
            bench_path,
        ]
    )
    text = (out or b"").decode("utf-8", "replace")
    m = re.search(r'["\'](\d+)\.', text)
    return int(m.group(1)) if m else None


def new_site(
    project: str,
    *,
    site: str,
    admin_password: str,
    bench: str | None = None,
    bench_path: str | None = None,
    apps: list[str] | None = None,
    default: bool = False,
    developer_mode: bool = False,
    db_root_password: str | None = None,
    on_event: OnEvent | None = None,
) -> Result[NewSiteReport]:
    emit = on_event or (lambda _e: None)
    warnings: list[Message] = []
    check_site_name(site)
    if not admin_password:
        raise CwcliError(
            ErrorKind.USAGE,
            "new_site.admin_password_required",
            "An Administrator password is required.",
        )
    wanted = [a for a in (apps or []) if a and a != "frappe"]
    for a in wanted:
        if not APP_NAME_RE.match(a):
            raise CwcliError(ErrorKind.USAGE, "new_site.app_invalid", f"'{a}' is not an app name.")

    container = core_docker.get_frappe_container(project)
    state = resolvers.resolve_container_state(project, container, offer_choice=True)
    if state.status is Status.NEEDS_CHOICE:
        return Result(status=Status.NEEDS_CHOICE, choice=state.choice)
    bench_result = resolvers.resolve_bench(project, bench, bench_path)
    if bench_result is None:
        bench_path = resolvers.DEFAULT_BENCH_PATH
        warnings.append(
            Message(
                "bench.default_used", f"No cached bench path found. Using default: {bench_path}"
            )
        )
    elif bench_result.status is Status.NEEDS_CHOICE:
        return Result(status=Status.NEEDS_CHOICE, choice=bench_result.choice, warnings=warnings)
    else:
        bench_path = bench_result.data
        warnings.extend(bench_result.warnings)
    assert bench_path is not None
    resolvers.validate_bench_path(bench_path)
    resolvers.require_bench_dir(container, bench_path)

    if _directory_exists(container, f"{bench_path}/sites/{site}"):
        raise CwcliError(
            ErrorKind.CONFLICT,
            "site.exists",
            f"A site named '{site}' already exists on {bench_path}.",
            hint="Pick another name, or open the existing site.",
        )
    for a in wanted:
        if not _directory_exists(container, f"{bench_path}/apps/{a}"):
            raise CwcliError(
                ErrorKind.NOT_FOUND,
                "new_site.app_missing",
                f"App '{a}' is not on this bench (no {bench_path}/apps/{a}).",
                hint=f"Fetch it first: cwcli apps install {project} <repo-url> --bench <bench> --fetch-only",
            )

    major = frappe_major(container, bench_path)
    mariadb_flag = _select_mariadb_flag(f"version-{major}" if major else "version-16")
    env = {
        "CWCLI_DB_ROOT_PASSWORD": db_password.resolve(project, db_root_password),
        "CWCLI_ADMIN_PASSWORD": admin_password,
    }
    cmd = _build_cd_command(
        bench_path,
        " ".join(
            [
                "bench",
                "new-site",
                "--db-root-password",
                '"$CWCLI_DB_ROOT_PASSWORD"',
                "--admin-password",
                '"$CWCLI_ADMIN_PASSWORD"',
                mariadb_flag,
                shlex.quote(site),
                "--verbose",
            ]
        ),
    )
    emit(InitStepStart(phase="new_site", item=site, message=f"Creating site {site}"))
    _run_exec(container, cmd, phase="new_site", emit=emit, environment=env, collect=True)
    emit(InitStepEnd(phase="new_site"))
    # Steering 041: Frappe scopes the new database user to the container's CURRENT
    # address; widen it to '%' now, so a restart that changes the IP cannot lock
    # the site out. A failure here is a warning, never a failed creation.
    from . import db_access

    widened, widen_warning = db_access.widen_after_create(
        container, bench_path, project=project, site=site, root_password=db_root_password
    )
    if widened:
        emit(
            InitStepStart(
                phase="db_access",
                item=site,
                message=f"{site}: database user now connects from any host",
            )
        )
        emit(InitStepEnd(phase="db_access"))
    if widen_warning is not None:
        warnings.append(widen_warning)

    installed: list[str] = []
    for a in wanted:
        emit(InitStepStart(phase="install_app", item=a, message=f"Installing {a} on {site}"))
        _run_exec(
            container,
            _build_cd_command(
                bench_path, f"bench --site {shlex.quote(site)} install-app {shlex.quote(a)}"
            ),
            phase="install_app",
            emit=emit,
            collect=True,
        )
        emit(InitStepEnd(phase="install_app"))
        installed.append(a)

    default_set = False
    if default:
        emit(InitStepStart(phase="use", item=site, message=f"Making {site} the default site"))
        _run_exec(
            container,
            _build_cd_command(bench_path, f"bench use {shlex.quote(site)}"),
            phase="use",
            emit=emit,
            collect=True,
        )
        emit(InitStepEnd(phase="use"))
        default_set = True

    dev_set = False
    if developer_mode:
        emit(InitStepStart(phase="developer_mode", item=site, message="Turning developer mode on"))
        _run_exec(
            container,
            _build_cd_command(
                bench_path, f"bench --site {shlex.quote(site)} set-config developer_mode 1"
            ),
            phase="developer_mode",
            emit=emit,
            collect=True,
        )
        emit(InitStepEnd(phase="developer_mode"))
        dev_set = True

    return Result(
        status=Status.WARNING if warnings else Status.OK,
        data=NewSiteReport(
            project=project,
            bench_path=bench_path,
            site=site,
            apps_installed=installed,
            default_set=default_set,
            developer_mode=dev_set,
        ),
        warnings=warnings,
    )
