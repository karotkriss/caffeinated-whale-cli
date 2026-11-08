"""``utils.startup`` unit tests - the BootUnit generalization (cred-bridge phase 3).

The boot-unit machinery was auto-inspect-only and hardcoded; it now takes a
:class:`BootUnit` spec with ``AUTO_INSPECT`` as the default parameter. Two
properties are load-bearing and pinned here:

* **the auto-inspect unit has a pinned shape** - the generated systemd service and
  LaunchAgent plist match the templates below exactly: ``/usr/bin/env cwcli`` on
  a PATH led by this cwcli's directory (so a removed or moved install falls
  through to the cwcli that remains), a bounded systemd restart, and no ExecStop;
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

# Captured at import, before the conftest's autouse stubs replace them per test.
_REAL_REPAIR_STARTUP = startup.repair_startup
_REAL_STALE_REASON = startup.stale_reason


def _parse(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read(path)
    return parser


def _service(path: Path) -> configparser.SectionProxy:
    return _parse(path)["Service"]


def _fake_cwcli(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A sandbox home + inert platform commands + a real (fake) cwcli to point at."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(startup, "repair_startup", _REAL_REPAIR_STARTUP)
    monkeypatch.setattr(startup, "stale_reason", _REAL_STALE_REASON)
    cwcli = _fake_cwcli(tmp_path / "install" / "bin" / "cwcli")
    current = {"path": str(cwcli)}
    monkeypatch.setattr(startup, "get_cwcli_path", lambda: current["path"])
    monkeypatch.setattr(startup.sys, "argv", [str(cwcli), "config"])
    calls: list[list[str]] = []

    def _run(cmd, **kwargs):
        calls.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(startup.subprocess, "run", _run)
    return SimpleNamespace(root=tmp_path, calls=calls, cwcli=cwcli, current=current)


def _unit_path(home) -> str:
    return (
        f"{home.cwcli.parent}:{home.root}/.local/bin:/usr/local/sbin:/usr/local/bin:"
        "/usr/sbin:/usr/bin:/sbin:/bin"
    )


# The exact systemd service written for auto-inspect: no ExecStop (see
# TestNoExecStop), a bounded restart and a PATH-resolved cwcli (see TestStaleUnits).
_AUTO_INSPECT_SERVICE = """[Unit]
Description=Caffeinated Whale CLI Auto-Inspect Service
After=network.target
StartLimitIntervalSec=600
StartLimitBurst=3

