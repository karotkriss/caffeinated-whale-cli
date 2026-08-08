"""``core.cred_bridge`` unit tests: the desired-state verbs, plain-data DTOs, and
core silence, against a tmp config and an inert faked daemon.

Mirrors ``test_core_auto_inspect`` - a second config-gated daemon, the same
validate/persist/fuse discipline. The exit-code-driving fields (report tokens,
never ``Result.status``) and the opt-in start-refusal are the load-bearing pins.
"""

from dataclasses import asdict
from types import SimpleNamespace

import pytest

from caffeinated_whale_cli.core import cred_bridge as core_cred
from caffeinated_whale_cli.core.envelope import Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from caffeinated_whale_cli.utils import config_utils
from caffeinated_whale_cli.utils import cred_daemon as daemon


def _assert_plain(data):
    def walk(value):
        if isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)
        else:
            assert value is None or isinstance(value, (str, int, float, bool)), repr(value)

    walk(asdict(data))


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config_utils, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config_utils, "CONFIG_FILE", cfg_dir / "config.toml")
    monkeypatch.setattr(daemon, "LOG_FILE", tmp_path / "run" / "credbridge.log")

    state = SimpleNamespace(
        running=False,
        pid=None,
        enabled=False,
        calls={"start": 0, "stop": 0, "ensure_running": 0, "disable_artifacts": 0},
    )

    def _start():
        state.calls["start"] += 1
        state.running = True
        state.pid = 5151

    def _stop():
        state.calls["stop"] += 1
        state.running = False
        state.pid = None

    monkeypatch.setattr(daemon, "is_running", lambda: state.running)
    monkeypatch.setattr(daemon, "get_pid", lambda: state.pid)
    monkeypatch.setattr(daemon, "is_enabled", lambda: state.enabled)
    monkeypatch.setattr(daemon, "start_daemon", _start)
    monkeypatch.setattr(daemon, "stop_daemon", _stop)
    monkeypatch.setattr(daemon, "transport", lambda: "unix")
    monkeypatch.setattr(daemon, "registered_projects", lambda: [])
    monkeypatch.setattr(daemon, "recent_audit", lambda: [])
    monkeypatch.setattr(
        daemon,
        "ensure_running_instances",
        lambda: state.calls.__setitem__("ensure_running", state.calls["ensure_running"] + 1) or 0,
    )
    monkeypatch.setattr(
        daemon,
        "disable_bridge_artifacts",
        lambda: state.calls.__setitem__("disable_artifacts", state.calls["disable_artifacts"] + 1),
    )
    # `enabled` follows the config the verbs write, so is_enabled tracks it.

    def _set_enabled(val):
        state.enabled = val

    monkeypatch.setattr(config_utils, "set_cred_bridge_enabled", _set_enabled)
    # gh present so enable() adds no missing-tool warning unless a test removes it.
    monkeypatch.setattr(core_cred.shutil, "which", lambda tool: "/usr/bin/" + tool)
    return state


class TestEnable:
    def test_enables_and_starts_in_one_call(self, cfg, capsys):
        result = core_cred.enable()
        assert result.status is Status.OK
        assert "config.enabled" in result.data.actions
        assert "daemon.started" in result.data.actions
        assert cfg.calls["start"] == 1
        assert cfg.enabled is True
        assert result.data.state.daemon_running is True
        assert result.data.state.daemon_pid == 5151
        _assert_plain(result.data)
        assert capsys.readouterr() == ("", "")  # the core prints nothing

    def test_idempotent_rerun_reports_already_running(self, cfg):
        core_cred.enable()
        result = core_cred.enable()
        assert "daemon.already_running" in result.data.actions
        assert cfg.calls["start"] == 1

    def test_missing_host_tools_is_a_warning_not_a_refusal(self, cfg, monkeypatch):
        monkeypatch.setattr(core_cred.shutil, "which", lambda tool: None)
        result = core_cred.enable()
        assert result.status is Status.WARNING
        assert [w.code for w in result.warnings] == ["cred_bridge.no_host_tool"]
        assert cfg.enabled is True  # still enabled - degrade, don't refuse
        assert cfg.calls["start"] == 1

    def test_enable_ensures_running_instances(self, cfg):
        core_cred.enable()
        assert cfg.calls["ensure_running"] == 1

    def test_daemon_start_failure_is_a_typed_internal_error(self, cfg, monkeypatch):
        def _boom():
            raise RuntimeError("no fork for you")

        monkeypatch.setattr(daemon, "start_daemon", _boom)
        with pytest.raises(CwcliError) as exc:
            core_cred.enable()
        assert exc.value.kind is ErrorKind.INTERNAL
        assert exc.value.code == "daemon.start_failed"


class TestDisable:
    def test_stops_clears_artifacts_and_disables(self, cfg, capsys):
        core_cred.enable()
        result = core_cred.disable()
        assert result.data.actions == ["daemon.stopped", "artifacts.cleared", "config.disabled"]
        assert cfg.enabled is False
        assert cfg.calls["disable_artifacts"] == 1
        assert capsys.readouterr() == ("", "")

    def test_disable_when_nothing_is_up_still_clears_and_disables(self, cfg):
        result = core_cred.disable()
        assert result.status is Status.OK
        assert result.data.actions == ["artifacts.cleared", "config.disabled"]
        assert cfg.calls["stop"] == 0


class TestStart:
    def test_refuses_when_disabled(self, cfg):
        with pytest.raises(CwcliError) as exc:
            core_cred.start()
        assert exc.value.kind is ErrorKind.USAGE
        assert exc.value.code == "cred_bridge.not_enabled"
        assert cfg.calls["start"] == 0

    def test_starts_when_enabled(self, cfg):
        cfg.enabled = True
        result = core_cred.start()
        assert result.data.actions == ["daemon.started"]
        assert cfg.calls["start"] == 1

    def test_already_running_is_a_noop(self, cfg):
        cfg.enabled = True
        cfg.running = True
        result = core_cred.start()
        assert result.data.actions == ["daemon.already_running"]
        assert cfg.calls["start"] == 0


class TestStop:
    def test_stops_a_running_daemon_only(self, cfg):
        cfg.enabled = True
        cfg.running = True
        result = core_cred.stop()
        assert result.data.actions == ["daemon.stopped"]
        assert cfg.enabled is True  # config untouched - it returns on next open/start

    def test_stop_when_not_running_is_idempotent(self, cfg):
        result = core_cred.stop()
        assert result.status is Status.OK
        assert result.data.actions == ["daemon.not_running"]
        assert cfg.calls["stop"] == 0


class TestStatus:
    def test_reads_state_without_touching_anything(self, cfg, capsys):
        cfg.running = True
        cfg.pid = 5151
        state = core_cred.status().data
        assert state.daemon_running is True
        assert state.daemon_pid == 5151
        assert state.transport == "unix"
        assert cfg.calls == {"start": 0, "stop": 0, "ensure_running": 0, "disable_artifacts": 0}
        _assert_plain(state)
        assert capsys.readouterr() == ("", "")

    def test_pid_is_none_when_stopped(self, cfg):
        assert core_cred.status().data.daemon_pid is None
