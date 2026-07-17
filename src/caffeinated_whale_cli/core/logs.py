"""``core.logs_plan`` - resolve WHICH log files ``cwcli logs`` will tail, without tailing.

**The tail stays in the frontend, on purpose, and this is the batch's load-bearing
decision.** ``logs_plan`` returns a declarative :class:`LogsPlan` (a container name,
the resolved log files, and the follow/lines the tail wants); ``commands/logs.py``
performs the ``docker exec -it ... tail`` itself. This is ``core.run``'s
``RunPlan`` seam minus ``run_stream``, and it is the same shape the rework settled
for ``open`` (``LaunchTarget``): the core resolves, the frontend acts.

**Why the tail is NOT re-pointed onto :mod:`core.exec_stream`.** ``logs --follow``
is not an operation *cwcli* streams; it is one ``tail`` streams, docker's TTY
relays, and cwcli merely awaits. The bytes never enter the Python process, so
there are no events to type, and locked decision 4 ("streaming operations return
typed event iterators") does not reach it. Re-pointing it onto ``exec_stream`` was
proposed and measured against real Docker and the real primitive, and it is worse
on every axis that matters:

- Closing the exec socket does not kill the exec'd process, and Docker exposes no
  kill-exec API, so a Ctrl+C leaks an orphan ``tail -F`` in the container per
  invocation - verified accumulating 1, 2, 3, 4, and surviving even a write to the
  log (SIGPIPE does not collect it).
- ``exec_stream._poll_exit_code`` then sees ``Running: True`` (the orphan genuinely
  is) and honestly raises ``exec.stream_lost`` after ~10s on every routine stop.
- Avoiding both needs ``tty=True``, which docker-py makes mutually exclusive with
  the ``demux=True`` stream tag locked as ``exec_stream`` contract point 2 and
  depended on by ``init``'s stderr routing and ``axi``'s stdout purity.

The mechanism it would replace is verified clean on the interactive path:
``docker exec -it`` under a real pty forwards ``^C`` into the container, ``tail``
exits 130, zero orphans. ``exec_stream`` remains the ONE way to exec-and-stream for
every consumer that streams in Python.

``read_logs`` (below) is the bounded ``tail -n N`` (no follow) behind the ``cwcli axi
logs`` verb - the reader deferred WITH that verb. It is NOT on ``exec_stream``: a
bounded read blocks to completion and returns finite output, so it is one buffered
``container.exec_run`` (the ``core.backup`` shape), not an event stream. It shares its
entire resolve with ``logs_plan`` via ``_resolve_log_files`` and returns the lines as
serializable data (``LogsRead``) - no live Docker object crosses its return boundary.
``exec_stream`` would be the primitive for a future STREAMING ``tail -n N`` if one were
ever wanted; a one-shot buffered read needs no event iterator.

``logs_plan`` deliberately resolves NO MORE than ``cwcli logs`` does today: no
default site, no bench-path metacharacter validation, no bench-dir probe, though
each primitive is one import away. Reaching for them would ADD failures the command
does not have (the trap recorded in ``core/run.py:24-32``). This module imports no
``subprocess``: a core module that can spawn a tail can drift back into owning it.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Any

from . import docker as core_docker
from . import resolvers, supervision
from .envelope import Choice, Message, Result, Status
from .errors import CwcliError, ErrorKind


@dataclass(frozen=True, slots=True, kw_only=True)
class LogsPlan:
    """What to tail, and where. Serializable: no live Docker object, and no argv.

    ``container_name`` is a NAME rather than an ID because the tail needs a name and
    nothing here needs a live handle, so unlike ``RunPlan`` there is nothing to
    bridge back. There is deliberately no ``docker exec`` argv: an argv would make
    the core emit ``docker`` CLI command lines while the rest of the core speaks
    docker-py, and would hand a future GUI a mechanism it cannot use (a GUI tails
    into its own widget, not a subprocess).
    """

    project: str
    container_name: str
    bench_path: str
    log_files: list[str]  # resolved, existence-checked, in tail order
    follow: bool
    lines: int
    not_cwcli_supervised: bool  # the fallback fired: these are raw bench logs


@dataclass(frozen=True, slots=True, kw_only=True)
class ProcessLog:
    """One process's tail. Serializable: a plain label, path, and lines - no Docker object."""

    process: str  # raw Procfile program key (TOON-safe), e.g. "web", "worker_default"
    file: str  # absolute path tailed
    lines: list[str]  # the tail, oldest-first, one string per line


