"""``core.run_plan`` / ``core.run_stream`` and the reseated ``cwcli run``.

`run` had NO tests of its own before this: `tests/README.md` recorded it as the
one command at 0% dedicated coverage, and its single existing test
(`test_exec_stream_decode.py`) pins the decode path rather than the command. It
is also the command this batch refactors onto new machinery, so these exist
because refactoring it blind was the actual risk.
"""

import subprocess
import types

import pytest
import typer
from typer.testing import CliRunner

from caffeinated_whale_cli.commands import run as run_mod
from caffeinated_whale_cli.core import docker as core_docker
from caffeinated_whale_cli.core import exec_stream as es
from caffeinated_whale_cli.core import resolvers
from caffeinated_whale_cli.core import run as core_run
from caffeinated_whale_cli.core.envelope import Result, Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from caffeinated_whale_cli.main import app as main_app
from caffeinated_whale_cli.utils import docker_utils

# ------------------------------------------------------------------ fakes


class FakeContainer:
    def __init__(self, status="running"):
        self.id = "container-abc"
        self.status = status
        self.labels = {"com.docker.compose.service": "frappe"}
        self.client = types.SimpleNamespace(api=None)

    def reload(self):
        pass


@pytest.fixture
def container(monkeypatch):
    c = FakeContainer()
    monkeypatch.setattr(core_docker, "get_project_containers", lambda *a, **k: [c])
    return c


@pytest.fixture
def no_benches(monkeypatch):
    monkeypatch.setattr(resolvers, "cached_benches", lambda _p: [])


def benches(monkeypatch, *paths):
    monkeypatch.setattr(
        resolvers,
        "cached_benches",
        lambda _p: [{"path": p, "label": None} for p in paths],
    )


# ------------------------------------------------------------------ run_plan


def test_plan_assembles_the_bench_command_and_resolves_the_bench(monkeypatch, container):
    benches(monkeypatch, "/workspace/frappe-bench")

    result = core_run.run_plan("proj", ["migrate"])

    assert result.status is Status.OK
    assert result.data.command == "bench migrate"
    assert result.data.bench_path == "/workspace/frappe-bench"
    assert result.data.project == "proj"


def test_plan_shell_quotes_every_argument(monkeypatch, container, no_benches):
    result = core_run.run_plan("proj", ["--site", "a b; rm -rf /", "migrate"])

    assert result.data.command == "bench --site 'a b; rm -rf /' migrate"


def test_plan_carries_a_container_id_string_not_a_live_object(monkeypatch, container, no_benches):
    """Locked decision 4 at the two-phase seam: a plan crosses a return boundary."""
    plan = core_run.run_plan("proj", ["migrate"]).data

    assert plan.container_id == "container-abc"
    for value in (plan.project, plan.container_id, plan.bench_path, plan.command):
        assert isinstance(value, str)


def test_plan_falls_back_to_the_default_bench_with_a_warning(monkeypatch, container, no_benches):
    """`run.py`'s silent `or "/workspace/frappe-bench"`, now carried in the envelope."""
    result = core_run.run_plan("proj", ["migrate"])

    assert result.data.bench_path == resolvers.DEFAULT_BENCH_PATH
    assert [w.code for w in result.warnings] == ["bench.default_used"]


def test_plan_uses_the_sole_bench_with_a_warning(monkeypatch, container):
    benches(monkeypatch, "/workspace/other-bench")

    result = core_run.run_plan("proj", ["migrate"])

    assert result.data.bench_path == "/workspace/other-bench"
    assert [w.code for w in result.warnings] == ["bench.sole"]


def test_plan_multi_bench_without_a_selector_is_a_choice(monkeypatch, container):
    benches(monkeypatch, "/workspace/a", "/workspace/b")

    result = core_run.run_plan("proj", ["migrate"])

    assert result.status is Status.NEEDS_CHOICE
    assert result.choice.kind == "select_bench"
    assert result.choice.param == "bench"
    assert result.data is None


def test_plan_resolves_an_explicit_bench_selector(monkeypatch, container):
    benches(monkeypatch, "/workspace/a", "/workspace/b")

    result = core_run.run_plan("proj", ["migrate"], bench="1")

    assert result.data.bench_path == "/workspace/b"


