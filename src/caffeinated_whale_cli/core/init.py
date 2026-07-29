"""``core.init`` - instance and bench provisioning behind typed envelopes.

TWO sequential plain functions, and the seam between them is this batch's
first-class structural finding (design Decision 1 of ``migrate-init-core``):
:func:`init_instance` produces an INSTANCE (project dir, compose file,
containers up and ready) and :func:`init_bench` produces a provisioned
BENCH + SITE inside it. The seam sits exactly where init's one mid-flow user
decision lives - the existing-bench question needs a running container (stage
1's own product) and fires on the COMMON interactive path (the devcontainer
image ships ``/workspace/frappe-bench``) - so resolving that ``NEEDS_CHOICE``
re-invokes ONLY stage 2, whose pre-decision work is a container resolve plus
two subsecond probes, instead of re-running host setup and instance readiness.
Neither prior two-call motivation applies (no generator laziness, nothing
destructive to preview); this does NOT reopen plan/apply.

Both are plain functions with the optional typed-event ``on_event`` callback
(the ``core.update`` shape); neither is a generator, for ``update``'s settled
maintenance-mode/GC reasons. Progress rides the event family below; the
terminal value is the returned envelope.

``cwcli axi init`` is a thin non-interactive frontend over the same two core
functions. It maps unresolved choices to typed errors and keeps the terminal
result as one TOON document.

Secrets (design Decision 3): ``bench new-site``'s two passwords ride the exec
``environment=`` and are referenced in the command string ONLY as unexpanded
``$CWCLI_*``; no event, DTO field, warning, or command-echo trace carries a
value the old path did not print.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..utils import config_utils, db_utils
from ..utils.port_utils import check_ports_in_use, format_port_list
from . import docker as core_docker
from . import resolvers
from .envelope import Choice, Message, Result, Status
from .errors import CwcliError, ErrorKind
from .exec_stream import ExecChunk, ExecDone, exec_stream

DEFAULT_FRAPPE_BRANCH = "version-16"

# Official SemVer 2.0.0 grammar (https://semver.org): MAJOR.MINOR.PATCH with an
# optional pre-release and build metadata. A full match resolves to a git tag;
# a bare integer major resolves to a version-N branch instead.
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)

_COMPOSE_URL = (
    "https://raw.githubusercontent.com/frappe/frappe_docker/refs/heads/main/"
    "devcontainer-example/docker-compose.yml"
)

# Version gating tables: Python/Node requirements per Frappe major. version-16
# uses the container-default Python/Node (no entry).
_BRANCH_PYTHON = {15: "3.12", 14: "3.10", 13: "3.9"}
_BRANCH_NODE = {14: "16", 13: "14"}


# ------------------------------------------------------------------ DTOs and events


@dataclass(frozen=True, slots=True, kw_only=True)
class InstanceUp:
    """Stage 1's outcome: the instance's containers are up and ready."""

    project: str
    conf_dir: str  # where the customized docker-compose.yml lives


@dataclass(frozen=True, slots=True, kw_only=True)
class InitReport:
    """Stage 2's outcome. Deliberately NO password field: the frontend generated
    or received the admin password, so it needs nothing back (Decision 3)."""

    project: str
    bench_name: str
    bench_path: str
    site_name: str
    bench_created: bool  # False when an existing bench was reused
    site_created: bool  # False on an idempotent re-run (site already existed)
    erpnext_installed: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class InitStepStart:
    """A provisioning step began. ``message`` is the step's canonical label."""

    phase: str
    item: str | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class InitOutput:
    """A chunk of a command's output, tagged with the stream it came from."""

    phase: str
    stream: str  # "stdout" | "stderr"
    text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class InitStepEnd:
    """A provisioning step finished."""

    phase: str
    status: str = "ok"


@dataclass(frozen=True, slots=True, kw_only=True)
class InitNotice:
    """Something the user should see in BOTH rendering modes, keyed by ``code``
    so the frontend picks its historical styling (and stream) per notice."""

    code: str
    text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class InitTrace:
    """A verbose-only diagnostic line. ``code="exec.command"`` marks the
    command-echo trace, whose text carries the ``$CWCLI_*`` references and
    never a secret value (Decision 3, pinned by test)."""

    text: str
    code: str = "trace"


InitEvent = InitStepStart | InitOutput | InitStepEnd | InitNotice | InitTrace

OnEvent = Callable[[InitEvent], None]


def _noop(_event: InitEvent) -> None:
    """The drain-and-discard consumption mode."""


# ------------------------------------------------------------------ validators (public)
#
# Public so the frontend can call them up front and preserve today's fail-fast
# ordering byte-for-byte (Decision 7); init_instance/init_bench call them again
# on their own params - a core function must not trust its caller's discipline.
# Deliberately distinct from resolvers.validate_site_name, which is the
# bench-op shell-metacharacter guard with different rules and purpose.


