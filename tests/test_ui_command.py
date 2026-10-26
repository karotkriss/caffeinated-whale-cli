"""``cwcli ui`` - the launcher: start or reuse the detached daemon, mint a sign-in
code, open the browser; ``--stop``; ``--foreground``.

No real daemon is spawned here (the real one is driven end to end by
``tests/e2e/test_serve_e2e.py``): the spawn, the readiness probe and the mint
call are the seams, so what these pin is the launcher's decisions - what it
spawns, what it writes where, how it reuses, and that the token never reaches
argv or the printed link.
"""

from __future__ import annotations

import json
import os
import stat
import sys

import pytest
from typer.testing import CliRunner

from caffeinated_whale_cli.commands import ui as ui_cmd
from caffeinated_whale_cli.main import app as root_app

runner = CliRunner()


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("CWCLI_HOME", str(tmp_path / ".cwcli"))
    monkeypatch.delenv("CWCLI_SERVE_TOKEN", raising=False)
    return tmp_path / ".cwcli"


def _child_up(port: int, token: str) -> None:
    """What a detached child does once bound: state file first, then its pid file."""
    ui_cmd._write_state(port, token)
    ui_cmd.daemon_identity.write_pid_file(ui_cmd.pid_file())


@pytest.fixture
def no_daemon(monkeypatch):
    monkeypatch.setattr(ui_cmd.daemon_identity, "is_running", lambda pid_file: False)


