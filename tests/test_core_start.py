"""``core.start`` branch coverage against faked I/O (task 2.4).

Every fork: the launch outcome, the idempotent no-op (no second supervisord), the
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
from tests.test_core_supervision import _PROCFILE, _PS_SINGLE, BENCH, FakeContainer


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
        # No supervisord yet -> a real launch. Start from a stack with no supervisord.
        frappe = FakeContainer(ps="1 0 5 0.0 1000 /sbin/init\n", cwds={})
        frappe.status = "exited"
        db = FakeSvc("db")
        wire(frappe, extra=[db])

        result = core_start.start("proj")
        assert result.status is Status.OK
        outcome = result.data
        assert outcome.already_running is False
        assert outcome.supervisor == "supervisord"
        assert outcome.bench_path == resolvers.DEFAULT_BENCH_PATH
        assert outcome.log_path == supervision.logs_dir(resolvers.DEFAULT_BENCH_PATH)
        # Stopped sibling container was started; a real launch happened; marker written.
        assert db.started is True
        assert frappe.launches, "supervisord must be launched"
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

    def test_launch_waits_for_web_and_reports_ready(self, wire):
        # A genuine launch blocks until the bench's OWN port serves, then reports
        # web_ready=True (the fake serves 200 by default) - closing the start->status
        # race.
        frappe = FakeContainer(ps="1 0 5 0.0 1000 /sbin/init\n", cwds={})
        wire(frappe)
        result = core_start.start("proj")
        assert result.data.web_ready is True
        assert not any(w.code == "start.web_not_ready" for w in result.warnings)

    def test_the_wait_polls_the_benchs_own_port(self, wire):
        # The audit's F5: this wait used to poll a hardcoded :8000, so
        # `cwcli start <p> --bench 1` spent 60 seconds watching bench 0's port and
        # then warned that a perfectly healthy bench had not started.
        frappe = FakeContainer(
            ps="1 0 5 0.0 1000 /sbin/init\n",
            cwds={},
            web_code={8001: "200"},
            configs={"/w/b1": {"webserver_port": 8001, "socketio_port": 9001}},
        )
        wire(frappe, benches=[{"path": "/w/b0"}, {"path": "/w/b1"}])
        result = core_start.start("proj", bench="1")
        assert result.data.web_ready is True
        probed = {cmd[-1] for cmd in frappe.calls if isinstance(cmd, list) and cmd[0] == "curl"}
        assert probed == {"http://localhost:8001"}

    def test_an_unresolvable_port_skips_the_wait_rather_than_guessing(self, wire, monkeypatch):
        # Fail honest: with no readable port the wait is SKIPPED and web_ready stays
        # None (its existing "not probed" value). Falling back to 8000 would spend the
        # whole timeout probing a sibling bench's server and then lie about the result.
        frappe = FakeContainer(ps="1 0 5 0.0 1000 /sbin/init\n", cwds={}, configs={BENCH: None})
        wire(frappe)
        called = {"n": 0}
        monkeypatch.setattr(
            supervision,
            "wait_web_ready",
            lambda *a, **k: called.__setitem__("n", called["n"] + 1),
        )
        result = core_start.start("proj")
        assert result.data.web_ready is None
        assert called["n"] == 0, "must never wait on a guessed port"
        assert any(w.code == "start.web_port_unknown" for w in result.warnings)
        assert not any(w.code == "start.web_not_ready" for w in result.warnings)

    def test_launch_warns_when_web_never_serves(self, wire, monkeypatch):
        # Web that never binds: start still succeeds (the stack IS launched) but
        # reports web_ready=False and emits the honest warning. wait_web_ready is
        # stubbed to return immediately so the test doesn't wait the real timeout.
        frappe = FakeContainer(ps="1 0 5 0.0 1000 /sbin/init\n", cwds={})
        wire(frappe)
        monkeypatch.setattr(supervision, "wait_web_ready", lambda *a, **k: False)
        result = core_start.start("proj")
        assert result.status is Status.OK
        assert result.data.web_ready is False
        assert any(w.code == "start.web_not_ready" for w in result.warnings)

    def test_launch_skips_web_wait_when_no_web_program(self, wire, monkeypatch):
        # A Procfile without a `web` program never waits on :8000 (web_ready=None).
        no_web_procfile = "redis_cache: redis-server\nschedule: bench schedule\n"
        frappe = FakeContainer(ps="1 0 5 0.0 1000 /sbin/init\n", cwds={}, procfile=no_web_procfile)
        wire(frappe)
        called = {"n": 0}
        real = supervision.wait_web_ready
        monkeypatch.setattr(
            supervision,
            "wait_web_ready",
            lambda *a, **k: (called.__setitem__("n", called["n"] + 1), real(*a, **k))[1],
        )
        result = core_start.start("proj")
        assert result.data.web_ready is None
        assert called["n"] == 0, "must not probe web when the Procfile has no web program"


class TestIdempotent:
    def test_already_running_is_a_clean_noop(self, wire):
        # supervisord already up for BENCH -> no second launch.
        frappe = FakeContainer()  # _PS_SINGLE has supervisord pid 100 at BENCH
        wire(frappe, benches=[{"path": BENCH}])
        result = core_start.start("proj")
        assert result.status is Status.OK
        assert result.data.already_running is True
        assert result.data.web_ready is None, "no-op must not re-probe the web"
        assert result.data.bench_path == BENCH
        assert frappe.launches == [], "must NOT launch a second supervisord"
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

    def test_already_running_reports_a_crashed_worker_as_down(self, wire):
        # supervisord up for BENCH, but the worker child died: the readout must
        # show the dead label, not omit it.
        ps = _PS_SINGLE.replace(
            "105 100 499 0.3 70000 /env/bin/python /env/bin/bench worker --queue default\n", ""
        )
        frappe = FakeContainer(ps=ps)
        wire(frappe, benches=[{"path": BENCH}])
        result = core_start.start("proj")
        assert result.data.already_running is True
        procs = {p.label: p.pid for p in result.data.processes}
        assert procs["worker:default"] is None
        assert procs["web"] is not None


class TestNoHostPortWarning:
    """A bench whose web port has no live host publish must warn loudly -
    "running" must stop meaning "reachable" for a container that was created
    (or recreated) without its port mapping. Covers both the fresh-launch
    path and the idempotent no-op, since a portless instance that is already
    running hits the no-op on every later ``cwcli start``."""

    def test_fresh_launch_warns_when_no_host_port_is_published(self, wire):
        # FakeContainer carries no `.ports` attribute by default - the same
        # shape as a real container created without any -p mapping.
        frappe = FakeContainer(ps="1 0 5 0.0 1000 /sbin/init\n", cwds={})
        wire(frappe)
        result = core_start.start("proj")
        assert result.status is Status.OK
        assert any(w.code == "start.no_host_port" for w in result.warnings)

    def test_fresh_launch_is_silent_when_the_host_port_is_published(self, wire):
        frappe = FakeContainer(ps="1 0 5 0.0 1000 /sbin/init\n", cwds={})
        frappe.ports = {"8000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "21000"}]}
        wire(frappe)
        result = core_start.start("proj")
        assert result.status is Status.OK
        assert not any(w.code == "start.no_host_port" for w in result.warnings)

    def test_stopped_ported_container_reloads_bindings_after_start(self, wire):
        class StoppedPortedContainer(FakeContainer):
            def __init__(self):
                super().__init__(ps="1 0 5 0.0 1000 /sbin/init\n", cwds={})
                self.status = "exited"
                self.ports = {}
                self.reloads = 0

            def start(self):
                self.status = "running"

            def reload(self):
                self.reloads += 1
                self.ports = {"8000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "21000"}]}

        frappe = StoppedPortedContainer()
        wire(frappe)
        result = core_start.start("proj")
        assert frappe.reloads == 1
        assert not any(w.code == "start.no_host_port" for w in result.warnings)

    def test_idempotent_noop_also_warns_when_portless(self, wire):
        frappe = FakeContainer()  # supervisord already up for BENCH
        wire(frappe, benches=[{"path": BENCH}])
        result = core_start.start("proj")
        assert result.data.already_running is True
        assert any(w.code == "start.no_host_port" for w in result.warnings)

    def test_idempotent_noop_is_silent_when_ported(self, wire):
        frappe = FakeContainer()
        frappe.ports = {"8000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "21000"}]}
        wire(frappe, benches=[{"path": BENCH}])
        result = core_start.start("proj")
        assert result.data.already_running is True
        assert not any(w.code == "start.no_host_port" for w in result.warnings)


class TestRestart:
    def test_restart_terminates_then_relaunches(self, wire):
        # supervisord already up, but restart=True forces a genuine relaunch (the
        # post-restore restart), NOT the idempotent no-op.
        frappe = FakeContainer()  # supervisord pid 100 at BENCH
        wire(frappe, benches=[{"path": BENCH}])
        result = core_start.start("proj", bench_path=BENCH, restart=True)
        assert result.status is Status.OK
        assert result.data.already_running is False
        assert frappe.killed, "restart must terminate the running supervisord"
        assert frappe.launches, "restart must relaunch supervisord"


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
        # supervisord for the explicit bench is already up -> no-op, proving path use.
        ps = (
            "1 0 5 0.0 1000 /sbin/init\n"
            "300 1 100 0.1 2000 /env/bin/python /env/bin/supervisord "
            f"-c {supervision._config_path(explicit)}\n"
            "301 300 99 0.4 80000 /env/bin/python /env/bin/bench serve\n"
        )
        frappe = FakeContainer(ps=ps, cwds={}, procfile=_PROCFILE)
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
