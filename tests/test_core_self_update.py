"""``core.self_update`` - upgrade the install cwcli runs from, then restart its services.

The upgrade half is driven with a fake upgrade process against a throwaway tool
env (no uv, no network); the service half with real short-lived ``sleep``
processes standing in for daemons, so the pid-file identity checks and the
SIGTERM are real while the start commands and systemctl are faked.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from caffeinated_whale_cli.core import self_update as su
from caffeinated_whale_cli.core import version as core_version
from caffeinated_whale_cli.core.errors import CwcliError
from caffeinated_whale_cli.utils import auto_inspect, cred_daemon, daemon_identity, startup

_REAL_RUN = subprocess.run  # the start commands are faked; spawning the stand-ins is not

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX processes")


def _info(**kw):
    base = dict(
        current="1.0.0",
        latest="1.1.0",
        method="uv",
        upgrade_command=["uv", "tool", "upgrade", core_version.DIST],
        is_outdated=True,
        is_dev=False,
    )
    base.update(kw)
    return core_version.VersionInfo(**base)


@pytest.fixture()
def tool_env(tmp_path, monkeypatch):
    """A uv tool env in a custom tool folder, its receipt naming the bin folder."""
    env = tmp_path / "opt" / "tools" / "caffeinated-whale-cli"
    site = env / "lib" / "site-packages"
    site.mkdir(parents=True)
    (site / "marker.txt").write_text("old")
    (env / "uv-receipt.toml").write_text(
        "[tool]\nentrypoints = [\n"
        f'  {{ name = "cwcli", install-path = "{tmp_path}/usr/bin/cwcli" }},\n]\n'
    )
    (tmp_path / "usr" / "bin").mkdir(parents=True)
    (tmp_path / "usr" / "bin" / "cwcli").symlink_to(env / "bin" / "cwcli")
    for mod in (core_version, su):
        monkeypatch.setattr(mod, "install_root", lambda: env)
    monkeypatch.setattr(su, "_install_dir", lambda: site)
    monkeypatch.setattr(su.shutil, "which", lambda name: f"/usr/bin/{name}")
    return env


class FakeUpgrade:
    """Stands in for the upgrade process: records argv/env, may edit the env."""

    def __init__(self, monkeypatch, *, code=0, lines=("Updated\n",), edit=None):
        self.calls: list[tuple[list[str], dict]] = []
        outer = self

        class Proc:
            def __init__(self, argv, **kw):
                outer.calls.append((argv, kw.get("env") or {}))
                if edit:
                    edit()
                self.stdout = iter(lines)

            def wait(self):
                return code

        monkeypatch.setattr(su.subprocess, "Popen", Proc)


class TestUpgrade:
    def test_uv_is_pointed_at_the_tool_folder_the_install_lives_in(self, tool_env, monkeypatch):
        fake = FakeUpgrade(monkeypatch)
        monkeypatch.setattr(su, "installed_version", lambda: "1.1.0")
        assert su.upgrade(_info()) == "1.1.0"
        argv, env = fake.calls[0]
        assert argv == ["/usr/bin/uv", "tool", "upgrade", core_version.DIST]
        # Not root's (or the caller's) default tool folder: the one this cwcli is in.
        assert env["UV_TOOL_DIR"] == str(tool_env.parent)
        assert env["UV_TOOL_BIN_DIR"] == str(tool_env.parent.parent.parent / "usr" / "bin")
        assert not tool_env.with_name(tool_env.name + ".cwcli-rollback").exists()

    def test_a_failed_upgrade_puts_the_env_back_and_says_why(self, tool_env, monkeypatch):
        marker = tool_env / "lib" / "site-packages" / "marker.txt"
        executable = tool_env.parent.parent.parent / "usr" / "bin" / "cwcli"
        FakeUpgrade(
            monkeypatch,
            code=1,
            lines=("Updated\n", "error: Failed to install executable\n", "  Permission denied\n"),
            # uv replaces the packages and removes the old executable, then fails.
            edit=lambda: (marker.write_text("new"), executable.unlink()),
        )
        monkeypatch.setattr(su, "installed_version", lambda: "1.0.0")
        with pytest.raises(CwcliError) as e:
            su.upgrade(_info())
        assert e.value.code == "self_update.failed"
        assert "Permission denied" in e.value.message
        assert "cwcli 1.0.0 is still installed" in e.value.message
        assert marker.read_text() == "old"  # the half-done upgrade was rolled back
        assert executable.is_symlink()  # and `cwcli` is back on PATH
        assert executable.readlink() == tool_env / "bin" / "cwcli"
        assert not tool_env.with_name(tool_env.name + ".cwcli-rollback").exists()

    def test_a_locked_env_that_cannot_be_moved_aside_is_left_intact(self, tool_env, monkeypatch):
        # Windows locks the running interpreter's files, so the env cannot be renamed.
        marker = tool_env / "lib" / "site-packages" / "marker.txt"
        FakeUpgrade(monkeypatch, code=1, lines=("error: network unreachable\n",))
        monkeypatch.setattr(su, "installed_version", lambda: "1.0.0")
        real_replace = os.replace

        def replace(src, dst):
            if Path(src) == tool_env:
                raise PermissionError("in use")
            real_replace(src, dst)

        real_rmtree = su.shutil.rmtree

        def rmtree(path, ignore_errors=False):
            if Path(path) == tool_env:  # deletes what it can and leaves the locked files
                marker.unlink(missing_ok=True)
                return
            real_rmtree(path, ignore_errors=ignore_errors)

        monkeypatch.setattr(su.os, "replace", replace)
        monkeypatch.setattr(su.shutil, "rmtree", rmtree)
        with pytest.raises(CwcliError) as e:
            su.upgrade(_info())
        assert "cwcli 1.0.0 is still installed" in e.value.message
        assert marker.read_text() == "old"  # nothing in the live env was deleted
        assert tool_env.with_name(tool_env.name + ".cwcli-rollback").is_dir()

    def test_an_interrupted_upgrade_stops_the_upgrader_before_rolling_back(
        self, tool_env, monkeypatch
    ):
        marker = tool_env / "lib" / "site-packages" / "marker.txt"
        events = []

        def lines():
            yield "Updated\n"
            raise KeyboardInterrupt  # a SIGTERM to cwcli alone, unwound by utils/signals

        class Proc:
            def __init__(self, argv, **kw):
                marker.write_text("new")
                self.stdout = lines()

            def terminate(self):
                events.append(("terminate", marker.read_text()))

            def wait(self, timeout=None):
                events.append(("wait", marker.read_text()))
                return -15

        monkeypatch.setattr(su.subprocess, "Popen", Proc)
        with pytest.raises(KeyboardInterrupt):
            su.upgrade(_info())
        assert events[:2] == [("terminate", "new"), ("wait", "new")]  # stopped first
        assert marker.read_text() == "old"  # then rolled back

    def test_an_upgrade_that_changed_nothing_is_not_reported_as_done(self, tool_env, monkeypatch):
        # The sudo bug: uv upgraded a different install and exited 0.
        FakeUpgrade(monkeypatch, lines=("Nothing to upgrade\n",))
        monkeypatch.setattr(su, "installed_version", lambda: "1.0.0")
        with pytest.raises(CwcliError) as e:
            su.upgrade(_info())
        assert e.value.code == "self_update.unchanged"
        assert "did not change to 1.1.0" in e.value.message

    def test_an_unwritable_install_is_refused_before_anything_runs(self, tool_env, monkeypatch):
        fake = FakeUpgrade(monkeypatch)
        monkeypatch.setattr(su.os, "access", lambda path, mode: False)
        with pytest.raises(CwcliError) as e:
            su.upgrade(_info())
        assert e.value.code == "self_update.not_writable"
        assert e.value.hint == "run `sudo cwcli self-update`"
        assert fake.calls == []

    def test_pip_runs_its_own_command_without_a_backup(self, tool_env, monkeypatch):
        argv = [sys.executable, "-m", "pip", "install", "--upgrade", core_version.DIST]
        fake = FakeUpgrade(monkeypatch)
        monkeypatch.setattr(su, "installed_version", lambda: "1.1.0")
        assert su.upgrade(_info(method="pip", upgrade_command=argv)) == "1.1.0"
        assert fake.calls[0][0] == argv
        assert "UV_TOOL_DIR" not in fake.calls[0][1]


# ------------------------------------------------------------------- services


@pytest.fixture()
def daemons(tmp_path, monkeypatch):
    """Real sleep processes recorded in pid files the way cwcli's daemons do."""
    monkeypatch.setenv("CWCLI_HOME", str(tmp_path / "home"))
    run = tmp_path / "home" / "run"
    run.mkdir(parents=True)
    monkeypatch.setattr(auto_inspect, "PID_FILE", run / "auto-inspect.pid")
    monkeypatch.setattr(cred_daemon, "PID_FILE", run / "credbridge.pid")
    monkeypatch.setattr(su, "_unit_running", lambda unit: False)
    pids: list[int] = []

    def spawn(pid_file: Path) -> int:
        # Backgrounded by a shell that exits at once, so init reaps it when it is
        # killed - a child of the test would linger as a zombie that reads alive.
        out = _REAL_RUN(
            ["sh", "-c", "sleep 60 >/dev/null 2>&1 & echo $!"], capture_output=True, text=True
        )
        pid = int(out.stdout.strip())
        pids.append(pid)
        pid_file.write_text(f"{pid}\n{daemon_identity.process_start_time(pid)}\n")
        return pid

    yield run, spawn
    for pid in pids:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)