def _validate_slug(value: str, field_label: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise CwcliError(ErrorKind.USAGE, "name.required", f"{field_label} is required.")

    allowed = "abcdefghijklmnopqrstuvwxyz0123456789-_"
    if not all(char in allowed for char in cleaned.lower()):
        raise CwcliError(
            ErrorKind.USAGE,
            "name.invalid_chars",
            f"{field_label} must contain only lowercase letters, "
            "numbers, dashes, or underscores.",
        )

    if cleaned[0] in "-_" or cleaned[-1] in "-_":
        raise CwcliError(
            ErrorKind.USAGE,
            "name.bad_edges",
            f"{field_label} cannot start or end with '-' or '_'.",
        )

    return cleaned.lower()


def validate_project_slug(value: str) -> str:
    """Validate and normalize a project name, raising ``USAGE`` on today's rules."""
    return _validate_slug(value, "Project name")


def validate_bench_slug(value: str) -> str:
    """Validate and normalize a bench name, raising ``USAGE`` on today's rules."""
    return _validate_slug(value, "Bench name")


def validate_new_site_name(value: str) -> str:
    """Validate and normalize a new site's name (init's naming policy)."""
    cleaned = value.strip().lower()
    if not cleaned:
        raise CwcliError(ErrorKind.USAGE, "site.required", "Site name is required.")
    if not cleaned.endswith(".localhost"):
        raise CwcliError(ErrorKind.USAGE, "site.suffix", "Site name must end with '.localhost'.")
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789-."
    if not all(char in allowed for char in cleaned):
        raise CwcliError(
            ErrorKind.USAGE,
            "site.invalid_chars",
            "Site name may only include lowercase letters, " "numbers, hyphens, and periods.",
        )
    return cleaned


# ------------------------------------------------------------------ version gating


def resolve_frappe_ref(version: str) -> str:
    """Resolve a ``--version`` value to a git ref for ``bench init``.

    - a bare integer ``N`` -> branch ``version-N`` (e.g. ``16`` -> ``version-16``)
    - a semantic version ``X.Y.Z`` -> tag ``vX.Y.Z`` (e.g. ``16.26.3`` -> ``v16.26.3``)

    The value must satisfy the SemVer 2.0.0 grammar to resolve to a tag; a
    malformed value (e.g. ``16.26`` or ``latest``) raises ``CwcliError(USAGE)``.
    """
    value = version.strip()
    if re.fullmatch(r"\d+", value):
        return f"version-{value}"
    if _SEMVER_RE.match(value):
        return f"v{value}"
    raise CwcliError(
        ErrorKind.USAGE,
        "version.invalid",
        f"Invalid --version value {version!r}: expected a bare major version "
        f"(e.g. 16 -> version-16) or a full semantic version "
        f"(e.g. 16.26.3 -> v16.26.3).",
    )


def _frappe_major_version(ref: str) -> int | None:
    """Extract the major Frappe version from a resolved git ref.

    Handles both the branch form (``version-16`` -> 16) and the tag form
    (``v16.26.3`` -> 16). Returns ``None`` for refs with no leading numeric
    major (e.g. ``develop``), so version gating falls through to the modern
    defaults.
    """
    m = re.match(r"^(?:version-|v)(\d+)", ref)
    return int(m.group(1)) if m else None


def _select_mariadb_flag(frappe_branch: str) -> str:
    """Select the ``bench new-site`` MariaDB flag for the given Frappe ref.

    ``--mariadb-user-host-login-scope`` only exists in bench/Frappe 15+, so
    versions 14 and older must fall back to ``--no-mariadb-socket``. Works for
    both branch refs (``version-14``) and tag refs (``v14.80.0``).
    """
    major = _frappe_major_version(frappe_branch)
    if major is not None and major <= 14:
        return "--no-mariadb-socket"
    return "--mariadb-user-host-login-scope=%"


# ------------------------------------------------------------------ private helpers


def _decode(output) -> str:
    if isinstance(output, (bytes, bytearray)):
        return bytes(output).decode("utf-8", errors="replace")
    return str(output) if output is not None else ""


def _build_cd_command(path: str, command: str) -> str:
    return f"cd {shlex.quote(path)} && {command}"


def _directory_exists(container, path: str) -> bool:
    """Return True if a directory exists inside the container (buffered probe)."""
    exit_code, _ = container.exec_run(["bash", "-lc", f"test -d {shlex.quote(path)}"])
    return bool(exit_code == 0)


def _ensure_directory(container, path: str) -> None:
    """Create a directory inside the container if it does not exist."""
    exit_code, output = container.exec_run(["bash", "-lc", f"mkdir -p {shlex.quote(path)}"])
    if exit_code != 0:
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "init.mkdir_failed",
            f"Failed to create directory '{path}': {_decode(output)}",
        )


def _run_exec(
    container,
    command: str,
    *,
    phase: str,
    emit: OnEvent,
    environment: dict | None = None,
    collect: bool = True,
) -> None:
    """One provisioning exec through the exec-stream contract.

    Emits the command-echo trace (the ``$``-refs, never a secret value), then
    forwards each chunk as an :class:`InitOutput` event. ``collect`` mirrors the
    pre-migration consumption split: True means the exec is drained and its full
    joined output feeds the failure ``detail``; False means a renderer is
    consuming the chunks live so nothing is fully buffered. Either way a bounded
    tail of output is always retained so the ENOSPC disk-full hint fires whether
    or not output is streamed (disk exhaustion is *most* likely during a long
    streaming bench build). A lost stream or unknowable exit code raises the
    contract's typed ``DOCKER`` errors instead of the old ``exit code None``.
    """
    emit(InitTrace(text=command, code="exec.command"))

    chunks: list[str] = []
    tail = ""  # bounded window of recent output for the ENOSPC scan (streaming path)
    done: ExecDone | None = None
    for event in exec_stream(container, ["bash", "-lc", command], environment=environment):
        if isinstance(event, ExecChunk):
            emit(InitOutput(phase=phase, stream=event.stream, text=event.text))
            if collect:
                chunks.append(event.text)
            else:
                # ponytail: last 8KB holds any ENOSPC marker even across chunk
                # boundaries, without unbounded buffering of a streamed build.
                tail = (tail + event.text)[-8192:]
        else:
            done = event
    assert done is not None  # exec_stream always terminates with ExecDone

    if done.exit_code == 0:
        return

    joined = "".join(chunks)
    scanned = joined if collect else tail
    if "ENOSPC" in scanned or "no space left on device" in scanned.lower():
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "init.disk_full",
            "No space left on device inside the container. " "Free up disk space and try again.",
            detail={"output": joined} if joined else None,
        )
    raise CwcliError(
        ErrorKind.PRECONDITION,
        "init.exec_failed",
        f"Command failed with exit code {done.exit_code}: {command}",
        detail={"output": joined} if joined else None,
    )