[Service]
Type=forking
Environment="PATH={path}"
ExecStart=/usr/bin/env cwcli config auto-inspect start
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
"""

# The exact LaunchAgent plist written for auto-inspect.
_AUTO_INSPECT_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.cwcli.auto-inspect</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/env</string>
        <string>cwcli</string>
        <string>config</string>
        <string>auto-inspect</string>
        <string>start</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{path}</string>
    </dict>
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
    def test_auto_inspect_service_has_the_pinned_shape(self, home, monkeypatch):
        monkeypatch.setattr(startup, "get_platform", lambda: "linux")
        assert startup.install_startup() == (True, None)  # the pre-existing no-arg call shape
        service = home.root / ".config" / "systemd" / "user" / "cwcli-auto-inspect.service"
        assert service.read_text() == _AUTO_INSPECT_SERVICE.format(path=_unit_path(home))

    def test_cred_bridge_service_execs_the_guarded_start_verb(self, home, monkeypatch):
        monkeypatch.setattr(startup, "get_platform", lambda: "linux")
        assert startup.install_startup(startup.CRED_BRIDGE) == (True, None)
        service = home.root / ".config" / "systemd" / "user" / "cwcli-cred-bridge.service"
        parsed = _service(service)
        assert parsed["Type"] == "forking"
        assert parsed["ExecStart"] == "/usr/bin/env cwcli config cred-bridge start"
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
        assert parsed["ExecStart"] == f"/usr/bin/env cwcli {' '.join(unit.start_args)}"
        assert "ExecStop" not in parsed

    def test_the_system_unit_has_no_execstop(self, home, monkeypatch, tmp_path):
        monkeypatch.setattr(startup, "get_platform", lambda: "linux")
        monkeypatch.setattr(startup, "SYSTEM_UNIT_DIR", tmp_path)
        startup.install_system_unit(startup.CRED_BRIDGE, user="cwcli", group="cwcli", pid_file="/p")
        parsed = _service(tmp_path / startup.CRED_BRIDGE.service_name)
        assert parsed["Type"] == "forking"
        assert parsed["ExecStart"] == f'"{home.cwcli}" config cred-bridge start'
        assert "ExecStop" not in parsed

    def test_an_installed_unit_with_execstop_is_rewritten_without_being_started(
        self, home, monkeypatch
    ):
        monkeypatch.setattr(startup, "get_platform", lambda: "linux")
        service = home.root / ".config" / "systemd" / "user" / "cwcli-cred-bridge.service"
        service.parent.mkdir(parents=True)
        service.write_text(
            f'[Service]\nExecStart="{home.cwcli}" config cred-bridge start\n'
            f'ExecStop="{home.cwcli}" config cred-bridge stop\n'
        )

        assert startup.repair_startup(startup.CRED_BRIDGE) is True

        parsed = _service(service)
        assert parsed["ExecStart"] == "/usr/bin/env cwcli config cred-bridge start"
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


def _write_unit(home, unit, text: str) -> Path:
    service = home.root / ".config" / "systemd" / "user" / unit.service_name
    service.parent.mkdir(parents=True, exist_ok=True)
    service.write_text(text)
    return service


@pytest.mark.parametrize("unit", [startup.AUTO_INSPECT, startup.CRED_BRIDGE])
class TestStaleUnits:
    """A unit left pointing at a removed cwcli flooded a server's journal.

    On a shared staging server the auto-inspect unit still ran a per-user cwcli
    under a pyenv interpreter path after the move to the shared install; with
    ``Restart=on-failure`` and no effective start limit systemd logged "Failed to
    execute command" every few seconds, around 85,000 lines an hour. A unit now
    retries a bounded number of times, finds cwcli on a PATH instead of one
    absolute path, and is reported and rewritten when stale.
    """

    def test_the_incident_unit_is_stale_because_its_cwcli_is_gone(self, home, monkeypatch, unit):
        monkeypatch.setattr(startup, "get_platform", lambda: "linux")
        _write_unit(
            home,
            unit,
            "[Service]\nType=forking\n"
            f'ExecStart="/home/frappe/.pyenv/versions/3.10.20/bin/cwcli" '
            f"{' '.join(unit.start_args)}\nRestart=on-failure\nRestartSec=10\n",
        )
        assert startup.stale_reason(unit) == (
            "it runs /home/frappe/.pyenv/versions/3.10.20/bin/cwcli, which no longer exists"
        )

        assert startup.repair_startup(unit) is True
        assert startup.stale_reason(unit) is None
        parsed = _parse(home.root / ".config" / "systemd" / "user" / unit.service_name)
        assert parsed["Service"]["ExecStart"] == f"/usr/bin/env cwcli {' '.join(unit.start_args)}"
        assert parsed["Service"]["Restart"] == "on-failure"
        assert parsed["Unit"]["StartLimitBurst"] == "3"
        assert parsed["Unit"]["StartLimitIntervalSec"] == "600"

    def test_a_working_unit_without_a_restart_limit_is_stale(self, home, monkeypatch, unit):
        monkeypatch.setattr(startup, "get_platform", lambda: "linux")
        _write_unit(
            home,
            unit,
            f'[Service]\nExecStart="{home.cwcli}" {" ".join(unit.start_args)}\n',
        )
        assert startup.stale_reason(unit) == (
            "it has no restart limit, so a failing start would retry forever"
        )

    def test_a_removed_install_falls_through_to_the_cwcli_that_remains(
        self, home, monkeypatch, unit
    ):
        """The move that caused the incident: per-user install removed, shared
        install present. The unit's PATH still finds a cwcli, so it keeps working."""
        monkeypatch.setattr(startup, "get_platform", lambda: "linux")
        shared = _fake_cwcli(home.root / ".local" / "bin" / "cwcli")
        startup.install_startup(unit)
        home.cwcli.unlink()
        home.current["path"] = str(shared)

        assert startup.stale_reason(unit) is None

    def test_a_working_unit_is_never_rewritten_by_another_install(self, home, monkeypatch, unit):
        """A dev checkout's `uv run cwcli` puts its own venv first on PATH; its
        start or status must not call the installed cwcli's unit stale or rewrite it."""
        monkeypatch.setattr(startup, "get_platform", lambda: "linux")
        startup.install_startup(unit)
        service = home.root / ".config" / "systemd" / "user" / unit.service_name
        written = service.read_text()
        other = _fake_cwcli(home.root / "checkout" / ".venv" / "bin" / "cwcli")
        home.current["path"] = str(other)
        monkeypatch.setattr(startup.sys, "argv", [str(other)])
        home.calls.clear()

        assert startup.stale_reason(unit) is None
        assert startup.repair_startup(unit) is False
        assert service.read_text() == written
        assert home.calls == []

    def test_a_rewrite_leads_the_unit_path_with_the_running_cwcli(self, home, monkeypatch, unit):
        """The running cwcli writes the unit, not whichever cwcli is first on PATH."""
        monkeypatch.setattr(startup, "get_platform", lambda: "linux")
        _write_unit(home, unit, f'[Service]\nExecStart="/gone/cwcli" {" ".join(unit.start_args)}\n')
        home.current["path"] = str(_fake_cwcli(home.root / "path-first" / "cwcli"))

        assert startup.repair_startup(unit) is True
        parsed = _service(home.root / ".config" / "systemd" / "user" / unit.service_name)
        assert parsed["Environment"].strip('"').removeprefix("PATH=").split(":")[0] == str(
            home.cwcli.parent
        )

    def test_without_a_cwcli_script_the_interpreter_directory_leads(self, home, monkeypatch, unit):
        monkeypatch.setattr(startup, "get_platform", lambda: "linux")
        monkeypatch.setattr(startup.sys, "argv", [str(home.root / "pkg" / "__main__.py")])
        monkeypatch.setattr(startup.sys, "executable", str(home.root / "venv" / "bin" / "python"))
        startup.install_startup(unit)
        parsed = _service(home.root / ".config" / "systemd" / "user" / unit.service_name)
        assert parsed["Environment"].strip('"').removeprefix("PATH=").split(":")[0] == str(
            home.root / "venv" / "bin"
        )

    def test_no_cwcli_anywhere_on_the_unit_path_is_stale(self, home, monkeypatch, unit):
        monkeypatch.setattr(startup, "get_platform", lambda: "linux")
        _write_unit(
            home,
            unit,
            "[Unit]\nStartLimitBurst=3\n[Service]\n"
            f'Environment="PATH={home.root}/nowhere"\n'
            f"ExecStart=/usr/bin/env cwcli {' '.join(unit.start_args)}\n",
        )
        assert startup.stale_reason(unit) == (
            "it runs cwcli (on the unit's PATH), which no longer exists"
        )

    def test_an_absent_unit_is_not_stale(self, home, monkeypatch, unit):
        monkeypatch.setattr(startup, "get_platform", lambda: "linux")
        assert startup.stale_reason(unit) is None
        assert startup.repair_startup(unit) is False

    def test_a_macos_absolute_path_plist_is_rewritten(self, home, monkeypatch, unit):
        monkeypatch.setattr(startup, "get_platform", lambda: "darwin")
        startup.install_startup(unit)
        plist = home.root / "Library" / "LaunchAgents" / f"{unit.label}.plist"
        plist.write_text(
            plist.read_text().replace(
                "<string>/usr/bin/env</string>\n        <string>cwcli</string>",
                "<string>/gone/cwcli</string>",
            )
        )
        assert startup.stale_reason(unit) == "it runs /gone/cwcli, which no longer exists"
        home.calls.clear()

        assert startup.repair_startup(unit) is True
        assert startup.stale_reason(unit) is None
        assert home.calls == []  # never loaded, so never started

    @pytest.mark.parametrize(
        "body",
        [
            b"<?xml version='1.0'?><plist version='1.0'><dict><key>Label",
            b"<?xml version='1.0'?><plist version='1.0'><array/></plist>",
            b"<?xml version='1.0'?><plist version='1.0'><dict>"
            b"<key>ProgramArguments</key><array/></dict></plist>",
        ],
        ids=["truncated", "non-dict-root", "empty-program-arguments"],
    )
    def test_an_unreadable_macos_plist_is_never_claimed_stale(self, home, monkeypatch, unit, body):
        monkeypatch.setattr(startup, "get_platform", lambda: "darwin")
        plist = home.root / "Library" / "LaunchAgents" / f"{unit.label}.plist"
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_bytes(body)
        assert startup.stale_reason(unit) is None
        assert startup.repair_startup(unit) is False
        assert plist.read_bytes() == body

    def test_a_windows_task_whose_program_is_gone_is_recreated(self, home, monkeypatch, unit):
        monkeypatch.setattr(startup, "get_platform", lambda: "windows")
        program = {"path": "C:\\gone\\cwcli.exe"}

        def _run(cmd, **kwargs):
            home.calls.append(list(cmd))
            if "/Create" in cmd:
                program["path"] = str(home.cwcli)
            xml = (
                '<?xml version="1.0" encoding="UTF-16"?>'
                '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
                f'<Actions><Exec><Command>"{program["path"]}"</Command>'
                "<Arguments>config</Arguments></Exec></Actions></Task>"
            )
            return SimpleNamespace(returncode=0, stdout=xml, stderr="")

        monkeypatch.setattr(startup.subprocess, "run", _run)
        assert startup.stale_reason(unit) == "it runs C:\\gone\\cwcli.exe, which no longer exists"
        assert startup.repair_startup(unit) is True
        assert any("/Create" in c for c in home.calls)
        assert startup.stale_reason(unit) is None


