"""``core.daemon_recovery.recover`` starts enabled, start-at-boot daemons that are down."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from caffeinated_whale_cli.core import auto_inspect, cred_bridge, daemon_recovery
from caffeinated_whale_cli.core.envelope import Result, Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind


def _state(*, enabled=True, startup=True, running=False):
    return SimpleNamespace(enabled=enabled, startup_enabled=startup, daemon_running=running)


@pytest.fixture
def modules(monkeypatch):
    started = []
    for name, module in (("auto-inspect", auto_inspect), ("credential bridge", cred_bridge)):
        monkeypatch.setattr(
            module, "status", lambda: Result(status=Status.OK, data=_state(running=True))
        )
        monkeypatch.setattr(
            module, "start", lambda n=name: started.append(n) or Result(status=Status.OK)
        )
    return started


def _set(monkeypatch, module, state):
    monkeypatch.setattr(module, "status", lambda: Result(status=Status.OK, data=state))


def test_starts_an_enabled_boot_hooked_daemon_that_is_down(modules, monkeypatch):
    _set(monkeypatch, cred_bridge, _state())
    notes = daemon_recovery.recover()
    assert modules == ["credential bridge"]
    assert [(n.daemon, n.started) for n in notes] == [("credential bridge", True)]


@pytest.mark.parametrize(
    "state",
    [_state(enabled=False), _state(startup=False), _state(running=True)],
    ids=["disabled", "no-boot-unit", "already-running"],
)
def test_leaves_everything_else_alone(modules, monkeypatch, state):
    _set(monkeypatch, auto_inspect, state)
    _set(monkeypatch, cred_bridge, state)
    assert daemon_recovery.recover() == []
    assert modules == []


def test_a_failed_start_says_why_and_does_not_stop_the_other_daemon(modules, monkeypatch):
    _set(monkeypatch, auto_inspect, _state())
    _set(monkeypatch, cred_bridge, _state())

    def _fail():
        raise CwcliError(ErrorKind.PRECONDITION, "daemon.system_owned", "managed by systemd")

    monkeypatch.setattr(auto_inspect, "start", _fail)
    notes = daemon_recovery.recover()
    assert [(n.daemon, n.started) for n in notes] == [
        ("auto-inspect", False),
        ("credential bridge", True),
    ]
    assert "managed by systemd" in notes[0].note
    assert "cwcli config auto-inspect enable" in notes[0].note


def test_a_failed_start_carries_the_errors_own_hint_over_the_generic_command(modules, monkeypatch):
    _set(monkeypatch, cred_bridge, _state())

    def _fail():
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "daemon.system_owned",
            "managed by the shared install's system unit",
            hint="sudo systemctl start cwcli-cred-bridge.service",
        )

    monkeypatch.setattr(cred_bridge, "start", _fail)
    (note,) = daemon_recovery.recover()
    assert not note.started
    assert "sudo systemctl start cwcli-cred-bridge.service" in note.note
    assert "cwcli config cred-bridge start" not in note.note