def test_plan_unknown_bench_selector_raises_not_found(monkeypatch, container):
    benches(monkeypatch, "/workspace/a")

    with pytest.raises(CwcliError) as excinfo:
        core_run.run_plan("proj", ["migrate"], bench="nope")

    assert excinfo.value.kind is ErrorKind.NOT_FOUND


def test_plan_bench_and_path_together_is_a_usage_error(monkeypatch, container, no_benches):
    with pytest.raises(CwcliError) as excinfo:
        core_run.run_plan("proj", ["migrate"], bench="0", bench_path="/x")

    assert excinfo.value.kind is ErrorKind.USAGE


def test_plan_raises_at_call_time_not_at_first_iteration(monkeypatch):
    """The whole reason resolve and stream are two calls.

    A generator's body does not run until first iteration, so a frontend's
    try/except around the call would catch nothing. `run_plan` is not a
    generator, so this raises from the call itself.
    """
    monkeypatch.setattr(core_docker, "get_project_containers", lambda *a, **k: [])

    with pytest.raises(CwcliError) as excinfo:
        core_run.run_plan("ghost", ["migrate"])

    assert excinfo.value.kind is ErrorKind.NOT_FOUND


def test_plan_stopped_container_is_a_confirm_start_choice(monkeypatch):
    stopped = FakeContainer(status="exited")
    monkeypatch.setattr(core_docker, "get_project_containers", lambda *a, **k: [stopped])
    monkeypatch.setattr(resolvers, "cached_benches", lambda _p: [])

    result = core_run.run_plan("proj", ["migrate"])

    assert result.status is Status.NEEDS_CHOICE
    assert result.choice.kind == "confirm_start"


def test_plan_does_not_probe_or_validate_the_bench_path(monkeypatch, container, no_benches):
    """Decision 8's trap: `run` resolves LESS than `unlock` does.

    The probe/validate primitives are one import away, and reaching for them
    would ADD failures `cwcli run` does not have today. A path with a shell
    metacharacter is passed to `exec_create(workdir=...)`, never interpolated
    into a shell string, so there is no injection to prevent.
    """
    called = []
    for name in ("validate_bench_path", "require_bench_dir", "resolve_default_site"):
        monkeypatch.setattr(
            resolvers, name, lambda *a, _n=name, **k: called.append(_n), raising=True
        )

    result = core_run.run_plan("proj", ["migrate"], bench_path="/weird; path")

    assert result.data.bench_path == "/weird; path"
    assert called == []


# ------------------------------------------------------------------ run_stream


def test_run_stream_execs_the_planned_container(monkeypatch, container, no_benches):
    plan = core_run.run_plan("proj", ["migrate"]).data
    seen = {}

    def fake_get_container(container_id):
        seen["id"] = container_id
        return container

    def fake_exec_stream(c, cmd, *, workdir=None, environment=None):
        seen["cmd"], seen["workdir"] = cmd, workdir
        yield es.ExecDone(exit_code=0)

    monkeypatch.setattr(core_docker, "get_container", fake_get_container)
    monkeypatch.setattr(core_run, "exec_stream", fake_exec_stream)

    assert list(core_run.run_stream(plan)) == [es.ExecDone(exit_code=0)]
    assert seen["id"] == "container-abc"  # the container PLANNED, not a re-resolution
    assert seen["cmd"] == "bench migrate"
    assert seen["workdir"] == resolvers.DEFAULT_BENCH_PATH


# ------------------------------------------------------------------ the cwcli run frontend


@pytest.fixture
def frontend(monkeypatch, container, no_benches):
    """Defuse the @handle_docker_errors preflight and the interactive prologue."""
    monkeypatch.setattr(docker_utils.shutil, "which", lambda _n: "/usr/bin/docker")
    monkeypatch.setattr(
        docker_utils.docker, "from_env", lambda: type("C", (), {"ping": lambda s: True})()
    )
    monkeypatch.setattr(run_mod, "ensure_containers_running", lambda *a, **k: None)
    monkeypatch.setattr(core_docker, "get_container", lambda _id: container)
    return container


def invoke(**overrides):
    kwargs = dict(
        project_name="proj",
        bench_args=["migrate"],
        bench=None,
        bench_path=None,
        yes=True,
        interactive=False,
        verbose=False,
    )
    kwargs.update(overrides)
    return run_mod.run(**kwargs)


def stream_of(*events):
    def _stream(_plan):
        yield from events

    return _stream


