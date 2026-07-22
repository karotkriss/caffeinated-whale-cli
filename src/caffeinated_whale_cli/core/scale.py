"""``core.scale`` - widen an instance's published port range so it can serve more
than six benches from one container, database-safe.

The problem this fixes (verified against a real instance, ``multibench-scale-verify``):
a Frappe instance's ``conf/docker-compose.yml`` publishes a FIXED port range - the
six web ports ``8000-8005`` and six socketio ports ``9000-9005`` cwcli writes at
init. Per-bench web/socketio ports are assigned by **bench init's own** ``make_ports``
(it scans sibling benches in the shared ``/workspace`` parent and increments from
8000/9000), stored in each bench's ``sites/common_site_config.json``. cwcli writes
ZERO port config - it only mirrors ports as derived cache data. So a 7th bench gets
``webserver_port: 8006``, binds ``:8006`` INSIDE the container fine, but is SILENTLY
unreachable from the host (no crash, no warning). That silent unreachability is the
core defect.

The five steps (each proven safe in the report):

1. **Read, don't invent, the port truth.** Each bench's ``sites/common_site_config.json``
   is the source of truth (``_read_assigned_ports``); this module never creates a
   second authoritative port store that could drift from bench's.
2. **Detect the ceiling and expand.** Reconcile the published range to cover
   ``[8000 .. max(webserver_port)]`` / ``[9000 .. max(socketio_port)]`` (and an
   optional ``--to N`` floor), preserving the host base cwcli chose at init.
3. **Recreate ONLY the frappe service** with ``docker compose ... up -d --no-deps
   frappe``. ``--no-deps`` is MANDATORY: it leaves MariaDB/Redis and the DB volume
   untouched. Never ``docker compose down``, never recreate without ``--no-deps`` -
   either destroys the database. This is a new capability: ``core.start`` uses the
   Docker SDK ``container.start()``, which cannot change a port mapping.
4. **Post-recreation toolchain repair, per bench, by major.** Recreation reverts
   ``/home/frappe`` to the image, wiping the RUNTIME-installed nvm node + pyenv
   python of v13/v14 benches (v15/v16 are image-baked and survive). We probe each
   bench's ``env/bin/python`` and re-run cwcli's existing idempotent installers only
   when the interpreter is broken - fail-honest and cheap.
5. **Relaunch supervisord for every bench** (recreation kills it) via ``core.start``.

Consent is a CORE decision, not a frontend-only one (the ``apps uninstall`` / rm
lesson): expansion restarts every serving bench in the instance - unavoidable, the
port map is a container-creation property - so when expansion is needed and
``consent`` is False, this returns ``NEEDS_CHOICE`` ``confirm_scale`` rather than
letting axi/GUI bypass the warning. A no-op (the range already covers every bench)
takes no consent - nothing restarts.

Memory, not ports, is the real ceiling past a few benches (two full serving benches
peaked ~8.4Gi on an 11Gi box); the verb surfaces that honestly and does not pretend
the port count is the binding limit.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass

from ..utils import config_utils
from ..utils.port_utils import check_ports_in_use, format_port_list
from . import docker as core_docker
from . import resolvers
from . import start as core_start
from .docker import align_container_user_to_host
from .envelope import Choice, Message, Result, Status
from .errors import CwcliError, ErrorKind
from .init import (
    _BRANCH_NODE,
    _BRANCH_PYTHON,
    _install_nvm_node,
    _install_pyenv_python,
    validate_project_slug,
)

# The container-side base ports every Frappe bench counts up from (make_ports).
_WEB_CONTAINER_BASE = 8000
_SOCKETIO_CONTAINER_BASE = 9000

# The published ``ports:`` lines cwcli writes at init look like
# ``- 16000-16005:8000-8005`` (web) and ``- 17000-17005:9000-9005`` (socketio).
# Capture the host low bound and the container high bound so we know the current
# published count and the host base to preserve when widening.
_WEB_PORTS_RE = re.compile(
    rf"(?m)^(?P<prefix>\s*-\s*)(?P<host_lo>\d+)-(?P<host_hi>\d+):"
    rf"{_WEB_CONTAINER_BASE}-(?P<cont_hi>\d+)\s*$"
)
_SOCKETIO_PORTS_RE = re.compile(
    rf"(?m)^(?P<prefix>\s*-\s*)(?P<host_lo>\d+)-(?P<host_hi>\d+):"
    rf"{_SOCKETIO_CONTAINER_BASE}-(?P<cont_hi>\d+)\s*$"
)


# ------------------------------------------------------------------ DTOs and events


@dataclass(frozen=True, slots=True, kw_only=True)
class BenchPortMap:
    """One bench's serving ports after the scale (serializable, no live object).

    ``webserver_port``/``socketio_port`` are the CONTAINER-side ports bench assigned
    (its ``common_site_config.json``); ``host_web_port``/``host_socketio_port`` are
    the host ports they now publish to; ``reachable`` is whether the host actually
    reaches this bench (True once the published range covers its assigned port).

    ``ports_verified`` is False when this bench's config could not be read live
    (a transient container/parse failure - see ``_read_assigned_ports``); in that
    case the port fields are None and ``reachable`` is False rather than a guessed
    8000/9000 reported as fact, matching the fail-honest contract the rest of this
    module (and ``core.where``) follows.
    """

    bench_path: str
    label: str | None
    webserver_port: int | None
    socketio_port: int | None
    host_web_port: int | None
    host_socketio_port: int | None
    reachable: bool
    ports_verified: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class ScaleReport:
    """The typed outcome of a scale (serializable, no live objects)."""

    project: str
    expanded: bool  # False on the idempotent no-op (range already covered every bench)
    previous_published_ports: int
    published_ports: int
    web_base: int
    socketio_base: int
    benches_restarted: int
    toolchain_repaired: list[str]  # bench paths whose v13/v14 toolchain was re-installed
    port_map: list[BenchPortMap]


@dataclass(frozen=True, slots=True, kw_only=True)
class ScaleProgress:
    """A coarse progress line. The CLI renders it to stderr; axi drains it."""

    message: str


OnEvent = Callable[[ScaleProgress], None]


def _noop(_event: ScaleProgress) -> None:
    """The drain-and-discard consumption mode (axi)."""


def _emit(on_event: OnEvent | None, message: str) -> None:
    (on_event or _noop)(ScaleProgress(message=message))


# ------------------------------------------------------------------ private helpers


def _decode(output) -> str:
    if isinstance(output, (bytes, bytearray)):
        return bytes(output).decode("utf-8", errors="replace")
    return str(output) if output is not None else ""


def _compose_path(project_name: str):
    """The project's cwcli-owned compose file, or NOT_FOUND naming init."""
    path = config_utils.PROJECTS_DIR / project_name / "conf" / "docker-compose.yml"
    if not path.exists():
        raise CwcliError(
            ErrorKind.NOT_FOUND,
            "compose.not_found",
            f"No cwcli compose file for project '{project_name}'.",
            hint=f"Was it created with 'cwcli init {project_name}'?",
        )
    return path