class TestMacosUnits:
    def test_auto_inspect_plist_has_the_pinned_shape(self, home, monkeypatch):
        monkeypatch.setattr(startup, "get_platform", lambda: "darwin")
        assert startup.install_startup() == (True, None)
        plist = home.root / "Library" / "LaunchAgents" / "com.cwcli.auto-inspect.plist"
        path = _unit_path(home).replace(
            f"{home.root}/.local/bin:", f"{home.root}/.local/bin:/opt/homebrew/bin:"
        )
        assert plist.read_text() == _AUTO_INSPECT_PLIST.format(path=path)

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
        assert f'"{home.cwcli}" config cred-bridge start' in create


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
    assert parser["Service"]["ExecStart"] == f'"{home.cwcli}" config cred-bridge start'
    assert parser["Unit"]["StartLimitBurst"] == str(startup.START_LIMIT_BURST)
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
    assert service["ExecStart"] == f'"{home.cwcli}" schedule daemon'


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


def test_repair_system_unit_keeps_a_forking_unit_forking(tmp_path, monkeypatch):
    monkeypatch.setattr(startup, "SYSTEM_UNIT_DIR", tmp_path)
    path = startup.system_unit_path(startup.CRED_BRIDGE)
    path.write_text(
        "[Unit]\nStartLimitBurst=3\n\n[Service]\nType=forking\nPIDFile=/p\n"
        f'ExecStart="{_fake_cwcli(tmp_path / "bin" / "cwcli")}" config cred-bridge start\n'
    )
    assert startup.repair_system_unit(startup.CRED_BRIDGE) is False
    service = _service(path)
    assert (service["Type"], service["PIDFile"]) == ("forking", "/p")


