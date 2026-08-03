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
from .errors import DOCKER_UNREACHABLE_HINT, CwcliError, ErrorKind

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
    """The typed outcome of a start (or an idempotent no-op).

    ``web_ready`` is the honest web-serving signal after a genuine launch: True once
    the bench's OWN web port answers, False if it did not within the bounded wait,
    and None when not probed (an idempotent no-op, a bench whose Procfile has no web,
    or a bench whose assigned port could not be read - cwcli never waits on a guess).
    """

    project: str
    container: str
    bench_path: str
    supervisor: str
    log_path: str
    already_running: bool
    processes: list[ProcessLaunch]
    web_ready: bool | None = None


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
            ErrorKind.DOCKER,
            "docker.unreachable",
            "Could not connect to Docker daemon.",
            hint=DOCKER_UNREACHABLE_HINT,
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
    # align `frappe` to the host uid and re-own only its mutable home state so the
    # supervisor writes its per-process logs to the host-owned bench dir and later
    # login-shell execs can refresh pyenv's shims. A no-op when the ids already match.
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

    # 6. Block until the web server actually binds THIS BENCH's port before reporting
    #    running. supervisord reports its programs up a beat before `bench serve`
    #    binds the port, so returning immediately makes the tool's "running" claim
    #    race the web (a scripted `cwcli start && cwcli status` catches a transient
    #    `degraded`). Only wait when the Procfile actually defines a web program;
    #    a timeout degrades to a warning, never a failure (the stack IS launched).
    #
    #    The port is READ from the bench's own config, never assumed: this wait used
    #    to poll a hardcoded :8000, so `cwcli start <p> --bench 1` sat 60 seconds
    #    watching bench 0's port and then warned that a perfectly healthy bench had
    #    not started - then pointed the user at `cwcli status`, which confirmed the
    #    phantom fault by making the same mistake. With no resolvable port the wait
    #    is SKIPPED and `web_ready` stays None (its existing "not probed" value):
    #    spending the timeout on a guess is worse than saying nothing.
    web_ready: bool | None = None
    if "web" in {p.label for p in processes}:
        ports = resolvers.resolve_assigned_ports(
            frappe_container, [resolved_path], fill_defaults=False
        ).get(resolved_path)
        if ports is None:
            warnings.append(
                Message(
                    "start.web_port_unknown",
                    "Dev services launched, but the bench's web port could not be read, "
                    f"so cwcli did not wait for it. Run 'cwcli inspect {project_name}' "
                    "to refresh, then check 'cwcli status'.",
                )
            )
        else:
            web_ready = supervision.wait_web_ready(frappe_container, port=ports[0])
            if not web_ready:
                warnings.append(
                    Message(
                        "start.web_not_ready",
                        "Dev services launched, but the web server did not begin serving "
                        f"on :{ports[0]} in time. Check 'cwcli status' / 'cwcli logs'.",
                    )
                )

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
            web_ready=web_ready,
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