@dataclass(frozen=True, slots=True)
class _PublishedRange:
    """The parsed published port range from the compose file."""

    web_base: int
    socketio_base: int
    count: int  # how many consecutive ports are published (6 at init)


def _parse_published_range(compose_text: str, project_name: str) -> _PublishedRange:
    """Read the currently published web/socketio range from the compose file.

    The host base is preserved on widening; the count is the reconciliation floor
    (we never shrink). An unrecognized ports block is a hard PRECONDITION rather
    than a silent guess - editing a port mapping we could not parse is how a scale
    would corrupt the compose file.
    """
    web = _WEB_PORTS_RE.search(compose_text)
    sio = _SOCKETIO_PORTS_RE.search(compose_text)
    if web is None or sio is None:
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "compose.ports_unrecognized",
            f"Could not read the published port range for project '{project_name}'.",
            hint="Expected 'ports:' entries like '16000-16005:8000-8005'.",
        )
    web_count = int(web.group("cont_hi")) - _WEB_CONTAINER_BASE + 1
    sio_count = int(sio.group("cont_hi")) - _SOCKETIO_CONTAINER_BASE + 1
    # Web and socketio are created symmetric and bench increments both together;
    # keep the published count the max of the two so a hand-edited asymmetry heals.
    return _PublishedRange(
        web_base=int(web.group("host_lo")),
        socketio_base=int(sio.group("host_lo")),
        count=max(web_count, sio_count),
    )


