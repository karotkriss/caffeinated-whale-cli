"""``core.run`` - resolve what ``cwcli run`` will exec, without execing it.

**Two-phase because generators are LAZY, not because of plan/apply.** This is the
distinction to keep: a generator function's body does not execute until first
iteration, so a core function returning ``Iterator[...]`` cannot raise
``CwcliError`` at call time (the frontend's ``try`` would wrap a call that merely
constructs a generator, and the error would erupt later from inside the rendering
loop, after output may already be on stdout), and cannot return ``NEEDS_CHOICE``
at all - a choice would have to be *yielded* as an output event, which is a
category error, since ``Result.choice`` is where the architecture puts it.

So: :func:`run_plan` resolves and returns an ordinary ``Result[RunPlan]``, then
:func:`~.exec_stream.exec_stream` streams. Plan/apply - which previews a
DESTRUCTIVE action for confirmation - is a different thing that arrives with
``restore``/``rm``. Same two-call shape, unrelated motivation. This batch does
NOT settle the plan/apply question.

:class:`RunPlan` carries a container ID STRING, never a live container: a plan
crosses a ``core.<verb>`` return boundary by construction, which is exactly what
the locked "no live Docker objects leak past the core boundary" rule forbids.
(Accepting a live container as a *parameter* stays fine, as
``resolvers.resolve_container_state`` and ``resolvers.require_bench_dir`` do.)

``run_plan`` resolves the container, its run-state, and the bench - and NOTHING
else. It deliberately does NOT resolve a default site, validate the site name or
bench path, or probe that the bench directory exists, though every one of those
primitives exists and is one import away. ``cwcli run`` does none of them today,
and it passes ``bench_path`` to ``exec_create(workdir=...)`` rather than
interpolating it into a shell string, so there is no injection for metacharacter
validation to prevent. Reaching for those primitives because they are there would
ADD failures ``cwcli run`` does not have - a behaviour change wearing a
migration's clothes.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterator
from dataclasses import dataclass

from . import docker as core_docker
from . import resolvers
from .envelope import Message, Result, Status
from .exec_stream import ExecEvent, exec_stream


@dataclass(frozen=True, slots=True, kw_only=True)
class RunPlan:
    """What to exec, and where. Serializable: no live Docker object."""

    project: str
    container_id: str  # a Docker ID string, NOT a Container
    bench_path: str
    command: str  # the assembled "bench <args>" string


def run_plan(
    project_name: str,
    args: list[str],
    *,
    bench: str | None = None,
    bench_path: str | None = None,
    auto_start: bool = False,
) -> Result[RunPlan]:
    """Resolve the container and bench for ``bench <args>``. See the module docstring."""
    warnings: list[Message] = []

    # 1. Resolve the frappe container (raises NOT_FOUND / DOCKER).
    frappe_container = core_docker.get_frappe_container(project_name)

    # 2. Container must be running; a stopped container is a confirm_start fork.
    state = resolvers.resolve_container_state(
        project_name, frappe_container, auto_start=auto_start, offer_choice=True
    )
    if state.status is Status.NEEDS_CHOICE:
        return Result(status=Status.NEEDS_CHOICE, choice=state.choice)

    # 3. Resolve which bench to run against (--bench/--path, else single, else
    # the historical default when nothing is cached).
    bench_result = resolvers.resolve_bench(project_name, bench, bench_path)
    if bench_result is None:
        bench_path = resolvers.DEFAULT_BENCH_PATH
        warnings.append(
            Message(
                "bench.default_used",
                f"No cached bench path found. Using default: {resolvers.DEFAULT_BENCH_PATH}",
            )
        )
    elif bench_result.status is Status.NEEDS_CHOICE:
        return Result(status=Status.NEEDS_CHOICE, choice=bench_result.choice)
    else:
        bench_path = bench_result.data
        warnings.extend(bench_result.warnings)

    assert bench_path is not None  # narrowed by the branches above

    # 4. Assemble the command. The quoting decision lives here with the rest of
    # the logic rather than in the frontend.
    command = "bench " + " ".join(shlex.quote(arg) for arg in args)

    return Result(
        status=Status.OK,
        data=RunPlan(
            project=project_name,
            container_id=frappe_container.id,
            bench_path=bench_path,
            command=command,
        ),
        warnings=warnings,
    )


def run_stream(plan: RunPlan) -> Iterator[ExecEvent]:
    """Phase 2: exec the plan, yielding its output then its exit code.

    The other side of the seam. ``plan.container_id`` is a string because a plan
    crosses a return boundary; this turns it back into a handle so the frontend
    never touches a container and the exec lands on the container that was
    PLANNED, not on whatever a re-resolution by project name would turn up.
    """
    container = core_docker.get_container(plan.container_id)
    yield from exec_stream(container, plan.command, workdir=plan.bench_path)
