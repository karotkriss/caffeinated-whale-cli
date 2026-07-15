"""``core.exec_stream``: the exec-stream contract.

The properties here are the ones the four hand-rolled loops each got wrong:
an honest exit code (never `None` coerced to success), a per-chunk stream tag,
a decode that survives a mid-character chunk split, and a stream that gets
closed. See `core/exec_stream.py`'s module docstring for the root causes.
"""

import pytest

from caffeinated_whale_cli.core import exec_stream as es
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind

# ------------------------------------------------------------------ fakes


class FakeStream:
    """A demuxed exec stream that records whether it was closed."""

    def __init__(self, frames):
        self._frames = list(frames)
        self.closed = False

    def __iter__(self):
        yield from self._frames

    def close(self):
        self.closed = True


class FakeContainer:
    """A live container handle, which is what `exec_stream` now takes."""

    def __init__(self, api):
        self.id = "cid"
        self.client = type("C", (), {"api": api})()


class FakeApi:
    """The slice of docker-py's APIClient that `exec_stream` touches."""

    def __init__(self, frames=(), inspects=None):
        self.stream = FakeStream(frames)
        # Successive exec_inspect returns; the last repeats forever.
        self.inspects = list(inspects or [{"ExitCode": 0, "Running": False}])
        self.inspect_calls = 0
        self.create_kwargs = None

    def exec_create(self, container_id, cmd, **kwargs):
        self.create_kwargs = {"container_id": container_id, "cmd": cmd, **kwargs}
        return {"Id": "exec-1"}

    def exec_start(self, exec_id, **kwargs):
        self.start_kwargs = kwargs
        return self.stream

    def exec_inspect(self, exec_id):
        self.inspect_calls += 1
        idx = min(self.inspect_calls - 1, len(self.inspects) - 1)
        return self.inspects[idx]


@pytest.fixture
def install(monkeypatch):
    """Point `exec_stream` at a FakeApi and collapse the poll bound.

    The bound is shrunk rather than honoured so the suite stays fast, and because
    these tests pin the BOUNDARY (does it settle, or give up?) rather than the
    timeout's value - the number is an implementation choice.
    """

    def _install(api):
        monkeypatch.setattr(es, "_EXIT_CODE_POLL_INTERVAL", 0)
        monkeypatch.setattr(es, "_EXIT_CODE_POLL_TIMEOUT", 0.01)
        return FakeContainer(api)

    return _install


def out(*chunks):
    """Frames carrying stdout only, as docker-py's demux adaptor yields them."""
    return [(c, None) for c in chunks]


# ------------------------------------------------------------------ events


def test_yields_tagged_chunks_then_one_terminal_done(install):
    container = install(FakeApi(frames=[(b"one", None), (None, b"two"), (b"three", None)]))

    events = list(es.exec_stream(container, "bench migrate"))

    assert events[:-1] == [
        es.ExecChunk(stream="stdout", text="one"),
        es.ExecChunk(stream="stderr", text="two"),
        es.ExecChunk(stream="stdout", text="three"),
    ]
    assert events[-1] == es.ExecDone(exit_code=0)
    # Exactly one terminal event, and nothing after it.
    assert sum(isinstance(e, es.ExecDone) for e in events) == 1


def test_command_with_no_output_still_reports_its_exit_code(install):
    container = install(FakeApi(frames=[], inspects=[{"ExitCode": 0, "Running": False}]))

    assert list(es.exec_stream(container, "true")) == [es.ExecDone(exit_code=0)]


def test_always_demuxes_and_passes_workdir_and_environment_through(install):
    container = install(FakeApi(frames=out(b"x")))

    list(es.exec_stream(container, "bench build", workdir="/b", environment={"K": "v"}))

    assert container.client.api.start_kwargs["demux"] is True
    assert container.client.api.start_kwargs["stream"] is True
    assert container.client.api.create_kwargs["workdir"] == "/b"
    assert container.client.api.create_kwargs["environment"] == {"K": "v"}
    assert container.client.api.create_kwargs["container_id"] == "cid"


def test_events_carry_only_builtins(install):
    """No live Docker object crosses the boundary (locked decision 4)."""
    container = install(FakeApi(frames=out(b"x")))

    for event in es.exec_stream(container, "true"):
        for value in vars(event).values() if hasattr(event, "__dict__") else _slots(event):
            assert isinstance(value, (str, int))


def _slots(event):
    return [getattr(event, name) for name in event.__slots__]


# ------------------------------------------------------------------ the honest exit code


def test_exit_code_none_while_running_is_polled_never_reported_as_success(install):
    """The bug this primitive exists to fix.

    `run.py` raised `typer.Exit(code=None)` here, which exits 0; `apps.py` did
    `result.get("ExitCode", 1) or 0`, which is 0. Both reported a bench command
    that never finished as a success.
    """
    container = install(
        FakeApi(
            frames=out(b"working"),
            inspects=[
                {"ExitCode": None, "Running": True},
                {"ExitCode": None, "Running": True},
                {"ExitCode": 3, "Running": False},
            ],
        )
    )

    events = list(es.exec_stream(container, "bench migrate"))

    assert events[-1] == es.ExecDone(exit_code=3)
    # It really polled rather than taking the daemon's first answer.
    assert container.client.api.inspect_calls == 3


def test_nonzero_exit_code_is_passed_through(install):
    container = install(FakeApi(frames=out(b"boom"), inspects=[{"ExitCode": 1, "Running": False}]))

    assert list(es.exec_stream(container, "false"))[-1] == es.ExecDone(exit_code=1)