def test_run_exits_with_the_bench_commands_code(monkeypatch, frontend):
    monkeypatch.setattr(run_mod, "run_stream", stream_of(es.ExecDone(exit_code=7)))

    with pytest.raises(typer.Exit) as exc:
        invoke()

    assert exc.value.exit_code == 7


def test_run_exits_zero_on_success(monkeypatch, frontend):
    monkeypatch.setattr(run_mod, "run_stream", stream_of(es.ExecDone(exit_code=0)))

    with pytest.raises(typer.Exit) as exc:
        invoke()

    assert exc.value.exit_code == 0


def test_run_renders_both_stream_tags_to_stdout(monkeypatch, frontend, capsys):
    monkeypatch.setattr(
        run_mod,
        "run_stream",
        stream_of(
            es.ExecChunk(stream="stdout", text="out\n"),
            es.ExecChunk(stream="stderr", text="err\n"),
            es.ExecDone(exit_code=0),
        ),
    )

    with pytest.raises(typer.Exit):
        invoke()

    # Combined onto stdout in yield order, as this command has always shown it.
    assert capsys.readouterr().out == "out\nerr\n"


def test_run_does_not_exit_zero_when_the_exit_code_is_unknown(monkeypatch, frontend):
    """THE BUG THIS BATCH EXISTS TO FIX.

    `run.py:78` read `result.get("ExitCode", 1)` - whose default is dead code,
    because the key is present and holds None - and raised
    `typer.Exit(code=None)`, which exits 0. A bench command that never finished
    reported success.
    """

    def _raise(_plan):
        raise CwcliError(ErrorKind.DOCKER, "exec.stream_lost", "Lost the stream")
        yield  # pragma: no cover - generator marker

    monkeypatch.setattr(run_mod, "run_stream", _raise)

    with pytest.raises(typer.Exit) as exc:
        invoke()

    assert exc.value.exit_code == 1


def test_run_reports_a_multi_bench_ambiguity_and_exits_nonzero(monkeypatch, frontend, capsys):
    benches(monkeypatch, "/workspace/a", "/workspace/b")

    with pytest.raises(typer.Exit) as exc:
        invoke()

    assert exc.value.exit_code == 1
    assert "--bench" in capsys.readouterr().err


def test_run_reports_a_typed_error_and_exits_nonzero(monkeypatch, frontend, capsys):
    monkeypatch.setattr(core_docker, "get_project_containers", lambda *a, **k: [])

    with pytest.raises(typer.Exit) as exc:
        invoke()

    assert exc.value.exit_code == 1
    assert "not found" in capsys.readouterr().err.lower()


def test_run_passes_the_bench_selector_through(monkeypatch, frontend):
    benches(monkeypatch, "/workspace/a", "/workspace/b")
    seen = {}

    def _stream(plan):
        seen["workdir"] = plan.bench_path
        yield es.ExecDone(exit_code=0)

    monkeypatch.setattr(run_mod, "run_stream", _stream)

    with pytest.raises(typer.Exit):
        invoke(bench="1")

    assert seen["workdir"] == "/workspace/b"


def test_run_retries_once_on_confirm_start_then_succeeds(monkeypatch, frontend):
    """Mirrors backup.py/unlock.py: a confirm_start race retries the start once,

    rather than printing the (wrong) --bench hint for a container-not-running
    error.
    """
    calls = {"ensure": 0, "plan": 0}

    def fake_ensure(*a, **k):
        calls["ensure"] += 1

    def fake_plan(*a, **k):
        calls["plan"] += 1
        if calls["plan"] == 1:
            from caffeinated_whale_cli.core.envelope import Choice, Result

            return Result(
                status=Status.NEEDS_CHOICE,
                choice=Choice(kind="confirm_start", param="yes", prompt="start it?"),
            )
        return core_run.run_plan(*a, **k)

    monkeypatch.setattr(run_mod, "ensure_containers_running", fake_ensure)
    monkeypatch.setattr(run_mod, "run_plan", fake_plan)
    monkeypatch.setattr(run_mod, "run_stream", stream_of(es.ExecDone(exit_code=0)))

    with pytest.raises(typer.Exit) as exc:
        invoke()

    assert exc.value.exit_code == 0
    assert calls["plan"] == 2
    assert calls["ensure"] == 2  # the interactive prologue, then exactly one retry


