"""Log storage: what the logs around an instance weigh, and how to trim them.

Three stores, three different owners, one report (the web UI's Storage page, ``cwcli
axi storage logs``):

1. **Docker container logs** - the ``json-file`` log of every container of an instance,
   written by the Docker daemon under its data root. The rotation that bounds them is a
   container-creation property (``HostConfig.LogConfig``), so the durable fix is the
   compose file: :func:`set_docker_log_rotation` writes a ``logging:`` block (``max-size``
   / ``max-file``) into every service and it takes effect when a container is next
   recreated; :func:`truncate_docker_logs` is the immediate relief, emptying the live
   file and removing its rotated siblings. Both go through a short-lived root helper
   container that mounts ONLY each container's own log directory (the ``core.rm``
   ``_empty_dir_as_root`` shape), never by hand under the data root: the daemon writes
   the file ``O_APPEND``, so truncating it is safe while the container runs, and the
   helper is what works on a rootful Linux daemon and inside Docker Desktop's VM alike.
   A ``local``/other driver's files are binary-framed and never touched.
2. **Bench logs** - ``<bench>/logs`` inside the frappe container: Frappe's rotating
   handlers and supervisord leave ``*.log.1``, ``*.log.2.gz``, ... behind the current
   file. :func:`prune_bench_logs` removes only those rotated files; the current file of
   every program is kept, so nothing a running bench holds open is touched.
3. **The host journal** - ``journalctl --disk-usage`` where cwcli runs on a Linux host
   with systemd-journald. Vacuuming needs root, so the report carries the commands and
   says plainly when there is no journal to manage (macOS, Windows, a WSL distro without
   systemd; Docker Desktop's VM keeps its own).

Consent for the two removals is a core parameter (``NEEDS_CHOICE`` until ``consent``),
the ``backup_store.prune`` shape: a frontend can never bypass the confirmation.
"""

from __future__ import annotations

import os
import platform
import posixpath
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import docker

from ..utils import config_utils
from . import docker as core_docker
from . import list as core_list
from . import resolvers
from .envelope import Choice, Message, Result, Status
from .errors import DOCKER_UNREACHABLE_HINT, CwcliError, ErrorKind

DEFAULT_MAX_SIZE = "10m"
DEFAULT_MAX_FILE = 3
_MAX_SIZE_RE = re.compile(r"^0*[1-9]\d*[kmg]$")
_ROTATED_RE = re.compile(r"\.log\.\d+(\.gz)?$")
_HELPER_IMAGE_PREFIX = "frappe/bench"
_JOURNAL_VACUUM = ("sudo journalctl --vacuum-size=500M", "sudo journalctl --vacuum-time=2weeks")


# -------------------------------------------------------------------------- DTOs


@dataclass(frozen=True, slots=True)
class DockerLog:
    """One container's log driver, rotation and measured size."""

    container: str
    service: str
    driver: str
    max_size: str | None
    max_file: str | None
    rotation: bool  # json-file with a max-size cap
    size: int | None  # current + rotated files, None when not measurable
    files: int | None
    log_path: str | None


@dataclass(frozen=True, slots=True)
class DockerLogs:
    project: str
    containers: list[DockerLog]
    total: int | None  # None when any container could not be measured
    method: str | None  # "host" | "helper" | None
    compose_rotation: dict[str, str] | None  # the compose file's logging options
    note: str | None


@dataclass(frozen=True, slots=True)
class BenchLogs:
    project: str
    bench_path: str
    bench_index: int | None  # the cached `--bench` index, None when nothing is cached
    current_bytes: int
    rotated_bytes: int
    rotated_files: list[str]
    files: int


@dataclass(frozen=True, slots=True)
class JournalReport:
    applies: bool
    reason: str | None
    usage_text: str | None
    size: int | None
    privileged: bool
    commands: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class InstanceLogStorage:
    project: str
    running: bool
    docker: DockerLogs | None
    benches: list[BenchLogs]
    note: str | None


@dataclass(frozen=True, slots=True)
class LogStorageReport:
    instances: list[InstanceLogStorage]
    journal: JournalReport
    docker_total: int | None
    bench_total: int


def to_dict(report: Any) -> dict[str, Any]:
    return asdict(report)


# ------------------------------------------------------------ docker container logs


def _log_config(container) -> tuple[str, dict[str, str]]:
    cfg = (container.attrs.get("HostConfig") or {}).get("LogConfig") or {}
    return str(cfg.get("Type") or ""), dict(cfg.get("Config") or {})