def _run_host_command(cmd: list[str], *, cwd: str, phase: str, emit: OnEvent) -> None:
    """Run a host command captured, streaming its output as events.

    Always captured (design Decision 6, the one disclosed ``-v`` drift): the
    core must not assume a terminal exists, so compose renders plain progress
    lines instead of TTY progress bars.
    """
    result = subprocess.run(cmd, cwd=cwd, capture_output=True)
    for tag, blob in (("stdout", result.stdout), ("stderr", result.stderr)):
        if blob:
            emit(InitOutput(phase=phase, stream=tag, text=_decode(blob)))
    if result.returncode != 0:
        raise CwcliError(
            ErrorKind.DOCKER,
            "compose.failed",
            f"Host command failed: {' '.join(cmd)}",
            detail={"output": _decode(result.stderr)},
        )


def _get_latest_bench_tag(emit: OnEvent) -> str:
    """Query Docker Hub for the latest semver tag of frappe/bench.

    Returns the most recently updated tag matching ``v<major>.<minor>.<patch>``.
    Fails OPEN to a known-good version when the API call fails (the network flat
    spot Decision 10 reports: the closed ErrorKind set has no network kind, and
    this path never needed one - it never raises).
    """
    fallback = "v5.29.1"
    api_url = (
        "https://hub.docker.com/v2/repositories/frappe/bench/tags/"
        "?page_size=25&ordering=last_updated"
    )

    try:
        req = urllib.request.Request(api_url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        semver_re = re.compile(r"^v\d+\.\d+\.\d+$")
        for result in data.get("results", []):
            tag: str = result.get("name", "")
            if semver_re.match(tag):
                emit(InitTrace(text=f"Resolved latest bench image tag: {tag}"))
                return tag
    except Exception:
        emit(InitTrace(text=f"Could not fetch latest bench tag, using fallback: {fallback}"))

    return fallback


def _download_github_file(url: str, dest_path: Path) -> None:
    """Download a file from a GitHub raw URL, or raise typed on a hard failure
    (``PRECONDITION`` - the closed ErrorKind set has no network kind, per
    design Decision 10)."""
    try:
        urllib.request.urlretrieve(url, dest_path)
    except Exception as e:
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "compose.download_failed",
            f"Failed to download {url}: {e}",
        ) from e


def _wait_for_running(project_name: str, *, attempts: int = 10, delay: float = 0.5) -> bool:
    """Poll for the frappe container to reach 'running' after ``compose up -d``.

    Right after ``compose up -d`` a container briefly reports
    ``created``/``starting``; a single status check therefore mis-reads a normal
    slow start as "not running". A bounded, silent poll over
    ``resolvers.resolve_container_state(auto_start=False, offer_choice=False)``
    with the ``NOT_RUNNING`` raise caught per attempt - it never prompts and
    never starts anything, which is what the old ``prompt=False`` achieved.
    """
    container = core_docker.get_frappe_container(project_name)
    for attempt in range(attempts):
        try:
            state = resolvers.resolve_container_state(
                project_name, container, auto_start=False, offer_choice=False
            )
        except CwcliError as e:
            if e.kind is not ErrorKind.NOT_RUNNING:
                raise
            state = None
        if state is not None and state.data is not None and state.data.running:
            return True
        if attempt < attempts - 1:
            time.sleep(delay)
    return False


# ------------------------------------------------------------------ stage 1: the instance


def _mounted_bench_parent(compose_content: str) -> str | None:
    """Read the frappe workspace mount's container target from a compose file.

    Matches the ``- <..host>:{target}:cached`` bench-workspace mount (source
    starts with ``..``), distinguishing it from ``mariadb-data``. Returns the
    container mount point (e.g. ``/workspace``) for old bind-mount and new
    ``../data`` instances alike, or ``None`` when no such mount is present.
    """
    match = re.search(r"-\s+\.\.[^\s:]*:([^\s:]+):cached", compose_content)
    return match.group(1) if match else None


def _project_containers_running(project_name: str) -> bool:
    """True when this project already has a running container of its own.

    ``get_project_containers`` returns None on a Docker connection error; that is
    NOT "running" - falling through to the port check is the honest answer, and
    the compose calls below surface the real daemon failure.
    """
    containers = core_docker.get_project_containers(project_name)
    if not containers:
        return False
    return any(c.status == "running" for c in containers)


