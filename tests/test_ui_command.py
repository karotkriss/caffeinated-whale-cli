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
            ui_cmd._write_state(8765, kwargs["env"][ui_cmd.serve_cmd.TOKEN_ENV])

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
        monkeypatch.setattr(
            ui_cmd.subprocess, "Popen", lambda argv, **kw: ui_cmd._write_state(9000, "t")
        )
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
        assert json.loads(path.read_text()) == {"port": 8765, "token": "secret-token"}
        assert ui_cmd._read_state() == {"port": 8765, "token": "secret-token"}


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
            lambda argv, **kw: spawned.append(argv) or ui_cmd._write_state(8765, "fresh"),
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
        assert seen["state_during"] == {"port": 8765, "token": seen["token"]}
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
        assert ui_cmd._read_state() == {"port": 8765, "token": "detached-token"}

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
        assert ui_cmd._read_state() == {"port": 8765, "token": "other-token"}