class TestRunningServices:
    def test_finds_each_live_daemon_and_the_web_ui_port(self, daemons):
        run, spawn = daemons
        inspect_pid = spawn(run / "auto-inspect.pid")
        ui_pid = spawn(run / "ui.pid")
        (run / "ui.json").write_text('{"port": 18765, "token": "t"}')
        (run / "credbridge.pid").write_text("999999\n")  # dead: not reported

        found = {s.kind: s for s in su.running_services()}
        assert set(found) == {"auto-inspect", "ui"}
        assert found["auto-inspect"].pid == inspect_pid
        assert found["ui"].pid == ui_pid and found["ui"].port == 18765
        assert "port 18765" in found["ui"].name

    def test_excluded_pids_are_left_out(self, daemons):
        run, spawn = daemons
        ui_pid = spawn(run / "ui.pid")
        assert su.running_services(exclude_pids=frozenset({ui_pid})) == []

    def test_a_running_system_unit_owns_the_credential_bridge(self, daemons, monkeypatch):
        run, spawn = daemons
        spawn(run / "credbridge.pid")
        monkeypatch.setattr(su, "_unit_running", lambda unit: unit is startup.CRED_BRIDGE)
        monkeypatch.setattr(su, "_unit_pid", lambda name: 4242)
        services = su.running_services()
        assert [(s.kind, s.unit) for s in services] == [
            ("cred-bridge", "cwcli-cred-bridge.service")
        ]