class TestStartDetached:
    def test_spawns_the_foreground_form_with_the_token_in_the_environment(
        self, home, no_daemon, monkeypatch
    ):
        spawned: dict = {}

        def _popen(argv, **kwargs):
            spawned["argv"] = argv
            spawned["env"] = kwargs["env"]
            spawned["stdin"] = kwargs["stdin"]
            spawned["session"] = kwargs.get("start_new_session")
            # The child writes the state file once it is up; emulate that.
            _child_up(8765, kwargs["env"][ui_cmd.serve_cmd.TOKEN_ENV])

        monkeypatch.setattr(ui_cmd.subprocess, "Popen", _popen)
        monkeypatch.setattr(ui_cmd, "_meta_answers", lambda port: True)
        minted: dict = {}

        def _mint(port, token):
            minted.update({"port": port, "token": token})
            return "one-time-code"

        monkeypatch.setattr(ui_cmd, "mint_code", _mint)
        opened: list = []
        monkeypatch.setattr(ui_cmd.webbrowser, "open", lambda url: opened.append(url) or True)

        result = runner.invoke(root_app, ["ui"])

        assert result.exit_code == 0, result.output
        assert spawned["argv"][:3] == [sys.executable, "-m", "caffeinated_whale_cli"]
        assert spawned["argv"][3:] == [
            "ui",
            "--foreground",
            "--no-browser",
            "--port",
            "8765",
        ]
        token = spawned["env"][ui_cmd.serve_cmd.TOKEN_ENV]
        assert token and token not in " ".join(spawned["argv"]), "the token never rides argv"
        assert spawned["stdin"] is ui_cmd.subprocess.DEVNULL
        if sys.platform != "win32":
            assert spawned["session"] is True
        assert minted == {"port": 8765, "token": token}
        assert opened == ["http://127.0.0.1:8765/#login=one-time-code"]
        assert token not in result.output, "the token is never printed"
        assert "#login=one-time-code" in result.output

    def test_no_browser_prints_the_link_only(self, home, no_daemon, monkeypatch):
        monkeypatch.setattr(ui_cmd.subprocess, "Popen", lambda argv, **kw: _child_up(9000, "t"))
        monkeypatch.setattr(ui_cmd, "_meta_answers", lambda port: True)
        monkeypatch.setattr(ui_cmd, "mint_code", lambda port, token: "code")
        monkeypatch.setattr(
            ui_cmd.webbrowser, "open", lambda url: pytest.fail("--no-browser must not open")
        )

        result = runner.invoke(root_app, ["ui", "--no-browser", "--port", "9000"])

        assert result.exit_code == 0, result.output
        assert "http://127.0.0.1:9000/#login=code" in result.output

    def test_a_daemon_that_never_answers_is_reported_with_the_log_path(
        self, home, no_daemon, monkeypatch
    ):
        monkeypatch.setattr(ui_cmd.subprocess, "Popen", lambda argv, **kw: None)
        monkeypatch.setattr(ui_cmd, "_meta_answers", lambda port: False)
        monkeypatch.setattr(ui_cmd, "_READY_TIMEOUT_S", 0.3)

        result = runner.invoke(root_app, ["ui", "--no-browser"])

        assert result.exit_code == 1
        assert "did not come up" in result.output
        assert str(ui_cmd.log_file()) in result.output

    def test_the_state_file_is_private_to_the_user(self, home):
        ui_cmd._write_state(8765, "secret-token")
        path = ui_cmd.state_file()
        if os.name == "posix":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert json.loads(path.read_text()) == {
            "port": 8765,
            "token": "secret-token",
            "build": ui_cmd.install_fingerprint(),
        }
        assert ui_cmd._read_state() == {
            "port": 8765,
            "token": "secret-token",
            "build": ui_cmd.install_fingerprint(),
        }

    @pytest.mark.skipif(os.name != "posix", reason="shared mode is POSIX-only")
    def test_shared_mode_keeps_run_files_per_user_and_the_token_private(
        self, home, monkeypatch, tmp_path
    ):
        marker = tmp_path / "shared.toml"
        marker.write_text("enabled = true\n")
        monkeypatch.setenv("CWCLI_SHARED_MARKER", str(marker))
        uid = os.getuid()

        ui_cmd._write_state(8765, "secret-token")

        assert ui_cmd.pid_file().name == f"ui-{uid}.pid"
        assert ui_cmd.log_file().name == f"ui-{uid}.log"
        assert ui_cmd.state_file().name == f"ui-{uid}.json"
        assert stat.S_IMODE(ui_cmd.state_file().stat().st_mode) == 0o600

    def test_a_frozen_build_spawns_its_own_binary(self, home, no_daemon, monkeypatch):
        spawned: dict = {}

        def _popen(argv, **kwargs):
            spawned["argv"] = argv
            _child_up(8765, kwargs["env"][ui_cmd.serve_cmd.TOKEN_ENV])

        monkeypatch.setattr(ui_cmd, "_is_frozen", lambda: True)
        monkeypatch.setattr(ui_cmd.sys, "argv", ["C:/tools/cwcli.exe", "ui"])
        monkeypatch.setattr(ui_cmd.subprocess, "Popen", _popen)
        monkeypatch.setattr(ui_cmd, "_meta_answers", lambda port: True)
        monkeypatch.setattr(ui_cmd, "mint_code", lambda port, token: "code")

        result = runner.invoke(root_app, ["ui", "--no-browser"])

        assert result.exit_code == 0, result.output
        assert spawned["argv"] == [
            "C:/tools/cwcli.exe",
            "ui",
            "--foreground",
            "--no-browser",
            "--port",
            "8765",
        ]

    def test_a_concurrent_launch_mints_with_the_token_of_the_daemon_that_won(
        self, home, no_daemon, monkeypatch
    ):
        monkeypatch.setattr(
            ui_cmd.subprocess, "Popen", lambda argv, **kw: _child_up(8765, "winner")
        )
        monkeypatch.setattr(ui_cmd, "_meta_answers", lambda port: True)
        minted: list = []
        monkeypatch.setattr(ui_cmd, "mint_code", lambda port, token: minted.append(token) or "c")

        result = runner.invoke(root_app, ["ui", "--no-browser"])

        assert result.exit_code == 0, result.output
        assert minted == ["winner"]

    def test_a_launch_that_loses_the_bind_reuses_the_winners_state_instead_of_erasing_it(
        self, home, no_daemon, monkeypatch
    ):
        _child_up(8765, "winner")
        monkeypatch.setattr(ui_cmd.subprocess, "Popen", lambda argv, **kw: None)
        monkeypatch.setattr(ui_cmd, "_meta_answers", lambda port: True)
        minted: list = []
        monkeypatch.setattr(ui_cmd, "mint_code", lambda port, token: minted.append(token) or "c")

        result = runner.invoke(root_app, ["ui", "--no-browser"])

        assert result.exit_code == 0, result.output
        assert minted == ["winner"]
        assert ui_cmd._read_state() == {
            "port": 8765,
            "token": "winner",
            "build": ui_cmd.install_fingerprint(),
        }
        assert ui_cmd.pid_file().exists()

    def test_a_stale_state_file_without_a_live_pid_is_not_taken_as_ready(
        self, home, no_daemon, monkeypatch
    ):
        ui_cmd._write_state(8765, "stale")
        monkeypatch.setattr(ui_cmd.subprocess, "Popen", lambda argv, **kw: None)
        monkeypatch.setattr(ui_cmd, "_meta_answers", lambda port: True)
        monkeypatch.setattr(ui_cmd, "mint_code", lambda port, token: pytest.fail("not ready"))
        monkeypatch.setattr(ui_cmd, "_READY_TIMEOUT_S", 0.3)

        result = runner.invoke(root_app, ["ui", "--no-browser"])

        assert result.exit_code == 1
        assert "did not come up" in result.output

    @pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
    def test_the_daemon_log_is_private_to_the_user(self, home, no_daemon, monkeypatch):
        ui_cmd.run_dir().mkdir(parents=True)
        ui_cmd.log_file().write_text("old\n")
        ui_cmd.log_file().chmod(0o644)
        monkeypatch.setattr(ui_cmd.subprocess, "Popen", lambda argv, **kw: _child_up(8765, "t"))
        monkeypatch.setattr(ui_cmd, "_meta_answers", lambda port: True)
        monkeypatch.setattr(ui_cmd, "mint_code", lambda port, token: "c")

        result = runner.invoke(root_app, ["ui", "--no-browser"])

        assert result.exit_code == 0, result.output
        assert stat.S_IMODE(ui_cmd.log_file().stat().st_mode) == 0o600
        assert ui_cmd.log_file().read_text() == "old\n"


