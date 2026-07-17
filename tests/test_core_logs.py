"""``core.logs_plan`` and its two moved reads.

The resolve moved off ``commands/logs.py`` onto ``core/logs.py``. Two things this
file pins that the command tests could not:

- ``_existing_files`` and ``_discover_bench_log_files`` now use
  ``container.exec_run`` (docker-py) rather than a ``docker exec`` shell-out, so
  they are covered against a container fake instead of being monkeypatched away
  wholesale (they were 0% before, the only substantive misses in the file).
- ``logs_plan``'s decisions are returned, not printed: the ``NEEDS_CHOICE`` forks
  (multi-bench, stopped container, unknown ``--process``), the two distinct no-logs
  errors, and the not-cwcli-supervised fallback firing and NOT firing.
"""

from __future__ import annotations

import shlex

import pytest

from caffeinated_whale_cli.core import docker as core_docker
from caffeinated_whale_cli.core import logs as core_logs
from caffeinated_whale_cli.core import resolvers, supervision
from caffeinated_whale_cli.core.envelope import Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind

# ---------------------------------------------------------------- container fake


class LogFsContainer:
    """A frappe container fake backed by an in-memory view of the two reads.

    Interprets EXACTLY the ``sh -c`` scripts ``_existing_files`` and
    ``_discover_bench_log_files`` emit: the ``if [ -f <path> ]; then echo <path>;
    fi`` existence probe (echoes the subset in ``existing``, order preserved) and
    the ``ls -1 .../*.log`` glob (echoes ``listing``). ``status`` defaults running so
    it survives ``resolve_container_state``.
    """

    def __init__(self, existing=(), listing=(), contents=None):
        self.existing = list(existing)
        self.listing = list(listing)
        # contents: {path: [line, ...]} - what a `tail -v` read of that file yields.
        self.contents = dict(contents or {})
        self.name = "proj-frappe-1"
        self.status = "running"
        self.labels = {"com.docker.compose.service": "frappe"}
        self.calls: list = []

    def reload(self):
        pass

    def exec_run(self, cmd, workdir=None, environment=None):
        self.calls.append(cmd)
        # `read_logs`' bounded tail: `tail -v -n N <paths...>`. Reproduce GNU tail's
        # multi-file framing - a `==> <path> <==` header per file, one blank line
        # between file blocks, no trailing blank after the last.
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "tail":
            argv = list(cmd)
            n = int(argv[argv.index("-n") + 1]) if "-n" in argv else 10
            paths = [a for a in argv if isinstance(a, str) and a.startswith("/")]
            out: list[str] = []
            for i, p in enumerate(paths):
                if i > 0:
                    out.append("")  # blank separator before subsequent headers
                out.append(f"==> {p} <==")
                out.extend(self.contents.get(p, [])[-n:])
            return (0, ("\n".join(out) + ("\n" if out else "")).encode())
        if not (isinstance(cmd, (list, tuple)) and list(cmd[:2]) == ["sh", "-c"]):
            return (1, b"")
        script = cmd[2]
        if "[ -f " in script:
            tokens = shlex.split(script)
            probed = [tokens[i + 1] for i, t in enumerate(tokens) if t == "-f"]
            found = [p for p in self.existing if p in probed]
            return (0, "".join(p + "\n" for p in found).encode())
        if "ls -1" in script:
            return (0, "".join(p + "\n" for p in self.listing).encode())
        return (1, b"")


@pytest.fixture
def wire(monkeypatch):
    """Wire the collaborators of ``logs_plan`` around a chosen container.

    Returns a setter: call ``wire(container, benches=[...], programs=[...],
    manager_up=...)`` to install it. ``programs`` are raw Procfile keys.
    """

    def _install(container, *, benches=None, programs=("web", "worker_default"), manager_up=False):
        monkeypatch.setattr(core_docker, "get_project_containers", lambda _n: [container])
        monkeypatch.setattr(
            resolvers,
            "cached_benches",
            lambda _p: (benches if benches is not None else [{"path": "/w/bench", "label": None}]),
        )
        monkeypatch.setattr(supervision, "procfile_programs", lambda _c, _b: list(programs))
        monkeypatch.setattr(
            supervision,
            "discover_unsupervised_stack",
            lambda _c, _b: supervision.UnsupervisedStack(manager_up=manager_up, processes=[]),
        )
        return container

    return _install


