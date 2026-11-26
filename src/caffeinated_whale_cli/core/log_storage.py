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
2. **Bench logs** - ``<bench>/logs`` and each site's ``<bench>/sites/<site>/logs``
   inside the frappe container: Frappe's rotating handlers and supervisord leave
   ``*.log.1``, ``*.log.2.gz``, ... behind the current file. :func:`prune_bench_logs`
   removes only those rotated files; the current file of every logger is kept, so
   nothing a running bench holds open is touched.
3. **The host journal** - ``journalctl --disk-usage`` where cwcli runs on a Linux host
   with systemd-journald. :func:`vacuum_journal` trims it when cwcli runs as root;
   otherwise the report carries the commands to copy and says why, and it says plainly
   when there is no journal to manage (macOS, Windows, a WSL distro without systemd;
   Docker Desktop's VM keeps its own).

Consent for the three removals is a core parameter (``NEEDS_CHOICE`` until ``consent``),
the ``backup_store.prune`` shape: a frontend can never bypass the confirmation.
"""

from __future__ import annotations

import os
import platform
import posixpath
import re
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import docker

from ..utils import config_utils
from . import docker as core_docker
from . import inspect as core_inspect
from . import list as core_list
from . import resolvers
from .envelope import Choice, Message, Result, Status
from .errors import DOCKER_UNREACHABLE_HINT, CwcliError, ErrorKind

DEFAULT_MAX_SIZE = "10m"
DEFAULT_MAX_FILE = 3
_MAX_SIZE_RE = re.compile(r"^0*[1-9]\d*[kmg]$")
_ROTATED_RE = re.compile(r"\.log\.\d+(\.gz)?$")
_HELPER_IMAGE_PREFIX = "frappe/bench"
DEFAULT_JOURNAL_SIZE = "500M"
DEFAULT_JOURNAL_TIME = "2weeks"
_JOURNAL_SIZE_RE = re.compile(r"^0*[1-9]\d*[KMGT]?$")
_JOURNAL_TIME_RE = re.compile(r"^0*[1-9]\d*(s|min|h|d|weeks?|months?|years?)$")
_JOURNAL_VACUUM = (
    f"sudo journalctl --vacuum-size={DEFAULT_JOURNAL_SIZE}",
    f"sudo journalctl --vacuum-time={DEFAULT_JOURNAL_TIME}",
)


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
    bench_total: int | None  # None when any instance's bench logs went unmeasured


def to_dict(report: Any) -> dict[str, Any]:
    return asdict(report)


# ------------------------------------------------------------ docker container logs


def _log_config(container) -> tuple[str, dict[str, str]]:
    cfg = (container.attrs.get("HostConfig") or {}).get("LogConfig") or {}
    return str(cfg.get("Type") or ""), dict(cfg.get("Config") or {})


def _log_path(container) -> str | None:
    path = container.attrs.get("LogPath")
    return str(path) if path else None


@contextmanager
def _docker_errors(what: str) -> Iterator[None]:
    """A docker-py failure (a container that stopped or vanished mid-read, a daemon
    that went away) as a typed ``DOCKER`` error, so a caller degrades instead of crashing."""
    try:
        yield
    except docker.errors.DockerException as e:
        raise CwcliError(ErrorKind.DOCKER, "logs.docker_failed", f"{what} failed: {e}") from None


def _docker_client():
    with _docker_errors("Connecting to Docker"):
        return docker.from_env()


def _helper_image(client, containers) -> list[str]:
    """Images a root helper may run: a local ``frappe/bench`` first (the ``core.rm``
    choice), else the instance's own images - every one is already present."""
    with _docker_errors("Listing the helper images"):
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


def _project_containers(project: str) -> list:
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
    return containers


def _json_paths(containers) -> list[str]:
    paths = [_log_path(c) for c in containers if _log_config(c)[0] == "json-file"]
    return [p for p in paths if p is not None]


def _measure(
    containers, paths: list[str]
) -> tuple[dict[str, tuple[int, int]], str | None, str | None]:
    """``(sizes, method, note)`` for ``paths``: from the host when the data root is
    readable, else through ONE read-only root helper mounting every log directory."""
    if not paths:
        return {}, None, None
    host = _host_sizes(paths)
    if host is not None:
        return host, "host", None
    try:
        return _helper_sizes(_docker_client(), containers, paths), "helper", None
    except CwcliError as e:
        return {}, None, f"Sizes could not be measured: {e.message}"


def docker_logs(project: str) -> DockerLogs:
    """Every container's log driver, rotation and size; measured from the host when the
    data root is readable, else through one read-only root helper."""
    containers = _project_containers(project)
    return _docker_logs_of(project, containers, *_measure(containers, _json_paths(containers)))


def _docker_logs_of(
    project: str,
    containers,
    sizes: dict[str, tuple[int, int]],
    method: str | None,
    note: str | None,
) -> DockerLogs:
    if not _json_paths(containers):
        method = note = None
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
    client = _docker_client()
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
    """``[(path, bytes)]`` for every regular file under ``<bench>/logs`` and each
    ``<bench>/sites/<site>/logs`` (one exec; the bench path rides as an argv positional,
    never inside the script text). A bench logs dir that is missing or unreadable
    raises rather than reading as an empty one."""
    with _docker_errors(f"Reading the bench logs at {bench_path}"):
        code, output = container.exec_run(
            [
                "sh",
                "-c",
                '[ -d "$1/logs" ] || exit 3; '
                'for d in "$1/logs" "$1"/sites/*/logs; do [ -d "$d" ] || continue; '
                'find "$d" -maxdepth 1 -type f -exec stat -c "%s %n" {} + 2>/dev/null '
                "|| exit 4; done",
                "sh",
                bench_path,
            ]
        )
    if code not in (0, None):
        raise CwcliError(
            ErrorKind.NOT_FOUND,
            "logs.bench_logs_unreadable",
            f"The bench logs at {bench_path}/logs could not be read.",
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
        try:
            core_inspect.inspect(project, refresh="auto", offer_choice=False)
        except CwcliError:
            raise
        except Exception:  # noqa: BLE001 - core.inspect.resolve_bench_with_fallback's degrade
            pass
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
    Current files are never touched, so a running bench keeps every open log. Without
    a selector, a cached bench whose logs cannot be read is skipped with a warning."""
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
    reports: list[BenchLogs] = []
    warnings: list[Message] = []
    for path in paths:
        try:
            reports.append(bench_logs(frappe, project, path))
        except CwcliError as e:
            if bench is not None:
                raise
            warnings.append(
                Message(
                    "logs.bench_skipped",
                    f"{e.message} Skipped; refresh the cache with "
                    f"`cwcli inspect {project} --update` or pass --bench.",
                )
            )
    paths = [r.bench_path for r in reports]
    doomed = [f for r in reports for f in r.rotated_files]
    freed = sum(r.rotated_bytes for r in reports)
    if not doomed:
        return Result(
            status=Status.OK,
            data={"project": project, "removed": [], "freed_bytes": 0, "benches": paths},
            warnings=[
                *warnings,
                Message("logs.nothing_rotated", "No rotated bench log to remove."),
            ],
        )
    if not consent:
        return Result(
            status=Status.NEEDS_CHOICE,
            warnings=warnings,
            choice=Choice(
                kind="confirm_prune_logs",
                param="consent",
                prompt=(
                    f"Remove {len(doomed)} rotated log file{'s' if len(doomed) != 1 else ''} "
                    f"({freed} bytes) from {project}? Every current log file is kept."
                ),
            ),
        )
    with _docker_errors("Removing the rotated log files"):
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
        warnings=warnings,
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
        partial = proc.returncode != 0 or "not seeing messages" in (proc.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as e:
        text, partial = f"journalctl --disk-usage failed: {e}", True
    privileged = _is_root()
    reason = (
        None
        if privileged
        else "cwcli does not run as root here, so it cannot vacuum the journal; "
        "run one of the commands in a terminal."
    )
    if partial:
        reason = (
            "journalctl could not read every journal file from this account, so the "
            "full size is unknown; run it as root or from the systemd-journal group."
        )
    return JournalReport(
        applies=True,
        reason=reason,
        usage_text=text,
        size=None if partial else _parse_usage(text),
        privileged=privileged,
        commands=list(_JOURNAL_VACUUM),
    )


def _is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def vacuum_journal(
    *,
    max_size: str = DEFAULT_JOURNAL_SIZE,
    max_time: str = DEFAULT_JOURNAL_TIME,
    consent: bool = False,
) -> Result[dict[str, Any]]:
    """Trim the host journal to ``max_size`` and ``max_time`` (``journalctl
    --vacuum-size``/``--vacuum-time``; archived files only, the active one is kept).
    Root only: anywhere else it refuses and names the command to copy."""
    if not _JOURNAL_SIZE_RE.match(max_size):
        raise CwcliError(
            ErrorKind.USAGE,
            "journal.max_size_invalid",
            f"max-size {max_size!r} is not a size like 500M, 200K or 2G.",
        )
    if not _JOURNAL_TIME_RE.match(max_time):
        raise CwcliError(
            ErrorKind.USAGE,
            "journal.max_time_invalid",
            f"max-time {max_time!r} is not an age like 2weeks, 14d or 1month.",
        )
    before = journal()
    if not before.applies:
        raise CwcliError(
            ErrorKind.PRECONDITION, "journal.not_applicable", before.reason or "No journal."
        )
    command = ["journalctl", f"--vacuum-size={max_size}", f"--vacuum-time={max_time}"]
    if not before.privileged:
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "journal.not_root",
            "Vacuuming the journal needs root, and cwcli does not run as root here.",
            hint=f"Run `sudo {' '.join(command)}` in a terminal.",
        )
    if not consent:
        return Result(
            status=Status.NEEDS_CHOICE,
            choice=Choice(
                kind="confirm_vacuum_journal",
                param="consent",
                prompt=(
                    f"Trim the host journal to at most {max_size} and {max_time} of history? "
                    "Older archived journal entries are deleted for the whole host."
                ),
            ),
        )
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise CwcliError(
            ErrorKind.PRECONDITION, "journal.vacuum_failed", f"journalctl failed: {e}"
        ) from None
    if proc.returncode != 0:
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "journal.vacuum_failed",
            "journalctl could not vacuum the journal.",
            detail={"output": (proc.stderr or proc.stdout or "").strip()},
        )
    after = journal()
    freed = before.size - after.size if before.size is not None and after.size is not None else None
    return Result(
        status=Status.OK,
        data={
            "max_size": max_size,
            "max_time": max_time,
            "size_before": before.size,
            "size_after": after.size,
            "freed_bytes": freed,
            "output": (proc.stderr or proc.stdout or "").strip(),
        },
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
    found: dict[str, list | str] = {}
    for inst in instances:
        try:
            found[inst.project_name] = _project_containers(inst.project_name)
        except CwcliError as e:
            found[inst.project_name] = e.message
    every = [c for v in found.values() if isinstance(v, list) for c in v]
    measured = _measure(every, _json_paths(every))
    rows: list[InstanceLogStorage] = []
    benches_measured = True
    for inst in instances:
        running = inst.status == "running"
        notes: list[str] = []
        containers = found[inst.project_name]
        dl: DockerLogs | None = None
        if isinstance(containers, list):
            dl = _docker_logs_of(inst.project_name, containers, *measured)
        else:
            notes.append(containers)
        benches: list[BenchLogs] = []
        if running:
            try:
                frappe = core_docker.get_frappe_container(inst.project_name)
                for p, i in _cached_paths(inst.project_name):
                    try:
                        benches.append(bench_logs(frappe, inst.project_name, p, i))
                    except CwcliError as e:
                        benches_measured = False
                        notes.append(e.message)
            except CwcliError as e:
                benches_measured = False
                notes.append(e.message)
        else:
            benches_measured = False
            notes.append("Start the instance to measure its bench logs.")
        rows.append(
            InstanceLogStorage(
                project=inst.project_name,
                running=running,
                docker=dl,
                benches=benches,
                note=" ".join(notes) or None,
            )
        )
    docker_sizes = [r.docker.total if r.docker is not None else None for r in rows]
    docker_total = (
        sum(s for s in docker_sizes if s is not None) if None not in docker_sizes else None
    )
    bench_total = (
        sum(b.current_bytes + b.rotated_bytes for r in rows for b in r.benches)
        if benches_measured
        else None
    )
    return Result(
        status=Status.OK,
        data=LogStorageReport(
            instances=rows, journal=journal(), docker_total=docker_total, bench_total=bench_total
        ),
    )