_EXPECTED_COMPOSE_SERVICES = frozenset({"frappe", "mariadb", "redis-cache", "redis-queue"})


def _running_compose_services(project_name: str) -> set[str]:
    """Return this project's running compose service names."""
    containers = core_docker.get_project_containers(project_name)
    if not containers:
        return set()
    return {
        service
        for c in containers
        if c.status == "running"
        and (service := c.labels.get("com.docker.compose.service")) is not None
    }


def init_instance(
    project_name: str,
    *,
    port: int = 8000,
    bench_parent: str = "/workspace",
    auto_start: bool = False,
    stream_output: bool = False,
    on_event: OnEvent | None = None,
) -> Result[InstanceUp]:
    """Create the project's host footprint and bring its containers up.

    Project dir + compose download (skipped when the file is already present),
    port/image customization (Docker Hub fails open to the pinned fallback),
    ``compose pull`` + ``up -d`` via captured subprocess. When this project's
    own frappe container is already running, image pulls are skipped and only
    missing sibling services are started with ``--no-deps``. Then the bounded
    silent readiness poll runs. On poll timeout: ``confirm_start`` choice when
    ``auto_start=False``; typed ``NOT_RUNNING`` when ``auto_start=True`` (the
    structural cap - the caller claimed the start was handled and the
    containers are still down; no core function performs a container start).

    ``stream_output`` is the consumption-mode flag: True means a renderer is
    consuming :class:`InitOutput` events live (compose pull runs without
    ``--quiet``, exactly as today's verbose mode).
    """
    emit = on_event or _noop
    project_name = validate_project_slug(project_name)

    # Port conflicts, ahead of any filesystem work (as today). Skipped on the
    # auto_start=True retry: that call shape is the frontend's stage-1 re-invoke
    # after ensure_containers_running has already started this project's own
    # containers, which bind exactly these ports - a self-conflict, not a real one.
    #
    # Skipped for the SAME reason when this project's own containers are already
    # up. Re-running init against a live instance is the ordinary way to add a
    # bench or a site to it (`cwcli init existing --reuse-bench --site other`),
    # and there the ports the check finds "in use" are held by the very instance
    # being initialized. The old check refused that case outright, telling the
    # caller to pick a different --port for an instance whose ports are frozen in
    # its compose file - advice that could not be followed. The check still runs
    # for a stopped or absent project, where a bound port genuinely belongs to
    # someone else.
    if not auto_start and not _project_containers_running(project_name):
        web_ports = list(range(port, port + 6))
        socketio_ports = list(range(port + 1000, port + 1006))
        port_status = check_ports_in_use(web_ports + socketio_ports)
        ports_in_use = [p for p, in_use in port_status.items() if in_use]
        if ports_in_use:
            raise CwcliError(
                ErrorKind.CONFLICT,
                "ports.in_use",
                f"The following ports are already in use: {format_port_list(ports_in_use)}",
                hint="Use the --port flag to select a different starting port.",
            )

    emit(InitStepStart(phase="project_dir", message="Creating project directory"))
    project_dir = config_utils.PROJECTS_DIR / project_name
    conf_dir = project_dir / "conf"
    conf_dir.mkdir(parents=True, exist_ok=True)
    emit(InitTrace(text=f"Project directory: {project_dir}"))
    emit(InitTrace(text=f"Config directory: {conf_dir}"))

    # The mount point is fixed when the compose file is created. --bench-parent
    # normalized once and reused for both the fresh rewrite and the re-init guard.
    bench_parent_path = bench_parent.rstrip("/") or "/workspace"

    compose_path = conf_dir / "docker-compose.yml"
    new_instance = not compose_path.exists()
    if new_instance:
        _download_github_file(_COMPOSE_URL, compose_path)
    else:
        # Frozen compose (backward-compat boundary): the mount point cannot
        # change. A --bench-parent that disagrees would previously mkdir an
        # ephemeral bench outside the mount; refuse instead of silently doing so.
        mounted_parent = _mounted_bench_parent(compose_path.read_text())
        if mounted_parent is not None and bench_parent_path != mounted_parent:
            raise CwcliError(
                ErrorKind.USAGE,
                "bench_parent.mismatch",
                f"Instance '{project_name}' mounts its workspace at "
                f"'{mounted_parent}', which cannot be changed after creation.",
                hint=f"Re-run without --bench-parent or with --bench-parent {mounted_parent}.",
            )

    # Customize ports and pin the bench image (never :latest).
    emit(
        InitStepStart(
            phase="customize_ports",
            message=(
                f"Customizing ports: {port}-{port+5} (web), " f"{port+1000}-{port+1005} (socketio)"
            ),
        )
    )
    content = compose_path.read_text()
    content = content.replace("8000-8005:8000-8005", f"{port}-{port+5}:8000-8005")
    socketio_start = port + 1000
    content = content.replace(
        "9000-9005:9000-9005", f"{socketio_start}-{socketio_start+5}:9000-9005"
    )
    # The upstream devcontainer template sets `working_dir: /workspace/development`
    # - a path cwcli never creates (its bench lives at /workspace/frappe-bench).
    # /workspace is a bind mount to CWCLI_HOME/projects/<name>/, so on `compose up`
    # the Docker daemon (root) creates that missing working_dir INSIDE the bind
    # mount as root:root, leaving a root-owned path in CWCLI_HOME that a non-root
    # `cwcli rm` / test cleanup cannot remove (it re-broke bare pytest by leaking
    # root-owned dirs into a shared temp home). Point it at the mount root, which
    # always exists, so no root-owned working_dir is ever created on the host.
    content = content.replace("working_dir: /workspace/development", "working_dir: /workspace")
    emit(
        InitStepStart(
            phase="resolve_bench_tag",
            message="Resolving latest bench image tag from Docker Hub",
        )
    )
    bench_tag = _get_latest_bench_tag(emit)
    emit(InitTrace(text=f"Pinning bench image: docker.io/frappe/bench:{bench_tag}"))
    content = content.replace(
        "docker.io/frappe/bench:latest", f"docker.io/frappe/bench:{bench_tag}"
    )
    if new_instance:
        # cwcli owns the bench workspace mount: bind the per-project host data/
        # dir at the resolved --bench-parent so bench data (apps, sites, files,
        # the supervisor's per-process logs, socket, and pid) persists on the
        # host and survives container recreation - the ephemeral-bench fix. The
        # mount covers the whole {bench_parent}/{bench} subtree; working_dir
        # sits at the mount root so it is valid for any --bench-parent and never
        # a root-owned dir outside the mounted data/ subtree.
        (project_dir / "data").mkdir(parents=True, exist_ok=True)
        content = content.replace("- ..:/workspace:cached", f"- ../data:{bench_parent_path}:cached")
        content = content.replace("working_dir: /workspace", f"working_dir: {bench_parent_path}")
    compose_path.write_text(content)

    compose_base = ["docker", "compose", "-p", project_name, "-f", "docker-compose.yml"]

    # Pull and whole-stack up are skipped when this project's OWN frappe container is already
    # running - the same self-conflict carve-out as the port check above,
    # applied to a higher-stakes pair of commands. Re-running init to add a
    # bench or a site to a live instance never needs to refetch or recreate
    # anything: bench provisioning happens over `docker exec` in stage 2. But
    # `pull` unconditionally re-fetches every image, including the compose
    # template's unpinned `redis:alpine` tag and `mariadb`'s own tag - if
    # upstream has pushed a newer image under that tag since this instance was
    # created, `up -d` (with no `--no-deps`/`--force-recreate` guard) silently
    # RECREATES that container to match, and for the frappe service that kills
    # every already-serving bench's supervisord with nothing in the report to
    # say so. Skipping pull+up here makes adding a bench to a running instance
    # structurally unable to disturb it, rather than merely unlikely to.
    running_services = _running_compose_services(project_name)
    if "frappe" in running_services:
        missing_services = sorted(_EXPECTED_COMPOSE_SERVICES - running_services)
        detail = (
            f" Starting missing services without touching frappe: {', '.join(missing_services)}."
            if missing_services
            else ""
        )
        emit(
            InitNotice(
                code="instance.already_running",
                text=(
                    f"Instance '{project_name}' is already running; skipping image pull "
                    f"and preserving its frappe container.{detail}"
                ),
            )
        )
        if missing_services:
            emit(
                InitStepStart(
                    phase="up",
                    message=f"Starting missing Docker Compose services: {', '.join(missing_services)}",
                )
            )
            _run_host_command(
                compose_base + ["up", "-d", "--no-deps", *missing_services],
                cwd=str(conf_dir),
                phase="up",
                emit=emit,
            )
    else:
        emit(InitStepStart(phase="pull", message="Pulling Docker images"))
        pull_cmd = compose_base + ["pull"]
        if not stream_output:
            pull_cmd.append("--quiet")
        _run_host_command(pull_cmd, cwd=str(conf_dir), phase="pull", emit=emit)

        emit(InitStepStart(phase="up", message="Starting Docker Compose containers"))
        _run_host_command(compose_base + ["up", "-d"], cwd=str(conf_dir), phase="up", emit=emit)

    emit(InitStepStart(phase="wait_ready", message="Waiting for containers to be ready"))
    if not _wait_for_running(project_name):
        if auto_start:
            # The structural cap: the caller claimed the start was handled and
            # the containers are still down. Fail closed, never loop.
            raise CwcliError(
                ErrorKind.NOT_RUNNING,
                "container.not_running",
                f"Frappe container for project '{project_name}' is not running.",
            )
        return Result(
            status=Status.NEEDS_CHOICE,
            choice=Choice(
                kind="confirm_start",
                param="auto_start",
                prompt=(
                    f"Frappe container for project '{project_name}' is not running. " "Start it?"
                ),
                default="true",
            ),
        )

    return Result(
        status=Status.OK,
        data=InstanceUp(project=project_name, conf_dir=str(conf_dir)),
    )