# The shared staging server's credential bridge runs as a system unit, so an
# older `cwcli setup shared` unit (no restart limit) loops the same way when its
# cwcli moves or the shared config disables the bridge.
_OLD_SHARED_BRIDGE_UNIT = """[Unit]
Description=Caffeinated Whale CLI Credential Bridge (shared)
After=network.target docker.service

[Service]
Type=forking
User=cwcli
Group=cwcli
PIDFile=/var/lib/cwcli/run/credbridge.pid
ExecStart="{cwcli}" config cred-bridge start
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
"""


@pytest.mark.parametrize("unit", [startup.CRED_BRIDGE, startup.SCHEDULER])
def test_an_old_system_unit_without_a_restart_limit_is_stale_and_repaired(
    tmp_path, monkeypatch, unit
):
    monkeypatch.setattr(startup, "SYSTEM_UNIT_DIR", tmp_path)
    monkeypatch.setattr(startup, "get_platform", lambda: "linux")
    cwcli = _fake_cwcli(tmp_path / "bin" / "cwcli")
    path = startup.system_unit_path(unit)
    path.write_text(_OLD_SHARED_BRIDGE_UNIT.format(cwcli=cwcli))

    assert startup.system_unit_stale_reason(unit) == (
        "it has no restart limit, so a failing start would retry forever"
    )
    assert startup.repair_system_unit(unit) is True
    parsed = _parse(path)
    assert parsed["Unit"]["StartLimitIntervalSec"] == "600"
    assert parsed["Unit"]["StartLimitBurst"] == "3"
    assert (parsed["Service"]["User"], parsed["Service"]["Group"]) == ("cwcli", "cwcli")
    assert parsed["Service"]["ExecStart"].startswith(f'"{cwcli}" ')
    assert startup.system_unit_stale_reason(unit) is None
    assert startup.repair_system_unit(unit) is False


