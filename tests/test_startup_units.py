"""``utils.startup`` unit tests - the BootUnit generalization (cred-bridge phase 3).

The boot-unit machinery was auto-inspect-only and hardcoded; it now takes a
:class:`BootUnit` spec with ``AUTO_INSPECT`` as the default parameter. Two
properties are load-bearing and pinned here:

* **the auto-inspect unit is BYTE-STABLE across the refactor** - the generated
  systemd service and LaunchAgent plist match the pre-generalization templates
  exactly, so an installed unit on a user's machine and a freshly generated one
  never disagree;
* **each unit's identity is independent** - installing the cred-bridge unit
  neither creates nor detects the auto-inspect one, and the cred-bridge unit
  execs ``cwcli config cred-bridge start`` verbatim, whose refuse-when-disabled
  guard keeps a stale unit inert after ``disable``.

All platform side effects (``systemctl``/``launchctl``/``schtasks``) are faked;
``Path.home`` is patched so nothing ever touches the real home directory.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from caffeinated_whale_cli.utils import startup


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A sandbox home + inert platform commands + a fixed cwcli path."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(startup, "get_cwcli_path", lambda: "/usr/local/bin/cwcli")
    calls: list[list[str]] = []

    def _run(cmd, **kwargs):
        calls.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(startup.subprocess, "run", _run)
    return SimpleNamespace(root=tmp_path, calls=calls)


# The exact systemd service the pre-generalization code shipped for auto-inspect.
_AUTO_INSPECT_SERVICE = """[Unit]
Description=Caffeinated Whale CLI Auto-Inspect Service
After=network.target

[Service]
Type=forking
ExecStart="/usr/local/bin/cwcli" config auto-inspect start
ExecStop="/usr/local/bin/cwcli" config auto-inspect stop
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
        assert startup.install_startup() is True  # the pre-existing no-arg call shape
        service = home.root / ".config" / "systemd" / "user" / "cwcli-auto-inspect.service"
        assert service.read_text() == _AUTO_INSPECT_SERVICE

    def test_cred_bridge_service_execs_the_guarded_start_verb(self, home, monkeypatch):
        monkeypatch.setattr(startup, "get_platform", lambda: "linux")
        assert startup.install_startup(startup.CRED_BRIDGE) is True
        service = home.root / ".config" / "systemd" / "user" / "cwcli-cred-bridge.service"
        text = service.read_text()
        assert 'ExecStart="/usr/local/bin/cwcli" config cred-bridge start' in text
        assert 'ExecStop="/usr/local/bin/cwcli" config cred-bridge stop' in text
        assert "Description=Caffeinated Whale CLI Credential Bridge" in text
        assert ["systemctl", "--user", "enable", "cwcli-cred-bridge.service"] in home.calls

    def test_unit_identities_are_independent(self, home, monkeypatch):
        monkeypatch.setattr(startup, "get_platform", lambda: "linux")
        startup.install_startup(startup.CRED_BRIDGE)
        assert startup.is_startup_installed(startup.CRED_BRIDGE) is True
        assert startup.is_startup_installed() is False  # auto-inspect untouched
        assert startup.uninstall_startup(startup.CRED_BRIDGE) is True
        assert startup.is_startup_installed(startup.CRED_BRIDGE) is False


class TestMacosUnits:
    def test_auto_inspect_plist_is_byte_stable_across_the_refactor(self, home, monkeypatch):
        monkeypatch.setattr(startup, "get_platform", lambda: "darwin")
        assert startup.install_startup() is True
        plist = home.root / "Library" / "LaunchAgents" / "com.cwcli.auto-inspect.plist"
        assert plist.read_text() == _AUTO_INSPECT_PLIST

    def test_cred_bridge_plist_carries_its_own_label_argv_and_logs(self, home, monkeypatch):
        monkeypatch.setattr(startup, "get_platform", lambda: "darwin")
        assert startup.install_startup(startup.CRED_BRIDGE) is True
        plist = home.root / "Library" / "LaunchAgents" / "com.cwcli.cred-bridge.plist"
        text = plist.read_text()
        assert "<string>com.cwcli.cred-bridge</string>" in text
        assert "<string>cred-bridge</string>" in text and "<string>start</string>" in text
        assert "/tmp/cwcli-cred-bridge.log" in text
        assert "auto-inspect" not in text


class TestWindowsUnits:
    def test_cred_bridge_task_uses_its_own_name_and_argv(self, home, monkeypatch):
        monkeypatch.setattr(startup, "get_platform", lambda: "windows")
        assert startup.install_startup(startup.CRED_BRIDGE) is True
        create = next(c for c in home.calls if "/Create" in c)
        assert "CaffeinatedWhaleCliCredBridge" in create
        assert '"/usr/local/bin/cwcli" config cred-bridge start' in create
