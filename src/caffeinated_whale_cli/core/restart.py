"""``core.restart_process`` - restart ONE supervised program, siblings untouched.

The mutation half of the per-process supervisor (openspec
``add-per-process-supervisor``). Under supervisord, restarting one Procfile
program (``supervisorctl restart <program>``) cycles just that program while
every sibling keeps running - the thing honcho's all-or-nothing model made
impossible. It prints, prompts, and exits nothing: it returns ``NEEDS_CHOICE``
for the multi-bench fork and for an unknown/ambiguous ``--process`` label (each
frontend resolves it its own way), raises :class:`~.errors.CwcliError` for hard
failures, and returns ``Result(OK, ProcessRestartOutcome(...))`` on success.

Whole-stack restart (``cwcli restart`` with no ``--process``) is NOT here - it
stays the container stop+start path in ``commands/restart.py`` (which relaunches
supervisord via ``core.start``), preserving today's behavior.
"""

from __future__ import annotations

from dataclasses import dataclass

from docker.errors import APIError, NotFound

from . import resolvers, supervision
from .docker import get_project_containers
from .envelope import Choice, Message, Result, Status
from .errors import DOCKER_UNREACHABLE_HINT, CwcliError, ErrorKind


@dataclass(frozen=True, slots=True, kw_only=True)
class ProcessRestartOutcome:
    """The typed outcome of a single-program restart (serializable, no live obj)."""

    project: str
    bench_path: str
    label: str  # the restarted program's discovery label (what `status` shows)
    old_pid: int | None  # discovered before the restart (None if it was down)
    new_pid: int | None  # from supervisord after the restart
    supervisor_state: str  # supervisord state after: RUNNING/STARTING/BACKOFF/FATAL/...


def restart_process(
    project_name: str,
    label: str,
    *,
    bench: str | None = None,
    bench_path: str | None = None,
) -> Result[ProcessRestartOutcome]:
    """Restart the single supervised program named by ``label``. See module docstring."""
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
            ErrorKind.NOT_FOUND, "project.not_found", f"No such project '{project_name}'."
        )

    frappe_container = next(
        (c for c in containers if c.labels.get("com.docker.compose.service") == "frappe"),
        None,
    )
    if frappe_container is None:
        raise CwcliError(
            ErrorKind.NOT_FOUND,
            "project.no_frappe_service",
            f"No 'frappe' service found for project '{project_name}'.",
        )
    try:
        frappe_container.reload()
    except (APIError, NotFound) as e:
        raise CwcliError(
            ErrorKind.DOCKER,
            "container.reload_failed",
            f"Could not read state of the frappe container for project '{project_name}'.",
            detail={"output": str(e)},
        ) from e
    if frappe_container.status != "running":
        raise CwcliError(
            ErrorKind.NOT_RUNNING,
            "container.not_running",
            f"Frappe container for project '{project_name}' is not running.",
            hint=f"Start it first with 'cwcli start {project_name}'.",
        )

    # 2. Resolve which bench (the SAME selector as start/status).
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

    # 3. The bench must be supervised (started) to restart a program of it.
    snapshot = supervision.discover_stack(frappe_container, resolved_path)
    if not snapshot.supervisor_up:
        raise CwcliError(
            ErrorKind.NOT_RUNNING,
            "supervisor.not_running",
            f"Bench '{resolved_path}' is not started (no supervisord running).",
            hint=f"Start it first with 'cwcli start {project_name}'.",
        )

    # 4. Resolve --process to a live supervisord program; unknown/ambiguous ->
    #    NEEDS_CHOICE listing the valid labels (never a prompt).
    states = supervision.supervisorctl_states(frappe_container, resolved_path)
    programs = list(states.keys())
    program = supervision.program_for_label(programs, label)
    if program is None:
        valid = [supervision._normalize_procfile_key(p) for p in programs]
        return Result(
            status=Status.NEEDS_CHOICE,
            choice=Choice(
                kind="select_process",
                param="process",
                prompt=(
                    f"No process '{label}' in bench '{resolved_path}'. "
                    "Select one of its programs."
                ),
                options=[{"value": v, "label": v} for v in valid],
            ),
            warnings=warnings,
        )

    resolved_label = supervision._normalize_procfile_key(program)
    old_pid = next((p.pid for p in snapshot.processes if p.label == resolved_label), None)

    # 5. Restart just this program; siblings keep running.
    supervision.restart_program(frappe_container, resolved_path, program)

    after = supervision.supervisorctl_states(frappe_container, resolved_path)
    new_state, new_pid = after.get(program, ("UNKNOWN", None))

    return Result(
        status=Status.OK,
        data=ProcessRestartOutcome(
            project=project_name,
            bench_path=resolved_path,
            label=resolved_label,
            old_pid=old_pid,
            new_pid=new_pid,
            supervisor_state=new_state,
        ),
        warnings=warnings,
    )