# ------------------------------------------------------------------ stage 2 helpers


def _get_pyenv_python_version(container, prefix: str, emit: OnEvent) -> str | None:
    """Find a pyenv Python version matching the given prefix inside the container."""
    exit_code, output = container.exec_run(["bash", "-lc", "ls ~/.pyenv/versions"])
    if exit_code != 0:
        emit(InitTrace(text="Could not list pyenv versions"))
        return None

    for version in _decode(output).split():
        if version.startswith(prefix + "."):
            emit(InitTrace(text=f"Found pyenv version: {version}"))
            return version

    emit(InitTrace(text=f"No pyenv version matching {prefix}.x found"))
    return None


def _install_pyenv_python(
    container, prefix: str, emit: OnEvent, warnings: list[Message]
) -> str | None:
    """Install the latest Python matching ``prefix`` via pyenv (soft-fail)."""
    exit_code, output = container.exec_run(["bash", "-lc", "pyenv install --list"])
    if exit_code != 0:
        emit(InitTrace(text="Could not list available pyenv versions"))
        return None

    pattern = re.compile(rf"^\s*({re.escape(prefix)}\.\d+)\s*$", re.MULTILINE)
    matches: list[str] = pattern.findall(_decode(output))
    if not matches:
        emit(InitTrace(text=f"No available pyenv version matching {prefix}.x"))
        return None

    # Last match is the latest patch version.
    target = matches[-1]
    emit(InitTrace(text=f"Installing Python {target} via pyenv..."))

    exit_code, install_output = container.exec_run(
        ["bash", "-lc", f"pyenv install {target}"],
        environment={"PYTHON_CONFIGURE_OPTS": "--enable-shared"},
    )
    if exit_code != 0:
        text = f"Failed to install Python {target} via pyenv"
        emit(InitNotice(code="python.install_failed", text=text))
        warnings.append(Message("python.install_failed", text))
        message = _decode(install_output)
        if message:
            emit(InitTrace(text=message))
        return None

    emit(InitTrace(text=f"Successfully installed Python {target}"))
    return target