class TestReuse:
    @pytest.fixture
    def live(self, home, monkeypatch):
        ui_cmd._write_state(8766, "live-token")
        monkeypatch.setattr(ui_cmd.daemon_identity, "is_running", lambda pid_file: True)
        monkeypatch.setattr(
            ui_cmd.subprocess, "Popen", lambda *a, **kw: pytest.fail("must reuse, not spawn")
        )

    def test_a_live_daemon_is_reused_and_a_fresh_code_minted(self, live, monkeypatch):
        minted: dict = {}
        monkeypatch.setattr(
            ui_cmd, "mint_code", lambda port, token: minted.update(port=port, token=token) or "c2"
        )

        result = runner.invoke(root_app, ["ui", "--no-browser"])

        assert result.exit_code == 0, result.output
        assert "already running" in result.output
        assert minted == {"port": 8766, "token": "live-token"}
        assert "http://127.0.0.1:8766/#login=c2" in result.output

    def test_a_different_explicit_port_is_refused_while_one_runs(self, live, monkeypatch):
        monkeypatch.setattr(ui_cmd, "mint_code", lambda port, token: pytest.fail("no mint"))

        result = runner.invoke(root_app, ["ui", "--no-browser", "--port", "9999"])

        assert result.exit_code == 1
        assert "already running on port 8766" in result.output
        assert "--stop" in result.output

    def test_a_stale_pid_file_is_cleared_and_a_new_daemon_started(self, home, monkeypatch):
        ui_cmd._write_state(8766, "stale")
        monkeypatch.setattr(ui_cmd.daemon_identity, "is_running", lambda pid_file: False)
        spawned: list = []
        monkeypatch.setattr(
            ui_cmd.subprocess,
            "Popen",
            lambda argv, **kw: spawned.append(argv) or _child_up(8765, "fresh"),
        )
        monkeypatch.setattr(ui_cmd, "_meta_answers", lambda port: True)
        monkeypatch.setattr(ui_cmd, "mint_code", lambda port, token: "c3")

        result = runner.invoke(root_app, ["ui", "--no-browser"])

        assert result.exit_code == 0, result.output
        assert spawned, "a stale record must not be mistaken for a live daemon"
        assert ui_cmd._read_state()["token"] == "fresh"


