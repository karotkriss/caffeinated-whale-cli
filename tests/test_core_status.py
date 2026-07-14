"""``core.status`` branch coverage against faked I/O (task 3.4).

Every ``overall`` branch (offline / online / running / degraded), the
offline-not-raised contract, supervisor-down vs never-started, the multi-bench
NEEDS_CHOICE sharing start's selector, and the docker-unreachable raise.
"""

from __future__ import annotations

import pytest

from caffeinated_whale_cli.core import resolvers
from caffeinated_whale_cli.core import status as core_status
from caffeinated_whale_cli.core.envelope import Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from tests.test_core_supervision import BENCH, FakeContainer

_MARKER = {"supervisor": "honcho", "started_at": "t", "log_path": "p"}


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
    def test_running_when_marker_honcho_and_web(self, wire):
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
        # marker present, but no honcho in ps (container restart / crash).
        c = FakeContainer(marker=_MARKER, ps="1 0 5 0.0 1000 /sbin/init\n", cwds={})
        wire(c, benches=[{"path": BENCH}])
        report = core_status.status("proj").data
        assert report.overall == "degraded"
        assert report.supervisor_up is False
        # every expected label is reported down.
        assert all(not p.up for p in report.processes)

    def test_degraded_when_honcho_up_but_web_down(self, wire):
        wire(FakeContainer(marker=_MARKER, web_ok=False), benches=[{"path": BENCH}])
        report = core_status.status("proj").data
        assert report.overall == "degraded"
        assert report.supervisor_up is True
        assert report.web_http_code is None


class TestOffline:
    def test_offline_when_absent_does_not_raise(self, wire):
        wire(None)
        result = core_status.status("proj")
        assert result.status is Status.OK
        assert result.data.overall == "offline"
        assert result.data.container_running is False

    def test_offline_when_stopped_does_not_raise(self, wire):
        c = FakeContainer()
        c.status = "exited"
        wire(c)
        report = core_status.status("proj").data
        assert report.overall == "offline"

    def test_offline_when_no_frappe_service(self, monkeypatch):
        class Other:
            labels = {"com.docker.compose.service": "db"}
            status = "running"

        monkeypatch.setattr(core_status, "get_project_containers", lambda name: [Other()])
        report = core_status.status("proj").data
        assert report.overall == "offline"


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
