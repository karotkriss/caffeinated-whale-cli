"""``utils.startup`` unit tests - the BootUnit generalization (cred-bridge phase 3).

The boot-unit machinery was auto-inspect-only and hardcoded; it now takes a
:class:`BootUnit` spec with ``AUTO_INSPECT`` as the default parameter. Two
properties are load-bearing and pinned here:

* **the auto-inspect unit is BYTE-STABLE across the refactor** - the generated
  systemd service and LaunchAgent plist match the pre-generalization templates
  exactly (less the systemd ExecStop, dropped because it stopped a running
  daemon, and repaired in place on an installed unit);
* **each unit's identity is independent** - installing the cred-bridge unit
  neither creates nor detects the auto-inspect one, and the cred-bridge unit
  execs ``cwcli config cred-bridge start`` verbatim, whose refuse-when-disabled
  guard keeps a stale unit inert after ``disable``.

All platform side effects (``systemctl``/``launchctl``/``schtasks``) are faked;
``Path.home`` is patched so nothing ever touches the real home directory.
"""

from __future__ import annotations

import configparser
from pathlib import Path
from types import SimpleNamespace

import pytest

from caffeinated_whale_cli.utils import startup

# Captured at import, before the conftest's autouse stub replaces it per test.
_REAL_REPAIR_STARTUP = startup.repair_startup


def _service(path: Path) -> configparser.SectionProxy:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read(path)
    return parser["Service"]


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A sandbox home + inert platform commands + a fixed cwcli path."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(startup, "repair_startup", _REAL_REPAIR_STARTUP)
    monkeypatch.setattr(startup, "get_cwcli_path", lambda: "/usr/local/bin/cwcli")
    calls: list[list[str]] = []

    def _run(cmd, **kwargs):
        calls.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(startup.subprocess, "run", _run)
    return SimpleNamespace(root=tmp_path, calls=calls)


# The exact systemd service the pre-generalization code shipped for auto-inspect,
# less its ExecStop (see TestNoExecStop).
_AUTO_INSPECT_SERVICE = """[Unit]
Description=Caffeinated Whale CLI Auto-Inspect Service
After=network.target

[Service]
Type=forking
ExecStart="/usr/local/bin/cwcli" config auto-inspect start
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
"""

# The exact LaunchAgent plist the pre-generalization code shipped for auto-inspect.
_AUTO_INSPECT_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.cwcli.auto-inspect</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/cwcli</string>
        <string>config</string>
        <string>auto-inspect</string>
        <string>start</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>StandardOutPath</key>
    <string>/tmp/cwcli-auto-inspect.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/cwcli-auto-inspect.err</string>