# ------------------------------------------------------------------ the two reads


def test_existing_files_returns_only_present_paths_in_order():
    c = LogFsContainer(existing=["/w/bench/logs/web.supervisor.log"])
    files = [
        "/w/bench/logs/web.supervisor.log",
        "/w/bench/logs/worker_default.supervisor.log",
    ]
    assert core_logs._existing_files(c, files) == ["/w/bench/logs/web.supervisor.log"]


def test_existing_files_empty_input_makes_no_exec():
    c = LogFsContainer()
    assert core_logs._existing_files(c, []) == []
    assert c.calls == []  # no exec for an empty candidate list


def test_discover_bench_log_files_excludes_supervisor_logs_and_sorts():
    c = LogFsContainer(
        listing=[
            "/w/bench/logs/worker.log",
            "/w/bench/logs/web.log",
            "/w/bench/logs/web.supervisor.log",  # supervisord's own: excluded
        ]
    )
    assert core_logs._discover_bench_log_files(c, "/w/bench") == [
        "/w/bench/logs/web.log",
        "/w/bench/logs/worker.log",
    ]


def test_program_log_matches():
    m = core_logs._program_log_matches
    assert m("/w/logs/web.log", "web")
    assert m("/w/logs/web.error.log", "web")
    assert not m("/w/logs/worker.log", "web")
    assert m("/w/logs/worker.log", "worker_default")
    assert m("/w/logs/worker.error.log", "worker_short")
    assert m("/w/logs/scheduler.log", "schedule")
    assert m("/w/logs/redis-cache.log", "redis_cache")
    assert not m("/w/logs/bench.log", "web")


# ------------------------------------------------------------------ logs_plan: OK


def test_plan_tails_every_program_log_when_no_process(wire):
    c = LogFsContainer(
        existing=[
            "/w/bench/logs/web.supervisor.log",
            "/w/bench/logs/worker_default.supervisor.log",
        ]
    )
    wire(c)
    result = core_logs.logs_plan("proj")
    assert result.status is Status.OK
    assert result.data.log_files == [
        "/w/bench/logs/web.supervisor.log",
        "/w/bench/logs/worker_default.supervisor.log",
    ]
    assert result.data.not_cwcli_supervised is False
    assert result.data.container_name == "proj-frappe-1"


def test_plan_process_tails_one_log(wire):
    c = LogFsContainer(existing=["/w/bench/logs/worker_default.supervisor.log"])
    wire(c)
    result = core_logs.logs_plan("proj", process="worker:default")
    assert result.status is Status.OK
    assert result.data.log_files == ["/w/bench/logs/worker_default.supervisor.log"]


def test_plan_carries_follow_and_lines(wire):
    c = LogFsContainer(existing=["/w/bench/logs/web.supervisor.log"])
    wire(c, programs=["web"])
    result = core_logs.logs_plan("proj", follow=True, lines=42)
    assert result.data.follow is True
    assert result.data.lines == 42


def test_plan_falls_back_to_default_bench_with_a_warning(wire):
    # No cached benches -> the historical default path, with a bench.default_used
    # warning (matches core.run_plan). The log paths hang off that default.
    c = LogFsContainer(existing=["/workspace/frappe-bench/logs/web.supervisor.log"])
    wire(c, benches=[], programs=["web"])
    result = core_logs.logs_plan("proj")
    assert result.status is Status.OK
    assert result.data.bench_path == resolvers.DEFAULT_BENCH_PATH
    assert any(w.code == "bench.default_used" for w in result.warnings)


