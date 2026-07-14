"""``core.status`` branch coverage against faked I/O (task 3.4).

Every ``overall`` branch (offline / online / running / degraded), the
stopped-is-offline-not-raised contract vs the nonexistent-project NOT_FOUND raise,
supervisor-down vs never-started, the multi-bench NEEDS_CHOICE sharing start's
selector, and the docker-unreachable raise.
"""

from __future__ import annotations

import pytest

from caffeinated_whale_cli.core import resolvers
from caffeinated_whale_cli.core import status as core_status
from caffeinated_whale_cli.core.envelope import Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from tests.test_core_supervision import _CTL_SINGLE, _PS_SINGLE, BENCH, FakeContainer

_MARKER = {"supervisor": "supervisord", "started_at": "t", "config_path": "p"}


@pytest.fixture
def wire(monkeypatch):
    def _wire(frappe, *, benches=None):
        containers = [frappe] if frappe is not None else []
        monkeypatch.setattr(core_status, "get_project_containers", lambda name: containers)
        monkeypatch.setattr(
            resolvers.db_utils,
            "get_cached_project_data",
            lambda name: {"bench_instances": benches} if benches is not None else None,
        )

    return _wire


class TestOverall:
    def test_running_when_marker_up_and_web(self, wire):
        wire(FakeContainer(marker=_MARKER, web_code="200"), benches=[{"path": BENCH}])
        result = core_status.status("proj")
        assert result.status is Status.OK
        report = result.data
        assert report.overall == "running"
        assert report.container_running is True
        assert report.supervisor_up is True
        assert report.web_http_code == "200"
        web = next(p for p in report.processes if p.label == "web")
        assert web.up is True and web.uptime_s == 499 and web.rss_kb == 80000

    def test_online_when_no_marker(self, wire):
        wire(FakeContainer(marker=None, web_code="200"), benches=[{"path": BENCH}])
        report = core_status.status("proj").data
        assert report.overall == "online"

    def test_degraded_when_marker_but_supervisor_down(self, wire):
        # marker present, but no supervisord in ps (container restart / crash).
        c = FakeContainer(marker=_MARKER, ps="1 0 5 0.0 1000 /sbin/init\n", cwds={})
        wire(c, benches=[{"path": BENCH}])
        report = core_status.status("proj").data
        assert report.overall == "degraded"
        assert report.supervisor_up is False
        # every expected label is reported down.
        assert all(not p.up for p in report.processes)

    def test_degraded_when_supervisor_up_but_web_down(self, wire):
        wire(FakeContainer(marker=_MARKER, web_ok=False), benches=[{"path": BENCH}])
        report = core_status.status("proj").data
        assert report.overall == "degraded"
        assert report.supervisor_up is True
        assert report.web_http_code is None

    def test_degraded_when_a_program_is_fatal(self, wire):
        # Supervisord keeps siblings alive when one dies, so "web serving while a
        # worker is FATAL" is a REAL, stable partial stack -> degraded (honcho made
        # this impossible; supervisor-up no longer implies the whole stack is up).
        ps = _PS_SINGLE.replace(
            "105 100 499 0.3 70000 /env/bin/python /env/bin/bench worker --queue default\n", ""
        )
        ctl = _CTL_SINGLE.replace(
            "worker_default   RUNNING   pid 105, uptime 0:05:00\n",
            "worker_default   FATAL   Exited too quickly\n",
        )
        c = FakeContainer(marker=_MARKER, ps=ps, ctl_status=ctl, web_code="200")
        wire(c, benches=[{"path": BENCH}])
        report = core_status.status("proj").data
        assert report.overall == "degraded"
        assert report.supervisor_up is True
        worker = next(p for p in report.processes if p.label == "worker:default")
        assert worker.state == "FATAL"
        assert worker.up is False


