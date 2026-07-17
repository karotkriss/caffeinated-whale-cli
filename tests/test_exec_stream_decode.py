"""Guard: a multi-byte character split across two exec-stream chunks must survive.

``bench`` is Python, and ``exec_create(tty=False)`` makes its stdout a pipe, so
CPython block-buffers it in 8KB blocks while Docker frames the socket at 32KB.
Any chatty bench command (``bench migrate``, ``bench build``) therefore hands the
consumer chunks that are cut at an arbitrary BYTE offset - routinely mid-character,
because Frappe emits unicode (``✓ ✗ → ─ │ └ ⚠ ✅``) as a matter of course.

Decoding each chunk independently is wrong at every one of those boundaries:
strict ``decode("utf-8")`` raises (``run``/``apps update`` died with a raw traceback
and delivered ZERO output), and ``errors="replace"`` silently corrupts the character
into U+FFFD (``apps install``/``init``). These tests feed each of the four consumers
a deliberately mid-character split and assert the text round-trips exactly.

All four consumers now reach the decoder through ``core.exec_stream`` rather
than through their own loops (``init`` was the last, via ``migrate-init-core``),
so these cases drive them through the primitive. The PROPERTY is what matters
and it pins a crash that SHIPPED: the helpers the four used to call are gone,
but this guard is not.
"""

import types

import pytest
import typer

from caffeinated_whale_cli.commands import apps as apps_mod
from caffeinated_whale_cli.commands import init as init_mod
from caffeinated_whale_cli.commands import run as run_mod
from caffeinated_whale_cli.commands import update as update_mod
from caffeinated_whale_cli.core import apps as core_apps
from caffeinated_whale_cli.core import docker as core_docker
from caffeinated_whale_cli.core import exec_stream as es
from caffeinated_whale_cli.core import update as core_update
from caffeinated_whale_cli.utils import docker_utils

# ------------------------------------------------------------------ split fixture

TEXT = "Updating DocType ✓\nMigrating ─│└ ⚠\nDone ✅\n"
PAYLOAD = TEXT.encode("utf-8")

# Cut one byte into the first "✓" (U+2713 = E2 9C 93), so chunk 1 ends holding an
# incomplete character and chunk 2 opens with its continuation bytes.
SPLIT = PAYLOAD.index("✓".encode()) + 1
CHUNKS = [PAYLOAD[:SPLIT], PAYLOAD[SPLIT:]]


def test_fixture_really_splits_mid_character():
    """The fixture is only a guard if the split is genuinely mid-character."""
    with pytest.raises(UnicodeDecodeError):
        CHUNKS[0].decode("utf-8")
    assert b"".join(CHUNKS) == PAYLOAD


class _SplitAPI:
    """Fake ``client.api`` that streams the payload split mid-character."""

    def __init__(self, chunks):
        self.chunks = chunks

    def exec_create(self, cid, cmd, workdir=None, tty=False, environment=None):
        return {"Id": "exec-1"}

    def exec_start(self, exec_id, stream=True, demux=False):
        for chunk in self.chunks:
            # demux=True (init) yields (stdout, stderr) pairs instead of raw bytes.
            yield (chunk, None) if demux else chunk

    def exec_inspect(self, exec_id):
        return {"ExitCode": 0}


class _SplitContainer:
    def __init__(self, chunks=CHUNKS):
        self.id = "cid"
        self.status = "running"
        self.labels = {"com.docker.compose.service": "frappe"}
        self.client = types.SimpleNamespace(api=_SplitAPI(chunks))

    def reload(self):
        pass


@pytest.fixture
def split_stream(monkeypatch):
    """A container whose exec stream splits the payload mid-character."""
    monkeypatch.setattr(es, "_EXIT_CODE_POLL_INTERVAL", 0)
    return _SplitContainer()


# ------------------------------------------------------------------ the four sites