def test_plan_carries_no_live_object_and_no_argv(wire):
    c = LogFsContainer(existing=["/w/bench/logs/web.supervisor.log"])
    wire(c, programs=["web"])
    plan = core_logs.logs_plan("proj").data
    # Serializable: a name string, plain paths, no container and no docker argv.
    assert isinstance(plan.container_name, str)
    assert all(isinstance(f, str) for f in plan.log_files)
    assert not any("docker" in f for f in plan.log_files)


# --------------------------------------------------------- logs_plan: NEEDS_CHOICE


def test_plan_multi_bench_without_selector_is_select_bench(wire):
    c = LogFsContainer()
    wire(c, benches=[{"path": "/w/a", "label": None}, {"path": "/w/b", "label": None}])
    result = core_logs.logs_plan("proj")
    assert result.status is Status.NEEDS_CHOICE
    assert result.choice.kind == "select_bench"


def test_plan_stopped_container_is_confirm_start(wire):
    c = LogFsContainer()
    c.status = "exited"
    wire(c)
    result = core_logs.logs_plan("proj", auto_start=False)
    assert result.status is Status.NEEDS_CHOICE
    assert result.choice.kind == "confirm_start"


def test_plan_unknown_process_is_select_process(wire):
    c = LogFsContainer(existing=["/w/bench/logs/web.supervisor.log"])
    wire(c)
    result = core_logs.logs_plan("proj", process="nope")
    assert result.status is Status.NEEDS_CHOICE
    assert result.choice.kind == "select_process"
    assert result.choice.prompt == "No process 'nope' in bench '/w/bench'."
    # The valid labels are the normalized program names.
    assert [o["value"] for o in result.choice.options] == ["web", "worker:default"]


# ------------------------------------------------------------- logs_plan: no logs


def test_plan_no_logs_and_no_manager_raises_not_running(wire):
    c = LogFsContainer(existing=[])  # no supervisord logs
    wire(c, manager_up=False)
    with pytest.raises(CwcliError) as excinfo:
        core_logs.logs_plan("proj")
    assert excinfo.value.kind is ErrorKind.NOT_RUNNING
    assert excinfo.value.code == "logs.no_manager"
    assert "cwcli start proj" in excinfo.value.hint


def test_plan_no_logs_names_the_process_in_the_message(wire):
    c = LogFsContainer(existing=[], listing=[])
    wire(c, manager_up=False)
    with pytest.raises(CwcliError) as excinfo:
        core_logs.logs_plan("proj", process="web")
    assert excinfo.value.message == "No logs found for process 'web' under '/w/bench/logs'."


def test_plan_no_logs_but_manager_up_raises_not_found_not_down(wire):
    c = LogFsContainer(existing=[], listing=[])  # running, but nothing written yet
    wire(c, manager_up=True)
    with pytest.raises(CwcliError) as excinfo:
        core_logs.logs_plan("proj")
    assert excinfo.value.kind is ErrorKind.NOT_FOUND
    assert excinfo.value.code == "logs.none_yet"
    # The running-but-quiet bench must NOT be told it may be down.
    assert "may not be running" not in (excinfo.value.hint or "")


# ---------------------------------------------------- logs_plan: the fallback path


def test_plan_falls_back_to_raw_logs_when_honcho_running(wire):
    real = ["/w/bench/logs/bench.log", "/w/bench/logs/web.log", "/w/bench/logs/worker.log"]
    c = LogFsContainer(existing=[], listing=real)  # no supervisor logs, honcho's real ones
    wire(c, manager_up=True)
    result = core_logs.logs_plan("proj")
    assert result.status is Status.OK
    assert result.data.not_cwcli_supervised is True
    assert result.data.log_files == real
    # It must not invent supervisord names that do not exist.
    assert not any(f.endswith(".supervisor.log") for f in result.data.log_files)


