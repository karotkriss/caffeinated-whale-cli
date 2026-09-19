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
from caffeinated_whale_cli.utils import config_utils, startup
from caffeinated_whale_cli.utils import cred_daemon as daemon


def _managed(cfg, *names):
    """Make each name a cwcli-managed instance (a PROJECTS_DIR/{name} dir)."""
    for name in names:
        (config_utils.PROJECTS_DIR / name).mkdir(parents=True, exist_ok=True)


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
    monkeypatch.setattr(config_utils, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(daemon, "LOG_FILE", tmp_path / "run" / "credbridge.log")

    state = SimpleNamespace(
        running=False,
        pid=None,
        enabled=False,
        installed=False,
        calls={
            "start": 0,
            "stop": 0,
            "ensure_running": 0,
            "disable_artifacts": 0,
            "install": 0,
            "uninstall": 0,
        },
    )

    def _install(unit=None):
        state.calls["install"] += 1
        state.installed = True
        return (True, None)

    def _uninstall(unit=None):
        state.calls["uninstall"] += 1
        state.installed = False
        return (True, None)

    monkeypatch.setattr(startup, "is_startup_installed", lambda unit=None: state.installed)
    monkeypatch.setattr(startup, "install_startup", _install)
    monkeypatch.setattr(startup, "uninstall_startup", _uninstall)

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
    def _ensure_running(only=None, on_plan=None):
        state.calls["ensure_running"] += 1
        state.ensure_only = only
        state.ensure_on_plan = on_plan
        return sorted(only) if only else []

    monkeypatch.setattr(daemon, "ensure_running_instances", _ensure_running)
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

    def test_unscoped_enable_sweeps_every_managed_instance(self, cfg):
        """No --project means the sweep is unrestricted (only=None)."""
        core_cred.enable()
        assert cfg.ensure_only is None

    def test_enable_reports_the_instances_it_wired(self, cfg, monkeypatch):
        # The mock returns the `only` set as the ensured list; drive it with a scope.
        _managed(cfg, "alpha", "beta")
        monkeypatch.setattr(core_cred, "get_frappe_container", lambda name: object())
        result = core_cred.enable(projects=["alpha", "beta"])
        assert sorted(result.data.ensured_projects) == ["alpha", "beta"]


class TestScopedEnable:
    """`enable --project` scopes the sweep and validates each name up front."""

    def test_scopes_the_sweep_to_the_named_projects(self, cfg, monkeypatch):
        _managed(cfg, "alpha")
        monkeypatch.setattr(core_cred, "get_frappe_container", lambda name: object())
        core_cred.enable(projects=["alpha"])
        assert cfg.ensure_only == {"alpha"}

    def test_validates_every_named_project_with_the_canonical_resolver(self, cfg, monkeypatch):
        _managed(cfg, "alpha", "beta")
        seen: list[str] = []
        monkeypatch.setattr(
            core_cred, "get_frappe_container", lambda name: seen.append(name) or object()
        )
        core_cred.enable(projects=["alpha", "beta"])
        assert seen == ["alpha", "beta"]

    def test_an_explicitly_named_unmanaged_project_is_refused(self, cfg, monkeypatch):
        # The container resolves (any running frappe) but cwcli does not manage it:
        # no PROJECTS_DIR/foreign dir exists, so the operator's named target is
        # refused loudly rather than silently dropped by the sweep filter.
        monkeypatch.setattr(core_cred, "get_frappe_container", lambda name: object())
        with pytest.raises(CwcliError) as exc:
            core_cred.enable(projects=["foreign"])
        assert exc.value.kind is ErrorKind.NOT_FOUND
        assert exc.value.code == "project.not_managed"
        assert "foreign" in exc.value.message
        # Fail-fast: nothing enabled, started, or swept.
        assert cfg.enabled is False
        assert cfg.calls["start"] == 0
        assert cfg.calls["ensure_running"] == 0

    def test_a_managed_named_project_still_succeeds(self, cfg, monkeypatch):
        _managed(cfg, "mine")
        monkeypatch.setattr(core_cred, "get_frappe_container", lambda name: object())
        result = core_cred.enable(projects=["mine"])
        assert cfg.enabled is True
        assert cfg.ensure_only == {"mine"}
        assert result.data.ensured_projects == ["mine"]

    def test_a_missing_project_fails_fast_with_no_side_effects(self, cfg, monkeypatch):
        def _resolve(name):
            raise CwcliError(ErrorKind.NOT_FOUND, "project.not_found", f"Project '{name}' not found.")

        monkeypatch.setattr(core_cred, "get_frappe_container", _resolve)
        with pytest.raises(CwcliError) as exc:
            core_cred.enable(projects=["ghost"])
        assert exc.value.kind is ErrorKind.NOT_FOUND
        assert exc.value.code == "project.not_found"
        # Validation precedes every mutation: nothing was enabled or started.
        assert cfg.enabled is False
        assert cfg.calls["start"] == 0
        assert cfg.calls["ensure_running"] == 0

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
        assert cfg.calls == {
            "start": 0,
            "stop": 0,
            "ensure_running": 0,
            "disable_artifacts": 0,
            "install": 0,
            "uninstall": 0,
        }
        _assert_plain(state)
        assert capsys.readouterr() == ("", "")

    def test_pid_is_none_when_stopped(self, cfg):
        assert core_cred.status().data.daemon_pid is None

    def test_state_reports_boot_and_allowlist_stores(self, cfg):
        state = core_cred.status().data
        assert state.startup_enabled is False
        assert state.boot_installed is False
        assert state.allowed_hosts == ["github.com", "gitlab.com"]


class TestBootPersistence:
    """Phase 3: the opt-in boot unit, the ``core.auto_inspect`` shape exactly."""

    def test_enable_at_boot_installs_the_cred_bridge_unit(self, cfg):
        result = core_cred.enable(at_boot=True)
        assert "hook.installed" in result.data.actions
        assert cfg.calls["install"] == 1
        assert result.data.state.startup_enabled is True
        assert result.data.state.boot_installed is True
        saved = config_utils.load_config()["cred_bridge"]
        assert saved["startup_enabled"] is True

    def test_enable_without_the_flag_leaves_the_unit_untouched(self, cfg):
        core_cred.enable(at_boot=True)
        result = core_cred.enable()
        assert "hook.installed" not in result.data.actions
        assert "hook.removed" not in result.data.actions
        assert cfg.calls["install"] == 1  # only the first, flagged enable
        assert cfg.calls["uninstall"] == 0
        assert result.data.state.startup_enabled is True  # the config store persists

    def test_enable_no_startup_removes_the_unit(self, cfg):
        core_cred.enable(at_boot=True)
        result = core_cred.enable(at_boot=False)
        assert "hook.removed" in result.data.actions
        assert cfg.calls["uninstall"] == 1
        assert config_utils.load_config()["cred_bridge"]["startup_enabled"] is False

    def test_disable_tears_the_unit_down_too(self, cfg):
        core_cred.enable(at_boot=True)
        result = core_cred.disable()
        assert "hook.removed" in result.data.actions
        assert cfg.calls["uninstall"] == 1
        assert config_utils.load_config()["cred_bridge"]["startup_enabled"] is False
        assert result.data.state.boot_installed is False

    def test_boot_unit_execs_the_guarded_start_verb(self):
        """The unit's argv is `cwcli config cred-bridge start`, whose refuse-when-
        disabled guard keeps a stale unit inert after `disable` - the load-bearing
        property the auto-inspect unit established."""
        assert startup.CRED_BRIDGE.start_args == ("config", "cred-bridge", "start")
        assert startup.CRED_BRIDGE.stop_args == ("config", "cred-bridge", "stop")
        # And the generalization did not move the auto-inspect unit's identity.
        assert startup.AUTO_INSPECT.start_args == ("config", "auto-inspect", "start")
        assert startup.AUTO_INSPECT.service_name == "cwcli-auto-inspect.service"
        assert startup.AUTO_INSPECT.task_name == "CaffeinatedWhaleCliAutoInspect"
        assert startup.AUTO_INSPECT.label == "com.cwcli.auto-inspect"
        assert startup.CRED_BRIDGE.service_name == "cwcli-cred-bridge.service"

    def test_a_failed_install_is_a_warning_never_a_refusal(self, cfg, monkeypatch, capsys):
        monkeypatch.setattr(
            startup,
            "install_startup",
            lambda unit=None: (False, "systemctl enable failed: no session"),
        )
        result = core_cred.enable(at_boot=True)
        assert result.status is Status.WARNING
        warning = next(w for w in result.warnings if w.code == "startup.install_failed")
        # The reason travels IN the structured warning (stated once, in order by the
        # renderer) - not leaked to stderr ahead of "enabled".
        assert "systemctl enable failed: no session" in warning.text
        assert "cwcli config cred-bridge start" in warning.text  # the manual-start verb
        assert capsys.readouterr() == ("", "")  # the core prints nothing
        assert cfg.enabled is True  # the enable itself still landed


class TestProjectsDirHardening:
    """Phase 3: `enable` tightens ~/.cwcli/projects to owner-only (0700)."""

    def test_enable_chmods_the_projects_dir_to_0700(self, cfg, tmp_path):
        projects = tmp_path / "projects"
        projects.mkdir(mode=0o755)
        result = core_cred.enable()
        assert "projects.hardened" in result.data.actions
        assert (projects.stat().st_mode & 0o777) == 0o700

    def test_an_already_hardened_dir_is_a_silent_noop(self, cfg, tmp_path):
        projects = tmp_path / "projects"
        projects.mkdir(mode=0o700)
        result = core_cred.enable()
        assert "projects.hardened" not in result.data.actions

    def test_a_missing_projects_dir_is_left_alone(self, cfg, tmp_path):
        result = core_cred.enable()
        assert "projects.hardened" not in result.data.actions
        assert not (tmp_path / "projects").exists()  # never mkdir'd as a side effect


class TestSharedModeSkipsProjectsHardening:
    """In shared mode ``enable`` must NOT tighten projects/ to 0700 - that tree is
    deliberately group-writable (2770 :cwcli) so every group member reaches the
    instance state; hardening it would lock them out, the failure shared mode
    exists to fix. The socket's own 0660 :cwcli gating is the protection there."""

    def test_harden_is_a_noop_in_shared_mode(self, cfg, monkeypatch):
        import stat as _stat

        monkeypatch.setattr(core_cred.shared_home, "shared_mode", lambda: True)
        projects = config_utils.PROJECTS_DIR
        projects.mkdir(parents=True)
        projects.chmod(0o2770)
        actions: list[str] = []
        core_cred._harden_projects_dir(actions)
        assert actions == []  # nothing hardened
        assert _stat.S_IMODE(projects.stat().st_mode) == 0o2770  # left group-writable

    def test_harden_still_runs_per_user(self, cfg, monkeypatch):
        import stat as _stat

        monkeypatch.setattr(core_cred.shared_home, "shared_mode", lambda: False)
        projects = config_utils.PROJECTS_DIR
        projects.mkdir(parents=True)
        projects.chmod(0o755)
        actions: list[str] = []
        core_cred._harden_projects_dir(actions)
        assert actions == ["projects.hardened"]
        assert _stat.S_IMODE(projects.stat().st_mode) == 0o700