def _get_nvm_node_version(container, major: str, emit: OnEvent) -> str | None:
    """Find an installed nvm Node.js version matching the given major."""
    exit_code, output = container.exec_run(["bash", "-lc", "ls ~/.nvm/versions/node/"])
    if exit_code != 0:
        emit(InitTrace(text="Could not list nvm Node.js versions"))
        return None

    for version in _decode(output).split():
        # Entries look like v16.20.2, v22.22.0
        if version.startswith(f"v{major}."):
            emit(InitTrace(text=f"Found Node.js version: {version}"))
            return version

    emit(InitTrace(text=f"No Node.js version matching v{major}.x found"))
    return None


def _install_nvm_node(container, major: str, emit: OnEvent, warnings: list[Message]) -> str | None:
    """Install Node.js for the given major version via nvm (soft-fail)."""
    emit(InitTrace(text=f"Installing Node.js {major} via nvm..."))

    exit_code, output = container.exec_run(
        ["bash", "-lc", f"source ~/.nvm/nvm.sh && nvm install {major}"]
    )
    if exit_code != 0:
        text = f"Failed to install Node.js {major} via nvm"
        emit(InitNotice(code="node.install_failed", text=text))
        warnings.append(Message("node.install_failed", text))
        message = _decode(output)
        if message:
            emit(InitTrace(text=message))
        return None

    installed = _get_nvm_node_version(container, major, _noop)
    if installed:
        emit(InitTrace(text=f"Successfully installed Node.js {installed}"))
    return installed


# ------------------------------------------------------------------ stage 2: the bench