def test_plan_fallback_filters_by_process(wire):
    real = ["/w/bench/logs/web.error.log", "/w/bench/logs/web.log", "/w/bench/logs/worker.log"]
    c = LogFsContainer(existing=[], listing=real)
    wire(c, manager_up=True)
    result = core_logs.logs_plan("proj", process="web")
    assert result.status is Status.OK
    assert result.data.log_files == ["/w/bench/logs/web.error.log", "/w/bench/logs/web.log"]


def test_plan_supervised_path_never_reaches_fallback(monkeypatch, wire):
    c = LogFsContainer(existing=["/w/bench/logs/web.supervisor.log"])
    wire(c, programs=["web"])

    def _boom(*a, **k):
        raise AssertionError("discover_unsupervised_stack called on the supervised path")

    monkeypatch.setattr(supervision, "discover_unsupervised_stack", _boom)
    result = core_logs.logs_plan("proj")
    assert result.status is Status.OK
    assert result.data.not_cwcli_supervised is False


# ------------------------------------------------------------------ purity


def test_core_logs_imports_no_subprocess():
    # The tail is the frontend's; a core module that can spawn one can drift back
    # into owning it. (rich/questionary/typer are banned by test_core_envelope.)
    import inspect

    source = inspect.getsource(core_logs)
    assert "import subprocess" not in source


# ============================================================ read_logs (bounded)
#
# `read_logs` shares `_resolve_log_files` with `logs_plan` (every NEEDS_CHOICE fork
# and the fallback are already pinned above), then runs one buffered `tail -v` and
# returns the lines as data. These pin what read_logs adds: the tail parse into
# per-process groups, the running-but-quiet empty-OK divergence, and the no-manager
# error.


def test_read_groups_lines_by_process(wire):
    c = LogFsContainer(
        existing=[
            "/w/bench/logs/web.supervisor.log",
            "/w/bench/logs/worker_default.supervisor.log",
        ],
        contents={
            "/w/bench/logs/web.supervisor.log": ["web line 1", "web line 2"],
            "/w/bench/logs/worker_default.supervisor.log": ["worker line 1"],
        },
    )
    wire(c)
    result = core_logs.read_logs("proj", lines=50)
    assert result.status is Status.OK
    groups = {g.process: g for g in result.data.logs}
    assert set(groups) == {"web", "worker_default"}
    assert groups["web"].lines == ["web line 1", "web line 2"]
    assert groups["web"].file == "/w/bench/logs/web.supervisor.log"
    assert groups["worker_default"].lines == ["worker line 1"]
    assert result.data.lines_requested == 50
    assert result.data.container_name == "proj-frappe-1"


def test_read_single_process_returns_only_that_group(wire):
    c = LogFsContainer(
        existing=["/w/bench/logs/worker_default.supervisor.log"],
        contents={"/w/bench/logs/worker_default.supervisor.log": ["only worker"]},
    )
    wire(c)
    result = core_logs.read_logs("proj", process="worker:default")
    assert result.status is Status.OK
    assert [g.process for g in result.data.logs] == ["worker_default"]
    assert result.data.logs[0].lines == ["only worker"]


def test_read_honours_the_lines_bound(wire):
    c = LogFsContainer(
        existing=["/w/bench/logs/web.supervisor.log"],
        contents={"/w/bench/logs/web.supervisor.log": [f"line {i}" for i in range(20)]},
    )
    wire(c, programs=["web"])
    result = core_logs.read_logs("proj", lines=3)
    assert result.data.logs[0].lines == ["line 17", "line 18", "line 19"]


def test_read_line_with_special_chars_survives_the_tail_parse(wire):
    # A log line carrying `:` `,` `"` must come back verbatim - the frontend emits
    # it as a raw TOON block line, so the parse must not split or mangle it.
    line = 'ERROR: {"key": "value", "n": 42} -> failed'
    c = LogFsContainer(
        existing=["/w/bench/logs/web.supervisor.log"],
        contents={"/w/bench/logs/web.supervisor.log": [line]},
    )
    wire(c, programs=["web"])
    result = core_logs.read_logs("proj")
    assert result.data.logs[0].lines == [line]