@dataclass(frozen=True, slots=True, kw_only=True)
class LogsRead:
    """A bounded read of a bench's logs. Every field serializable: no live Docker object.

    ``read_logs`` returns this INSTEAD of a ``LogsPlan``: where ``LogsPlan`` describes a tail
    for the frontend to perform (``--follow`` is an interactive TTY stream), ``LogsRead``
    carries the already-tailed lines as data, because an ``axi`` agent needs a value it can
    branch on, not a stream it must relay.
    """

    project: str
    container_name: str
    bench_path: str
    lines_requested: int
    not_cwcli_supervised: bool  # the fallback fired: these are raw bench logs
    logs: list[ProcessLog]


@dataclass(frozen=True, slots=True, kw_only=True)
class _ResolvedLogs:
    """The shared resolve's success payload. PRIVATE: it carries a live ``container``, so it
    must never cross a public ``core.<verb>`` return boundary - only ``logs_plan``/``read_logs``
    consume it, and each builds its own serializable DTO from it.
    """

    container: Any  # live Container, internal only (both callers exec against it)
    container_name: str
    bench_path: str
    log_files: list[str]  # existence-checked, tail order; empty means "no logs found"
    not_cwcli_supervised: bool
    manager_up: bool  # only meaningful when log_files is empty (see _raise_no_logs)


def _existing_files(container, files: list[str]) -> list[str]:
    """The subset of ``files`` that exist in the container (one exec), order preserved.

    ``container.exec_run`` (docker-py), not a ``docker exec`` shell-out: the shared
    idiom in ``core/supervision.py``, and what makes this coverable by a container
    fake. ``tail`` errors on a missing path, so a program that has produced no
    output yet must be filtered out here.
    """
    if not files:
        return []
    checks = "".join(f"if [ -f {shlex.quote(f)} ]; then echo {shlex.quote(f)}; fi;" for f in files)
    _exit_code, output = container.exec_run(["sh", "-c", checks])
    return [line for line in supervision._decode(output).splitlines() if line.strip()]


def _discover_bench_log_files(container, bench_path: str) -> list[str]:
    """The real ``*.log`` files a bench writes under ``logs/`` (one exec, sorted).

    The not-cwcli-supervised fallback: a bench running under honcho / ``bench start``
    (a pre-v3 instance, or a plain ``bench start``) writes differently-named log
    files than supervisord's ``<program>.supervisor.log``, so DISCOVER what is
    actually present rather than assume supervisord names. Excludes supervisord's
    own per-process logs (the supervised path stays the sole source for those); the
    ``*.log`` glob already skips cwcli's dotfile state (``.cwcli-*``).
    """
    logs = shlex.quote(f"{bench_path}/logs")
    _exit_code, output = container.exec_run(["sh", "-c", f"ls -1 {logs}/*.log 2>/dev/null"])
    files = [line.strip() for line in supervision._decode(output).splitlines() if line.strip()]
    return sorted(f for f in files if not f.endswith(supervision._PROC_LOG_SUFFIX))


def _program_log_matches(file_path: str, program: str) -> bool:
    """Whether a discovered log file belongs to Procfile ``program`` (honcho naming).

    honcho/``bench start`` log names are not the supervisord ``<program>.supervisor.log``
    form, so ``--process`` filtering in the fallback matches on the file stem:
    ``web`` -> ``web.log``/``web.error.log``, ``worker_default`` -> ``worker.log``,
    ``schedule`` -> ``schedule.log``/``scheduler.log``. Underscores and dashes are
    treated alike (``redis_cache`` -> ``redis-cache.log``).
    """
    stem = file_path.rsplit("/", 1)[-1]
    if stem.endswith(".log"):
        stem = stem[:-4]
    base = "worker" if program.startswith(("worker_", "worker:")) else program

    def _norm(s: str) -> str:
        return s.lower().replace("_", "-")

    return _norm(stem).startswith(_norm(base))