def _log_path(container) -> str | None:
    path = container.attrs.get("LogPath")
    return str(path) if path else None


def _helper_image(client, containers) -> list[str]:
    """Images a root helper may run: a local ``frappe/bench`` first (the ``core.rm``
    choice), else the instance's own images - every one is already present."""
    images = [i.id for i in client.images.list(name=_HELPER_IMAGE_PREFIX)]
    for c in containers:
        image_id = (c.attrs.get("Image") or "").strip()
        if image_id and image_id not in images:
            images.append(image_id)
    return images


def _run_helper(client, images: list[str], volumes: dict[str, dict[str, str]], script: str) -> str:
    """One short-lived root helper over ``volumes`` (no network); its stdout."""
    last: Exception | None = None
    for image in images:
        try:
            out = client.containers.run(
                image,
                entrypoint=["sh", "-c", script],
                user="0:0",
                network_mode="none",
                volumes=volumes,
                remove=True,
            )
            return out.decode("utf-8", errors="replace") if isinstance(out, bytes) else str(out)
        except Exception as e:  # one image failing is not the answer; try the next
            last = e
    raise CwcliError(
        ErrorKind.DOCKER,
        "logs.helper_failed",
        "The log helper container could not run.",
        detail={"output": str(last) if last else "no image available"},
    )


def _host_sizes(paths: list[str]) -> dict[str, tuple[int, int]] | None:
    """``{log_path: (bytes, files)}`` read straight from the host, or None when the
    data root is not readable from here: a rootful daemon's ``/var/lib/docker`` is
    root-only (EACCES), and under Docker Desktop the path belongs to the VM (ENOENT).
    Either way the answer is the helper's, never a silent 0."""
    sizes: dict[str, tuple[int, int]] = {}
    for path in paths:
        p = Path(path)
        try:
            total, count = os.stat(p).st_size, 1
            with os.scandir(p.parent) as entries:
                for entry in entries:
                    if entry.name.startswith(p.name + ".") and entry.is_file():
                        total += entry.stat().st_size
                        count += 1
        except OSError:
            return None
        sizes[path] = (total, count)
    return sizes


def _helper_sizes(client, containers, paths: list[str]) -> dict[str, tuple[int, int]]:
    """The same sizes through a read-only root helper mounting each log directory."""
    volumes = {
        posixpath.dirname(path): {"bind": f"/logs/{i}", "mode": "ro"}
        for i, path in enumerate(paths)
    }
    script = 'for f in /logs/*/*-json.log*; do [ -f "$f" ] && stat -c \'%s %n\' "$f"; done; true'
    out = _run_helper(client, _helper_image(client, containers), volumes, script)
    by_dir: dict[str, tuple[int, int]] = {}
    for line in out.splitlines():
        size, _sep, name = line.strip().partition(" ")
        if not size.isdigit() or not name.startswith("/logs/"):
            continue
        slot = name.split("/")[2]
        total, count = by_dir.get(slot, (0, 0))
        by_dir[slot] = (total + int(size), count + 1)
    return {path: by_dir.get(str(i), (0, 0)) for i, path in enumerate(paths)}


def compose_rotation(project: str) -> dict[str, str] | None:
    """The ``logging:`` options the instance's compose file declares, or None."""
    path = config_utils.PROJECTS_DIR / project / "conf" / "docker-compose.yml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    size = re.search(r"max-size:\s*\"?([0-9]+[kmgKMG]?)\"?", text)
    count = re.search(r"max-file:\s*\"?([0-9]+)\"?", text)
    if not size:
        return None
    return {"max_size": size.group(1), "max_file": count.group(1) if count else "1"}