def test_read_empty_file_is_a_group_with_no_lines(wire):
    c = LogFsContainer(existing=["/w/bench/logs/web.supervisor.log"], contents={})
    wire(c, programs=["web"])
    result = core_logs.read_logs("proj")
    assert result.status is Status.OK
    assert result.data.logs[0].process == "web"
    assert result.data.logs[0].lines == []


def test_read_running_but_quiet_is_empty_success_not_error(wire):
    # Manager up, but no supervisord log files exist yet and the fallback finds no
    # real logs: a bounded read returns OK with empty logs + logs.none_yet, NOT the
    # error logs_plan raises. Exit-0 empty read is the agent-surface contract.
    c = LogFsContainer(existing=[], listing=[])
    wire(c, manager_up=True)
    result = core_logs.read_logs("proj")
    assert result.status is Status.OK
    assert result.data.logs == []
    assert any(w.code == "logs.none_yet" for w in result.warnings)


def test_read_no_manager_is_the_honest_error(wire):
    # Container up but nothing supervising the bench: read_logs RAISES the same
    # NOT_RUNNING/logs.no_manager error logs_plan does, with the start hint.
    c = LogFsContainer(existing=[], listing=[])
    wire(c, manager_up=False)
    with pytest.raises(CwcliError) as excinfo:
        core_logs.read_logs("proj")
    assert excinfo.value.kind is ErrorKind.NOT_RUNNING
    assert excinfo.value.code == "logs.no_manager"


def test_read_stopped_container_is_confirm_start(wire):
    c = LogFsContainer(existing=[])
    c.status = "exited"
    wire(c)
    result = core_logs.read_logs("proj")
    assert result.status is Status.NEEDS_CHOICE
    assert result.choice.kind == "confirm_start"


def test_read_multi_bench_without_selector_is_select_bench(wire):
    c = LogFsContainer()
    wire(c, benches=[{"path": "/w/a", "label": None}, {"path": "/w/b", "label": None}])
    result = core_logs.read_logs("proj")
    assert result.status is Status.NEEDS_CHOICE
    assert result.choice.kind == "select_bench"


def test_read_unknown_process_is_select_process(wire):
    c = LogFsContainer(existing=["/w/bench/logs/web.supervisor.log"])
    wire(c, programs=["web", "worker_default"])
    result = core_logs.read_logs("proj", process="nope")
    assert result.status is Status.NEEDS_CHOICE
    assert result.choice.kind == "select_process"


def test_read_fallback_labels_raw_logs_by_stem(wire):
    # A honcho / `bench start` bench: no supervisord files, real *.log discovered.
    # not_cwcli_supervised is set and the process label is the file stem.
    real = ["/w/bench/logs/web.log", "/w/bench/logs/worker.log"]
    c = LogFsContainer(
        existing=[],
        listing=real,
        contents={"/w/bench/logs/web.log": ["w"], "/w/bench/logs/worker.log": ["k"]},
    )
    wire(c, manager_up=True)
    result = core_logs.read_logs("proj")
    assert result.status is Status.OK
    assert result.data.not_cwcli_supervised is True
    assert {g.process for g in result.data.logs} == {"web", "worker"}


def test_read_carries_no_live_object_and_is_plain_data(wire):
    from dataclasses import asdict

    c = LogFsContainer(
        existing=["/w/bench/logs/web.supervisor.log"],
        contents={"/w/bench/logs/web.supervisor.log": ["x"]},
    )
    wire(c, programs=["web"])
    data = core_logs.read_logs("proj").data
    # asdict must yield plain data: no live container, no argv anywhere.
    dumped = asdict(data)
    assert isinstance(dumped, dict)
    assert "docker" not in repr(dumped)
    assert all(isinstance(g["file"], str) for g in dumped["logs"])
