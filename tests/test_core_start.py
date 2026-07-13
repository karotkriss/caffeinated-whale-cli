"""``core.start`` branch coverage against faked I/O (task 2.4).

Every fork: the launch outcome, the idempotent no-op (no second honcho), the
multi-bench NEEDS_CHOICE, an explicit ``bench_path`` used verbatim, and the
missing-project / docker-unreachable hard errors. The core prints/prompts/exits
nothing, so these are plain function calls.
"""

from __future__ import annotations

import pytest

from caffeinated_whale_cli.core import resolvers, supervision
from caffeinated_whale_cli.core import start as core_start
from caffeinated_whale_cli.core.envelope import Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from tests.test_core_supervision import _PROCFILE, BENCH, FakeContainer


class FakeSvc:
    """A non-frappe project container (db/redis) to prove start brings it up."""

    def __init__(self, service, status="exited"):
        self.labels = {"com.docker.compose.service": service}
        self.status = status
        self.started = False

    def start(self):
        self.started = True
        self.status = "running"

    def reload(self):
        pass


@pytest.fixture
def wire(monkeypatch):
    def _wire(frappe, *, extra=None, benches=None):
        containers = [*(extra or []), frappe]
        monkeypatch.setattr(core_start, "get_project_containers", lambda name: containers)
        monkeypatch.setattr(
            resolvers.db_utils,
            "get_cached_project_data",
            lambda name: {"bench_instances": benches} if benches is not None else None,
        )
        return containers

    return _wire


class TestLaunch:
    def test_launch_returns_process_set_and_writes_marker(self, wire):
        # No honcho yet -> a real launch. Start from a stack with no honcho.
        frappe = FakeContainer(ps="1 0 5 0.0 1000 /sbin/init\n", cwds={})
        frappe.status = "exited"
        db = FakeSvc("db")
        wire(frappe, extra=[db])

        result = core_start.start("proj")
        assert result.status is Status.OK
        outcome = result.data
        assert outcome.already_running is False
        assert outcome.supervisor == "honcho"
        assert outcome.bench_path == resolvers.DEFAULT_BENCH_PATH
        assert outcome.log_path == supervision.bench_start_log_path(resolvers.DEFAULT_BENCH_PATH)
        # Stopped sibling container was started; a real launch happened; marker written.
        assert db.started is True
        assert frappe.launches, "bench start must be launched"
        assert supervision._marker_path(resolvers.DEFAULT_BENCH_PATH) in frappe.writes
        # The launched set is the EXPECTED Procfile labels (from the live Procfile).
        assert {p.label for p in outcome.processes} == {
            "redis_cache",
            "redis_queue",
            "web",
            "socketio",
            "watch",
            "schedule",
            "worker:default",
        }

    def test_no_cache_warns_default_used(self, wire):
        frappe = FakeContainer(ps="1 0 5 0.0 1000 /sbin/init\n", cwds={})
        wire(frappe)
        result = core_start.start("proj")
        assert any(w.code == "bench.default_used" for w in result.warnings)


class TestIdempotent:
    def test_already_running_is_a_clean_noop(self, wire):
        # honcho already up for BENCH -> no second launch.
        frappe = FakeContainer()  # _PS_SINGLE has honcho pid 100 at BENCH
        wire(frappe, benches=[{"path": BENCH}])
        result = core_start.start("proj")
        assert result.status is Status.OK
        assert result.data.already_running is True
        assert result.data.bench_path == BENCH
        assert frappe.launches == [], "must NOT launch a second honcho"
        # No marker rewrite on the no-op path (started_at is preserved).
        assert supervision._marker_path(BENCH) not in frappe.writes
        assert {p.label for p in result.data.processes} == {
            "web",
            "socketio",
            "schedule",
            "watch",
            "worker:default",
            "redis_cache",
            "redis_queue",
        }


class TestRestart:
    def test_restart_terminates_then_relaunches(self, wire):
        # honcho already up, but restart=True forces a genuine relaunch (the
        # post-restore restart), NOT the idempotent no-op.
        frappe = FakeContainer()  # honcho pid 100 at BENCH
        wire(frappe, benches=[{"path": BENCH}])
        result = core_start.start("proj", bench_path=BENCH, restart=True)
        assert result.status is Status.OK
        assert result.data.already_running is False
        assert frappe.killed, "restart must terminate the running honcho"
        assert frappe.launches, "restart must relaunch bench start"


class TestChoices:
    def test_multi_bench_returns_select_bench(self, wire):
        frappe = FakeContainer(ps="1 0 5 0.0 1000 /sbin/init\n", cwds={})
        wire(frappe, benches=[{"path": "/w/b0"}, {"path": "/w/b1"}])
        result = core_start.start("proj")
        assert result.status is Status.NEEDS_CHOICE
        assert result.choice.kind == "select_bench"
        assert frappe.launches == [], "nothing started on an unresolved multi-bench"


class TestExplicitPath:
    def test_explicit_bench_path_is_used_verbatim(self, wire):
        explicit = "/workspace/restored-bench"
        # honcho for the explicit bench is already up -> no-op, proving the path was used.
        ps = (
            "1 0 5 0.0 1000 /sbin/init\n"
            "300 1 100 0.1 2000 /env/bin/python /env/bin/honcho start\n"
            "301 300 99 0.4 80000 /env/bin/python /env/bin/bench serve\n"
        )
        frappe = FakeContainer(ps=ps, cwds={300: explicit}, procfile=_PROCFILE)
        # Multi-bench cache present, but bench_path must win verbatim (post-restore).
        wire(frappe, benches=[{"path": "/w/b0"}, {"path": "/w/b1"}])
        result = core_start.start("proj", bench_path=explicit)
        assert result.status is Status.OK
        assert result.data.bench_path == explicit
        assert result.data.already_running is True


class TestHardErrors:
    def test_missing_project_raises_not_found(self, monkeypatch):
        monkeypatch.setattr(core_start, "get_project_containers", lambda name: [])
        with pytest.raises(CwcliError) as exc:
            core_start.start("ghost")
        assert exc.value.kind is ErrorKind.NOT_FOUND
        assert exc.value.code == "project.not_found"

    def test_docker_unreachable_raises_docker(self, monkeypatch):
        monkeypatch.setattr(core_start, "get_project_containers", lambda name: None)
        with pytest.raises(CwcliError) as exc:
            core_start.start("proj")
        assert exc.value.kind is ErrorKind.DOCKER

    def test_no_frappe_service_raises_not_found(self, wire):
        wire(FakeSvc("db", status="running"))  # only a db, no frappe
        with pytest.raises(CwcliError) as exc:
            core_start.start("proj")
        assert exc.value.code == "frappe.not_found"
