"""``core.stop`` - every branch, plus the boundary and stdout-purity properties.

`core.stop` replaces `commands/stop.py:_stop_project`, which printed rich markup
to STDOUT and was nonetheless imported by four modules - including `rm`'s
destructive path and the `axi` agent surface, where a stray stdout line corrupts
the one-TOON-document contract. These tests pin the replacement's contract: a
typed NOT_FOUND instead of the `None` sentinel, names instead of live Docker
objects, and no printing at all.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from caffeinated_whale_cli.core import stop as core_stop
from caffeinated_whale_cli.core.envelope import Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from caffeinated_whale_cli.core.stop import StopOutcome


def _container(name, status="running"):
    c = MagicMock()
    c.name = name
    c.status = status
    return c


@pytest.fixture
def wire(monkeypatch):
    def _wire(containers):
        monkeypatch.setattr(core_stop, "get_project_containers", lambda name: containers)

    return _wire


class TestStop:
    def test_stops_running_containers_and_reports_them(self, wire):
        frappe, db = _container("proj-frappe-1"), _container("proj-db-1")
        wire([frappe, db])

        result = core_stop.stop("proj")

        assert result.status is Status.OK
        assert result.data == StopOutcome(
            project="proj",
            stopped=2,
            already_stopped=False,
            containers=["proj-frappe-1", "proj-db-1"],
        )
        frappe.stop.assert_called_once()
        db.stop.assert_called_once()

    def test_only_running_containers_are_stopped(self, wire):
        running, exited = _container("a"), _container("b", status="exited")
        wire([running, exited])

        result = core_stop.stop("proj")

        assert result.data.stopped == 1
        assert result.data.containers == ["a"]
        exited.stop.assert_not_called()

    def test_already_stopped_is_a_success_not_a_failure(self, wire):
        """Found, but nothing running: a legitimate zero, never an error."""
        wire([_container("a", status="exited")])

        result = core_stop.stop("proj")

        assert result.status is Status.OK
        assert result.data.already_stopped is True
        assert result.data.stopped == 0

    def test_a_project_without_a_frappe_service_still_stops(self, wire):
        """stop is project-wide: routing via the frappe accessor would newly fail this."""
        db = _container("proj-db-1")
        db.labels = {"com.docker.compose.service": "mariadb"}
        wire([db])

        result = core_stop.stop("proj")

        assert result.data.stopped == 1
        db.stop.assert_called_once()


class TestTypedErrors:
    def test_missing_project_raises_not_found_instead_of_returning_none(self, wire):
        wire([])
        with pytest.raises(CwcliError) as exc:
            core_stop.stop("proj")
        assert exc.value.kind is ErrorKind.NOT_FOUND

    def test_unreachable_daemon_raises_docker_not_not_found(self, wire):
        """`_stop_project` reported a dead daemon as "project not found"; the typed
        taxonomy tells the two apart."""
        wire(None)
        with pytest.raises(CwcliError) as exc:
            core_stop.stop("proj")
        assert exc.value.kind is ErrorKind.DOCKER


class TestBoundary:
    def test_no_live_docker_object_crosses_the_return_boundary(self, wire):
        wire([_container("proj-frappe-1")])
        result = core_stop.stop("proj")
        assert all(isinstance(c, str) for c in result.data.containers)

    def test_core_stop_prints_nothing(self, wire, capsys):
        """The whole point: a callee that cannot corrupt a machine-readable stdout."""
        wire([_container("a"), _container("b", status="exited")])
        core_stop.stop("proj")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_core_stop_prints_nothing_on_already_stopped(self, wire, capsys):
        """`_stop_project`'s "already stopped" line went to STDOUT - the axi hole."""
        wire([_container("a", status="exited")])
        core_stop.stop("proj")
        assert capsys.readouterr().out == ""
