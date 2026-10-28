"""Developer mode per site: read where it comes from, set or unset it.

Frappe reads ``developer_mode`` from the merge of ``sites/common_site_config.json``
(bench-wide) and ``sites/<site>/site_config.json`` (the site overrides), so the
state a site runs with has a SOURCE: ``site``, ``common``, or ``default`` (off).
The read reports all three values so a page can say "on, set bench-wide in
common_site_config.json" rather than a bare "on".

Setting it is the same ``bench --site <site> set-config developer_mode 1|0``
(or ``bench set-config -g`` for the bench-wide value) followed by
``bench --site <site> clear-cache`` an operator runs by hand; it needs the
instance running, and consent is a CORE parameter (``NEEDS_CHOICE
confirm_dev_mode`` without it) so no frontend can skip the confirmation.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..utils import db_utils
from . import docker as core_docker
from . import resolvers
from .app_catalog import _host_bench_dir
from .envelope import Choice, Message, Result, Status
from .errors import CwcliError, ErrorKind

OnEvent = Callable[[Any], None]


@dataclass(frozen=True, slots=True, kw_only=True)
class DevModeState:
    project: str
    bench_path: str
    site: str
    site_value: bool | None  # developer_mode in site_config.json, None when unset
    common_value: bool | None  # developer_mode in common_site_config.json, None when unset
    effective: bool
    source: str  # "site" | "common" | "default"
    verified: bool  # read live from the files (True) or from cwcli's cache (False)


@dataclass(frozen=True, slots=True, kw_only=True)
class DevModeOutcome:
    before: DevModeState
    after: DevModeState
    scope: str  # "site" | "common"
    on: bool
    cache_cleared: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class DevModeStep:
    phase: str
    message: str
    status: str = "start"  # start | done


def _truthy(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _fold(site_value: bool | None, common_value: bool | None) -> tuple[bool, str]:
    if site_value is not None:
        return site_value, "site"
    if common_value is not None:
        return common_value, "common"
    return False, "default"


def _read_json_in_container(container, path: str) -> dict[str, Any] | None:
    code, out = container.exec_run(["cat", path])
    if code != 0:
        return None
    try:
        data = json.loads(out.decode("utf-8", "replace"))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _read_json_on_host(path: str) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_dev_mode(project: str, bench_path: str, site: str) -> DevModeState:
    """The site's developer-mode state: live from the files when the instance runs
    (or its bench is on this host's disk), else from cwcli's cache."""
    resolvers.validate_site_name(site)
    container = None
    try:
        container = core_docker.get_frappe_container(project)
    except CwcliError:
        container = None
    site_cfg = common_cfg = None
    verified = False
    if container is not None and getattr(container, "status", "") == "running":
        site_cfg = _read_json_in_container(container, f"{bench_path}/sites/{site}/site_config.json")
        common_cfg = _read_json_in_container(
            container, f"{bench_path}/sites/common_site_config.json"
        )
        verified = site_cfg is not None or common_cfg is not None
    else:
        host_dir = _host_bench_dir(project, bench_path)
        if host_dir is not None:
            site_cfg = _read_json_on_host(os.path.join(host_dir, "sites", site, "site_config.json"))
            common_cfg = _read_json_on_host(
                os.path.join(host_dir, "sites", "common_site_config.json")
            )
            verified = site_cfg is not None or common_cfg is not None
    if not verified:
        site_cfg = db_utils.get_site_config(project, site, bench_path)
        common_cfg = db_utils.get_common_site_config(project, bench_path)
    site_value = _truthy((site_cfg or {}).get("developer_mode"))
    common_value = _truthy((common_cfg or {}).get("developer_mode"))
    effective, source = _fold(site_value, common_value)
    return DevModeState(
        project=project,
        bench_path=bench_path,
        site=site,
        site_value=site_value,
        common_value=common_value,
        effective=effective,
        source=source,
        verified=verified,
    )


def read_dev_modes(project: str) -> dict[str, dict[str, Any]]:
    """Every cached site of the project, keyed ``<bench_path>/<site>``, from the cache
    (the detail route's cheap read; the site page does the live read)."""
    out: dict[str, dict[str, Any]] = {}
    data = db_utils.get_cached_project_data(project) or {}
    for bench in data.get("bench_instances") or []:
        bench_path = bench.get("path")
        if not bench_path:
            continue
        common_value = _truthy((bench.get("common_site_config") or {}).get("developer_mode"))
        for s in bench.get("sites") or []:
            if not isinstance(s, dict) or not s.get("name"):
                continue
            site_value = _truthy((s.get("site_config") or {}).get("developer_mode"))
            effective, source = _fold(site_value, common_value)
            out[f"{bench_path}/{s['name']}"] = {
                "effective": effective,
                "source": source,
                "site_value": site_value,
                "common_value": common_value,
            }
    return out


def set_dev_mode(
    project: str,
    *,
    site: str,
    on: bool,
    bench: str | None = None,
    bench_path: str | None = None,
    scope: str = "site",
    consent: bool = False,
    on_event: OnEvent | None = None,
) -> Result[DevModeOutcome]:
    """Turn developer mode on or off for ``site`` (``scope="site"``) or for the whole
    bench (``scope="common"``), then clear the site's cache. Needs the instance
    running (a stopped one is the ``confirm_start`` choice); refuses without
    ``consent`` as ``NEEDS_CHOICE confirm_dev_mode``."""
    emit = on_event or (lambda _e: None)
    warnings: list[Message] = []
    if scope not in ("site", "common"):
        raise CwcliError(
            ErrorKind.USAGE, "dev_mode.scope_invalid", "scope must be 'site' or 'common'."
        )
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
    resolvers.validate_site_name(site)
    resolvers.validate_bench_path(bench_path)
    resolvers.require_bench_dir(container, bench_path)
    resolvers.require_site_dir(container, bench_path, site)

    before = read_dev_mode(project, bench_path, site)
    if not consent:
        where = (
            "bench-wide in common_site_config.json"
            if scope == "common"
            else f"for {site} in its site_config.json"
        )
        return Result(
            status=Status.NEEDS_CHOICE,
            choice=Choice(
                kind="confirm_dev_mode",
                param="consent",
                prompt=(
                    f"Turn developer mode {'ON' if on else 'OFF'} {where}? "
                    + (
                        "On: DocType and other customisations save to the app's source files, "
                        "errors show full tracebacks in the browser, and assets rebuild on change. "
                        "Never leave it on where real users work."
                        if on
                        else "Off: customisations stop writing to source files and tracebacks are hidden again."
                    )
                    + " The site's cache is cleared afterwards."
                ),
            ),
            warnings=warnings,
        )

    value = "1" if on else "0"
    if scope == "common":
        cmd = ["bench", "set-config", "-g", "developer_mode", value]
    else:
        cmd = ["bench", "--site", site, "set-config", "developer_mode", value]
    emit(DevModeStep(phase="set", message=f"Setting developer_mode={value} ({scope})"))
    code, out = container.exec_run(cmd, user="frappe", workdir=bench_path)
    if code != 0:
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "dev_mode.set_failed",
            f"bench set-config failed (exit {code}).",
            detail={"output": out.decode("utf-8", "replace")[-2000:]},
        )
    emit(DevModeStep(phase="set", message="developer_mode written", status="done"))
    emit(DevModeStep(phase="clear-cache", message=f"Clearing {site}'s cache"))
    code, out = container.exec_run(
        ["bench", "--site", site, "clear-cache"], user="frappe", workdir=bench_path
    )
    cache_cleared = code == 0
    if not cache_cleared:
        warnings.append(
            Message(
                "dev_mode.cache_not_cleared",
                f"developer_mode is set, but 'bench --site {site} clear-cache' failed (exit {code}); run it by hand.",
            )
        )
    emit(
        DevModeStep(
            phase="clear-cache",
            message="cache cleared" if cache_cleared else "cache not cleared",
            status="done",
        )
    )
    after = read_dev_mode(project, bench_path, site)
    return Result(
        status=Status.OK if cache_cleared else Status.WARNING,
        data=DevModeOutcome(
            before=before, after=after, scope=scope, on=on, cache_cleared=cache_cleared
        ),
        warnings=warnings,
    )
