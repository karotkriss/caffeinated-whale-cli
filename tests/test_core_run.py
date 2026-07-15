"""``core.run_plan`` / ``core.run_stream`` and the reseated ``cwcli run``.

`run` had NO tests of its own before this: `tests/README.md` recorded it as the
one command at 0% dedicated coverage, and its single existing test
(`test_exec_stream_decode.py`) pins the decode path rather than the command. It
is also the command this batch refactors onto new machinery, so these exist
because refactoring it blind was the actual risk.
"""

import types

import pytest
import typer

from caffeinated_whale_cli.commands import run as run_mod
from caffeinated_whale_cli.core import docker as core_docker
from caffeinated_whale_cli.core import exec_stream as es
from caffeinated_whale_cli.core import resolvers
from caffeinated_whale_cli.core import run as core_run
from caffeinated_whale_cli.core.envelope import Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
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


def test_run_verbose_reports_the_envelope_warnings(monkeypatch, frontend, capsys):
    monkeypatch.setattr(run_mod, "run_stream", stream_of(es.ExecDone(exit_code=0)))

    with pytest.raises(typer.Exit):
        invoke(verbose=True)

    err = capsys.readouterr().err
    assert "No cached bench path found" in err
    assert "bench migrate" in err