def docker_logs(project: str) -> DockerLogs:
    """Every container's log driver, rotation and size; measured from the host when the
    data root is readable, else through one read-only root helper per instance."""
    containers = core_docker.get_project_containers(project)
    if containers is None:
        raise CwcliError(
            ErrorKind.DOCKER,
            "docker.unreachable",
            "Could not connect to Docker daemon.",
            hint=DOCKER_UNREACHABLE_HINT,
        )
    if not containers:
        raise CwcliError(
            ErrorKind.NOT_FOUND, "project.not_found", f"Project '{project}' not found."
        )
    json_paths = [
        _log_path(c) for c in containers if _log_config(c)[0] == "json-file" and _log_path(c)
    ]
    paths = [p for p in json_paths if p is not None]
    method: str | None = None
    note: str | None = None
    sizes: dict[str, tuple[int, int]] = {}
    if paths:
        host = _host_sizes(paths)
        if host is not None:
            sizes, method = host, "host"
        else:
            try:
                sizes, method = _helper_sizes(docker.from_env(), containers, paths), "helper"
            except CwcliError as e:
                note = f"Sizes could not be measured: {e.message}"
    rows: list[DockerLog] = []
    for c in containers:
        driver, cfg = _log_config(c)
        path = _log_path(c)
        measured = sizes.get(path) if path and driver == "json-file" else None
        rows.append(
            DockerLog(
                container=c.name,
                service=c.labels.get("com.docker.compose.service", ""),
                driver=driver,
                max_size=cfg.get("max-size"),
                max_file=cfg.get("max-file"),
                rotation=driver == "json-file" and bool(cfg.get("max-size")),
                size=measured[0] if measured else None,
                files=measured[1] if measured else None,
                log_path=path,
            )
        )
    json_rows = [r for r in rows if r.driver == "json-file"]
    total = (
        sum(r.size or 0 for r in json_rows) if all(r.size is not None for r in json_rows) else None
    )
    if not json_rows and note is None:
        note = "No container uses the json-file driver; its logs are driver-managed."
    return DockerLogs(
        project=project,
        containers=rows,
        total=total,
        method=method,
        compose_rotation=compose_rotation(project),
        note=note,
    )


def truncate_docker_logs(project: str, *, consent: bool = False) -> Result[dict[str, Any]]:
    """Empty every json-file container log of the instance and remove its rotated
    siblings, through one root helper mounting only those log directories."""
    before = docker_logs(project)
    targets = [r for r in before.containers if r.driver == "json-file" and r.log_path]
    if not targets:
        return Result(
            status=Status.OK,
            data={"project": project, "truncated": [], "freed_bytes": 0},
            warnings=[
                Message("logs.nothing_to_truncate", "No json-file container log to truncate.")
            ],
        )
    if not consent:
        return Result(
            status=Status.NEEDS_CHOICE,
            choice=Choice(
                kind="confirm_truncate_logs",
                param="consent",
                prompt=(
                    f"Empty the Docker logs of {len(targets)} container"
                    f"{'s' if len(targets) != 1 else ''} of {project}? "
                    "Their `docker logs` history is lost; the containers keep running."
                ),
            ),
        )
    client = docker.from_env()
    containers = core_docker.get_project_containers(project) or []
    volumes = {
        posixpath.dirname(r.log_path): {"bind": f"/logs/{i}", "mode": "rw"}
        for i, r in enumerate(targets)
        if r.log_path
    }
    script = (
        'for f in /logs/*/*-json.log; do [ -f "$f" ] && : > "$f"; done; '
        "rm -f /logs/*/*-json.log.*; true"
    )
    _run_helper(client, _helper_image(client, containers), volumes, script)
    sizes = [r.size for r in targets]
    freed = None if None in sizes else sum(s for s in sizes if s is not None)
    return Result(
        status=Status.OK,
        data={
            "project": project,
            "truncated": [r.container for r in targets],
            "freed_bytes": freed,
        },
    )


def _validate_rotation(max_size: str, max_file: int) -> None:
    if not _MAX_SIZE_RE.match(max_size.lower()):
        raise CwcliError(
            ErrorKind.USAGE,
            "logs.max_size_invalid",
            f"max-size {max_size!r} is not a size like 10m, 500k or 1g.",
        )
    if not 1 <= max_file <= 100:
        raise CwcliError(
            ErrorKind.USAGE, "logs.max_file_invalid", "max-file must be between 1 and 100."
        )


def _logging_block(max_size: str, max_file: int) -> str:
    return (
        "    logging:\n"
        "      driver: json-file\n"
        "      options:\n"
        f'        max-size: "{max_size}"\n'
        f'        max-file: "{max_file}"\n'
    )


def with_log_rotation(
    compose_text: str, max_size: str = DEFAULT_MAX_SIZE, max_file: int = DEFAULT_MAX_FILE
) -> str:
    """``compose_text`` with a json-file ``logging:`` block on every active service
    (an existing block is replaced). Text-based like every other cwcli compose edit:
    no YAML dependency, and the rest of the file stays byte-identical."""
    _validate_rotation(max_size, max_file)
    lines = compose_text.splitlines(keepends=True)
    out: list[str] = []
    in_services = False
    skipping_logging = False
    for line in lines:
        stripped = line.rstrip("\n")
        if skipping_logging:
            # The block ends at the first blank line or at the next key of the service.
            if not stripped.strip() or (len(stripped) - len(stripped.lstrip(" "))) <= 4:
                skipping_logging = False
            else:
                continue
        if re.match(r"^\S", stripped) and stripped.strip():
            in_services = stripped.startswith("services:")
        if in_services and re.match(r"^    logging:\s*$", stripped):
            skipping_logging = True
            continue
        out.append(line)
        if in_services and re.match(r"^    image:\s*\S", stripped):
            out.append(_logging_block(max_size, max_file))
    return "".join(out)