def test_run_fails_closed_on_a_repeated_confirm_start(monkeypatch, frontend, capsys):
    """A second confirm_start after an attempted start means the start didn't

    take (crash-loop/teardown race) - fail closed rather than spin forever.
    """
    from caffeinated_whale_cli.core.envelope import Choice, Result

    def always_confirm_start(*a, **k):
        return Result(
            status=Status.NEEDS_CHOICE,
            choice=Choice(kind="confirm_start", param="yes", prompt="start it?"),
        )

    monkeypatch.setattr(run_mod, "ensure_containers_running", lambda *a, **k: None)
    monkeypatch.setattr(run_mod, "run_plan", always_confirm_start)

    with pytest.raises(typer.Exit) as exc:
        invoke()

    assert exc.value.exit_code == 1
    assert "failed to start" in capsys.readouterr().err.lower()


def test_run_verbose_reports_the_envelope_warnings(monkeypatch, frontend, capsys):
    monkeypatch.setattr(run_mod, "run_stream", stream_of(es.ExecDone(exit_code=0)))

    with pytest.raises(typer.Exit):
        invoke(verbose=True)

    err = capsys.readouterr().err
    assert "No cached bench path found" in err
    assert "bench migrate" in err


# ------------------------------------------------------------------ --interactive
#
# `cwcli run` could not carry a bench command that PROMPTS: nothing forwarded
# stdin, so `bench new-app` (frappe's boilerplate uses `click.prompt`) was
# undrivable through the wrapper the project's own convention says every
# suggested bench command must be phrased as, pushing the user back to raw
# `docker exec`.
#
# The fix is a SECOND mechanism, not a widened first one: `core.exec_stream`
# starts its exec with `stream=True` (a read-only generator) and pins
# `demux=True`, which is mutually exclusive with the `tty=True` an interactive
# prompt needs. These tests pin that separation as much as the behaviour.


class FakeStream:
    def __init__(self, tty):
        self._tty = tty

    def isatty(self):
        return self._tty


def terminals(monkeypatch, *, stdin, stdout):
    monkeypatch.setattr(run_mod.sys, "stdin", FakeStream(stdin))
    monkeypatch.setattr(run_mod.sys, "stdout", FakeStream(stdout))


@pytest.fixture
def spy_exec(monkeypatch):
    """Capture the argv handed to `docker`, without running anything."""
    seen = {}

    class Completed:
        returncode = 0

    def fake_run(argv, *a, **k):
        seen["argv"] = argv
        return seen.get("result", Completed())

    monkeypatch.setattr(run_mod.subprocess, "run", fake_run)
    return seen


def test_interactive_attaches_stdin_via_docker_exec(monkeypatch, frontend, spy_exec):
    terminals(monkeypatch, stdin=False, stdout=False)

    with pytest.raises(typer.Exit):
        invoke(interactive=True, bench_args=["new-app", "my_app"])

    assert spy_exec["argv"][:7] == [
        "docker",
        "exec",
        "-i",
        "-w",
        resolvers.DEFAULT_BENCH_PATH,
        "container-abc",  # the container PLANNED, not a re-resolution
        "setsid",
    ]
    assert spy_exec["argv"][-3:] == [
        "bench",
        "new-app",
        "my_app",
    ]


def test_interactive_never_goes_through_the_exec_stream_contract(monkeypatch, frontend, spy_exec):
    """The separation, pinned. Bending `exec_stream` to carry stdin would mean
    giving up the locked `demux=True` stream tag; this path must not touch it."""
    terminals(monkeypatch, stdin=False, stdout=False)

    def explode(_plan):
        raise AssertionError("the interactive path must not use core.exec_stream")
        yield  # pragma: no cover - generator marker

    monkeypatch.setattr(run_mod, "run_stream", explode)

    with pytest.raises(typer.Exit):
        invoke(interactive=True)


def test_interactive_requests_a_tty_only_with_a_terminal_on_both_ends(
    monkeypatch, frontend, spy_exec
):
    """`-t` is what makes raw-mode prompting work, and it is also what makes the
    container emit colour and CRLF. Asking for it when stdout is a pipe would
    corrupt captured output; asking for it without a TTY on stdin fails
    outright ("the input device is not a TTY")."""
    for stdin_tty, stdout_tty, expected in (
        (True, True, True),
        (True, False, False),
        (False, True, False),
        (False, False, False),
    ):
        terminals(monkeypatch, stdin=stdin_tty, stdout=stdout_tty)

        with pytest.raises(typer.Exit):
            invoke(interactive=True)

        assert ("-t" in spy_exec["argv"]) is expected, (stdin_tty, stdout_tty)
        assert "-i" in spy_exec["argv"]  # stdin is forwarded in every combination