</dict>
</plist>
"""


class TestLinuxUnits:
    def test_auto_inspect_service_is_byte_stable_across_the_refactor(self, home, monkeypatch):
        monkeypatch.setattr(startup, "get_platform", lambda: "linux")
        assert startup.install_startup() == (True, None)  # the pre-existing no-arg call shape
        service = home.root / ".config" / "systemd" / "user" / "cwcli-auto-inspect.service"
        assert service.read_text() == _AUTO_INSPECT_SERVICE

    def test_cred_bridge_service_execs_the_guarded_start_verb(self, home, monkeypatch):
        monkeypatch.setattr(startup, "get_platform", lambda: "linux")
        assert startup.install_startup(startup.CRED_BRIDGE) == (True, None)
        service = home.root / ".config" / "systemd" / "user" / "cwcli-cred-bridge.service"
        parsed = _service(service)
        assert parsed["Type"] == "forking"
        assert parsed["ExecStart"] == '"/usr/local/bin/cwcli" config cred-bridge start'
        assert "ExecStop" not in parsed
        assert ["systemctl", "--user", "enable", "cwcli-cred-bridge.service"] in home.calls

    def test_unit_identities_are_independent(self, home, monkeypatch):
        monkeypatch.setattr(startup, "get_platform", lambda: "linux")
        startup.install_startup(startup.CRED_BRIDGE)
        assert startup.is_startup_installed(startup.CRED_BRIDGE) is True
        assert startup.is_startup_installed() is False  # auto-inspect untouched
        assert startup.uninstall_startup(startup.CRED_BRIDGE) == (True, None)
        assert startup.is_startup_installed(startup.CRED_BRIDGE) is False


@pytest.mark.parametrize("unit", [startup.AUTO_INSPECT, startup.CRED_BRIDGE])
def test_removing_a_user_unit_never_stops_the_daemon_it_started(home, monkeypatch, unit):
    monkeypatch.setattr(startup, "get_platform", lambda: "linux")
    startup.install_startup(unit)
    home.calls.clear()

    assert startup.uninstall_startup(unit) == (True, None)

    assert startup.is_startup_installed(unit) is False
    assert ["systemctl", "--user", "disable", unit.service_name] in home.calls
    assert not any(call[:3] == ["systemctl", "--user", "stop"] for call in home.calls)


class TestNoExecStop:
    """A unit must never stop a daemon it does not own.

    Enabling with start-at-boot installs the unit and starts it while the daemon
    is often already running (enable just started it). The guarded start then
    no-ops, the unit is left with no process, systemd stops it at once and runs
    ExecStop - which was `cwcli config <daemon> stop`, so turning on "Start at
    boot" killed the live daemon (seen in a real user journal). Reproduced against
    a real systemd user manager in the PR; these pin the unit shape and the repair.
    """

    @pytest.mark.parametrize("unit", [startup.AUTO_INSPECT, startup.CRED_BRIDGE])
    def test_a_fresh_user_unit_has_no_execstop(self, home, monkeypatch, unit):
        monkeypatch.setattr(startup, "get_platform", lambda: "linux")
        startup.install_startup(unit)
        parsed = _service(home.root / ".config" / "systemd" / "user" / unit.service_name)
        assert parsed["Type"] == "forking"
        assert parsed["ExecStart"] == f'"/usr/local/bin/cwcli" {" ".join(unit.start_args)}'
        assert "ExecStop" not in parsed

    def test_the_system_unit_has_no_execstop(self, home, monkeypatch, tmp_path):
        monkeypatch.setattr(startup, "get_platform", lambda: "linux")
        monkeypatch.setattr(startup, "SYSTEM_UNIT_DIR", tmp_path)
        startup.install_system_unit(startup.CRED_BRIDGE, user="cwcli", group="cwcli", pid_file="/p")
        parsed = _service(tmp_path / startup.CRED_BRIDGE.service_name)
        assert parsed["Type"] == "forking"
        assert parsed["ExecStart"] == '"/usr/local/bin/cwcli" config cred-bridge start'
        assert "ExecStop" not in parsed

    def test_an_installed_unit_with_execstop_is_repaired_without_being_started(
        self, home, monkeypatch
    ):
        monkeypatch.setattr(startup, "get_platform", lambda: "linux")
        service = home.root / ".config" / "systemd" / "user" / "cwcli-cred-bridge.service"
        service.parent.mkdir(parents=True)
        service.write_text(
            '[Service]\nExecStart="/old/cwcli" config cred-bridge start\n'
            'ExecStop="/old/cwcli" config cred-bridge stop\n'
        )

        assert startup.repair_startup(startup.CRED_BRIDGE) is True

        parsed = _service(service)
        assert parsed["ExecStart"] == '"/old/cwcli" config cred-bridge start'
        assert "ExecStop" not in parsed

        assert home.calls == [["systemctl", "--user", "daemon-reload"]]  # never started
        assert startup.repair_startup(startup.CRED_BRIDGE) is False  # idempotent

    def test_repair_leaves_a_missing_or_current_unit_alone(self, home, monkeypatch):
        monkeypatch.setattr(startup, "get_platform", lambda: "linux")
        assert startup.repair_startup(startup.CRED_BRIDGE) is False
        startup.install_startup(startup.CRED_BRIDGE)
        home.calls.clear()
        assert startup.repair_startup(startup.CRED_BRIDGE) is False
        assert home.calls == []

    def test_a_failed_reload_keeps_the_old_unit_so_the_next_call_retries(self, home, monkeypatch):
        monkeypatch.setattr(startup, "get_platform", lambda: "linux")
        service = home.root / ".config" / "systemd" / "user" / "cwcli-cred-bridge.service"
        service.parent.mkdir(parents=True)
        service.write_text(
            '[Service]\nExecStart="/old/cwcli" config cred-bridge start\n'
            'ExecStop="/old/cwcli" config cred-bridge stop\n'
        )

        monkeypatch.setattr(
            startup.subprocess,
            "run",
            lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="failed"),
        )

        with pytest.raises(OSError, match="daemon-reload failed"):
            startup.repair_startup(startup.CRED_BRIDGE)
        assert "ExecStop" in _service(service)
        with pytest.raises(OSError, match="daemon-reload failed"):
            startup.repair_startup(startup.CRED_BRIDGE)


class TestMacosUnits:
    def test_auto_inspect_plist_is_byte_stable_across_the_refactor(self, home, monkeypatch):
        monkeypatch.setattr(startup, "get_platform", lambda: "darwin")
        assert startup.install_startup() == (True, None)
        plist = home.root / "Library" / "LaunchAgents" / "com.cwcli.auto-inspect.plist"
        assert plist.read_text() == _AUTO_INSPECT_PLIST

    def test_cred_bridge_plist_carries_its_own_label_argv_and_logs(self, home, monkeypatch):
        monkeypatch.setattr(startup, "get_platform", lambda: "darwin")
        assert startup.install_startup(startup.CRED_BRIDGE) == (True, None)
        plist = home.root / "Library" / "LaunchAgents" / "com.cwcli.cred-bridge.plist"
        text = plist.read_text()
        assert "<string>com.cwcli.cred-bridge</string>" in text
        assert "<string>cred-bridge</string>" in text and "<string>start</string>" in text
        assert "/tmp/cwcli-cred-bridge.log" in text
        assert "auto-inspect" not in text


class TestWindowsUnits:
    def test_cred_bridge_task_uses_its_own_name_and_argv(self, home, monkeypatch):
        monkeypatch.setattr(startup, "get_platform", lambda: "windows")
        assert startup.install_startup(startup.CRED_BRIDGE) == (True, None)
        create = next(c for c in home.calls if "/Create" in c)
        assert "CaffeinatedWhaleCliCredBridge" in create
        assert '"/usr/local/bin/cwcli" config cred-bridge start' in create


# --------------------------------- shared-mode system unit (as a service account) ---


def test_install_system_unit_writes_a_service_account_unit(home, tmp_path, monkeypatch):
    """The machine-wide bridge unit runs as User=cwcli in /etc/systemd/system
    (system-wide, at boot), not the per-user `systemctl --user` unit."""
    monkeypatch.setattr(startup, "SYSTEM_UNIT_DIR", tmp_path / "systemd")
    (tmp_path / "systemd").mkdir()
    monkeypatch.setattr(startup, "get_platform", lambda: "linux")

    ok = startup.install_system_unit(
        startup.CRED_BRIDGE,
        user="cwcli",
        group="cwcli",
        pid_file="/var/lib/cwcli/run/credbridge.pid",
    )
    assert ok is True
    content = startup.system_unit_path(startup.CRED_BRIDGE).read_text()

    parser = configparser.ConfigParser()
    parser.optionxform = str
    parser.read_string(content)
    assert parser["Service"]["User"] == "cwcli"
    assert parser["Service"]["Group"] == "cwcli"
    assert parser["Service"]["PIDFile"] == "/var/lib/cwcli/run/credbridge.pid"
    assert parser["Service"]["ExecStart"] == '"/usr/local/bin/cwcli" config cred-bridge start'
    assert parser["Install"]["WantedBy"] == "multi-user.target"
    # Enabled system-wide (never `--user`).
    assert ["systemctl", "enable", "--now", "cwcli-cred-bridge.service"] in home.calls
    assert not any("--user" in c for c in home.calls)


def test_install_system_unit_refuses_off_linux(monkeypatch):
    monkeypatch.setattr(startup, "get_platform", lambda: "darwin")
    with pytest.raises(OSError):
        startup.install_system_unit(startup.CRED_BRIDGE, user="cwcli", group="cwcli", pid_file="/x")


def test_scheduler_system_unit_is_simple_because_the_daemon_stays_in_the_foreground(
    home, tmp_path, monkeypatch
):
    """`cwcli schedule daemon` never forks, so a Type=forking unit waits for a fork
    that never comes, kills the scheduler at the start timeout and loops."""
    monkeypatch.setattr(startup, "SYSTEM_UNIT_DIR", tmp_path / "systemd")
    (tmp_path / "systemd").mkdir()
    monkeypatch.setattr(startup, "get_platform", lambda: "linux")

    startup.install_system_unit(startup.SCHEDULER, user="cwcli", group="cwcli", pid_file="/p")
    service = _service(startup.system_unit_path(startup.SCHEDULER))
    assert service["Type"] == "simple"
    assert "PIDFile" not in service
    assert service["ExecStart"] == '"/usr/local/bin/cwcli" schedule daemon'


def test_repair_system_unit_fixes_an_old_forking_scheduler_unit(tmp_path, monkeypatch):
    monkeypatch.setattr(startup, "SYSTEM_UNIT_DIR", tmp_path)
    old = (
        "[Service]\nType=forking\nUser=cwcli\nGroup=cwcli\nPIDFile=/var/lib/cwcli/x.pid\n"
        'ExecStart="/usr/local/bin/cwcli" schedule daemon\n'
    )
    path = startup.system_unit_path(startup.SCHEDULER)
    path.write_text(old)

    assert startup.repair_system_unit(startup.SCHEDULER) is True
    service = _service(path)
    assert service["Type"] == "simple"
    assert "PIDFile" not in service
    assert service["User"] == "cwcli"
    assert startup.repair_system_unit(startup.SCHEDULER) is False  # already right


def test_repair_system_unit_leaves_forking_units_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(startup, "SYSTEM_UNIT_DIR", tmp_path)
    path = startup.system_unit_path(startup.CRED_BRIDGE)
    path.write_text("[Service]\nType=forking\nPIDFile=/p\n")
    assert startup.repair_system_unit(startup.CRED_BRIDGE) is False
    assert "Type=forking" in path.read_text()