def test_run_streams_split_character_intact(monkeypatch, capsys, split_stream):
    """``cwcli run <p> migrate`` - crashed with a raw traceback, zero output."""
    # Defuse the @handle_docker_errors preflight so the command body runs
    # (no real Docker on the unit tier).
    monkeypatch.setattr(docker_utils.shutil, "which", lambda _n: "/usr/bin/docker")
    monkeypatch.setattr(
        docker_utils.docker, "from_env", lambda: type("C", (), {"ping": lambda s: True})()
    )
    monkeypatch.setattr(run_mod, "ensure_containers_running", lambda *a, **k: None)
    monkeypatch.setattr(core_docker, "get_project_containers", lambda *a, **k: [split_stream])
    # run_stream turns the plan's container_id back into a handle; keep it fake.
    monkeypatch.setattr(core_docker, "get_container", lambda _id: split_stream)

    with pytest.raises(typer.Exit) as exc:
        run_mod.run(
            project_name="p",
            bench_args=["migrate"],
            bench=None,
            bench_path=None,
            yes=True,
            verbose=False,
        )

    assert exc.value.exit_code == 0
    assert capsys.readouterr().out == TEXT


def test_apps_update_streams_split_character_intact(capsys, split_stream):
    """``apps update`` / ``update`` - same crash.

    Re-pointed at ``core.update`` (openspec `migrate-update-core`): the state machine
    and its streaming moved there, so ``update._stream_command`` is gone. The PROPERTY
    is what this file guards and it pins a crash that SHIPPED, so it is driven through
    the real rendering path - the core's stream step feeding the CLI's own renderer,
    which is what actually writes bench output to stdout.
    """
    code, lost = core_update._stream_step(
        split_stream,
        "bench migrate",
        workdir="/workspace/frappe-bench",
        phase="migrate",
        item="a.localhost",
        emit=update_mod._Renderer(verbose=True),
        warnings=[],
    )

    assert code == 0
    assert lost is None
    assert capsys.readouterr().out == TEXT


def test_apps_install_streams_split_character_intact(capsys, split_stream):
    """``apps install``/``uninstall`` - errors="replace", so it corrupted silently.

    Re-pointed at ``core.apps`` (openspec `migrate-apps-core`), exactly as the
    ``update`` case above was by batch 4 and for the same reason: the fan-out and
    its streaming moved there, so ``apps._stream_bench`` is gone. The PROPERTY is
    what this file guards and it pins a crash that SHIPPED, so it is driven through
    the real rendering path - the core's stream step feeding the CLI's own
    renderer, which is what actually writes bench output to stdout.
    """
    exit_code = core_apps._run_step(
        split_stream,
        "bench install-app erpnext",
        "/workspace/frappe-bench",
        emit=apps_mod._make_renderer(json_output=False, verbose=False),
        phase="install-app",
        app="erpnext",
        site="a.localhost",
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "�" not in out
    assert out == TEXT


def _init_verbose_renderer():
    """The real rendering path: core events feeding the CLI's own renderer,
    which is what actually writes bench output raw to stdout/stderr."""
    return init_mod._InitRenderer(verbose=True, show_tips=False, project="p")


def test_init_streams_split_character_intact(capsys):
    """``init`` - demuxed, so stdout and stderr each need their own decoder.

    Re-pointed at ``core.init`` (openspec `migrate-init-core`), exactly as the
    other three consumers were by batches 4/5 and for the same reason: the
    exec-and-decode loop moved onto ``core.exec_stream``, so
    ``init._exec_in_container`` is gone. Driven through the core's exec step
    feeding the CLI's verbose renderer.
    """
    from caffeinated_whale_cli.core import init as core_init

    core_init._run_exec(
        _SplitContainer(),
        "bench new-site x",
        phase="new_site",
        emit=_init_verbose_renderer(),
        collect=False,
    )

    out = capsys.readouterr().out
    assert "�" not in out
    assert out == TEXT


def test_init_decodes_stdout_and_stderr_independently(capsys):
    """A partial char on one demuxed stream must not corrupt the other.

    init's two streams interleave; sharing one decoder between them would feed
    stderr's bytes into stdout's pending character and mangle both.
    """
    from caffeinated_whale_cli.core import init as core_init

    err = "警告\n".encode()
    container = _SplitContainer(chunks=[])
    container.client.api.chunks = [
        (CHUNKS[0], err[:1]),
        (CHUNKS[1], err[1:]),
    ]

    def _demux_start(exec_id, stream=True, demux=False):
        yield from container.client.api.chunks

    container.client.api.exec_start = _demux_start
    core_init._run_exec(
        container,
        "bench new-site x",
        phase="new_site",
        emit=_init_verbose_renderer(),
        collect=False,
    )

    captured = capsys.readouterr()
    assert captured.out == TEXT
    assert captured.err == "警告\n"
