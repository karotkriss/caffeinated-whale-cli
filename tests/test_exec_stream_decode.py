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
"""

import types

import pytest
import typer

from caffeinated_whale_cli.commands import apps as apps_mod
from caffeinated_whale_cli.commands import init as init_mod
from caffeinated_whale_cli.commands import run as run_mod
from caffeinated_whale_cli.commands import update as update_mod

# ------------------------------------------------------------------ split fixture

TEXT = "Updating DocType ✓\nMigrating ─│└ ⚠\nDone ✅\n"
PAYLOAD = TEXT.encode("utf-8")

# Cut one byte into the first "✓" (U+2713 = E2 9C 93), so chunk 1 ends holding an
# incomplete character and chunk 2 opens with its continuation bytes.
SPLIT = PAYLOAD.index("✓".encode("utf-8")) + 1
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


# ------------------------------------------------------------------ the four sites


def test_run_streams_split_character_intact(monkeypatch, capsys):
    """``cwcli run <p> migrate`` - crashed with a raw traceback, zero output."""
    container = _SplitContainer()
    monkeypatch.setattr(run_mod, "ensure_containers_running", lambda *a, **k: None)
    monkeypatch.setattr(run_mod, "resolve_bench_path", lambda *a, **k: "/workspace/frappe-bench")
    monkeypatch.setattr(run_mod, "get_project_containers", lambda *a, **k: [container])

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


def test_apps_update_streams_split_character_intact(capsys):
    """``apps update`` / ``update`` via update._stream_command - same crash."""
    exit_code = update_mod._stream_command(
        _SplitContainer(), "bench migrate", "/workspace/frappe-bench", verbose=True
    )

    assert exit_code == 0
    assert capsys.readouterr().out == TEXT


def test_apps_install_streams_split_character_intact(capsys):
    """``apps install``/``uninstall`` - errors="replace", so it corrupted silently."""
    exit_code = apps_mod._stream_bench(
        _SplitContainer(), "bench install-app erpnext", "/workspace/frappe-bench"
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "�" not in out
    assert out == TEXT


def test_init_streams_split_character_intact(capsys):
    """``init`` - demuxed, so stdout and stderr each need their own decoder."""
    init_mod._exec_in_container(_SplitContainer(), "bench new-site x", stream_output=True)

    out = capsys.readouterr().out
    assert "�" not in out
    assert out == TEXT


def test_init_decodes_stdout_and_stderr_independently(capsys):
    """A partial char on one demuxed stream must not corrupt the other.

    init's two streams interleave; sharing one decoder between them would feed
    stderr's bytes into stdout's pending character and mangle both.
    """
    err = "警告\n".encode("utf-8")
    container = _SplitContainer(chunks=[])
    container.client.api.chunks = [
        (CHUNKS[0], err[:1]),
        (CHUNKS[1], err[1:]),
    ]

    def _demux_start(exec_id, stream=True, demux=False):
        yield from container.client.api.chunks

    container.client.api.exec_start = _demux_start
    init_mod._exec_in_container(container, "bench new-site x", stream_output=True)

    captured = capsys.readouterr()
    assert captured.out == TEXT
    assert captured.err == "警告\n"
