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


class TestStopBench:
    """``core.stop_bench`` - the per-bench half.

    Stopping ONE bench is a different operation from stopping the instance, not a
    narrowed one: benches are siblings in a single container, so there is no
    container to stop. It ends that bench's own supervisord and leaves every
    container - and every sibling bench - running. Before it existed, taking one
    bench down meant reaching past cwcli with ``docker exec ... supervisorctl``.
    """

    @pytest.fixture
    def bench_wire(self, monkeypatch):
        """Wire a running frappe container plus the bench/supervision reads."""

        def _wire(*, supervisor_up=True, processes=(), benches=(("/w/b0", None),)):
            frappe = _container("proj-frappe-1")
            frappe.labels = {"com.docker.compose.service": "frappe"}
            monkeypatch.setattr(core_stop, "get_project_containers", lambda name: [frappe])
            monkeypatch.setattr(
                core_stop.resolvers,
                "cached_benches",
                lambda p: [{"path": path, "label": label} for path, label in benches],
            )
            snapshot = MagicMock()
            snapshot.supervisor_up = supervisor_up
            snapshot.processes = [
                MagicMock(label=label, up=up) for label, up in processes
            ]
            monkeypatch.setattr(
                core_stop.supervision, "discover_stack", lambda c, p: snapshot
            )
            calls: dict[str, list] = {"stopped": [], "cleared": []}
            monkeypatch.setattr(
                core_stop.supervision,
                "stop_supervisor",
                lambda c, p: calls["stopped"].append(p) or True,
            )
            monkeypatch.setattr(
                core_stop.supervision,
                "clear_marker",
                lambda c, p: calls["cleared"].append(p),
            )
            return frappe, calls

        return _wire

    def test_stops_the_named_bench_and_reports_what_was_up(self, bench_wire):
        _frappe, calls = bench_wire(
            processes=[("web", True), ("worker", True), ("watch", False)],
            benches=(("/w/b0", None), ("/w/b1", None)),
        )

        result = core_stop.stop_bench("proj", bench="1")

        assert result.status is Status.OK
        assert result.data.bench_path == "/w/b1"
        assert result.data.already_stopped is False
        assert result.data.stopped_processes == ["web", "worker"]  # not the down one
        # Only the NAMED bench's supervisor was signalled; the sibling is untouched.
        assert calls["stopped"] == ["/w/b1"]

    def test_process_labels_are_deduplicated(self, bench_wire):
        """Discovery is per-PID and one program can hold several processes (the
        bench wrapper plus what it execs); reporting `web, web` would read as two."""
        bench_wire(processes=[("web", True), ("web", True), ("worker", True)])

        result = core_stop.stop_bench("proj", bench="0")

        assert result.data.stopped_processes == ["web", "worker"]

    def test_a_bench_that_is_not_running_is_a_clean_success(self, bench_wire):
        """Idempotent, like the project-wide stop: an agent can stop twice."""
        _frappe, calls = bench_wire(supervisor_up=False)

        result = core_stop.stop_bench("proj", bench="0")

        assert result.status is Status.OK
        assert result.data.already_stopped is True
        assert result.data.stopped_processes == []
        assert calls["stopped"] == []  # nothing was signalled

    def test_the_launch_marker_is_cleared_so_a_stop_does_not_read_as_a_fault(
        self, bench_wire
    ):
        """The marker is what tells "started, supervisor died" (degraded) from
        "never started" (online). A DELIBERATE stop must clear it, or every
        stopped bench leaves the instance permanently `degraded` with nothing
        wrong - a health signal crying wolf."""
        _frappe, calls = bench_wire(processes=[("web", True)])

        core_stop.stop_bench("proj", bench="0")

        assert calls["cleared"] == ["/w/b0"]

    def test_multi_bench_without_a_selector_is_a_choice_not_a_guess(self, bench_wire):
        _frappe, calls = bench_wire(benches=(("/w/b0", None), ("/w/b1", None)))

        result = core_stop.stop_bench("proj")

        assert result.status is Status.NEEDS_CHOICE
        assert result.choice.kind == "select_bench"
        assert calls["stopped"] == []  # nothing was stopped while asking

    def test_a_stopped_container_is_not_running_not_a_silent_success(self, monkeypatch):
        frappe = _container("proj-frappe-1", status="exited")
        frappe.labels = {"com.docker.compose.service": "frappe"}
        monkeypatch.setattr(core_stop, "get_project_containers", lambda name: [frappe])

        with pytest.raises(CwcliError) as exc:
            core_stop.stop_bench("proj", bench="0")
        assert exc.value.kind is ErrorKind.NOT_RUNNING

    def test_core_stop_bench_prints_nothing(self, bench_wire, capsys):
        bench_wire(processes=[("web", True)])
        core_stop.stop_bench("proj", bench="0")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""