def test_interactive_propagates_the_bench_commands_exit_code(monkeypatch, frontend, spy_exec):
    terminals(monkeypatch, stdin=False, stdout=False)
    spy_exec["result"] = types.SimpleNamespace(returncode=42)

    with pytest.raises(typer.Exit) as exc:
        invoke(interactive=True)

    assert exc.value.exit_code == 42


def test_interactive_reports_a_ctrl_c_as_130_not_success(monkeypatch, frontend):
    """Without `-t` the SIGINT lands on this process. A bench command the user
    interrupted did not succeed, and must never exit 0."""
    terminals(monkeypatch, stdin=False, stdout=False)

    calls = []

    def interrupted(argv, *a, **k):
        calls.append(argv)
        if len(calls) == 1:
            raise KeyboardInterrupt
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(run_mod.subprocess, "run", interrupted)

    with pytest.raises(typer.Exit) as exc:
        invoke(interactive=True)

    assert exc.value.exit_code == 130
    assert calls[0][6:9] == ["setsid", "sh", "-c"]
    assert "echo $$ >" in calls[0][9]
    pidfile = next(arg for arg in calls[0] if arg.startswith("/tmp/cwcli-run-"))
    assert calls[1][:3] == ["docker", "exec", "container-abc"]
    assert "[ ! -s " in calls[1][-1]
    assert 'kill -TERM -- "-$pid"' in calls[1][-1]
    assert 'kill -KILL -- "-$pid"' in calls[1][-1]
    assert 'if kill -0 -- "-$pid"' in calls[1][-1]
    assert pidfile in calls[1][-1]