def set_docker_log_rotation(
    project: str, *, max_size: str = DEFAULT_MAX_SIZE, max_file: int = DEFAULT_MAX_FILE
) -> Result[dict[str, Any]]:
    """Write the rotation into the instance's compose file. It binds when a container
    is next recreated (``docker compose up`` after an image or port change); the running
    containers keep the log config they were created with, which the report shows."""
    path = config_utils.PROJECTS_DIR / project / "conf" / "docker-compose.yml"
    if not path.exists():
        raise CwcliError(
            ErrorKind.NOT_FOUND,
            "compose.not_found",
            f"No cwcli compose file for project '{project}'.",
            hint=f"Was it created with 'cwcli init {project}'?",
        )
    text = path.read_text(encoding="utf-8")
    updated = with_log_rotation(text, max_size, max_file)
    if updated != text:
        path.write_text(updated, encoding="utf-8")
    return Result(
        status=Status.OK,
        data={
            "project": project,
            "max_size": max_size,
            "max_file": max_file,
            "changed": updated != text,
            "applies_on_recreate": True,
        },
        warnings=[
            Message(
                "logs.rotation_on_recreate",
                "Running containers keep their current log settings; the rotation "
                "applies when each container is next recreated.",
            )
        ],
    )


# ------------------------------------------------------------------- bench logs


def _bench_log_listing(container, bench_path: str) -> list[tuple[str, int]]:
    """``[(path, bytes)]`` for every regular file under ``<bench>/logs`` (one exec; the
    bench path rides as an argv positional, never inside the script text)."""
    code, output = container.exec_run(
        [
            "sh",
            "-c",
            'find "$1/logs" -maxdepth 1 -type f -exec stat -c "%s %n" {} + 2>/dev/null',
            "sh",
            bench_path,
        ]
    )
    rows: list[tuple[str, int]] = []
    for line in (
        output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output or "")
    ).splitlines():
        size, _sep, path = line.strip().partition(" ")
        if size.isdigit() and path:
            rows.append((path, int(size)))
    return rows


def is_rotated(path: str) -> bool:
    """A rotated backup (``web.log.1``, ``frappe.log.3.gz``), never a current file."""
    return bool(_ROTATED_RE.search(path.rsplit("/", 1)[-1]))


def _cached_paths(project: str) -> list[tuple[str, int | None]]:
    rows = resolvers.cached_benches(project)
    if not rows:
        return [(resolvers.DEFAULT_BENCH_PATH, None)]
    return [(str(r["path"]), r.get("index")) for r in rows]


def bench_logs(
    container, project: str, bench_path: str, bench_index: int | None = None
) -> BenchLogs:
    rows = _bench_log_listing(container, bench_path)
    rotated = [(p, s) for p, s in rows if is_rotated(p)]
    return BenchLogs(
        project=project,
        bench_path=bench_path,
        bench_index=bench_index,
        current_bytes=sum(s for p, s in rows if not is_rotated(p)),
        rotated_bytes=sum(s for _p, s in rotated),
        rotated_files=sorted(p for p, _s in rotated),
        files=len(rows),
    )