def _resolve_log_files(
    project_name: str,
    *,
    bench: str | None,
    bench_path: str | None,
    process: str | None,
    auto_start: bool,
) -> Result[_ResolvedLogs]:
    """The resolve shared by ``logs_plan`` and ``read_logs``: WHICH log files to read.

    Everything both callers need between their arguments and the tail - the container lookup,
    the run-state fork, bench resolution, ``--process`` selection, the existence probe, and the
    not-cwcli-supervised fallback - lives here ONCE. It returns the resolved files (possibly
    empty) plus ``manager_up``, so each caller decides what an empty result means: for a plan a
    tail of nothing is an error, for a bounded read a running-but-quiet bench is an empty
    success. A PURE READ; launches nothing on any path.
    """
    warnings: list[Message] = []

    # 1. Resolve the frappe container (raises NOT_FOUND / DOCKER).
    frappe_container = core_docker.get_frappe_container(project_name)

    # 2. Container must be running; a stopped container is a confirm_start fork. The
    #    frontend runs its interactive prologue first, so this only fires on a race.
    state = resolvers.resolve_container_state(
        project_name, frappe_container, auto_start=auto_start, offer_choice=True
    )
    if state.status is Status.NEEDS_CHOICE:
        return Result(status=Status.NEEDS_CHOICE, choice=state.choice)

    # 3. Resolve which bench (--bench/--path, else single, else the historical
    #    default when nothing is cached). Same selector as run/restart/status.
    bench_result = resolvers.resolve_bench(project_name, bench, bench_path)
    if bench_result is None:
        resolved_path = resolvers.DEFAULT_BENCH_PATH
        warnings.append(
            Message(
                "bench.default_used",
                f"No cached bench path found. Using default: {resolvers.DEFAULT_BENCH_PATH}",
            )
        )
    elif bench_result.status is Status.NEEDS_CHOICE:
        return Result(status=Status.NEEDS_CHOICE, choice=bench_result.choice, warnings=warnings)
    else:
        assert bench_result.data is not None
        resolved_path = bench_result.data
        warnings.extend(bench_result.warnings)

    # 4. supervisord writes one log file per Procfile program. --process tails just
    #    that one; otherwise every program's file for a combined view. An unknown
    #    --process is a select_process choice, NOT a printed error (mirrors
    #    core.restart_process on the same substrate).
    programs = supervision.procfile_programs(frappe_container, resolved_path)
    program: str | None = None
    if process:
        program = supervision.program_for_label(programs, process)
        if program is None:
            valid = [supervision._normalize_procfile_key(p) for p in programs]
            return Result(
                status=Status.NEEDS_CHOICE,
                choice=Choice(
                    kind="select_process",
                    param="process",
                    prompt=f"No process '{process}' in bench '{resolved_path}'.",
                    options=[{"value": v, "label": v} for v in valid],
                ),
                warnings=warnings,
            )
        candidate_files = [supervision.process_log_path(resolved_path, program)]
    else:
        candidate_files = [supervision.process_log_path(resolved_path, p) for p in programs]

    # 5. Keep only the log files that actually exist (a program that has produced no
    #    output yet has no file). When none exist, the bench may instead be running
    #    under honcho / `bench start`, whose real logs are named differently: ask the
    #    fallback discoverer and use those. Never launches anything - a pure read.
    existing = _existing_files(frappe_container, candidate_files)
    not_cwcli_supervised = False
    manager_up = True  # supervisord files present => a manager is up; only read when empty
    if not existing:
        fallback = supervision.discover_unsupervised_stack(frappe_container, resolved_path)
        manager_up = fallback.manager_up
        if fallback.manager_up:
            real = _discover_bench_log_files(frappe_container, resolved_path)
            if program is not None:
                real = [f for f in real if _program_log_matches(f, program)]
            if real:
                existing = real
                not_cwcli_supervised = True

    return Result(
        status=Status.OK,
        data=_ResolvedLogs(
            container=frappe_container,
            container_name=frappe_container.name,
            bench_path=resolved_path,
            log_files=existing,
            not_cwcli_supervised=not_cwcli_supervised,
            manager_up=manager_up,
        ),
        warnings=warnings,
    )