def test_exit_code_is_typed_int_so_the_fail_open_is_unrepresentable(install):
    container = install(FakeApi(frames=[], inspects=[{"ExitCode": 0, "Running": False}]))

    done = list(es.exec_stream(container, "true"))[-1]

    assert isinstance(done.exit_code, int)
    assert done.exit_code is not None


def test_lost_stream_while_still_running_raises_rather_than_guessing(install):
    """A dropped connection is indistinguishable from a clean EOF.

    Pinned on the BOUNDARY (the poll gives up while the exec still reports
    running), not on the timeout's value.
    """
    container = install(
        FakeApi(
            frames=out(b"partial"),
            inspects=[{"ExitCode": None, "Running": True}],
        )
    )

    with pytest.raises(CwcliError) as excinfo:
        list(es.exec_stream(container, "bench migrate"))

    assert excinfo.value.kind is ErrorKind.DOCKER
    assert excinfo.value.code == "exec.stream_lost"


def test_finished_with_no_exit_code_raises(install):
    container = install(FakeApi(frames=[], inspects=[{"ExitCode": None, "Running": False}]))

    with pytest.raises(CwcliError) as excinfo:
        list(es.exec_stream(container, "true"))

    assert excinfo.value.kind is ErrorKind.DOCKER
    assert excinfo.value.code == "exec.exit_code_unknown"


def test_poll_settles_within_the_bound_rather_than_expiring(monkeypatch, install):
    """The other half of the boundary: it must not give up on the normal race."""
    container = install(
        FakeApi(
            frames=[],
            inspects=[
                {"ExitCode": None, "Running": True},
                {"ExitCode": 0, "Running": False},
            ],
        )
    )
    # A bound long enough for one more poll: the code must be found, not abandoned.
    monkeypatch.setattr(es, "_EXIT_CODE_POLL_TIMEOUT", 5.0)

    assert list(es.exec_stream(container, "true"))[-1] == es.ExecDone(exit_code=0)


# ------------------------------------------------------------------ decode


SPLIT_TEXT = "Migrating ✓ done\n"
PAYLOAD = SPLIT_TEXT.encode("utf-8")
CUT = PAYLOAD.index("✓".encode()) + 1  # one byte into the multi-byte character


def test_fixture_really_splits_mid_character():
    """The guard is only a guard if the split is genuinely mid-character."""
    with pytest.raises(UnicodeDecodeError):
        PAYLOAD[:CUT].decode("utf-8")


def test_character_split_across_chunks_round_trips(install):
    container = install(FakeApi(frames=out(PAYLOAD[:CUT], PAYLOAD[CUT:])))

    text = "".join(e.text for e in es.exec_stream(container, "bench migrate") if hasattr(e, "text"))

    assert text == SPLIT_TEXT
    assert "�" not in text


def test_stdout_and_stderr_decode_independently(install):
    """Sharing one decoder would feed stderr's bytes into stdout's pending character."""
    o, e = "out ✓\n".encode(), "err ─\n".encode()
    ocut, ecut = o.index("✓".encode()) + 1, e.index("─".encode()) + 1
    container = install(
        FakeApi(
            frames=[
                (o[:ocut], None),
                (None, e[:ecut]),
                (o[ocut:], None),
                (None, e[ecut:]),
            ]
        )
    )

    events = [ev for ev in es.exec_stream(container, "x") if isinstance(ev, es.ExecChunk)]

    assert "".join(c.text for c in events if c.stream == "stdout") == "out ✓\n"
    assert "".join(c.text for c in events if c.stream == "stderr") == "err ─\n"


def test_trailing_incomplete_character_is_surfaced_not_dropped(install):
    container = install(FakeApi(frames=out(PAYLOAD[:CUT])))  # stream truncates mid-character

    text = "".join(e.text for e in es.exec_stream(container, "x") if hasattr(e, "text"))

    assert text.startswith("Migrating ")
    assert "�" in text  # visible corruption beats silent truncation


# ------------------------------------------------------------------ the stream is closed


def test_stream_is_closed_after_normal_completion(install):
    container = install(FakeApi(frames=out(b"x")))

    list(es.exec_stream(container, "true"))

    assert container.client.api.stream.closed is True


def test_stream_is_closed_when_the_consumer_breaks_early(install):
    container = install(FakeApi(frames=out(b"one", b"two", b"three")))

    gen = es.exec_stream(container, "bench migrate")
    next(gen)
    gen.close()  # what a `break` out of a for-loop does

    assert container.client.api.stream.closed is True


def test_stream_is_closed_when_an_exception_escapes_mid_render(install):
    container = install(FakeApi(frames=out(b"one", b"two")))

    gen = es.exec_stream(container, "x")
    next(gen)
    with pytest.raises(RuntimeError):
        gen.throw(RuntimeError("render blew up"))

    assert container.client.api.stream.closed is True


def test_a_stream_without_close_is_tolerated(install):
    """Closing must never mask the real outcome."""
    api = FakeApi(frames=out(b"x"))
    api.stream = [(b"x", None)]  # a plain list: no .close()
    container = install(api)

    assert list(es.exec_stream(container, "true"))[-1] == es.ExecDone(exit_code=0)


# ------------------------------------------------------------------ daemon errors


def test_exec_start_failure_is_a_typed_error():
    """A docker-py exception must not escape the core as a raw SDK error."""
    from docker.errors import DockerException

    class BoomApi:
        def exec_create(self, *args, **kwargs):
            raise DockerException("daemon gone")

    with pytest.raises(CwcliError) as excinfo:
        list(es.exec_stream(FakeContainer(BoomApi()), "true"))

    assert excinfo.value.kind is ErrorKind.DOCKER
    assert excinfo.value.code == "exec.start_failed"