def test_interactive_refuses_to_claim_termination_when_cleanup_fails(monkeypatch, frontend, capsys):
    terminals(monkeypatch, stdin=False, stdout=False)
    calls = 0

    def cleanup_fails(_argv, *a, **k):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise KeyboardInterrupt
        return types.SimpleNamespace(returncode=4)

    monkeypatch.setattr(run_mod.subprocess, "run", cleanup_fails)

    with pytest.raises(typer.Exit) as exc:
        invoke(interactive=True)

    assert exc.value.exit_code == 1
    assert "termination could not be verified" in capsys.readouterr().err


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired("docker", 5), OSError("lost")])
def test_interactive_cleanup_exceptions_fail_closed(monkeypatch, failure):
    def fail(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(run_mod.subprocess, "run", fail)

    assert not run_mod._kill_interactive_process_group("container-abc", "/tmp/run.pid")


def test_interactive_quoting_survives_the_round_trip(monkeypatch, frontend, spy_exec):
    """`core.run_plan` quotes for docker-py (which splits a command STRING); the
    docker CLI takes an argv, so the frontend splits it back. An argument with a
    space must arrive as ONE argv entry, not two."""
    terminals(monkeypatch, stdin=False, stdout=False)

    with pytest.raises(typer.Exit):
        invoke(interactive=True, bench_args=["--site", "a b", "migrate"])

    assert spy_exec["argv"][-4:] == ["bench", "--site", "a b", "migrate"]


# ------------------------------------------------------------------ the argv surface
#
# These drive the REAL `main.app` through Typer's parser, because the thing under
# test IS the parser configuration (`ignore_unknown_options` on main.py's `run`
# registration). Every test above calls `run_mod.run(**kwargs)` directly, which
# skips parsing entirely - which is exactly how `cwcli run p get-app --branch
# develop <url>` shipped exiting 2, with click suggesting `--bench` (cwcli's bench
# SELECTOR) for what the user meant as a git BRANCH.


@pytest.fixture
def parsed(monkeypatch, frontend):
    """Capture what the parser hands `run_plan`, without execing anything."""
    calls = {}

    def fake_run_plan(project_name, args, *, bench=None, bench_path=None, auto_start=False):
        calls.update(project=project_name, args=args, bench=bench, path=bench_path)
        return Result(
            status=Status.OK,
            data=core_run.RunPlan(
                project=project_name, container_id="cid", bench_path="/b", command="bench x"
            ),
        )

    monkeypatch.setattr(run_mod, "run_plan", fake_run_plan)
    monkeypatch.setattr(run_mod, "run_stream", stream_of(es.ExecDone(exit_code=0)))
    return calls


def cli(parsed, *argv):
    result = CliRunner().invoke(main_app, ["run", "proj", *argv])
    assert result.exit_code == 0, result.output
    return parsed


def test_run_passes_an_unknown_flag_through_to_bench(parsed):
    """The defect: bench's own flags must reach bench, not exit 2."""
    calls = cli(parsed, "get-app", "--branch", "develop", "https://github.com/x/y.git")

    assert calls["args"] == ["get-app", "--branch", "develop", "https://github.com/x/y.git"]
    assert calls["bench"] is None  # --branch is NOT cwcli's bench selector


def test_run_never_suggests_the_bench_selector_for_an_unknown_flag(frontend):
    """`--bench` is a bench selector; suggesting it for `--branch` steers the user wrong."""
    result = CliRunner().invoke(main_app, ["run", "proj", "get-app", "--branch", "develop"])

    assert result.exit_code != 2
    assert "Did you mean" not in result.output
    assert "No such option" not in result.output


def test_run_still_claims_its_own_flags_after_the_bench_args(parsed):
    """The documented `cwcli run p migrate --bench staging` form keeps working."""
    calls = cli(parsed, "migrate", "--bench", "staging")

    assert calls["args"] == ["migrate"]
    assert calls["bench"] == "staging"


def test_run_honors_the_double_dash_separator(parsed):
    """`--` still hands everything after it to bench, collisions included."""
    calls = cli(parsed, "--", "--site", "development.localhost", "migrate")

    assert calls["args"] == ["--site", "development.localhost", "migrate"]
    assert calls["bench"] is None


def test_run_double_dash_shields_a_flag_that_collides_with_cwclis_own(parsed):
    """A bench flag NAMED like one of cwcli's own is what `--` is for."""
    calls = cli(parsed, "--", "build", "--verbose")

    assert calls["args"] == ["build", "--verbose"]


def test_run_claims_dash_i_as_its_own_interactive_flag(monkeypatch, parsed, spy_exec):
    """`-i` is cwcli's, like `-y`/`-v`/`-p`: it never reaches bench as an arg."""
    terminals(monkeypatch, stdin=False, stdout=False)

    calls = cli(parsed, "-i", "new-app", "my_app")

    assert calls["args"] == ["new-app", "my_app"]
    assert spy_exec["argv"][2] == "-i"  # it took the interactive path


def test_run_double_dash_shields_a_bench_dash_i(parsed):
    """And the documented escape hatch still hands `-i` to bench when meant for it."""
    calls = cli(parsed, "--", "some-cmd", "-i")

    assert calls["args"] == ["some-cmd", "-i"]


# ------------------------------------------------------------------ credential bridge
#
# Phase 2 of the persistent credential bridge (scout report §5.9): both `run`
# exec paths wrap in the SAME per-invocation `credential_bridge` the apps/init/
# update git ops use, so `cwcli run <p> get-app <private-url>` authenticates
# like `apps install` - EXCEPT when the persistent daemon already serves the
# instance (`ensure_bridge` returns an outcome), where a second helper line for
# the duration would be redundant: the daemon-skip.


@pytest.fixture
def bridge_spy(monkeypatch):
    """Fake the per-invocation bridge and default the daemon to not-serving."""
    import contextlib

    events = []

    @contextlib.contextmanager
    def fake_bridge(container, bench_path):
        events.append(("enter", container.id, bench_path))
        try:
            yield
        finally:
            events.append(("exit",))

    monkeypatch.setattr(run_mod, "credential_bridge", fake_bridge)
    monkeypatch.setattr(run_mod.cred_daemon, "ensure_bridge", lambda *a, **k: None)
    return events


class TestRunWrapsTheCredentialBridge:
    def test_streamed_path_execs_inside_the_bridge(self, monkeypatch, frontend, bridge_spy):
        """The bridge is up before the exec streams and torn down after it drains."""

        def _stream(_plan):
            bridge_spy.append(("stream",))
            yield es.ExecDone(exit_code=0)

        monkeypatch.setattr(run_mod, "run_stream", _stream)

        with pytest.raises(typer.Exit) as exc:
            invoke()

        assert bridge_spy == [
            ("enter", "container-abc", resolvers.DEFAULT_BENCH_PATH),
            ("stream",),
            ("exit",),
        ]
        assert exc.value.exit_code == 0

    def test_interactive_path_execs_inside_the_bridge(self, monkeypatch, frontend, bridge_spy):
        terminals(monkeypatch, stdin=False, stdout=False)

        def fake_run(argv, *a, **k):
            bridge_spy.append(("exec",))
            return types.SimpleNamespace(returncode=0)

        monkeypatch.setattr(run_mod.subprocess, "run", fake_run)

        with pytest.raises(typer.Exit):
            invoke(interactive=True)

        assert bridge_spy == [
            ("enter", "container-abc", resolvers.DEFAULT_BENCH_PATH),
            ("exec",),
            ("exit",),
        ]

    def test_ensure_bridge_is_the_run_call_site(self, monkeypatch, frontend, bridge_spy):
        """`run` is one of the persistent bridge's ensure call sites (report
        §5.6, next to open/core.start): the PLANNED container, bench, and
        project go in, so a recreated container self-heals on the next run."""
        seen = {}

        def fake_ensure(container, bench_path, project):
            seen["args"] = (container, bench_path, project)
            return None

        monkeypatch.setattr(run_mod.cred_daemon, "ensure_bridge", fake_ensure)
        monkeypatch.setattr(run_mod, "run_stream", stream_of(es.ExecDone(exit_code=0)))

        with pytest.raises(typer.Exit):
            invoke()

        container, bench_path, project = seen["args"]
        assert container is frontend  # the planned container, not a re-resolution
        assert bench_path == resolvers.DEFAULT_BENCH_PATH
        assert project == "proj"

    def test_daemon_skip_stands_up_no_second_bridge(self, monkeypatch, frontend, bridge_spy):
        """When the persistent daemon serves this instance, the stable helper
        already answers - the per-invocation bridge must NOT add a second,
        redundant helper line for the duration."""
        from caffeinated_whale_cli.utils import cred_daemon

        outcome = cred_daemon.EnsureOutcome(
            project="proj",
            workspace="/tmp/ws",
            config_value="!/usr/bin/python3 /workspace/.cwcli-git-credential.py",
            added_config=False,
            daemon_started=False,
        )
        monkeypatch.setattr(run_mod.cred_daemon, "ensure_bridge", lambda *a, **k: outcome)
        monkeypatch.setattr(run_mod, "run_stream", stream_of(es.ExecDone(exit_code=0)))

        with pytest.raises(typer.Exit) as exc:
            invoke()

        assert bridge_spy == []  # the exec still ran; only the bridge was skipped
        assert exc.value.exit_code == 0

    def test_daemon_skip_covers_the_interactive_path_too(self, monkeypatch, frontend, bridge_spy):
        from caffeinated_whale_cli.utils import cred_daemon

        terminals(monkeypatch, stdin=False, stdout=False)
        outcome = cred_daemon.EnsureOutcome(
            project="proj",
            workspace="/tmp/ws",
            config_value="!/usr/bin/python3 /workspace/.cwcli-git-credential.py",
            added_config=False,
            daemon_started=False,
        )
        monkeypatch.setattr(run_mod.cred_daemon, "ensure_bridge", lambda *a, **k: outcome)
        ran = {}
        monkeypatch.setattr(
            run_mod.subprocess,
            "run",
            lambda *a, **k: ran.setdefault("done", True) and types.SimpleNamespace(returncode=0),
        )

        with pytest.raises(typer.Exit):
            invoke(interactive=True)

        assert bridge_spy == []
        assert ran["done"] is True

    def test_bridge_tears_down_when_the_stream_dies(self, monkeypatch, frontend, bridge_spy):
        """A lost stream must still unwind through the bridge's teardown, so the
        helper line and shim never outlive the run (the apps-op guarantee)."""

        def _raise(_plan):
            raise CwcliError(ErrorKind.DOCKER, "exec.stream_lost", "Lost the stream")
            yield  # pragma: no cover - generator marker

        monkeypatch.setattr(run_mod, "run_stream", _raise)

        with pytest.raises(typer.Exit) as exc:
            invoke()

        assert bridge_spy == [
            ("enter", "container-abc", resolvers.DEFAULT_BENCH_PATH),
            ("exit",),
        ]
        assert exc.value.exit_code == 1