class TestStop:
    def test_stop_signals_the_recorded_pid_and_clears_the_files(self, home, monkeypatch):
        ui_cmd._write_state(8765, "t")
        ui_cmd.pid_file().parent.mkdir(parents=True, exist_ok=True)
        ui_cmd.pid_file().write_text("4242")
        monkeypatch.setattr(ui_cmd.daemon_identity, "is_running", lambda pid_file: True)
        killed: list = []
        monkeypatch.setattr(ui_cmd.os, "kill", lambda pid, sig: killed.append((pid, sig)))
        monkeypatch.setattr(ui_cmd.daemon_identity, "pid_alive", lambda pid: False)

        result = runner.invoke(root_app, ["ui", "--stop"])

        assert result.exit_code == 0, result.output
        assert killed == [(4242, ui_cmd.signal.SIGTERM)]
        assert not ui_cmd.state_file().exists()
        assert not ui_cmd.pid_file().exists()
        assert "stopped" in result.output

    def test_stop_when_nothing_runs_is_a_clean_no_op(self, home, monkeypatch):
        monkeypatch.setattr(ui_cmd.daemon_identity, "is_running", lambda pid_file: False)
        monkeypatch.setattr(ui_cmd.os, "kill", lambda pid, sig: pytest.fail("nothing to kill"))

        result = runner.invoke(root_app, ["ui", "--stop"])

        assert result.exit_code == 0
        assert "not running" in result.output

    def test_stop_and_foreground_are_mutually_exclusive(self, home):
        result = runner.invoke(root_app, ["ui", "--stop", "--foreground"])
        assert result.exit_code == 2


class TestForeground:
    def test_runs_the_daemon_in_process_with_pid_and_state_files_then_clears_them(
        self, home, monkeypatch
    ):
        seen: dict = {}

        def _run(host, port, interval, *, env_token, on_ready=None):
            seen.update(host=host, port=port, interval=interval, token=env_token)
            assert not ui_cmd.pid_file().exists(), "nothing is recorded before the bind"
            on_ready("http://127.0.0.1:8765/#login=x")
            seen["pid_file_during"] = ui_cmd.pid_file().read_text().splitlines()[0]
            seen["state_during"] = ui_cmd._read_state()

        monkeypatch.setattr(ui_cmd.serve_cmd, "run_daemon", _run)
        opened: list = []
        monkeypatch.setattr(ui_cmd.webbrowser, "open", lambda url: opened.append(url) or True)

        result = runner.invoke(root_app, ["ui", "--foreground", "--no-browser"])

        assert result.exit_code == 0, result.output
        assert seen["host"] == "127.0.0.1" and seen["port"] == 8765
        assert seen["pid_file_during"] == str(os.getpid())
        assert seen["state_during"] == {
            "port": 8765,
            "token": seen["token"],
            "build": ui_cmd.install_fingerprint(),
        }
        assert opened == [], "--no-browser"
        # Torn down on exit so a later `cwcli ui` does not reuse a dead record.
        assert not ui_cmd.pid_file().exists() and not ui_cmd.state_file().exists()

    def test_a_configured_token_is_honoured_instead_of_a_generated_one(self, home, monkeypatch):
        monkeypatch.setenv("CWCLI_SERVE_TOKEN", "operator-token")
        seen: dict = {}
        monkeypatch.setattr(
            ui_cmd.serve_cmd,
            "run_daemon",
            lambda host, port, interval, *, env_token, on_ready=None: seen.update(token=env_token),
        )

        result = runner.invoke(root_app, ["ui", "--foreground", "--no-browser"])

        assert result.exit_code == 0, result.output
        assert seen["token"] == "operator-token"

    def test_a_running_detached_daemon_is_refused_and_its_record_kept(self, home, monkeypatch):
        ui_cmd._write_state(8765, "detached-token")
        ui_cmd.pid_file().write_text("4242")
        monkeypatch.setattr(ui_cmd.daemon_identity, "is_running", lambda pid_file: True)
        monkeypatch.setattr(
            ui_cmd.serve_cmd, "run_daemon", lambda *a, **kw: pytest.fail("must not start")
        )

        result = runner.invoke(root_app, ["ui", "--foreground", "--no-browser"])

        assert result.exit_code == 1
        assert "--stop" in result.output
        assert ui_cmd.pid_file().read_text() == "4242"
        assert ui_cmd._read_state() == {
            "port": 8765,
            "token": "detached-token",
            "build": ui_cmd.install_fingerprint(),
        }

    def test_a_failed_bind_leaves_another_daemons_record_alone(self, home, monkeypatch):
        ui_cmd._write_state(8765, "other-token")
        ui_cmd.pid_file().write_text("4242")
        monkeypatch.setattr(ui_cmd.daemon_identity, "is_running", lambda pid_file: False)

        def _bind_fails(*a, **kw):
            raise ui_cmd.typer.Exit(code=1)

        monkeypatch.setattr(ui_cmd.serve_cmd, "run_daemon", _bind_fails)

        result = runner.invoke(root_app, ["ui", "--foreground", "--no-browser"])

        assert result.exit_code == 1
        assert ui_cmd.pid_file().read_text() == "4242"
        assert ui_cmd._read_state() == {
            "port": 8765,
            "token": "other-token",
            "build": ui_cmd.install_fingerprint(),
        }