def _read_assigned_ports(container, bench_paths: list[str]) -> dict[str, tuple[int, int]]:
    """Each bench's assigned (webserver_port, socketio_port) from its OWN config.

    Reads ``sites/common_site_config.json`` live inside the container - the source
    of truth (bench's ``make_ports`` output). A bench with no explicit value uses
    Frappe's defaults (8000/9000), same as bench itself. A bench whose config is
    unreadable is skipped (it cannot be reconciled), not defaulted, so a transient
    read error never shrinks the target.
    """
    assigned: dict[str, tuple[int, int]] = {}
    for bench_path in bench_paths:
        config_file = f"{bench_path.rstrip('/')}/sites/common_site_config.json"
        exit_code, output = container.exec_run(["bash", "-lc", f"cat {config_file}"])
        if exit_code != 0:
            continue
        try:
            config = json.loads(_decode(output))
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(config, dict):
            continue
        web = config.get("webserver_port", _WEB_CONTAINER_BASE)
        sio = config.get("socketio_port", _SOCKETIO_CONTAINER_BASE)
        try:
            assigned[bench_path] = (int(web), int(sio))
        except (TypeError, ValueError):
            continue
    return assigned


def _widen_ports_block(compose_text: str, published: _PublishedRange, count: int) -> str:
    """Rewrite both published ports lines to publish ``count`` consecutive ports.

    Preserves the host base cwcli chose at init; only the high bounds move up.
    """
    web_hi_host = published.web_base + count - 1
    web_hi_cont = _WEB_CONTAINER_BASE + count - 1
    sio_hi_host = published.socketio_base + count - 1
    sio_hi_cont = _SOCKETIO_CONTAINER_BASE + count - 1

    def web_repl(match: re.Match) -> str:
        return (
            f"{match.group('prefix')}{published.web_base}-{web_hi_host}:"
            f"{_WEB_CONTAINER_BASE}-{web_hi_cont}"
        )

    def sio_repl(match: re.Match) -> str:
        return (
            f"{match.group('prefix')}{published.socketio_base}-{sio_hi_host}:"
            f"{_SOCKETIO_CONTAINER_BASE}-{sio_hi_cont}"
        )

    compose_text = _WEB_PORTS_RE.sub(web_repl, compose_text, count=1)
    compose_text = _SOCKETIO_PORTS_RE.sub(sio_repl, compose_text, count=1)
    return compose_text


def _recreate_frappe(project_name: str, conf_dir: str) -> None:
    """Recreate ONLY the frappe service so the widened port map takes effect.

    ``--no-deps`` is mandatory and load-bearing: it recreates the frappe container
    alone and leaves MariaDB/Redis (and the DB volume) untouched. NEVER add
    ``down`` and never drop ``--no-deps`` - either takes the database with it.
    """
    cmd = [
        "docker",
        "compose",
        "-p",
        project_name,
        "-f",
        "docker-compose.yml",
        "up",
        "-d",
        "--no-deps",
        "frappe",
    ]
    result = subprocess.run(cmd, cwd=conf_dir, capture_output=True)
    if result.returncode != 0:
        raise CwcliError(
            ErrorKind.DOCKER,
            "compose.recreate_failed",
            "Failed to recreate the frappe service with the widened port range.",
            detail={"output": _decode(result.stderr)},
        )