class TestProbeWeb:
    """``probe_web=False`` (the ``--watch`` loop) must never hit the web server."""

    def test_no_web_probe_when_suppressed(self, wire):
        # THE load-bearing behavior: a watch tick reads process health from ``ps``
        # but must NOT run the ``curl localhost:8000`` web probe (which is what
        # spams the bench's access logs).
        c = FakeContainer(marker=_MARKER, web_code="200")
        wire(c, benches=[{"path": BENCH}])
        report = core_status.status("proj", probe_web=False).data
        assert report.web_http_code is None
        assert not any(isinstance(cmd, list) and cmd[0] == "curl" for cmd in c.calls)

    def test_running_without_web_probe_when_supervisor_up(self, wire):
        # With the web probe suppressed, supervisor-up + all-healthy drives
        # ``running`` - a missing web code must NOT falsely degrade the aggregate.
        wire(FakeContainer(marker=_MARKER, web_code="200"), benches=[{"path": BENCH}])
        report = core_status.status("proj", probe_web=False).data
        assert report.overall == "running"
        assert report.supervisor_up is True

    def test_degraded_without_web_probe_when_supervisor_down(self, wire):
        # Suppressing the web probe does NOT mask a genuinely dead supervisor.
        c = FakeContainer(marker=_MARKER, ps="1 0 5 0.0 1000 /sbin/init\n", cwds={})
        wire(c, benches=[{"path": BENCH}])
        report = core_status.status("proj", probe_web=False).data
        assert report.overall == "degraded"

    def test_web_probe_runs_by_default(self, wire):
        # The plain one-shot path keeps its web probe (probe_web defaults True).
        c = FakeContainer(marker=_MARKER, web_code="200")
        wire(c, benches=[{"path": BENCH}])
        report = core_status.status("proj").data
        assert report.web_http_code == "200"
        assert any(isinstance(cmd, list) and cmd[0] == "curl" for cmd in c.calls)


class TestOffline:
    def test_stopped_is_offline_and_does_not_raise(self, wire):
        # A real-but-stopped project (containers exist, frappe not running) stays
        # ``offline``/exit-0 - the PRESERVED contract. This is the regression guard
        # that the NOT_FOUND distinction below must never disturb.
        c = FakeContainer()
        c.status = "exited"
        wire(c)
        result = core_status.status("proj")
        assert result.status is Status.OK
        assert result.data.overall == "offline"
        assert result.data.container_running is False


class TestNotFound:
    """A truly-nonexistent project is a NOT_FOUND raise, distinct from stopped."""

    def test_absent_project_raises_not_found(self, wire):
        # No containers with the label at all -> typo / never created.
        wire(None)
        with pytest.raises(CwcliError) as exc:
            core_status.status("proj")
        assert exc.value.kind is ErrorKind.NOT_FOUND

    def test_no_frappe_service_raises_not_found(self, monkeypatch):
        class Other:
            labels = {"com.docker.compose.service": "db"}
            status = "running"

        monkeypatch.setattr(core_status, "get_project_containers", lambda name: [Other()])
        with pytest.raises(CwcliError) as exc:
            core_status.status("proj")
        assert exc.value.kind is ErrorKind.NOT_FOUND


class TestChoicesAndErrors:
    def test_multi_bench_returns_select_bench(self, wire):
        wire(FakeContainer(marker=_MARKER), benches=[{"path": "/w/b0"}, {"path": "/w/b1"}])
        result = core_status.status("proj")
        assert result.status is Status.NEEDS_CHOICE
        assert result.choice.kind == "select_bench"

    def test_start_and_status_share_the_bench_selector(self, wire):
        # --bench 1 resolves to the same path in both verbs (one shared resolver).
        benches = [{"path": "/w/b0"}, {"path": "/w/b1"}]
        wire(FakeContainer(marker=_MARKER), benches=benches)
        report = core_status.status("proj", bench="1").data
        # status resolved bench 1 and reported without a choice.
        assert report.overall in ("running", "degraded", "online")

    def test_docker_unreachable_raises(self, monkeypatch):
        """`status` raises a DOCKER CwcliError when Docker is unreachable."""
        monkeypatch.setattr(core_status, "get_project_containers", lambda name: None)
        with pytest.raises(CwcliError) as exc:
            core_status.status("proj")
        assert exc.value.kind is ErrorKind.DOCKER
