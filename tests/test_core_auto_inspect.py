"""``core.auto_inspect`` unit tests: every branch of the fused desired-state
verbs, the F3 validate-before-write structure, plain-data DTOs, and core
silence - all against a tmp_path config and inert daemon/startup fakes."""

from dataclasses import asdict
from types import SimpleNamespace

import pytest

from caffeinated_whale_cli.core import auto_inspect as core_ai
from caffeinated_whale_cli.core.envelope import Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from caffeinated_whale_cli.utils import auto_inspect, config_utils, startup


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
    run_dir = tmp_path / "run"
    monkeypatch.setattr(auto_inspect, "PID_DIR", run_dir)
    monkeypatch.setattr(auto_inspect, "PID_FILE", run_dir / "auto-inspect.pid")
    monkeypatch.setattr(auto_inspect, "LOG_FILE", run_dir / "auto-inspect.log")

    state = SimpleNamespace(
        running=False,
        pid=None,
        installed=False,
        calls={"start": 0, "stop": 0, "install": 0, "uninstall": 0},
        install_result=True,
        uninstall_result=True,
    )

    def _start():
        state.calls["start"] += 1
        state.running = True
        state.pid = 4242

    def _stop():
        state.calls["stop"] += 1
        state.running = False
        state.pid = None

    # The startup fakes take the (defaulted) BootUnit param the real functions
    # grew when the boot-unit machinery was generalized for the cred bridge.
    def _install(unit=None):
        state.calls["install"] += 1
        if state.install_result:
            state.installed = True
        return state.install_result

    def _uninstall(unit=None):
        state.calls["uninstall"] += 1
        if state.uninstall_result:
            state.installed = False
        return state.uninstall_result

    monkeypatch.setattr(auto_inspect, "is_running", lambda: state.running)
    monkeypatch.setattr(auto_inspect, "get_pid", lambda: state.pid)
    monkeypatch.setattr(auto_inspect, "start_daemon", _start)
    monkeypatch.setattr(auto_inspect, "stop_daemon", _stop)
    monkeypatch.setattr(startup, "is_startup_installed", lambda unit=None: state.installed)
    monkeypatch.setattr(startup, "install_startup", _install)
    monkeypatch.setattr(startup, "uninstall_startup", _uninstall)
    return state


def _saved():
    return config_utils.load_config()["auto_inspect"]


class TestEnable:
    def test_enables_and_starts_in_one_call(self, cfg, capsys):
        result = core_ai.enable()
        assert result.status is Status.OK
        assert "config.enabled" in result.data.actions
        assert "daemon.started" in result.data.actions
        assert cfg.calls["start"] == 1
        assert _saved()["enabled"] is True
        assert result.data.state.daemon_running is True
        assert result.data.state.daemon_pid == 4242
        _assert_plain(result.data)
        assert capsys.readouterr() == ("", "")  # the core prints nothing at all

    def test_invalid_interval_raises_before_anything_persists(self, cfg):
        """The F3 fix, structurally: validation precedes the single write."""
        with pytest.raises(CwcliError) as exc:
            core_ai.enable(interval=30)
        assert exc.value.kind is ErrorKind.USAGE
        assert exc.value.code == "interval.too_small"
        assert not config_utils.CONFIG_FILE.exists()
        assert cfg.calls["start"] == 0

    def test_sets_the_interval_in_the_same_write(self, cfg):
        result = core_ai.enable(interval=900)
        assert _saved()["interval"] == 900
        assert "interval.set" in result.data.actions

    def test_idempotent_rerun_reports_already_running(self, cfg):
        core_ai.enable()
        result = core_ai.enable()
        assert "daemon.already_running" in result.data.actions
        assert cfg.calls["start"] == 1

    def test_interval_change_restarts_a_running_daemon(self, cfg):
        core_ai.enable()
        result = core_ai.enable(interval=900)
        assert "daemon.restarted" in result.data.actions
        assert cfg.calls["stop"] == 1
        assert cfg.calls["start"] == 2

    def test_same_interval_rerun_does_not_restart(self, cfg):
        core_ai.enable(interval=900)
        result = core_ai.enable(interval=900)
        assert "daemon.already_running" in result.data.actions
        assert cfg.calls["stop"] == 0

    def test_at_boot_true_installs_the_hook(self, cfg):
        result = core_ai.enable(at_boot=True)
        assert "hook.installed" in result.data.actions
        assert _saved()["startup_enabled"] is True
        assert result.data.state.boot_installed is True

    def test_at_boot_true_with_hook_present_is_a_no_op(self, cfg):
        cfg.installed = True
        result = core_ai.enable(at_boot=True)
        assert cfg.calls["install"] == 0
        assert "hook.installed" not in result.data.actions

    def test_at_boot_false_removes_the_hook_but_keeps_running(self, cfg):
        cfg.installed = True
        result = core_ai.enable(at_boot=False)
        assert "hook.removed" in result.data.actions
        assert _saved()["startup_enabled"] is False
        assert result.data.state.daemon_running is True

    def test_at_boot_none_leaves_the_hook_untouched(self, cfg):
        cfg.installed = True
        result = core_ai.enable()
        assert cfg.calls["uninstall"] == 0
        assert result.data.state.boot_installed is True

    def test_hook_install_failure_is_a_warning_not_an_error(self, cfg):
        cfg.install_result = False
        result = core_ai.enable(at_boot=True)
        assert result.status is Status.WARNING
        assert [w.code for w in result.warnings] == ["startup.install_failed"]
        # The desired state persisted; the DTO's split keeps the miss visible.
        assert result.data.state.startup_enabled is True
        assert result.data.state.boot_installed is False

    def test_unsupported_platform_is_a_warning(self, cfg, monkeypatch):
        # The real util raises OSError from install_startup on an unknown
        # platform (is_startup_installed just returns False there).
        def _boom(unit=None):
            raise OSError("Unsupported platform: plan9")

        monkeypatch.setattr(startup, "install_startup", _boom)
        result = core_ai.enable(at_boot=True)
        assert result.status is Status.WARNING
        assert [w.code for w in result.warnings] == ["startup.sync_failed"]

    def test_daemon_start_failure_is_a_typed_internal_error(self, cfg, monkeypatch):
        def _boom():
            raise RuntimeError("no fork for you")

        monkeypatch.setattr(auto_inspect, "start_daemon", _boom)
        with pytest.raises(CwcliError) as exc:
            core_ai.enable()
        assert exc.value.kind is ErrorKind.INTERNAL
        assert exc.value.code == "daemon.start_failed"