def _wait_for_frappe_running(project_name: str, timeout: float = 30.0) -> bool:
    """Block until the recreated frappe container reports running (bounded)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            container = core_docker.get_frappe_container(project_name)
            container.reload()
            if container.status == "running":
                return True
        except CwcliError:
            pass
        time.sleep(1.0)
    return False


def _python_ok(container, bench_path: str) -> bool:
    """Whether the bench's ``env/bin/python`` is a working interpreter.

    A dangling venv interpreter (the v13/v14 wipe) is bench-fatal, so this is the
    fail-honest probe that decides whether toolchain repair is needed - we reinstall
    only when it is actually broken, not by guessing from the major alone.
    """
    python_bin = f"{bench_path.rstrip('/')}/env/bin/python"
    exit_code, _ = container.exec_run(["bash", "-lc", f"{python_bin} --version"])
    return bool(exit_code == 0)


def _bench_frappe_major(container, bench_path: str) -> int | None:
    """The bench's Frappe major, read from ``apps/frappe/frappe/__init__.py``.

    Read from the source file (not ``env/bin/python -c import frappe``) precisely
    because we only ask this when the venv interpreter is broken.
    """
    version_file = f"{bench_path.rstrip('/')}/apps/frappe/frappe/__init__.py"
    exit_code, output = container.exec_run(["bash", "-lc", f"cat {version_file}"])
    if exit_code != 0:
        return None
    match = re.search(r"""__version__\s*=\s*['"](\d+)\.""", _decode(output))
    return int(match.group(1)) if match else None


def _repair_toolchains(
    container, bench_paths: list[str], on_event: OnEvent | None, warnings: list[Message]
) -> list[str]:
    """Re-install the runtime toolchain of any v13/v14 bench recreation wiped.

    Probes each bench's ``env/bin/python``; only a BROKEN interpreter triggers a
    repair. Aligns the container ``frappe`` user to the host (``chown_home`` so the
    installers can write ``/home/frappe``) once, up front, only when at least one
    bench needs it. Re-runs cwcli's OWN idempotent installers - pyenv python FIRST
    (bench-fatal if skipped), then nvm node. v15/v16 benches are image-baked and
    survive untouched, so this is a no-op for them.
    """
    broken = [p for p in bench_paths if not _python_ok(container, p)]
    if not broken:
        return []

    _emit(on_event, "Re-aligning the container 'frappe' user to the host")
    _, remap_err = align_container_user_to_host(container, chown_home=True)
    if remap_err:
        warnings.append(Message("scale.uid_align_failed", remap_err))

    repaired: list[str] = []
    for bench_path in broken:
        major = _bench_frappe_major(container, bench_path)
        py_prefix = _BRANCH_PYTHON.get(major) if major is not None else None
        node_major = _BRANCH_NODE.get(major) if major is not None else None
        if not py_prefix and not node_major:
            # A broken interpreter on an image-baked major (v15/v16) is not
            # something a runtime re-install fixes; report it honestly.
            warnings.append(
                Message(
                    "scale.repair_unavailable",
                    f"Bench '{bench_path}' has a broken interpreter but its Frappe major "
                    "has no runtime toolchain to re-install; check the bench manually.",
                )
            )
            continue

        if py_prefix:
            _emit(on_event, f"Re-installing Python {py_prefix} for {bench_path} (pyenv)")
            _install_pyenv_python(container, py_prefix, _init_noop, warnings)
        if node_major:
            _emit(on_event, f"Re-installing Node.js {node_major} for {bench_path} (nvm)")
            _install_nvm_node(container, node_major, _init_noop, warnings)

        if _python_ok(container, bench_path):
            repaired.append(bench_path)
        else:
            # ponytail: pyenv reinstall restores the same patch version on the same
            # image, so the venv symlink target reappears and the interpreter works
            # again without a venv rebuild. If a future image bumps the patch
            # version the symlink would dangle and need re-pointing - warn instead
            # of silently claiming the bench is fixed.
            warnings.append(
                Message(
                    "scale.repair_incomplete",
                    f"Re-installed the toolchain for '{bench_path}' but its venv interpreter "
                    "is still broken; it may need 'cwcli run <project> --bench <b> "
                    "setup requirements' or a venv rebuild.",
                )
            )
    return repaired


def _init_noop(_event) -> None:
    """Drain the init installers' own event stream (their progress rides ours)."""


# ------------------------------------------------------------------ the verb


def scale(
    project_name: str,
    *,
    to: int | None = None,
    consent: bool = False,
    on_event: OnEvent | None = None,
) -> Result[ScaleReport]:
    """Widen the instance's published port range to cover every serving bench.

    ``to`` is an optional floor: ensure at least ``to`` benches are host-reachable
    (publish at least ``to`` web ports). The reconciliation target is always the
    max of the current published count, the max port any bench was assigned, and
    ``to`` - so this only ever EXPANDS and is a safe idempotent no-op when the range
    already covers every bench.

    Returns ``NEEDS_CHOICE`` ``confirm_scale`` when expansion is needed and
    ``consent`` is False: expanding recreates the frappe container, which restarts
    every serving bench in the instance. A no-op needs no consent.
    """
    project_name = validate_project_slug(project_name)
    if to is not None and to < 1:
        raise CwcliError(
            ErrorKind.USAGE,
            "scale.bad_to",
            "--to must be a positive number of benches.",
        )

    compose_path = _compose_path(project_name)

    # The container must be running: we read each bench's live config, recreate the
    # service, and relaunch supervisord. Refuse a stopped instance rather than
    # auto-starting it (the port map only matters when benches serve).
    frappe_container = core_docker.get_frappe_container(project_name)
    resolvers.resolve_container_state(
        project_name,
        frappe_container,
        auto_start=False,
        offer_choice=False,
        not_running_hint=f"Start it first with 'cwcli start {project_name}'.",
    )

    benches = resolvers.cached_benches(project_name)
    if not benches:
        raise CwcliError(
            ErrorKind.NOT_FOUND,
            "benches.none_cached",
            f"No cached benches for project '{project_name}'.",
            hint=f"Run 'cwcli inspect {project_name}' first.",
        )
    bench_paths = [b["path"] for b in benches]
    labels = {b["path"]: b.get("label") for b in benches}

    compose_text = compose_path.read_text()
    published = _parse_published_range(compose_text, project_name)
    assigned = _read_assigned_ports(frappe_container, bench_paths)

    # Reconciliation target: cover [8000..max web] / [9000..max socketio], honor the
    # --to floor, and never shrink below what is already published.
    max_web = max((w for w, _ in assigned.values()), default=_WEB_CONTAINER_BASE)
    max_sio = max((s for _, s in assigned.values()), default=_SOCKETIO_CONTAINER_BASE)
    needed = max(max_web - _WEB_CONTAINER_BASE + 1, max_sio - _SOCKETIO_CONTAINER_BASE + 1)
    target = max(published.count, needed, to or 0)
    expanded = target > published.count

    if expanded and not consent:
        return Result(
            status=Status.NEEDS_CHOICE,
            choice=Choice(
                kind="confirm_scale",
                param="consent",
                prompt=(
                    f"Expanding project '{project_name}' from {published.count} to {target} "
                    f"published ports recreates the frappe container, which RESTARTS every "
                    f"serving bench in the instance. Continue?"
                ),
                default="false",
            ),
        )

    warnings: list[Message] = []
    toolchain_repaired: list[str] = []
    benches_restarted = 0

    if expanded:
        # Pre-check the NEWLY claimed host ports (never the already-published ones -
        # those are this project's own container's current binding, not a conflict)
        # before any filesystem write, the same check-before-write order
        # core.init_instance uses via the same utils/port_utils.py helpers.
        new_web_ports = list(
            range(published.web_base + published.count, published.web_base + target)
        )
        new_socketio_ports = list(
            range(published.socketio_base + published.count, published.socketio_base + target)
        )
        port_status = check_ports_in_use(new_web_ports + new_socketio_ports)
        ports_in_use = [p for p, in_use in port_status.items() if in_use]
        if ports_in_use:
            raise CwcliError(
                ErrorKind.CONFLICT,
                "scale.ports_in_use",
                f"The following newly needed ports are already in use: "
                f"{format_port_list(ports_in_use)}",
                hint="Free the ports (or stop whatever is using them) and retry.",
            )

        _emit(on_event, f"Widening the published range from {published.count} to {target} ports")
        compose_path.write_text(_widen_ports_block(compose_text, published, target))

        conf_dir = str(compose_path.parent)
        _emit(on_event, "Recreating the frappe service (--no-deps: the database is untouched)")
        try:
            _recreate_frappe(project_name, conf_dir)
        except CwcliError:
            # The compose command itself never applied the widened file - restore
            # the original so a retry re-attempts the expansion instead of reading
            # the file's already-widened range as a completed, idempotent no-op.
            compose_path.write_text(compose_text)
            raise

        if not _wait_for_frappe_running(project_name):
            raise CwcliError(
                ErrorKind.NOT_RUNNING,
                "scale.container_not_running",
                f"The frappe container for '{project_name}' did not come back up after "
                "recreation.",
            )
        # Re-resolve: the old handle points at the destroyed container.
        frappe_container = core_docker.get_frappe_container(project_name)

        toolchain_repaired = _repair_toolchains(frappe_container, bench_paths, on_event, warnings)

        _emit(on_event, "Relaunching every bench's dev processes")
        for bench_path in bench_paths:
            try:
                core_start.start(project_name, bench_path=bench_path, restart=True)
                benches_restarted += 1
            except CwcliError as exc:
                warnings.append(
                    Message(
                        "scale.bench_restart_failed",
                        f"Could not relaunch bench '{bench_path}': {exc.message}",
                    )
                )

    # Recompute the map from the (possibly re-read) assigned ports so the report is
    # honest even on the no-op path.
    if expanded:
        assigned = _read_assigned_ports(frappe_container, bench_paths)
    published_after = target

    port_map: list[BenchPortMap] = []
    unverified_benches: list[str] = []
    for bench_path in bench_paths:
        ports = assigned.get(bench_path)
        if ports is None:
            unverified_benches.append(bench_path)
            port_map.append(
                BenchPortMap(
                    bench_path=bench_path,
                    label=labels.get(bench_path),
                    webserver_port=None,
                    socketio_port=None,
                    host_web_port=None,
                    host_socketio_port=None,
                    reachable=False,
                    ports_verified=False,
                )
            )
            continue
        web, sio = ports
        web_offset = web - _WEB_CONTAINER_BASE
        sio_offset = sio - _SOCKETIO_CONTAINER_BASE
        reachable = web_offset < published_after and sio_offset < published_after
        port_map.append(
            BenchPortMap(
                bench_path=bench_path,
                label=labels.get(bench_path),
                webserver_port=web,
                socketio_port=sio,
                host_web_port=published.web_base + web_offset,
                host_socketio_port=published.socketio_base + sio_offset,
                reachable=reachable,
                ports_verified=True,
            )
        )

    if unverified_benches:
        warnings.append(
            Message(
                "scale.bench_ports_unverified",
                f"Could not read the assigned ports for {len(unverified_benches)} bench(es) "
                f"({', '.join(unverified_benches)}); their reachability is unknown, not "
                "confirmed - re-run once the container is responsive.",
            )
        )

    # Memory, not ports, is the practical ceiling past a few serving benches - do
    # not let the tool imply the port count is the binding limit.
    if expanded and target > 6:
        warnings.append(
            Message(
                "scale.memory_ceiling",
                "Publishing more ports does not add memory: several full benches serving at "
                "once can exhaust host RAM well before the port range does.",
            )
        )

    status = Status.WARNING if warnings else Status.OK
    return Result(
        status=status,
        data=ScaleReport(
            project=project_name,
            expanded=expanded,
            previous_published_ports=published.count,
            published_ports=published_after,
            web_base=published.web_base,
            socketio_base=published.socketio_base,
            benches_restarted=benches_restarted,
            toolchain_repaired=toolchain_repaired,
            port_map=port_map,
        ),
        warnings=warnings,
    )