def test_a_system_unit_whose_cwcli_is_gone_runs_the_current_cwcli(tmp_path, monkeypatch):
    monkeypatch.setattr(startup, "SYSTEM_UNIT_DIR", tmp_path)
    monkeypatch.setattr(startup, "get_platform", lambda: "linux")
    current = _fake_cwcli(tmp_path / "usr-local-bin" / "cwcli")
    monkeypatch.setattr(startup, "get_cwcli_path", lambda: str(current))
    path = startup.system_unit_path(startup.CRED_BRIDGE)
    path.write_text(_OLD_SHARED_BRIDGE_UNIT.format(cwcli="/home/frappe/.pyenv/bin/cwcli"))

    assert startup.system_unit_stale_reason(startup.CRED_BRIDGE) == (
        "it runs /home/frappe/.pyenv/bin/cwcli, which no longer exists"
    )
    assert startup.repair_system_unit(startup.CRED_BRIDGE) is True
    assert _service(path)["ExecStart"] == f'"{current}" config cred-bridge start'
    assert startup.system_unit_stale_reason(startup.CRED_BRIDGE) is None


def test_an_absent_or_unreadable_system_unit_is_not_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(startup, "SYSTEM_UNIT_DIR", tmp_path)
    monkeypatch.setattr(startup, "get_platform", lambda: "linux")
    assert startup.system_unit_stale_reason(startup.CRED_BRIDGE) is None
    assert startup.repair_system_unit(startup.CRED_BRIDGE) is False
    startup.system_unit_path(startup.SCHEDULER).write_text("[Service]\nExecStart='unclosed\n")
    assert startup.system_unit_stale_reason(startup.SCHEDULER) is None