def logs_plan(
    project_name: str,
    *,
    bench: str | None = None,
    bench_path: str | None = None,
    process: str | None = None,
    follow: bool = False,
    lines: int = 100,
    auto_start: bool = False,
) -> Result[LogsPlan]:
    """Resolve which bench log files ``cwcli logs`` should tail. See the module docstring.

    A PURE READ: it never launches, installs, or restarts supervisord or the bench,
    on any path including the not-cwcli-supervised fallback.
    """
    resolved = _resolve_log_files(
        project_name, bench=bench, bench_path=bench_path, process=process, auto_start=auto_start
    )
    if resolved.status is Status.NEEDS_CHOICE:
        return Result(
            status=Status.NEEDS_CHOICE, choice=resolved.choice, warnings=resolved.warnings
        )
    r = resolved.data
    assert r is not None  # OK always carries a _ResolvedLogs

    # A plan of nothing is an error: there is nothing to tail. Keep the two no-logs outcomes
    # distinct exactly as before (manager up -> a wait; no manager -> may be down).
    if not r.log_files:
        _raise_no_logs(project_name, r.bench_path, process, manager_up=r.manager_up)

    return Result(
        status=Status.OK,
        data=LogsPlan(
            project=project_name,
            container_name=r.container_name,
            bench_path=r.bench_path,
            log_files=r.log_files,
            follow=follow,
            lines=lines,
            not_cwcli_supervised=r.not_cwcli_supervised,
        ),
        warnings=resolved.warnings,
    )


def read_logs(
    project_name: str,
    *,
    bench: str | None = None,
    bench_path: str | None = None,
    process: str | None = None,
    lines: int = 100,
    auto_start: bool = False,
) -> Result[LogsRead]:
    """Bounded ``tail -n N`` (NO follow) of a bench's per-process logs; the reader behind
    ``cwcli axi logs``.

    Where ``logs_plan`` returns a declarative ``LogsPlan`` for the frontend's interactive
    ``docker exec -it ... tail -F`` (see the module docstring for why that follow stays in the
    frontend), ``read_logs`` runs the tail ITSELF as one buffered ``container.exec_run`` (the
    ``core.backup`` shape, NOT ``exec_stream``: a bounded read blocks and returns finite output)
    and returns the lines as serializable data, because an agent needs a value to branch on, not
    a stream to relay. No live Docker object crosses this return boundary.

    A PURE READ: launches nothing on any path, including the not-cwcli-supervised fallback.
    """
    resolved = _resolve_log_files(
        project_name, bench=bench, bench_path=bench_path, process=process, auto_start=auto_start
    )
    if resolved.status is Status.NEEDS_CHOICE:
        return Result(
            status=Status.NEEDS_CHOICE, choice=resolved.choice, warnings=resolved.warnings
        )
    r = resolved.data
    assert r is not None  # OK always carries a _ResolvedLogs
    warnings = list(resolved.warnings)

    if not r.log_files:
        # A running-but-quiet bench (manager up, nothing written yet) is a successful EMPTY
        # read on the agent surface, NOT the error logs_plan raises: a read that determines
        # "there is nothing yet" has not failed (the axi self-update --check / axi status
        # precedent). Only a bench with NO live manager is the honest error.
        if r.manager_up:
            warnings.append(
                Message("logs.none_yet", f"No logs written yet under '{r.bench_path}/logs'.")
            )
            return Result(
                status=Status.OK,
                data=LogsRead(
                    project=project_name,
                    container_name=r.container_name,
                    bench_path=r.bench_path,
                    lines_requested=lines,
                    not_cwcli_supervised=r.not_cwcli_supervised,
                    logs=[],
                ),
                warnings=warnings,
            )
        _raise_no_logs(project_name, r.bench_path, process, manager_up=False)

    groups = _tail_files(r.container, r.log_files, lines)
    return Result(
        status=Status.OK,
        data=LogsRead(
            project=project_name,
            container_name=r.container_name,
            bench_path=r.bench_path,
            lines_requested=lines,
            not_cwcli_supervised=r.not_cwcli_supervised,
            logs=groups,
        ),
        warnings=warnings,
    )