def init_bench(
    project_name: str,
    *,
    bench_name: str,
    site_name: str,
    bench_parent: str = "/workspace",
    frappe_ref: str = DEFAULT_FRAPPE_BRANCH,
    db_root_password: str = "123",
    admin_password: str,
    reuse_bench: bool | None = None,
    install_erpnext: bool = False,
    erpnext_branch: str,
    auto_start: bool = False,
    stream_output: bool = False,
    on_event: OnEvent | None = None,
) -> Result[InitReport]:
    """Provision a bench and site inside a running instance.

    Bench resolve + the tri-state ``reuse_bench`` (True reuse / False
    ``CONFLICT`` / None-and-exists the NEW ``confirm_reuse_bench`` choice),
    search-path registration, version gating, ``bench init``, the bench
    configs, ``bench new-site`` (secrets via ``exec_stream(environment=)``),
    the optional ERPNext pair, and the cache clear.

    ``db_root_password`` keeps its ``"123"`` default HERE because the coupling
    it mirrors - the downloaded compose's hardcoded ``MYSQL_ROOT_PASSWORD:
    123`` - lives in the compose file the core itself downloads; it is
    shielded off the argv/echo, not randomized.
    """
    emit = on_event or _noop
    warnings: list[Message] = []

    project_name = validate_project_slug(project_name)
    bench_name = validate_bench_slug(bench_name)
    site_name = validate_new_site_name(site_name)

    frappe_container = core_docker.get_frappe_container(project_name)

    # The race backstop (run_plan's exact shape): a container stopped between
    # stage 1 and here is a confirm_start fork, not an exec failure.
    state = resolvers.resolve_container_state(
        project_name, frappe_container, auto_start=auto_start, offer_choice=True
    )
    if state.status is Status.NEEDS_CHOICE:
        return Result(status=Status.NEEDS_CHOICE, choice=state.choice)

    # Align the container's `frappe` user (uid 1000 by image default) with the
    # host user BEFORE any bench command writes the bind-mounted workspace, so the
    # files it creates are owned by the host user and `cwcli rm` can remove them on
    # any host uid (a CI runner is 1001; a dev box is often 1000). `chown_home` is
    # paid here, once, so this first provision's pyenv/nvm/pip installs can write
    # the (now host-owned) home. A no-op when the ids already match - but the
    # caller can't know that in advance, and when it is NOT a no-op the recursive
    # `chown -R` over /home/frappe is a single blocking exec with no progress
    # output of its own, minutes long on a slow disk. Announcing the phase BEFORE
    # running it (not just reporting on completion) is what keeps this window
    # from reading as a hang: the reported defect was this exact step reporting
    # only its own completion, with nothing printed while it ran.
    emit(
        InitStepStart(
            phase="align_uid",
            message="Aligning container user to host uid/gid (first run can take several minutes)",
        )
    )
    remapped, remap_err = core_docker.align_container_user_to_host(
        frappe_container, chown_home=True
    )
    emit(InitStepEnd(phase="align_uid"))
    if remap_err:
        emit(InitNotice(code="init.uid_align_failed", text=remap_err))
        warnings.append(Message("init.uid_align_failed", remap_err))
    elif remapped:
        emit(InitTrace(text="Aligned the container 'frappe' user to the host uid/gid."))

    bench_parent_path = bench_parent.rstrip("/") or "/workspace"
    _ensure_directory(frappe_container, bench_parent_path)

    bench_full_path = f"{bench_parent_path}/{bench_name}"
    bench_exists = _directory_exists(frappe_container, bench_full_path)

    if bench_exists:
        if reuse_bench is False:
            raise CwcliError(
                ErrorKind.CONFLICT,
                "bench.exists",
                f"Bench '{bench_full_path}' already exists and --no-reuse-bench was "
                "given. Pass a different --bench name to create a new bench.",
            )
        if reuse_bench is None:
            # The mid-flow decision the two-call seam exists for: the frontend
            # resolves it (prompt, or the non-TTY refusal) and re-invokes.
            return Result(
                status=Status.NEEDS_CHOICE,
                choice=Choice(
                    kind="confirm_reuse_bench",
                    param="reuse_bench",
                    prompt=(
                        f"Reuse the existing bench '{bench_name}' and continue " "with site setup?"
                    ),
                    options=[{"value": bench_name, "label": bench_full_path}],
                    default="true",
                ),
            )
        # reuse_bench is True: reuse it (bench init is skipped below).

    # Register the bench in the custom search paths, on every proceeding
    # outcome, as the old add_path command-call did - now via the util the
    # command was a wrapper around (the last frontend-calling-frontend edge).
    added = config_utils.add_custom_path(bench_full_path)
    emit(
        InitNotice(
            code="search_path.added" if added else "search_path.exists",
            text=bench_full_path,
        )
    )

    frappe_major = _frappe_major_version(frappe_ref)

    bench_created = False
    if not bench_exists:
        # Version gating: Python and Node.js requirements per Frappe major.
        env_prefix = ""
        nvm_prefix = ""

        python_prefix = _BRANCH_PYTHON.get(frappe_major) if frappe_major is not None else None
        if python_prefix:
            py_version = _get_pyenv_python_version(frappe_container, python_prefix, emit)
            if not py_version:
                emit(
                    InitNotice(
                        code="python.installing",
                        text=f"Python {python_prefix} not found, installing via pyenv...",
                    )
                )
                # Compiling CPython from source is minutes long with no output of
                # its own; announce the phase before running it, same reasoning
                # as the uid-alignment step above.
                emit(
                    InitStepStart(
                        phase="python_install",
                        message=(
                            f"Installing Python {python_prefix} via pyenv "
                            "(can take several minutes)"
                        ),
                    )
                )
                py_version = _install_pyenv_python(frappe_container, python_prefix, emit, warnings)
                emit(InitStepEnd(phase="python_install"))
            if py_version:
                env_prefix = f"PYENV_VERSION={py_version} "
                emit(InitTrace(text=f"Using PYENV_VERSION={py_version} for {frappe_ref}"))

        node_major = _BRANCH_NODE.get(frappe_major) if frappe_major is not None else None
        if node_major:
            node_version = _get_nvm_node_version(frappe_container, node_major, emit)
            if not node_version:
                emit(
                    InitNotice(
                        code="node.installing",
                        text=f"Node.js {node_major} not found, installing via nvm...",
                    )
                )
                emit(
                    InitStepStart(
                        phase="node_install",
                        message=f"Installing Node.js {node_major} via nvm (can take a few minutes)",
                    )
                )
                node_version = _install_nvm_node(frappe_container, node_major, emit, warnings)
                emit(InitStepEnd(phase="node_install"))
            if node_version:
                nvm_prefix = f"source ~/.nvm/nvm.sh && nvm use {node_version} && "
                emit(InitTrace(text=f"Using Node.js {node_version} for {frappe_ref}"))
                emit(InitTrace(text=f"Installing yarn globally for Node.js {node_version}..."))
                yarn_exit_code, _ = frappe_container.exec_run(
                    [
                        "bash",
                        "-lc",
                        f"source ~/.nvm/nvm.sh && nvm use {node_version} && " "npm install -g yarn",
                    ]
                )
                if yarn_exit_code != 0:
                    text = "Failed to install yarn globally."
                    emit(InitNotice(code="yarn.install_failed", text=text))
                    warnings.append(Message("yarn.install_failed", text))
                else:
                    emit(InitTrace(text="yarn installed successfully."))

        bench_init_cmd = _build_cd_command(
            bench_parent_path,
            nvm_prefix
            + env_prefix
            + " ".join(
                [
                    "bench",
                    "init",
                    "--skip-redis-config-generation",
                    "--frappe-branch",
                    shlex.quote(frappe_ref),
                    shlex.quote(bench_name),
                    "--verbose",
                ]
            ),
        )

        emit(InitStepStart(phase="bench_init", item=bench_name))
        _run_exec(
            frappe_container,
            bench_init_cmd,
            phase="bench_init",
            emit=emit,
            collect=not stream_output,
        )
        emit(InitStepEnd(phase="bench_init"))
        bench_created = True

    # Pin setuptools<82 for version-13 to retain pkg_resources.
    if frappe_major == 13:
        emit(InitTrace(text="Pinning setuptools<82 for version-13..."))
        pin_cmd = _build_cd_command(bench_full_path, "./env/bin/pip install 'setuptools<82'")
        pin_exit_code, pin_output = frappe_container.exec_run(["bash", "-lc", pin_cmd])
        if pin_exit_code != 0:
            text = "Failed to pin setuptools<82."
            emit(InitNotice(code="setuptools.pin_failed", text=text))
            warnings.append(Message("setuptools.pin_failed", text))
            message = _decode(pin_output)
            if message:
                emit(InitTrace(text=message))
        else:
            emit(InitTrace(text="setuptools pinned successfully."))

    # Configure bench hosts inside the container (db and redis services).
    emit(
        InitStepStart(
            phase="configure_bench",
            item=bench_name,
            message="Configuring bench database and Redis connections",
        )
    )
    for command in (
        "bench set-config -g db_host mariadb",
        "bench set-config -g redis_cache redis://redis-cache:6379",
        "bench set-config -g redis_queue redis://redis-queue:6379",
        "bench set-config -g redis_socketio redis://redis-queue:6379",
    ):
        _run_exec(
            frappe_container,
            _build_cd_command(bench_full_path, command),
            phase="configure_bench",
            emit=emit,
        )

    site_path = f"{bench_full_path}/sites/{site_name}"
    site_exists = _directory_exists(frappe_container, site_path)
    emit(InitStepEnd(phase="configure_bench"))

    site_created = False
    if not site_exists:
        mariadb_flag = _select_mariadb_flag(frappe_ref)
        # Both passwords ride in the environment and are referenced as unexpanded
        # $VARs, so they stay out of the command-echo trace and off the bash -lc
        # wrapper argv - the traced command is identical to the executed one.
        # Mirrors restore.py's M5 pattern. (The leaf process still receives the
        # expanded value on its own argv, an unavoidable consequence of bench's
        # flag-only interface - see the cwcli-lifecycle skill.)
        new_site_env = {
            "CWCLI_DB_ROOT_PASSWORD": db_root_password,
            "CWCLI_ADMIN_PASSWORD": admin_password,
        }
        new_site_cmd = _build_cd_command(
            bench_full_path,
            " ".join(
                [
                    "bench",
                    "new-site",
                    "--db-root-password",
                    '"$CWCLI_DB_ROOT_PASSWORD"',
                    "--admin-password",
                    '"$CWCLI_ADMIN_PASSWORD"',
                    mariadb_flag,
                    shlex.quote(site_name),
                    "--verbose",
                ]
            ),
        )

        emit(InitStepStart(phase="new_site", item=site_name))
        _run_exec(
            frappe_container,
            new_site_cmd,
            phase="new_site",
            emit=emit,
            environment=new_site_env,
            collect=not stream_output,
        )
        emit(InitStepEnd(phase="new_site"))
        site_created = True

    # Final configuration: enable developer mode and server script support.
    emit(
        InitStepStart(
            phase="finalize",
            item=site_name,
            message="Enabling developer mode and server scripts",
        )
    )
    for command in (
        f"bench --site {shlex.quote(site_name)} set-config developer_mode 1",
        "bench set-config -g server_script_enabled 1",
    ):
        _run_exec(
            frappe_container,
            _build_cd_command(bench_full_path, command),
            phase="finalize",
            emit=emit,
        )
    emit(InitStepEnd(phase="finalize"))

    erpnext_installed = False
    if install_erpnext:
        emit(
            InitStepStart(
                phase="erpnext_get",
                item=erpnext_branch,
                message=f"Fetching ERPNext app (branch: {erpnext_branch})",
            )
        )
        _run_exec(
            frappe_container,
            _build_cd_command(
                bench_full_path,
                f"bench get-app --branch {shlex.quote(erpnext_branch)} " "--resolve-deps erpnext",
            ),
            phase="erpnext_get",
            emit=emit,
            collect=not stream_output,
        )
        emit(InitStepEnd(phase="erpnext_get"))

        emit(
            InitStepStart(
                phase="erpnext_install",
                item=site_name,
                message=f"Installing ERPNext on site '{site_name}'",
            )
        )
        _run_exec(
            frappe_container,
            _build_cd_command(
                bench_full_path,
                f"bench --site {shlex.quote(site_name)} install-app erpnext",
            ),
            phase="erpnext_install",
            emit=emit,
            collect=not stream_output,
        )
        emit(InitStepEnd(phase="erpnext_install"))
        erpnext_installed = True

    # Clear any stale cached data for this project; inspect repopulates it.
    db_utils.clear_cache_for_project(project_name)

    return Result(
        status=Status.WARNING if warnings else Status.OK,
        data=InitReport(
            project=project_name,
            bench_name=bench_name,
            bench_path=bench_full_path,
            site_name=site_name,
            bench_created=bench_created,
            site_created=site_created,
            erpnext_installed=erpnext_installed,
        ),
        warnings=warnings,
    )
