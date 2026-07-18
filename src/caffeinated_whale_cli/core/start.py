"""``core.start`` - start a project's containers + bench, idempotently, on the core.

Owns what ``commands/start.py:_start_project`` used to do: start the project's
stopped containers, resolve which bench to run, and launch **supervisord** over
the bench's dev Procfile (openspec ``add-per-process-supervisor``) while writing
the supervisor marker. It prints, prompts, and exits nothing: it returns
``NEEDS_CHOICE`` for the multi-bench fork, raises :class:`~.errors.CwcliError` for
hard failures (including a failed ``pip install supervisor``), and returns
``Result(OK, StartOutcome(...))`` on success.

Idempotent (openspec ``migrate-start-status-core`` D4): an already-running bench
is detected by its DISCOVERED supervisord supervisor PID keyed to the resolved
bench path. When the bench is already running, ``core.start`` no-ops and returns
``already_running=True`` (leaving the running config, and its autorestart state,
untouched).

Port-conflict resolution stays a CLI-frontend host-side pre-step (D6); this core
assumes the host ports are already clear (a documented precondition, exactly as
the old ``_start_project`` documented). Container start is unconditional here -
``start``'s whole job is to bring things up - so there is no ``confirm_start``
fork (that stays in the OTHER commands that need an already-running container).
"""

from __future__ import annotations

from dataclasses import dataclass

from . import resolvers, supervision
from .docker import align_container_user_to_host, get_project_containers
from .envelope import Message, Result, Status
from .errors import CwcliError, ErrorKind

# Shell metacharacters rejected in the bench path (it is interpolated into the
# launch shell command; command-injection guard, mirrors core.backup).
_INVALID_CHARS = [";", "&", "|", "$", "`", "(", ")", "<", ">", "\n", "\r", "\\"]


@dataclass(frozen=True, slots=True, kw_only=True)
class ProcessLaunch:
    """One launched/observed Procfile process (serializable, no live object)."""

    label: str
    pid: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class StartOutcome:
    """The typed outcome of a start (or an idempotent no-op)."""

    project: str
    container: str
    bench_path: str
    supervisor: str
    log_path: str
    already_running: bool
    processes: list[ProcessLaunch]


def start(
    project_name: str,
    *,
    bench: str | None = None,
    bench_path: str | None = None,
    auto_start: bool = False,
    restart: bool = False,
    autorestart: bool = True,
) -> Result[StartOutcome]:
    """Start a project's containers and its bench. See module docstring.

    ``auto_start`` is accepted for signature symmetry with the family; ``start``
    always brings the containers up regardless (bringing things up is its job).

    ``restart=True`` forces a genuine relaunch: an already-running supervisord for
    the bench is terminated (by discovered PID) and a fresh one is launched,
    instead of the idempotent no-op. The post-restore restart uses it so the app
    reconnects to the restored/migrated DB (``already_running`` is always False).

    ``autorestart`` sets the generated supervisord config's per-program self-heal
    (True -> ``autorestart=unexpected``, the ``--autorestart`` default; False ->
    ``--no-autorestart``, a crashed program stays down until an explicit restart).
    It only takes effect on a genuine (re)launch, not the idempotent no-op.
    """
    warnings: list[Message] = []

    # 1. Resolve the project's containers (raises DOCKER / NOT_FOUND).
    containers = get_project_containers(project_name)
    if containers is None:
        raise CwcliError(
            ErrorKind.DOCKER, "docker.unreachable", "Could not connect to Docker daemon."
        )
    if not containers:
        raise CwcliError(
            ErrorKind.NOT_FOUND, "project.not_found", f"Project '{project_name}' not found."
        )

    # 2. Start every stopped container (unconditional - start's whole job).
    for container in containers:
        if container.status != "running":
            container.start()

    frappe_container = next(
        (c for c in containers if c.labels.get("com.docker.compose.service") == "frappe"),
        None,
    )
    if frappe_container is None:
        raise CwcliError(
            ErrorKind.NOT_FOUND,
            "frappe.not_found",
            f"No 'frappe' service found for project '{project_name}'.",
        )

    # A just-recreated container reverts `frappe` to the image uid (1000), but the
    # workspace on the bind mount is owned by the host uid it was built under. Re-
    # align `frappe` to the host uid (cheap: usermod only, no home chown) so the
    # supervisor writes its per-process logs to the host-owned bench dir instead of
    # failing on a permission mismatch. A no-op when the ids already match.
    _, remap_err = align_container_user_to_host(frappe_container)
    if remap_err:
        warnings.append(Message("start.uid_align_failed", remap_err))

    # 3. Resolve which bench to run (--bench/--path, else single, else default).
    resolved = resolvers.resolve_bench(project_name, bench, bench_path)
    if resolved is None:
        resolved_path = resolvers.DEFAULT_BENCH_PATH
        warnings.append(
            Message(
                "bench.default_used",
                f"No cached bench path found. Using default: {resolvers.DEFAULT_BENCH_PATH}",
            )
        )
    elif resolved.status is Status.NEEDS_CHOICE:
        return Result(status=Status.NEEDS_CHOICE, choice=resolved.choice)
    else:
        assert resolved.data is not None
        resolved_path = resolved.data
        warnings.extend(resolved.warnings)

    if any(char in resolved_path for char in _INVALID_CHARS):
        raise CwcliError(
            ErrorKind.USAGE,
            "bench_path.invalid_chars",
            f"Invalid bench path '{resolved_path}'. Paths cannot contain special shell characters.",
        )

    log_path = supervision.logs_dir(resolved_path)

    # 4. Idempotency: an already-running supervisord for THIS bench is a clean no-op
    #    - UNLESS restart=True, which terminates it first for a genuine relaunch.
    snapshot = supervision.discover_stack(frappe_container, resolved_path)
    if snapshot.supervisor_up:
        if not restart:
            return Result(
                status=Status.OK,
                data=StartOutcome(
                    project=project_name,
                    container=frappe_container.name,
                    bench_path=resolved_path,
                    supervisor=supervision.SUPERVISOR,
                    log_path=log_path,
                    already_running=True,
                    processes=_launched_processes(frappe_container, resolved_path, snapshot),
                ),
                warnings=warnings,
            )
        supervision.stop_supervisor(frappe_container, resolved_path)

    # 5. Launch supervisord over the Procfile, write the marker, discover the set.
    supervision.launch(frappe_container, resolved_path, autorestart=autorestart)
    supervision.write_marker(frappe_container, resolved_path)
    launched = supervision.discover_stack(frappe_container, resolved_path)
    processes = _launched_processes(frappe_container, resolved_path, launched)

    return Result(
        status=Status.OK,
        data=StartOutcome(
            project=project_name,
            container=frappe_container.name,
            bench_path=resolved_path,
            supervisor=supervision.SUPERVISOR,
            log_path=log_path,
            already_running=False,
            processes=processes,
        ),
        warnings=warnings,
    )


def _launched_processes(frappe_container, bench_path, snapshot) -> list[ProcessLaunch]:
    """The launched set: every expected Procfile label, with its PID if already up.

    The launch is detached, so the just-started children may still be coming up;
    reporting the EXPECTED set (from the live Procfile) with whatever PIDs are
    already discovered gives an honest, complete outcome without blocking.
    """
    discovered = {p.label: p.pid for p in snapshot.processes}
    labels = supervision.expected_labels(frappe_container, bench_path) or list(discovered)
    ordered: list[str] = []
    for label in labels + list(discovered):
        if label not in ordered:
            ordered.append(label)
    return [ProcessLaunch(label=label, pid=discovered.get(label)) for label in ordered]
