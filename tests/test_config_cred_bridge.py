"""Frontend tests for ``cwcli config cred-bridge`` - the renderer wiring and the
exit-code mapping over the UI-pure ``core.cred_bridge`` verbs.

Self-contained (no shared ``cfg`` fixture) so it is unaffected by the
``pytest_plugins`` collection quirk in ``test_config_frontend``: it fakes the
daemon layer directly and drives Typer's real parser via ``CliRunner``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from caffeinated_whale_cli.main import app
from caffeinated_whale_cli.utils import config_utils
from caffeinated_whale_cli.utils import cred_daemon as daemon

runner = CliRunner()


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config_utils, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config_utils, "CONFIG_FILE", cfg_dir / "config.toml")
    monkeypatch.setattr(daemon, "LOG_FILE", tmp_path / "run" / "credbridge.log")

    state = SimpleNamespace(running=False, pid=None, enabled=False)
    monkeypatch.setattr(daemon, "is_running", lambda: state.running)
    monkeypatch.setattr(daemon, "get_pid", lambda: state.pid)
    monkeypatch.setattr(daemon, "is_enabled", lambda: state.enabled)
    monkeypatch.setattr(daemon, "transport", lambda: "unix")
    monkeypatch.setattr(daemon, "registered_projects", lambda: [])
    monkeypatch.setattr(daemon, "recent_audit", lambda: [])
    monkeypatch.setattr(daemon, "ensure_running_instances", lambda: 0)
    monkeypatch.setattr(daemon, "disable_bridge_artifacts", lambda: None)

    def _start():
        state.running = True
        state.pid = 9

    def _stop():
        state.running = False
        state.pid = None

    monkeypatch.setattr(daemon, "start_daemon", _start)
    monkeypatch.setattr(daemon, "stop_daemon", _stop)

    def _set_enabled(val):
        state.enabled = val

    monkeypatch.setattr(config_utils, "set_cred_bridge_enabled", _set_enabled)
    return state


def test_enable_starts_and_reports(bridge):
    result = runner.invoke(app, ["config", "cred-bridge", "enable"])
    assert result.exit_code == 0
    assert "Credential bridge enabled." in result.output
    assert bridge.enabled is True
    assert bridge.running is True


def test_start_refuses_when_disabled_with_exit_two(bridge):
    result = runner.invoke(app, ["config", "cred-bridge", "start"])
    assert result.exit_code == 2  # USAGE -> exit 2
    assert bridge.running is False


def test_start_when_enabled(bridge):
    bridge.enabled = True
    result = runner.invoke(app, ["config", "cred-bridge", "start"])
    assert result.exit_code == 0
    assert bridge.running is True


def test_stop_is_idempotent(bridge):
    result = runner.invoke(app, ["config", "cred-bridge", "stop"])
    assert result.exit_code == 0
    assert "not running" in result.output.lower()


def test_disable(bridge):
    bridge.enabled = True
    bridge.running = True
    result = runner.invoke(app, ["config", "cred-bridge", "disable"])
    assert result.exit_code == 0
    assert bridge.enabled is False
    assert bridge.running is False


def test_status_json_is_one_object(bridge):
    bridge.enabled = True
    bridge.running = True
    bridge.pid = 9
    result = runner.invoke(app, ["config", "cred-bridge", "status", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["enabled"] is True
    assert data["daemon_running"] is True
    assert data["daemon_pid"] == 9
    assert data["transport"] == "unix"
    assert "registered_projects" in data


def test_status_human_table(bridge):
    result = runner.invoke(app, ["config", "cred-bridge", "status"])
    assert result.exit_code == 0
    # The title wraps at narrow test widths; assert on the always-present rows.
    assert "Transport" in result.output
    assert "unix" in result.output