class TestDisable:
    def test_stops_disables_and_removes_the_hook(self, cfg, capsys):
        core_ai.enable(at_boot=True)
        result = core_ai.disable()
        assert result.data.actions == ["daemon.stopped", "config.disabled", "hook.removed"]
        assert _saved()["enabled"] is False
        assert _saved()["startup_enabled"] is False
        assert cfg.installed is False
        assert capsys.readouterr() == ("", "")

    def test_disable_when_nothing_is_up_is_a_clean_success(self, cfg):
        result = core_ai.disable()
        assert result.status is Status.OK
        assert result.data.actions == ["config.disabled"]

    def test_hook_removal_failure_is_a_warning(self, cfg):
        cfg.installed = True
        cfg.uninstall_result = False
        result = core_ai.disable()
        assert result.status is Status.WARNING
        assert [w.code for w in result.warnings] == ["startup.uninstall_failed"]

    def test_daemon_stop_failure_is_a_typed_internal_error(self, cfg, monkeypatch):
        cfg.running = True

        def _boom():
            raise OSError("kill failed")

        monkeypatch.setattr(auto_inspect, "stop_daemon", _boom)
        with pytest.raises(CwcliError) as exc:
            core_ai.disable()
        assert exc.value.code == "daemon.stop_failed"


class TestStop:
    def test_stops_a_running_daemon_only(self, cfg):
        core_ai.enable(at_boot=True)
        result = core_ai.stop()
        assert result.data.actions == ["daemon.stopped"]
        # Config and hook are untouched: it returns at boot if hooked.
        assert _saved()["enabled"] is True
        assert cfg.installed is True

    def test_stop_when_not_running_is_an_idempotent_success(self, cfg):
        result = core_ai.stop()
        assert result.status is Status.OK
        assert result.data.actions == ["daemon.not_running"]
        assert cfg.calls["stop"] == 0


class TestStatus:
    def test_reads_all_three_stores_without_touching_anything(self, cfg, capsys):
        cfg.running = True
        cfg.pid = 4242
        cfg.installed = True
        result = core_ai.status()
        state = result.data
        assert state.enabled is False
        assert state.daemon_running is True
        assert state.daemon_pid == 4242
        assert state.boot_installed is True
        assert cfg.calls == {"start": 0, "stop": 0, "install": 0, "uninstall": 0}
        _assert_plain(state)
        assert capsys.readouterr() == ("", "")

    def test_pid_is_none_when_stopped(self, cfg):
        assert core_ai.status().data.daemon_pid is None


class TestLogTail:
    def test_tails_the_log(self, cfg):
        auto_inspect.PID_DIR.mkdir(parents=True, exist_ok=True)
        auto_inspect.LOG_FILE.write_text("".join(f"line {i}\n" for i in range(10)))
        result = core_ai.log_tail(3)
        assert result.data.content == "line 7\nline 8\nline 9\n"
        assert result.data.lines == 3
        _assert_plain(result.data)

    def test_missing_log_file_is_reported_in_content(self, cfg):
        assert core_ai.log_tail().data.content == "No log file found"

    def test_read_failure_is_a_typed_internal_error(self, cfg, monkeypatch):
        def _boom(lines):
            raise OSError("permission denied")

        monkeypatch.setattr(auto_inspect, "get_log_tail", _boom)
        with pytest.raises(CwcliError) as exc:
            core_ai.log_tail()
        assert exc.value.kind is ErrorKind.INTERNAL
        assert exc.value.code == "logs.read_failed"