def _tail_header_path(line: str) -> str | None:
    """The path in a ``tail -v`` ``==> <path> <==`` header line, else None."""
    if line.startswith("==> ") and line.endswith(" <=="):
        return line[len("==> ") : -len(" <==")]
    return None


def _process_label_for_file(file_path: str) -> str:
    """The process label a discovered log file belongs to.

    A supervisord file is ``<program>.supervisor.log`` -> the RAW program key (the inverse of
    ``supervision.process_log_path``); a raw honcho/``bench start`` file is ``<name>.log`` ->
    its stem. Deliberately the RAW key (``worker_default``), NOT the colon-normalized display
    label ``status`` shows (``worker:default``): a colon is unsafe in a TOON block key, and the
    raw key both matches the log filename and round-trips through ``--process`` (which
    ``supervision.program_for_label`` accepts in either form).
    """
    stem = file_path.rsplit("/", 1)[-1]
    if stem.endswith(supervision._PROC_LOG_SUFFIX):
        return stem[: -len(supervision._PROC_LOG_SUFFIX)]
    if stem.endswith(".log"):
        return stem[:-4]
    return stem


def _tail_files(container, files: list[str], lines: int) -> list[ProcessLog]:
    """One buffered ``tail -v -n N`` over ``files``, split back into per-process groups.

    ``tail -v`` forces the ``==> <path> <==`` header even for a single file, so combined and
    single-process reads parse uniformly; each header's group runs until the next one, minus
    tail's one-blank-line separator. ``container.exec_run`` (the buffered ``core.backup`` shape),
    not ``exec_stream``: a bounded read blocks and returns finite output, coverable by the same
    container fakes the rest of this module uses.

    ``ponytail:`` the ``==> <path> <==`` header is the parse boundary; a log line literally
    equal to one would mis-split. Upgrade path if it ever bites: one ``exec_run`` per file.
    """
    if not files:
        return []
    _exit_code, output = container.exec_run(["tail", "-v", "-n", str(lines), *files])
    text = supervision._decode(output)

    groups: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        header = _tail_header_path(line)
        if header is not None:
            if groups and groups[-1][1] and groups[-1][1][-1] == "":
                groups[-1][1].pop()  # drop tail's one blank line before the next header
            groups.append((header, []))
            continue
        if not groups:
            groups.append((files[0], []))  # output before any header (defensive; -v emits one)
        groups[-1][1].append(line)

    return [
        ProcessLog(process=_process_label_for_file(path), file=path, lines=lns)
        for path, lns in groups
    ]


def _raise_no_logs(
    project_name: str, bench_path: str, process: str | None, *, manager_up: bool
) -> None:
    """Raise the right no-logs error, keeping the two outcomes distinguishable.

    A running-but-quiet bench must NOT be told it may be down: manager up ->
    ``NOT_FOUND``/``logs.none_yet`` (a wait, not a failure), no manager ->
    ``NOT_RUNNING``/``logs.no_manager`` with the start hint.
    """
    if process:
        message = f"No logs found for process '{process}' under '{bench_path}/logs'."
    else:
        message = f"No process logs found under '{bench_path}/logs'."

    if manager_up:
        raise CwcliError(
            ErrorKind.NOT_FOUND,
            "logs.none_yet",
            message,
            hint="The bench is running but has not written those logs yet.",
        )
    raise CwcliError(
        ErrorKind.NOT_RUNNING,
        "logs.no_manager",
        message,
        hint=f"The bench may not be running. Start it with: cwcli start {project_name}",
    )