def prune_bench_logs(
    project: str, *, bench: str | None = None, consent: bool = False
) -> Result[dict[str, Any]]:
    """Remove the rotated log files of one bench (``bench``) or of every cached bench.
    Current files are never touched, so a running bench keeps every open log."""
    frappe = core_docker.get_frappe_container(project)
    # A stopped instance is a typed NOT_RUNNING (never an auto-start): the files live
    # inside the container, and a log cleanup must not start what a user stopped.
    resolvers.resolve_container_state(
        project,
        frappe,
        offer_choice=False,
        not_running_hint=f"Start it with `cwcli start {project}`.",
    )
    if bench is not None:
        resolved = resolvers.resolve_container_and_bench(project, bench, None)
        if isinstance(resolved, Result):
            return resolved
        paths = [resolved[1]]
    else:
        paths = [p for p, _i in _cached_paths(project)]
    reports = [bench_logs(frappe, project, p) for p in paths]
    doomed = [f for r in reports for f in r.rotated_files]
    freed = sum(r.rotated_bytes for r in reports)
    if not doomed:
        return Result(
            status=Status.OK,
            data={"project": project, "removed": [], "freed_bytes": 0, "benches": paths},
            warnings=[Message("logs.nothing_rotated", "No rotated bench log to remove.")],
        )
    if not consent:
        return Result(
            status=Status.NEEDS_CHOICE,
            choice=Choice(
                kind="confirm_prune_logs",
                param="consent",
                prompt=(
                    f"Remove {len(doomed)} rotated log file{'s' if len(doomed) != 1 else ''} "
                    f"({freed} bytes) from {project}? Every current log file is kept."
                ),
            ),
        )
    code, output = frappe.exec_run(["rm", "-f", "--", *doomed])
    if code not in (0, None):
        raise CwcliError(
            ErrorKind.DOCKER,
            "logs.prune_failed",
            "Removing the rotated log files failed.",
            detail={
                "output": (
                    output.decode("utf-8", errors="replace")
                    if isinstance(output, bytes)
                    else str(output)
                )
            },
        )
    return Result(
        status=Status.OK,
        data={"project": project, "removed": doomed, "freed_bytes": freed, "benches": paths},
    )


# --------------------------------------------------------------------- journal

_USAGE_RE = re.compile(r"take up ([\d.]+)\s*([KMGT]?)B?", re.IGNORECASE)
_UNIT = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}


def _parse_usage(text: str) -> int | None:
    m = _USAGE_RE.search(text)
    if not m:
        return None
    return int(float(m.group(1)) * _UNIT[m.group(2).upper()])


def journal() -> JournalReport:
    """``journalctl --disk-usage`` where this host runs systemd-journald; elsewhere a
    plain statement of why there is nothing to manage. Vacuuming needs root."""
    system = platform.system()
    if system != "Linux":
        where = "macOS" if system == "Darwin" else system or "this platform"
        return JournalReport(
            applies=False,
            reason=f"{where} has no systemd journal; Docker Desktop's VM keeps its own logs.",
            usage_text=None,
            size=None,
            privileged=False,
        )
    if shutil.which("journalctl") is None or not Path("/run/systemd/system").exists():
        return JournalReport(
            applies=False,
            reason="This Linux host does not run systemd-journald (no journal to manage).",
            usage_text=None,
            size=None,
            privileged=False,
        )
    try:
        proc = subprocess.run(
            ["journalctl", "--disk-usage"], capture_output=True, text=True, timeout=15
        )
        text = (proc.stdout or proc.stderr or "").strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        text = f"journalctl --disk-usage failed: {e}"
    privileged = hasattr(os, "geteuid") and os.geteuid() == 0
    return JournalReport(
        applies=True,
        reason=None if privileged else "Vacuuming needs root; run one of the commands.",
        usage_text=text,
        size=_parse_usage(text),
        privileged=privileged,
        commands=list(_JOURNAL_VACUUM),
    )


# ---------------------------------------------------------------------- report


def report(project: str | None = None) -> Result[LogStorageReport]:
    """Every instance's Docker and bench log sizes plus the host journal, one read."""
    instances = core_list.list_instances().data or []
    if project is not None:
        instances = [i for i in instances if i.project_name == project]
        if not instances:
            raise CwcliError(
                ErrorKind.NOT_FOUND, "project.not_found", f"Project '{project}' not found."
            )
    rows: list[InstanceLogStorage] = []
    for inst in instances:
        running = inst.status == "running"
        note: str | None = None
        try:
            dl: DockerLogs | None = docker_logs(inst.project_name)
        except CwcliError as e:
            dl, note = None, e.message
        benches: list[BenchLogs] = []
        if running:
            try:
                frappe = core_docker.get_frappe_container(inst.project_name)
                benches = [
                    bench_logs(frappe, inst.project_name, p, i)
                    for p, i in _cached_paths(inst.project_name)
                ]
            except CwcliError as e:
                note = e.message
        else:
            note = "Start the instance to measure its bench logs."
        rows.append(
            InstanceLogStorage(
                project=inst.project_name, running=running, docker=dl, benches=benches, note=note
            )
        )
    docker_sizes = [r.docker.total if r.docker is not None else None for r in rows]
    docker_total = (
        sum(s for s in docker_sizes if s is not None)
        if docker_sizes and None not in docker_sizes
        else None
    )
    bench_total = sum(b.current_bytes + b.rotated_bytes for r in rows for b in r.benches)
    return Result(
        status=Status.OK,
        data=LogStorageReport(
            instances=rows, journal=journal(), docker_total=docker_total, bench_total=bench_total
        ),
    )