_SURVIVOR = """
import sys
from caffeinated_whale_cli.commands import serve
from caffeinated_whale_cli.core import fleet
from caffeinated_whale_cli.core.envelope import Result, Status
fleet.list_instances = lambda: Result(status=Status.OK, data=[])
fleet.event_loop = fleet.probe_loop = lambda *a, **kw: None
serve.KEEPALIVE_S = 0.1
from caffeinated_whale_cli.main import cli
sys.argv = ["cwcli", "ui", "--foreground", "--no-browser", "--port", sys.argv[1]]
cli()
"""


@pytest.mark.skipif(sys.platform == "win32", reason="SIGPIPE is POSIX-only")
def test_a_browser_dropping_its_stream_does_not_kill_the_foreground_daemon(home):
    """``main.cli`` resets SIGPIPE to SIG_DFL for ``cwcli ls | head``; in the
    long-lived daemon that let a closed tab's next keepalive write kill the whole
    process. Driven through the real ``cli()`` entry the detached spawn uses."""
    import socket
    import subprocess
    import time
    import urllib.request

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "CWCLI_SERVE_TOKEN": "sigpipe-token"}
    proc = subprocess.Popen(
        [sys.executable, "-c", _SURVIVOR, str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def _meta():
        try:
            return urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/meta", timeout=1).status
        except OSError:
            return None

    try:
        deadline = time.monotonic() + 8
        while _meta() != 200:
            assert proc.poll() is None and time.monotonic() < deadline, "daemon never came up"
            time.sleep(0.05)
        client = socket.create_connection(("127.0.0.1", port))
        client.sendall(
            f"GET /api/v1/stream HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            "Authorization: Bearer sigpipe-token\r\n\r\n".encode()
        )
        client.settimeout(2)
        seen = b""
        while b"event: snapshot" not in seen:
            chunk = client.recv(65536)
            assert chunk, "stream closed before its snapshot"
            seen += chunk
        client.settimeout(0.2)
        try:
            client.recv(65536)  # take a keepalive, then hang up like a closed tab
        except socket.timeout:
            pass
        client.close()
        time.sleep(1.0)  # ten keepalives: the second write after the RST raised SIGPIPE

        assert proc.poll() is None, f"daemon died (returncode {proc.returncode})"
        assert _meta() == 200
    finally:
        proc.terminate()
        proc.wait(5)


def test_a_frozen_exe_reenters_the_job_worker_through_the_hidden_job_flag(home, monkeypatch):
    """A standalone exe has no ``python -m caffeinated_whale_cli.jobs``; ``ui --job`` is its route."""
    from caffeinated_whale_cli import jobs

    seen: list[str] = []
    monkeypatch.setattr(jobs, "run_job", lambda job_id: seen.append(job_id) or 3)

    result = runner.invoke(root_app, ["ui", "--job", "20261024120000-abcdef123456"])

    assert seen == ["20261024120000-abcdef123456"]
    assert result.exit_code == 3
    assert "--job" not in runner.invoke(root_app, ["ui", "--help"]).output


class TestStaleDaemon:
    """After an upgrade the running daemon predates the installed code; `cwcli ui` replaces it."""

    @pytest.fixture(autouse=True)
    def _installed(self, monkeypatch):
        monkeypatch.setattr(ui_cmd, "install_fingerprint", lambda: "3.4.0:current")

    def _running(self, monkeypatch, state):
        monkeypatch.setattr(ui_cmd.daemon_identity, "is_running", lambda pid_file: True)
        monkeypatch.setattr(ui_cmd, "_read_state", lambda: state)
        monkeypatch.setattr(ui_cmd, "_recover_daemons", lambda: None)
        monkeypatch.setattr(ui_cmd, "mint_code", lambda port, token: "code")

    def test_a_daemon_from_before_the_build_check_is_stale(self, monkeypatch):
        assert ui_cmd._is_stale({"port": 1, "token": "t"})

    def test_a_daemon_on_the_installed_build_is_current(self):
        assert not ui_cmd._is_stale(
            {"port": 1, "token": "t", "build": ui_cmd.install_fingerprint()}
        )

    def test_a_changed_build_is_stale_but_a_mid_reinstall_reading_is_not(self, monkeypatch):
        state = {"port": 1, "token": "t", "build": "3.4.0:aaaa"}
        monkeypatch.setattr(ui_cmd, "install_fingerprint", lambda: "3.4.0:bbbb")
        assert ui_cmd._is_stale(state)
        monkeypatch.setattr(ui_cmd, "install_fingerprint", lambda: "3.4.0:?")
        assert not ui_cmd._is_stale(state)

    def test_cwcli_ui_stops_a_stale_daemon_and_starts_a_fresh_one(self, home, monkeypatch):
        calls = []
        self._running(monkeypatch, {"port": 8765, "token": "old"})
        monkeypatch.setattr(ui_cmd, "_stop", lambda: calls.append("stop"))
        monkeypatch.setattr(
            ui_cmd, "_spawn_detached", lambda port, token, session_id=None: calls.append("spawn")
        )
        monkeypatch.setattr(ui_cmd, "_meta_answers", lambda port: True)
        monkeypatch.setattr(ui_cmd.daemon_identity, "get_pid", lambda pf: 1)
        states = iter([{"port": 8765, "token": "old"}, {"port": 8765, "token": "new"}])
        monkeypatch.setattr(ui_cmd, "_read_state", lambda: next(states))
        result = runner.invoke(root_app, ["ui", "--no-browser"])
        assert result.exit_code == 0, result.output
        assert calls == ["stop", "spawn"]
        assert "was updated" in result.output

    def test_a_current_daemon_is_reused(self, home, monkeypatch):
        calls = []
        self._running(
            monkeypatch,
            {"port": 8765, "token": "t", "build": ui_cmd.install_fingerprint()},
        )
        monkeypatch.setattr(ui_cmd, "_stop", lambda: calls.append("stop"))
        monkeypatch.setattr(ui_cmd, "_spawn_detached", lambda *a, **k: calls.append("spawn"))
        result = runner.invoke(root_app, ["ui", "--no-browser"])
        assert result.exit_code == 0, result.output
        assert calls == []