class TestRestart:
    def test_a_daemon_is_stopped_and_started_on_the_new_code(self, daemons, monkeypatch):
        run, spawn = daemons
        spawn(run / "auto-inspect.pid")
        (svc,) = su.running_services()
        started = []

        def fake_start(argv, **kw):
            started.append(argv)
            spawn(run / "auto-inspect.pid")  # the new daemon records itself
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(su.subprocess, "run", fake_start)
        (result,) = su.restart([svc], "1.1.0")
        assert result.ok, result.detail
        assert result.pid != svc.pid
        assert not daemon_identity.pid_alive(svc.pid)
        assert started[0][-3:] == ["config", "auto-inspect", "start"]

    def test_a_web_ui_that_answers_with_the_old_version_is_reported(self, daemons, monkeypatch):
        run, spawn = daemons
        spawn(run / "ui.pid")
        (run / "ui.json").write_text('{"port": 18765, "token": "t"}')
        (svc,) = su.running_services()
        calls = []
        monkeypatch.setattr(
            su.subprocess,
            "run",
            lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0),
        )
        monkeypatch.setattr(su, "_ui_version", lambda port: "1.0.0")
        (result,) = su.restart([svc], "1.1.0")
        assert not result.ok
        assert "1.0.0" in result.detail
        assert calls[0][-4:] == ["ui", "--no-browser", "--port", "18765"]

    def test_a_start_failure_is_reported_and_the_rest_still_restart(self, daemons, monkeypatch):
        run, spawn = daemons
        spawn(run / "auto-inspect.pid")
        spawn(run / "ui.pid")
        (run / "ui.json").write_text('{"port": 18765, "token": "t"}')
        services = su.running_services()

        def fake_start(argv, **kw):
            if "auto-inspect" in argv:
                return subprocess.CompletedProcess(argv, 1, "", "Error: Docker is down\n")
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(su.subprocess, "run", fake_start)
        monkeypatch.setattr(su, "_ui_version", lambda port: "1.1.0")
        results = {r.kind: r for r in su.restart(services, "1.1.0")}
        assert not results["auto-inspect"].ok
        assert "Docker is down" in results["auto-inspect"].detail
        assert results["ui"].ok

    def test_a_recycled_pid_is_never_signalled(self, daemons, monkeypatch):
        # The daemon stopped during the upgrade and an unrelated process took its pid.
        run, spawn = daemons
        stranger = spawn(run / "other.pid")
        svc = su.Service(kind="auto-inspect", name="auto-inspect", pid=stranger, started="gone")
        monkeypatch.setattr(su.subprocess, "run", lambda *a, **kw: pytest.fail("restarted"))
        (result,) = su.restart([svc], "1.1.0")
        assert daemon_identity.pid_alive(stranger)
        assert result.ok and "left stopped" in result.detail

    def test_another_users_daemon_needs_sudo(self, daemons, monkeypatch):
        svc = su.Service(kind="ui", name="web UI (bob)", pid=1, owner=12345, port=1)
        monkeypatch.setattr(su.os, "geteuid", lambda: 1000)
        (result,) = su.restart([svc], "1.1.0")
        assert not result.ok and "use sudo" in result.detail

    def test_a_system_unit_is_restarted_by_systemd_and_repaired_first(self, monkeypatch):
        svc = su.Service(kind="scheduler", name="scheduler", pid=10, unit="cwcli-scheduler.service")
        calls = []
        pids = iter([10, 20, 20, 20])

        def fake_systemctl(*args):
            calls.append(args)
            out = "active" if args[0] == "is-active" else ""
            return subprocess.CompletedProcess(args, 0, out, "")

        monkeypatch.setattr(su.os, "geteuid", lambda: 0)
        monkeypatch.setattr(su, "_systemctl", fake_systemctl)
        monkeypatch.setattr(su, "_unit_pid", lambda name: next(pids))
        monkeypatch.setattr(su.time, "sleep", lambda s: None)
        monkeypatch.setattr(startup, "repair_system_unit", lambda unit: True)
        (result,) = su.restart([svc], "1.1.0")
        assert result.ok and result.pid == 20
        assert calls[:2] == [("daemon-reload",), ("restart", "cwcli-scheduler.service")]

    def test_a_system_unit_without_root_is_reported_not_restarted(self, monkeypatch):
        svc = su.Service(kind="scheduler", name="scheduler", pid=10, unit="x.service")
        monkeypatch.setattr(su.os, "geteuid", lambda: 1000)
        (result,) = su.restart([svc], "1.1.0")
        assert not result.ok and "sudo" in result.detail
