"""``core.status`` branch coverage against faked I/O (task 3.4).

Every ``overall`` branch (offline / online / running / degraded), the
stopped-is-offline-not-raised contract vs the nonexistent-project NOT_FOUND raise,
supervisor-down vs never-started, the multi-bench NEEDS_CHOICE sharing start's
selector, and the docker-unreachable raise.
"""

from __future__ import annotations

import json

import pytest

from caffeinated_whale_cli.core import maintenance as core_maintenance
from caffeinated_whale_cli.core import resolvers
from caffeinated_whale_cli.core import status as core_status
from caffeinated_whale_cli.core.envelope import Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from tests import maintenance_fakes
from tests.test_core_supervision import (
    _CTL_SINGLE,
    _PROCFILE,
    _PS_HONCHO,
    _PS_SINGLE,
    BENCH,
    FakeContainer,
)

_MARKER = {"supervisor": "supervisord", "started_at": "t", "config_path": "p"}


def _bench(report, index: int = 0):
    """The single bench a one-bench report carries.

    ``supervisor_up``/``web_http_code``/``processes``/``not_cwcli_supervised`` are
    per-BENCH facts and now live where they belong; the report itself carries only
    the instance-level fold.
    """
    return report.benches[index]


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
        assert _bench(report).supervisor_up is True
        assert _bench(report).web_http_code == "200"
        web = next(p for p in _bench(report).processes if p.label == "web")
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
        assert _bench(report).supervisor_up is False
        # every expected label is reported down.
        assert all(not p.up for p in _bench(report).processes)

    def test_degraded_when_supervisor_up_but_web_down(self, wire):
        wire(FakeContainer(marker=_MARKER, web_ok=False), benches=[{"path": BENCH}])
        report = core_status.status("proj").data
        assert report.overall == "degraded"
        assert _bench(report).supervisor_up is True
        assert _bench(report).web_http_code is None

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
        assert _bench(report).supervisor_up is True
        worker = next(p for p in _bench(report).processes if p.label == "worker:default")
        assert worker.state == "FATAL"
        assert worker.up is False


class TestNotCwcliSupervisedFallback:
    """The REGRESSION guard: a bench under honcho (no cwcli supervisord) must report
    its processes UP + the not-cwcli-supervised flag/hint, NOT a false all-down."""

    def test_honcho_instance_reports_processes_up_not_all_down(self, wire):
        # No marker (cwcli never started it), honcho running the Procfile, web up.
        c = FakeContainer(marker=None, ps=_PS_HONCHO, cwds={200: BENCH}, web_code="200")
        wire(c, benches=[{"path": BENCH}])
        report = core_status.status("proj").data
        # Not the regression's false all-down: every expected process is UP.
        assert _bench(report).not_cwcli_supervised is True
        assert _bench(report).supervisor_up is False  # cwcli's supervisord is NOT up
        assert all(p.up for p in _bench(report).processes)
        web = next(p for p in _bench(report).processes if p.label == "web")
        assert web.up is True and web.pid == 201
        # supervisord state is unavailable in the fallback (up is the ps truth).
        assert web.state is None
        # overall is the real state, never offline/online-all-down.
        assert report.overall == "running"

    def test_hint_warning_is_surfaced(self, wire):
        c = FakeContainer(marker=None, ps=_PS_HONCHO, cwds={200: BENCH}, web_code="200")
        wire(c, benches=[{"path": BENCH}])
        result = core_status.status("proj")
        assert any("cwcli start" in w.text for w in result.warnings)

    def test_degraded_when_a_process_is_missing_under_honcho(self, wire):
        # honcho up but the worker died (not in ps) -> honestly degraded, still
        # flagged not-cwcli-supervised (not all-down).
        ps = _PS_HONCHO.replace(
            "205 200 499 0.3 70000 /env/bin/python /env/bin/bench worker --queue default\n", ""
        )
        c = FakeContainer(marker=None, ps=ps, cwds={200: BENCH}, web_code="200")
        wire(c, benches=[{"path": BENCH}])
        report = core_status.status("proj").data
        assert _bench(report).not_cwcli_supervised is True
        assert report.overall == "degraded"
        worker = next(p for p in _bench(report).processes if p.label == "worker:default")
        assert worker.up is False

    def test_no_manager_at_all_is_not_flagged(self, wire):
        # Neither cwcli supervisord nor honcho, no marker -> the never-started
        # (online) path, NOT the not-cwcli-supervised fallback.
        c = FakeContainer(marker=None, ps="1 0 5 0.0 1000 /sbin/init\n", cwds={})
        wire(c, benches=[{"path": BENCH}])
        report = core_status.status("proj").data
        assert _bench(report).not_cwcli_supervised is False
        assert report.overall == "online"

    def test_supervised_instance_is_not_flagged(self, wire):
        # The v3 supervisord path is untouched: never flagged not-cwcli-supervised.
        wire(FakeContainer(marker=_MARKER, web_code="200"), benches=[{"path": BENCH}])
        report = core_status.status("proj").data
        assert _bench(report).not_cwcli_supervised is False
        assert _bench(report).supervisor_up is True
        assert report.overall == "running"


class TestProbeWeb:
    """``probe_web=False`` (the ``--watch`` loop) must never hit the web server."""

    def test_no_web_probe_when_suppressed(self, wire):
        # THE load-bearing behavior: a watch tick reads process health from ``ps``
        # but must NOT run the ``curl localhost:8000`` web probe (which is what
        # spams the bench's access logs).
        c = FakeContainer(marker=_MARKER, web_code="200")
        wire(c, benches=[{"path": BENCH}])
        report = core_status.status("proj", probe_web=False).data
        assert _bench(report).web_http_code is None
        assert not any(isinstance(cmd, list) and cmd[0] == "curl" for cmd in c.calls)

    def test_running_without_web_probe_when_supervisor_up(self, wire):
        # With the web probe suppressed, supervisor-up + all-healthy drives
        # ``running`` - a missing web code must NOT falsely degrade the aggregate.
        wire(FakeContainer(marker=_MARKER, web_code="200"), benches=[{"path": BENCH}])
        report = core_status.status("proj", probe_web=False).data
        assert report.overall == "running"
        assert _bench(report).supervisor_up is True

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
        assert _bench(report).web_http_code == "200"
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
    def test_status_never_returns_needs_choice(self, wire):
        # REPLACES test_multi_bench_returns_select_bench. The multi-bench refusal is
        # gone: the bare form ANSWERS the question it used to send the caller away to
        # reconstruct. Asserted across every shape, so the refusal cannot return by
        # accident through some path that was not re-pointed.
        for benches in (
            None,
            [{"path": BENCH}],
            [{"path": "/w/b0"}, {"path": "/w/b1"}],
            [{"path": "/w/b0"}, {"path": "/w/b1"}, {"path": "/w/b2"}],
        ):
            wire(FakeContainer(marker=_MARKER), benches=benches)
            for kwargs in (
                {},
                {"bench": "1"} if benches and len(benches) > 1 else {},
                {"probe_web": False},
            ):
                result = core_status.status("proj", **kwargs)
                assert result.status is not Status.NEEDS_CHOICE
                assert result.choice is None

    def test_multi_bench_reports_every_bench_each_named(self, wire):
        wire(FakeContainer(marker=_MARKER), benches=[{"path": "/w/b0"}, {"path": "/w/b1"}])
        report = core_status.status("proj").data
        assert [b.bench_path for b in report.benches] == ["/w/b0", "/w/b1"]
        assert [b.index for b in report.benches] == [0, 1]

    def test_start_and_status_share_the_bench_selector(self, wire):
        # DE-TAUTOLOGIZED. This test's name has always claimed that --bench 1 resolves
        # to the same path in both verbs, but the old assertion (`overall in (...)`)
        # admitted every non-offline value, so it passed whichever bench was resolved -
        # it could not fail when the property it is named for was broken, BECAUSE the
        # DTO had no bench field to assert on. It does now.
        benches = [{"path": "/w/b0"}, {"path": "/w/b1"}]
        wire(FakeContainer(marker=_MARKER), benches=benches)
        report = core_status.status("proj", bench="1").data
        assert [b.bench_path for b in report.benches] == ["/w/b1"]
        assert report.benches[0].index == 1

    def test_docker_unreachable_raises(self, monkeypatch):
        """`status` raises a DOCKER CwcliError when Docker is unreachable."""
        monkeypatch.setattr(core_status, "get_project_containers", lambda name: None)
        with pytest.raises(CwcliError) as exc:
            core_status.status("proj")
        assert exc.value.kind is ErrorKind.DOCKER


# --------------------------------------------------------------- the per-bench defect
#
# One instance is ONE container holding sibling benches under /workspace, each
# serving the port bench's own make_ports assigned it. The probe used to hardcode
# localhost:8000, so on any bench past the first it measured a DIFFERENT bench's web
# server. These fakes are the two-bench shape the runtime audit proved it on.

_B0 = "/w/b0"
_B1 = "/w/b1"

_PS_TWO_BENCH = f"""\
1 0 99999 0.0 1000 /sbin/init
100 1 500 0.1 2000 /env/bin/python /env/bin/supervisord -c {_B0}/logs/.cwcli-supervisor.conf
101 100 499 0.5 80000 /env/bin/python /env/bin/bench serve --port 8000
102 100 499 0.2 60000 node {_B0}/apps/frappe/socketio.js
103 100 499 0.1 50000 /env/bin/python /env/bin/bench schedule
104 100 499 0.1 50000 /env/bin/python /env/bin/bench watch
105 100 499 0.3 70000 /env/bin/python /env/bin/bench worker --queue default
106 100 499 0.0 3000 redis-server {_B0}/config/redis_cache.conf
107 100 499 0.0 3000 redis-server {_B0}/config/redis_queue.conf
200 1 500 0.1 2000 /env/bin/python /env/bin/supervisord -c {_B1}/logs/.cwcli-supervisor.conf
201 200 499 0.5 80000 /env/bin/python /env/bin/bench serve --port 8001
202 200 499 0.2 60000 node {_B1}/apps/frappe/socketio.js
203 200 499 0.1 50000 /env/bin/python /env/bin/bench schedule
204 200 499 0.1 50000 /env/bin/python /env/bin/bench watch
205 200 499 0.3 70000 /env/bin/python /env/bin/bench worker --queue default
206 200 499 0.0 3000 redis-server {_B1}/config/redis_cache.conf
207 200 499 0.0 3000 redis-server {_B1}/config/redis_queue.conf
"""

# Only bench 1 is up: bench 0 was never started, which is the audit's headline flow
# (`cwcli start <p> --bench 1`).
_PS_ONLY_B1 = "".join(
    line + "\n" for line in _PS_TWO_BENCH.splitlines() if line.startswith(("1 0", "2"))
)

_TWO_BENCH_CONFIGS = {
    _B0: {"webserver_port": 8000, "socketio_port": 9000},
    _B1: {"webserver_port": 8001, "socketio_port": 9001},
}
_TWO_BENCHES = [{"path": _B0}, {"path": _B1}]


def _by_path(report):
    return {b.bench_path: b for b in report.benches}


class TestPerBenchWebProbe:
    """F3/F4: the probe asks each bench's OWN port, and says which one it asked."""

    def test_f3_a_healthy_bench_past_the_first_is_running_not_degraded(self, wire):
        # THE headline regression. Bench 1 is genuinely healthy and serves :8001;
        # bench 0 was never started so nothing answers :8000. Probing the hardcoded
        # 8000 got no code and folded that into `degraded` for a bench whose every
        # process was RUNNING - a document contradicting itself on its own face.
        c = FakeContainer(
            ps=_PS_ONLY_B1,
            cwds={200: _B1},
            markers={_B1: _MARKER},
            web_code={8001: "200"},  # nothing answers :8000
            configs=_TWO_BENCH_CONFIGS,
        )
        wire(c, benches=_TWO_BENCHES)
        report = core_status.status("proj").data

        b1 = _by_path(report)[_B1]
        assert b1.overall == "running"
        assert b1.web_port == 8001 and b1.web_port_verified is True
        assert b1.web_http_code == "200"
        # No bench pairs a RUNNING web process with a null web code.
        for b in report.benches:
            web = next((p for p in b.processes if p.label == "web"), None)
            assert not (web is not None and web.up and b.web_http_code is None)
        # A never-started bench is `online`, not a fault; the instance fold takes the
        # running bench over it (see _fold).
        assert _by_path(report)[_B0].overall == "online"
        assert report.overall == "running"

    def test_f4_a_dead_bench_never_reports_a_siblings_live_code(self, wire):
        # The other output direction: bench 1's web is down while bench 0 serves. The
        # hardcoded probe answered with bench 0's live code, so a bench serving
        # NOTHING looked healthy.
        ps = _PS_TWO_BENCH.replace(
            "201 200 499 0.5 80000 /env/bin/python /env/bin/bench serve --port 8001\n", ""
        )
        ctl_b1 = _CTL_SINGLE.replace(
            "web   RUNNING   pid 101, uptime 0:05:00\n", "web   STOPPED   Not started\n"
        )
        c = FakeContainer(
            ps=ps,
            cwds={100: _B0, 200: _B1},
            markers={_B0: _MARKER, _B1: _MARKER},
            web_code={8000: "200"},  # only bench 0 answers
            ctl_status={_B0: _CTL_SINGLE, _B1: ctl_b1},
            configs=_TWO_BENCH_CONFIGS,
        )
        wire(c, benches=_TWO_BENCHES)
        report = core_status.status("proj").data

        b1 = _by_path(report)[_B1]
        assert b1.web_port == 8001
        assert b1.web_http_code is None  # NOT bench 0's "200"
        assert b1.overall == "degraded"
        assert _by_path(report)[_B0].web_http_code == "200"

    def test_each_bench_is_probed_on_its_own_port(self, wire):
        c = FakeContainer(
            ps=_PS_TWO_BENCH,
            cwds={100: _B0, 200: _B1},
            markers={_B0: _MARKER, _B1: _MARKER},
            web_code={8000: "200", 8001: "404"},
            configs=_TWO_BENCH_CONFIGS,
        )
        wire(c, benches=_TWO_BENCHES)
        core_status.status("proj")
        probed = [cmd[-1] for cmd in c.calls if isinstance(cmd, list) and cmd[0] == "curl"]
        assert probed == ["http://localhost:8000", "http://localhost:8001"]


class TestFailHonestPort:
    """An unreadable port is reported unknown, never guessed as 8000."""

    def test_unreadable_config_reports_unknown_and_issues_zero_probes(self, wire):
        # THE load-bearing constraint: falling back to 8000 IS the bug, so a bench
        # whose config cannot be read is not probed at all.
        c = FakeContainer(marker=_MARKER, web_code="200", configs={BENCH: None})
        wire(c, benches=[{"path": BENCH}])
        result = core_status.status("proj")
        bench = _bench(result.data)
        assert bench.web_port is None
        assert bench.web_port_verified is False
        assert bench.web_http_code is None
        assert not any(isinstance(cmd, list) and cmd[0] == "curl" for cmd in c.calls)
        assert any(w.code == "status.web_port_unknown" for w in result.warnings)

    def test_an_otherwise_healthy_bench_does_not_degrade_on_an_unknown_port(self, wire):
        # Degrading here would manufacture a fresh instance of F3 while fixing the
        # old one: every program RUNNING, reported broken because a JSON read failed.
        c = FakeContainer(marker=_MARKER, configs={BENCH: None})
        wire(c, benches=[{"path": BENCH}])
        report = core_status.status("proj").data
        assert report.overall == "running"
        assert _bench(report).overall == "running"

    def test_a_config_that_omits_the_port_is_probed_on_its_procfile_port(self, wire):
        # The key is optional: a bench served by its hostname through a reverse proxy
        # drops it so Frappe builds URLs without the internal port. Its web process
        # still listens where the Procfile's web line says - 8001 here, so the probe
        # goes there and never to a guessed 8000 (bench 0's server on a multi-bench
        # instance).
        procfile = _PROCFILE.replace("--port 8000", "--port 8001")
        c = FakeContainer(
            marker=_MARKER,
            web_code={8001: "200"},
            configs={BENCH: {"some_other_key": 1}},
            procfile=procfile,
        )
        wire(c, benches=[{"path": BENCH}])
        result = core_status.status("proj")
        bench = _bench(result.data)
        assert bench.web_port == 8001
        assert bench.web_port_verified is True
        assert bench.web_http_code == "200"
        probed = [cmd[-1] for cmd in c.calls if isinstance(cmd, list) and cmd[0] == "curl"]
        assert probed == ["http://localhost:8001"]
        assert not any(w.code == "status.web_port_unknown" for w in result.warnings)

    def test_no_key_and_no_procfile_web_line_is_unresolved_never_8000(self, wire):
        procfile = "\n".join(line for line in _PROCFILE.splitlines() if not line.startswith("web:"))
        c = FakeContainer(
            marker=_MARKER,
            web_code="200",
            configs={BENCH: {"some_other_key": 1}},
            procfile=procfile,
        )
        wire(c, benches=[{"path": BENCH}])
        result = core_status.status("proj")
        bench = _bench(result.data)
        assert bench.web_port is None
        assert bench.web_port_verified is False
        assert not any(isinstance(cmd, list) and cmd[0] == "curl" for cmd in c.calls)
        assert any(w.code == "status.web_port_unknown" for w in result.warnings)


class TestInstanceFold:
    """Four tokens, no fifth. `degraded` dominates; `running` beats never-started."""

    @staticmethod
    def _folded(*overalls):
        benches = [
            core_status.BenchStatus(
                index=i,
                bench_path=f"/w/b{i}",
                label=None,
                overall=o,
                supervisor_up=True,
                web_port=8000 + i,
                web_port_verified=True,
                web_site="site.localhost",
                web_http_code="200",
                processes=[],
            )
            for i, o in enumerate(overalls)
        ]
        return core_status._fold(benches)

    def test_running_beats_a_never_started_online_bench(self):
        # The one non-obvious rule, and the one the audit's headline scenario turns
        # on. A plain worst-wins fold ordered degraded > online > running would say
        # `online` - "nothing is started" - while a bench serves real traffic.
        assert self._folded("online", "running") == "running"
        assert self._folded("running", "online") == "running"

    def test_degraded_dominates_everything(self):
        assert self._folded("running", "degraded") == "degraded"
        assert self._folded("online", "degraded") == "degraded"
        assert self._folded("degraded", "running", "online") == "degraded"

    def test_all_online_stays_online(self):
        assert self._folded("online", "online") == "online"

    def test_container_down_is_offline_with_no_benches(self, wire):
        c = FakeContainer()
        c.status = "exited"
        wire(c, benches=_TWO_BENCHES)
        report = core_status.status("proj").data
        assert report.overall == "offline"
        assert report.benches == []


class TestTheProbeNamesTheSite:
    """Frappe routes by ``Host``, so the probe must name a site.

    A host-less request names no site, and Frappe correctly answers 404 - which is
    what a fully healthy bench reported on every read. The aggregate was never wrong
    (any code counts as serving), but a health field whose normal value is an error
    code teaches its reader to discount it, and that habit is what would make a
    genuine ``degraded`` go unread.
    """

    def _sites(self, monkeypatch, mapping):
        """Stand in for the cached per-bench site lists the resolver reads."""
        monkeypatch.setattr(resolvers.db_utils, "get_default_site", lambda p, b=None: None)
        monkeypatch.setattr(
            resolvers.db_utils,
            "get_all_site_configs",
            lambda p, b=None: {s: {} for s in mapping.get(b, [])},
        )

    def test_the_probe_sends_the_benchs_site_as_host(self, wire, monkeypatch):
        self._sites(monkeypatch, {BENCH: ["one.localhost"]})
        c = FakeContainer(marker=_MARKER, web_code="200")
        wire(c, benches=[{"path": BENCH}])

        report = core_status.status("proj").data

        curl = next(cmd for cmd in c.calls if isinstance(cmd, list) and cmd[0] == "curl")
        assert "Host: one.localhost" in curl
        # And the report says WHICH site the code belongs to, so the number stays
        # attributable the same way web_port makes it attributable to a bench.
        assert _bench(report).web_site == "one.localhost"

    def test_each_bench_is_probed_for_its_own_site(self, wire, monkeypatch):
        self._sites(monkeypatch, {_B0: ["one.localhost"], _B1: ["two.localhost"]})
        c = FakeContainer(
            ps=_PS_TWO_BENCH,
            cwds={100: _B0, 200: _B1},
            markers={_B0: _MARKER, _B1: _MARKER},
            web_code={8000: "200", 8001: "200"},
            configs=_TWO_BENCH_CONFIGS,
        )
        wire(c, benches=_TWO_BENCHES)

        report = core_status.status("proj").data

        by_path = _by_path(report)
        assert by_path[_B0].web_site == "one.localhost"
        assert by_path[_B1].web_site == "two.localhost"
        hosts = [
            cmd[cmd.index("-H") + 1]
            for cmd in c.calls
            if isinstance(cmd, list) and cmd[0] == "curl" and "-H" in cmd
        ]
        assert sorted(hosts) == ["Host: one.localhost", "Host: two.localhost"]

    def test_a_bench_with_no_site_probes_host_less_and_says_so(self, wire, monkeypatch):
        self._sites(monkeypatch, {})
        c = FakeContainer(marker=_MARKER, web_code="404")
        wire(c, benches=[{"path": BENCH}])

        report = core_status.status("proj").data

        curl = next(cmd for cmd in c.calls if isinstance(cmd, list) and cmd[0] == "curl")
        assert "-H" not in curl
        assert _bench(report).web_site is None  # unknown, never guessed

    def test_watch_mode_reports_no_site_because_it_probed_nothing(self, wire, monkeypatch):
        # web_site describes a probe that happened. With the probe suppressed there
        # is no site to attribute, and claiming one would imply a request was made.
        self._sites(monkeypatch, {BENCH: ["one.localhost"]})
        wire(FakeContainer(marker=_MARKER, web_code="200"), benches=[{"path": BENCH}])

        report = core_status.status("proj", probe_web=False).data

        assert _bench(report).web_site is None
        assert _bench(report).web_http_code is None


class TestBenchPresence:
    """``bench_present`` - whether the bench this row is ABOUT still exists.

    The bench list comes from the cache, which outlives the benches it describes.
    No amount of live health probing settles this: a deleted bench has no marker
    and no supervisord, which is EXACTLY what a bench that was never started looks
    like, so a removed bench reported ``online`` - "here, just not up".

    Positive first, deliberately: a check that answered "gone" about everything
    would pass the removed-bench assertion and be worthless.
    """

    def test_a_live_bench_is_reported_present(self, wire):
        wire(FakeContainer(marker=_MARKER, web_code="200"), benches=[{"path": BENCH}])
        report = core_status.status("proj").data

        assert _bench(report).bench_present == "present"
        # Untouched: presence is reported ALONGSIDE health, never folded into it.
        assert _bench(report).overall == "running"
        assert report.overall == "running"

    def test_a_removed_bench_is_absent_not_a_never_started_one(self, wire):
        # b0 is gone from disk; b1 is a genuinely healthy bench. Before this token
        # both rows were `overall`-only and b0 read as `online`.
        c = FakeContainer(
            ps=_PS_ONLY_B1,
            cwds={200: _B1},
            markers={_B1: _MARKER},
            web_code={8001: "200"},
            configs=_TWO_BENCH_CONFIGS,
            absent_paths={_B0},
        )
        wire(c, benches=_TWO_BENCHES)
        result = core_status.status("proj")
        report = result.data

        by_path = _by_path(report)
        assert by_path[_B1].bench_present == "present"
        assert by_path[_B0].bench_present == "absent"
        # Still reported, never pruned - and the remedy is named.
        assert len(report.benches) == 2
        stale = next(w for w in result.warnings if w.code == "status.stale_benches")
        assert _B0 in stale.text and "inspect" in stale.text
        assert stale.detail == {"benches": [_B0]}
        # `overall` keeps its four tokens; the live half of the answer is unchanged.
        assert by_path[_B1].overall == "running"

    def test_an_unaskable_probe_is_unverified_never_all_absent(self, wire):
        # Fail-honest in BOTH directions: a probe that could not run must not read
        # as "every bench is gone", which is the same wrong answer, just louder.
        wire(
            FakeContainer(marker=_MARKER, web_code="200", presence_probe_fails=True),
            benches=[{"path": BENCH}],
        )
        result = core_status.status("proj")

        assert _bench(result.data).bench_present == "unverified"
        assert [w.code for w in result.warnings] == []

    def test_the_fused_tier_answers_it_too(self, wire):
        # `cwcli serve`'s FAST tier trades round trips, never honesty.
        wire(
            FakeContainer(marker=_MARKER, web_code="200", absent_paths={BENCH}),
            benches=[{"path": BENCH}],
        )
        report = core_status.status("proj", fused=True).data

        assert _bench(report).bench_present == "absent"


class TestFusedFastPath:
    """``fused=True`` - the exec-budget option ``cwcli serve``'s FAST tier polls on.

    The contract it must NOT break: the same report shape, the same tokens, and no
    honesty traded for round trips. Each test below pins one half of that.
    """

    def test_supervised_bench_is_one_exec_and_agrees_with_the_default_path(self, wire):
        wire(FakeContainer(marker=_MARKER, web_code="200"), benches=[{"path": BENCH}])
        slow = core_status.status("proj").data
        wire(FakeContainer(marker=_MARKER, web_code="200"), benches=[{"path": BENCH}])
        fast = core_status.status("proj", fused=True).data

        assert fast.overall == slow.overall == "running"
        b_fast, b_slow = _bench(fast), _bench(slow)
        assert b_fast.supervisor_up == b_slow.supervisor_up
        # The fused tier never sends an HTTP request: it reports the TCP verdict.
        assert b_slow.web_http_code == "200"
        assert b_fast.web_http_code is None
        assert b_fast.web_listening is True
        assert b_fast.web_port == b_slow.web_port == 8000
        assert {p.label for p in b_fast.processes} == {p.label for p in b_slow.processes}

    def test_the_health_read_itself_is_a_single_exec(self, wire):
        c = FakeContainer(marker=_MARKER, web_code="200")
        wire(c, benches=[{"path": BENCH}])
        core_status.status("proj", fused=True)

        # ps / supervisorctl / curl / Procfile / marker collapse into one bash -c.
        bash = [cmd for cmd in c.calls if isinstance(cmd, list) and cmd[0] == "bash"]
        assert len(bash) == 1
        assert not [cmd for cmd in c.calls if isinstance(cmd, list) and cmd[0] in ("ps", "curl")]

    def test_the_fused_tier_never_sends_an_http_request(self, wire):
        # Every tick of the web UI daemon's loop lands here; a curl would make the
        # bench's web server log a "GET /" line each time. Covers the supervised
        # steady state AND the honcho bench that delegates to the full read.
        for c in (
            FakeContainer(marker=_MARKER, web_code="200"),
            FakeContainer(marker=None, ps=_PS_HONCHO, cwds={200: BENCH}, web_code="200"),
        ):
            wire(c, benches=[{"path": BENCH}])
            core_status.status("proj", fused=True)
            argv = [" ".join(map(str, x)) if isinstance(x, list) else str(x) for x in c.calls]
            assert not [a for a in argv if "http://" in a]

    def test_a_port_that_refuses_degrades_a_fused_read(self, wire):
        wire(FakeContainer(marker=_MARKER, web_ok=False), benches=[{"path": BENCH}])
        report = core_status.status("proj", fused=True).data
        assert _bench(report).web_listening is False
        assert report.overall == "degraded"

    def test_a_fatal_program_still_degrades(self, wire):
        # supervisorctl enumerates every program it manages, so the fused read sees
        # a crash-looping program with no live process WITHOUT the Procfile read.
        ps = _PS_SINGLE.replace(
            "105 100 499 0.3 70000 /env/bin/python /env/bin/bench worker --queue default\n", ""
        )
        ctl = _CTL_SINGLE.replace(
            "worker_default   RUNNING   pid 105, uptime 0:05:00\n",
            "worker_default   FATAL   Exited too quickly\n",
        )
        wire(
            FakeContainer(marker=_MARKER, ps=ps, ctl_status=ctl, web_code="200"),
            benches=[{"path": BENCH}],
        )
        report = core_status.status("proj", fused=True).data

        assert report.overall == "degraded"
        worker = next(p for p in _bench(report).processes if p.label == "worker:default")
        assert worker.state == "FATAL" and worker.up is False

    def test_never_started_still_reads_online_not_offline(self, wire):
        # The marker distinction the fused script cannot make: an unsupervised bench
        # DELEGATES to the full read rather than guessing. Speed never buys a
        # never-started bench being reported as a crashed one.
        c = FakeContainer(marker=None, ps="1 0 5 0.0 1000 /sbin/init\n", cwds={})
        wire(c, benches=[{"path": BENCH}])
        assert core_status.status("proj", fused=True).data.overall == "online"

    def test_supervisor_died_still_reads_degraded(self, wire):
        c = FakeContainer(marker=_MARKER, ps="1 0 5 0.0 1000 /sbin/init\n", cwds={})
        wire(c, benches=[{"path": BENCH}])
        assert core_status.status("proj", fused=True).data.overall == "degraded"

    def test_a_honcho_bench_still_gets_the_unsupervised_fallback(self, wire):
        # The regression the fallback exists for: a bench genuinely serving under
        # `bench start` must not read as all-down just because the caller asked for
        # the cheap path.
        c = FakeContainer(marker=None, ps=_PS_HONCHO, cwds={200: BENCH}, web_code="200")
        wire(c, benches=[{"path": BENCH}])
        report = core_status.status("proj", fused=True).data

        assert _bench(report).not_cwcli_supervised is True
        assert all(p.up for p in _bench(report).processes)
        assert report.overall == "running"

    def test_probe_web_false_leaves_no_http_trace(self, wire):
        c = FakeContainer(marker=_MARKER, web_code="200")
        wire(c, benches=[{"path": BENCH}])
        report = core_status.status("proj", fused=True, probe_web=False).data

        script = next(cmd for cmd in c.calls if isinstance(cmd, list) and cmd[0] == "bash")[2]
        assert "curl" not in script
        assert _bench(report).web_http_code is None
        assert _bench(report).web_site is None
        assert report.overall == "running"  # no web signal must not degrade it


def _maint_warnings(result):
    return [w for w in result.warnings if w.code == "status.maintenance_mode"]


class TestMaintenanceMode:
    """``status`` says PLAINLY when sites are in maintenance mode, across EVERY site of
    the bench, and names the one command that takes them out (BUG-11: a killed migrate
    leaves a site 503 forever with no other signal)."""

    @pytest.fixture(autouse=True)
    def _site(self, monkeypatch):
        monkeypatch.setattr(
            core_status.resolvers, "resolve_representative_site", lambda *a, **k: "dev.localhost"
        )

    def test_reports_a_site_in_its_own_maintenance_and_names_the_way_out(self, wire):
        c = FakeContainer(
            marker=_MARKER,
            web_code="503",
            site_configs={(BENCH, "dev.localhost"): {"maintenance_mode": 1}},
            ping_answers={"dev.localhost": ("503", True)},
        )
        wire(c, benches=[{"path": BENCH}])

        result = core_status.status("proj")

        bench = _bench(result.data)
        assert bench.maintenance_mode is True
        assert bench.maintenance.down == ["dev.localhost"]
        assert bench.maintenance.sites[0].cause == "site"
        (w,) = _maint_warnings(result)
        assert "dev.localhost" in w.text
        assert "own site_config.json" in w.text
        assert w.text.endswith("Take it out with: cwcli status proj --clear-maintenance")
        assert w.detail == {"bench_path": BENCH}

    def test_every_site_of_the_bench_is_read_not_only_the_probed_one(self, wire):
        c = FakeContainer(
            marker=_MARKER,
            site_configs={
                (BENCH, "dev.localhost"): {},
                (BENCH, "other.localhost"): {"maintenance_mode": 1},
            },
            ping_answers={"other.localhost": ("503", True)},
        )
        wire(c, benches=[{"path": BENCH}])

        bench = _bench(core_status.status("proj").data)

        assert bench.maintenance_mode is False  # the probed site serves
        assert bench.maintenance.down == ["other.localhost"]

    def test_no_warning_and_no_probe_when_nothing_is_in_maintenance(self, wire):
        c = FakeContainer(marker=_MARKER, site_configs={(BENCH, "dev.localhost"): {}})
        wire(c, benches=[{"path": BENCH}])

        result = core_status.status("proj")

        assert _bench(result.data).maintenance_mode is False
        assert _bench(result.data).maintenance.down == []
        assert not _maint_warnings(result)
        # The healthy case costs the one read: no per-site HTTP probe.
        assert not any(maintenance_fakes.is_probe(cmd) for cmd in c.calls)

    def test_a_site_still_answering_session_stopped_is_reported_whatever_the_files_say(self, wire):
        # Frappe v16 answers from a 60-second config cache, so a flag cleared seconds
        # ago is still answered. What the site ANSWERS is the truth.
        c = FakeContainer(
            marker=_MARKER,
            configs={BENCH: {**_PORTS, "maintenance_mode": 1}},
            site_configs={(BENCH, "dev.localhost"): {}, (BENCH, "b.localhost"): {}},
            ping_answers={"dev.localhost": ("503", True), "b.localhost": ("503", True)},
        )
        wire(c, benches=[{"path": BENCH}])
        c.site_configs[(BENCH, "b.localhost")] = {"maintenance_mode": 0}

        bench = _bench(core_status.status("proj").data)

        b = next(s for s in bench.maintenance.sites if s.site == "b.localhost")
        assert (b.on, b.cause, b.session_stopped) == (True, "cached", True)
        assert bench.maintenance.probed is True

    def test_the_fused_tier_reports_it_too_without_any_http_request(self, wire):
        # `cwcli ui`'s FAST tier polls fused=True; its pages show and offer this.
        c = FakeContainer(
            marker=_MARKER,
            web_code="503",
            site_configs={(BENCH, "dev.localhost"): {"maintenance_mode": 1}},
        )
        wire(c, benches=[{"path": BENCH}])

        bench = _bench(core_status.status("proj", fused=True).data)

        assert bench.supervisor_up is True  # the fused path itself, not the delegation
        assert bench.maintenance_mode is True
        assert bench.maintenance.down == ["dev.localhost"]
        assert bench.web_site == "dev.localhost"
        assert not any(maintenance_fakes.is_probe(cmd) for cmd in c.calls)

    def test_watch_mode_skips_the_maintenance_read(self, wire):
        c = FakeContainer(
            marker=_MARKER, site_configs={(BENCH, "dev.localhost"): {"maintenance_mode": 1}}
        )
        wire(c, benches=[{"path": BENCH}])

        result = core_status.status("proj", probe_web=False)

        # The quiet --watch loop leaves the bench untouched.
        assert not any(maintenance_fakes.is_read(cmd) for cmd in c.calls)
        assert _bench(result.data).maintenance_mode is None
        assert _bench(result.data).maintenance is None


_PORTS = {"webserver_port": 8000, "socketio_port": 9000}


class _BenchConfigContainer(FakeContainer):
    """``FakeContainer`` whose benches also answer status's LIVE config reads.

    ``configs[bench]`` is the bench's ``sites/common_site_config.json`` - the same
    dict the port read serves - so a test sets the bench-wide flags where bench
    writes them. ``site_configs[(bench, site)]`` is that site's own file.
    """

    def __init__(self, *, site_configs=None, **kwargs):
        super().__init__(site_configs=site_configs, **kwargs)

    def exec_run(self, cmd, detach=False, workdir=None, environment=None, user=None):
        if isinstance(cmd, str) and cmd.startswith("cat sites/"):
            self.calls.append(cmd)
            if cmd == "cat sites/common_site_config.json":
                config = self.configs.get(workdir)
            else:
                config = self.site_configs.get((workdir, cmd.split("/")[1]))
            return (1, b"") if config is None else (0, json.dumps(config).encode())
        return super().exec_run(
            cmd, detach=detach, workdir=workdir, environment=environment, user=user
        )


_FAILED_UPDATE_FLAGS = {
    "webserver_port": 8000,
    "socketio_port": 9000,
    "maintenance_mode": 1,
    "pause_scheduler": 1,
}


class TestBenchWideFlags:
    """A failed or killed ``bench update`` leaves ``maintenance_mode``/``pause_scheduler``
    set in the bench's COMMON config: every site that does not override them answers
    503 and runs no scheduled job. ``status`` says so, and the one command it names
    clears the bench-wide flags and resumes the scheduler with them."""

    @pytest.fixture(autouse=True)
    def _site(self, monkeypatch):
        monkeypatch.setattr(
            core_status.resolvers, "resolve_representative_site", lambda *a, **k: "dev.localhost"
        )

    def test_a_bench_left_down_by_bench_update_reports_both_flags_and_the_way_out(self, wire):
        c = _BenchConfigContainer(
            marker=_MARKER,
            web_code="503",
            configs={BENCH: _FAILED_UPDATE_FLAGS},
            site_configs={(BENCH, "dev.localhost"): {"db_name": "x"}},
            ping_answers={"dev.localhost": ("503", True)},
        )
        wire(c, benches=[{"path": BENCH}])

        result = core_status.status("proj")

        bench = _bench(result.data)
        assert bench.maintenance_mode is True  # the merged view
        assert bench.maintenance.bench_flags == ["maintenance_mode", "pause_scheduler"]
        (maintenance,) = _maint_warnings(result)
        assert "maintenance_mode is set bench-wide" in maintenance.text
        assert "pause_scheduler" in maintenance.text
        assert "cwcli status proj --clear-maintenance" in maintenance.text
        # The per-site remedy would bring back only one site and leave the rest down.
        assert "set-maintenance-mode off" not in maintenance.text
        assert maintenance.detail == {"bench_path": BENCH}
        scheduler = next(w for w in result.warnings if w.code == "status.scheduler_paused")
        assert "resumes it in the same step" in scheduler.text
        assert scheduler.detail == {"bench_path": BENCH}

    def test_the_bench_wide_flag_is_reported_even_when_every_site_overrides_it(self, wire):
        # `cwcli migrate --site s` ends with `set-maintenance-mode off`, which writes an
        # explicit 0 into s alone: s serves, but the next new site would inherit the 1.
        c = _BenchConfigContainer(
            marker=_MARKER,
            configs={BENCH: _FAILED_UPDATE_FLAGS},
            site_configs={(BENCH, "dev.localhost"): {"maintenance_mode": 0}},
        )
        wire(c, benches=[{"path": BENCH}])

        result = core_status.status("proj")

        assert _bench(result.data).maintenance_mode is False
        (w,) = _maint_warnings(result)
        assert "a new site would inherit it" in w.text

    def test_the_incident_a_site_whose_own_flag_is_0_is_down_when_served_as_the_default(self, wire):
        # Reproduced on a real v16 bench: `bench serve` with a default site answers
        # every Host as that site, so a site whose own file says 0 still answers
        # SessionStopped when the default site inherits the bench-wide 1.
        c = _BenchConfigContainer(
            marker=_MARKER,
            configs={BENCH: {**_FAILED_UPDATE_FLAGS, "default_site": "dev.localhost"}},
            site_configs={
                (BENCH, "dev.localhost"): {},
                (BENCH, "new.localhost"): {"maintenance_mode": 0},
            },
            ping_answers={"dev.localhost": ("503", True), "new.localhost": ("503", True)},
        )
        wire(c, benches=[{"path": BENCH}])

        result = core_status.status("proj")

        report = _bench(result.data).maintenance
        assert report.served_as == "dev.localhost"
        assert report.down == ["dev.localhost", "new.localhost"]
        new = next(s for s in report.sites if s.site == "new.localhost")
        assert (new.cause, new.served_as) == ("served_as", "dev.localhost")
        (w,) = _maint_warnings(result)
        assert "answers every site as the default site 'dev.localhost'" in w.text

    def test_a_paused_scheduler_alone_is_its_own_warning(self, wire):
        c = _BenchConfigContainer(
            marker=_MARKER,
            configs={BENCH: {**_PORTS, "pause_scheduler": 1}},
            site_configs={(BENCH, "dev.localhost"): {}},
        )
        wire(c, benches=[{"path": BENCH}])

        result = core_status.status("proj")

        assert _bench(result.data).maintenance_mode is False
        scheduler = [
            w
            for w in result.warnings
            if w.code in ("status.maintenance_mode", "status.scheduler_paused")
        ]
        assert [w.code for w in scheduler] == ["status.scheduler_paused"]
        assert "cwcli run proj set-config --global --parse pause_scheduler 0" in scheduler[0].text

    def test_a_site_also_set_on_its_own_is_one_warning_naming_both_writes(self, wire):
        # Clearing only the bench-wide flag would leave this site at 503; the one
        # command clears both.
        c = _BenchConfigContainer(
            marker=_MARKER,
            configs={BENCH: _FAILED_UPDATE_FLAGS},
            site_configs={(BENCH, "dev.localhost"): {"maintenance_mode": 1}},
            ping_answers={"dev.localhost": ("503", True)},
        )
        wire(c, benches=[{"path": BENCH}])

        result = core_status.status("proj")

        (w,) = _maint_warnings(result)
        assert "set bench-wide" in w.text and "own site_config.json: dev.localhost" in w.text

    def test_a_clear_bench_warns_nothing(self, wire):
        c = _BenchConfigContainer(
            marker=_MARKER,
            configs={BENCH: {**_PORTS, "maintenance_mode": 0, "pause_scheduler": 0}},
            site_configs={(BENCH, "dev.localhost"): {}},
        )
        wire(c, benches=[{"path": BENCH}])

        result = core_status.status("proj")

        assert _bench(result.data).maintenance_mode is False
        assert not any(
            w.code in ("status.maintenance_mode", "status.scheduler_paused")
            for w in result.warnings
        )

    def test_on_a_multi_bench_instance_the_remedy_names_the_bench(self, wire):
        c = _BenchConfigContainer(
            marker=_MARKER,
            configs={
                "/w/b0": {**_PORTS},
                "/w/b1": {
                    "webserver_port": 8001,
                    "socketio_port": 9001,
                    "maintenance_mode": 1,
                    "pause_scheduler": 1,
                },
            },
            site_configs={("/w/b0", "dev.localhost"): {}, ("/w/b1", "dev.localhost"): {}},
        )
        wire(c, benches=[{"path": "/w/b0"}, {"path": "/w/b1", "label": "staging"}])

        result = core_status.status("proj")

        flagged = [
            w
            for w in result.warnings
            if w.code in ("status.maintenance_mode", "status.scheduler_paused")
        ]
        assert {w.detail["bench_path"] for w in flagged} == {"/w/b1"}
        (w,) = _maint_warnings(result)
        assert "cwcli status proj --bench staging --clear-maintenance" in w.text

    def test_an_uncached_bench_path_gets_a_path_remedy(self, wire):
        c = _BenchConfigContainer(
            marker=_MARKER,
            configs={"/w/other": _FAILED_UPDATE_FLAGS},
            site_configs={("/w/other", "dev.localhost"): {}},
        )
        wire(c, benches=[{"path": BENCH}])

        result = core_status.status("proj", bench_path="/w/other")

        (w,) = _maint_warnings(result)
        assert "cwcli status proj --path /w/other --clear-maintenance" in w.text

    def test_every_config_is_read_in_one_exec(self, wire):
        c = _BenchConfigContainer(
            marker=_MARKER,
            configs={BENCH: _FAILED_UPDATE_FLAGS},
            site_configs={(BENCH, "dev.localhost"): {}, (BENCH, "b.localhost"): {}},
        )
        wire(c, benches=[{"path": BENCH}])

        core_status.status("proj")

        assert sum(1 for cmd in c.calls if maintenance_fakes.is_read(cmd)) == 1
        assert not any(isinstance(cmd, str) and cmd.startswith("cat sites/") for cmd in c.calls)


class TestMaintenanceOrigin:
    """A flag cwcli set and could not clear names the interrupted operation; a flag
    with no record is reported neutrally; a running operation is never offered a
    clear."""

    @pytest.fixture(autouse=True)
    def _site(self, monkeypatch):
        monkeypatch.setattr(
            core_status.resolvers, "resolve_representative_site", lambda *a, **k: "dev.localhost"
        )

    def _container(self, **kwargs):
        return FakeContainer(
            marker=_MARKER,
            site_configs={(BENCH, "dev.localhost"): {"maintenance_mode": 1}},
            ping_answers={"dev.localhost": ("503", True)},
            **kwargs,
        )

    def test_a_leftover_names_the_interrupted_job(self, wire, monkeypatch):
        owner = {
            "verb": "apps update",
            "job_id": "20261101-abc",
            "pid": 4242,
            "process_started": "t0",
            "host": "box",
            "at": "2026-11-01T10:00:00Z",
        }
        monkeypatch.setattr(core_maintenance.socket, "gethostname", lambda: "box")
        monkeypatch.setattr(core_maintenance.daemon_identity, "pid_alive", lambda pid: False)
        wire(
            self._container(maintenance_owners={(BENCH, "dev.localhost"): owner}),
            benches=[{"path": BENCH}],
        )

        result = core_status.status("proj")

        (w,) = _maint_warnings(result)
        assert "cwcli turned it on for 'apps update' (job 20261101-abc)" in w.text
        assert "no longer running" in w.text
        assert "--clear-maintenance" in w.text
        assert _bench(result.data).maintenance.owners[0].running is False

    def test_a_flag_with_no_record_is_reported_neutrally(self, wire):
        wire(self._container(), benches=[{"path": BENCH}])

        (w,) = _maint_warnings(core_status.status("proj"))

        assert "no record of setting it, so it may have been set on purpose" in w.text
        assert "interrupted" not in w.text

    def test_a_running_owner_is_busy_and_no_clear_is_offered(self, wire, monkeypatch):
        owner = {"verb": "migrate", "pid": 4242, "process_started": "t0", "host": "box"}
        monkeypatch.setattr(core_maintenance.socket, "gethostname", lambda: "box")
        monkeypatch.setattr(core_maintenance.daemon_identity, "pid_alive", lambda pid: True)
        monkeypatch.setattr(
            core_maintenance.daemon_identity, "process_start_time", lambda pid: "t0"
        )
        wire(
            self._container(maintenance_owners={(BENCH, "dev.localhost"): owner}),
            benches=[{"path": BENCH}],
        )

        result = core_status.status("proj")

        (w,) = _maint_warnings(result)
        assert "Not cleared while it is still in use" in w.text
        assert (
            "the cwcli 'migrate' that turned it on for 'dev.localhost' is still running" in w.text
        )
        assert "--clear-maintenance" not in w.text

    def test_an_in_container_migrate_is_busy(self, wire):
        procs = {
            5150: [
                f"{BENCH}/env/bin/python",
                "-m",
                "frappe.utils.bench_helper",
                "frappe",
                "--site",
                "dev.localhost",
                "migrate",
            ],
            5151: [
                f"{BENCH}/env/bin/python",
                "-m",
                "frappe.utils.bench_helper",
                "frappe",
                "serve",
                "--port",
                "8000",
            ],
        }
        wire(self._container(maintenance_procs=procs), benches=[{"path": BENCH}])

        report = _bench(core_status.status("proj").data).maintenance

        assert report.busy == ["an in-container 'bench migrate' is still running (pid 5150)"]


def wire_bench_left_down_by_bench_update(monkeypatch):
    """Wire the REAL ``core.status`` to one bench a failed ``bench update`` left down.

    Shared with the human and ``axi`` frontend tests, so they render the warnings
    the core genuinely produces rather than hand-copied text.
    """
    container = _BenchConfigContainer(
        marker=_MARKER,
        web_code="503",
        configs={BENCH: _FAILED_UPDATE_FLAGS},
        site_configs={(BENCH, "dev.localhost"): {"db_name": "x"}},
        ping_answers={"dev.localhost": ("503", True)},
    )
    monkeypatch.setattr(core_status, "get_project_containers", lambda name: [container])
    monkeypatch.setattr(
        resolvers.db_utils,
        "get_cached_project_data",
        lambda name: {"bench_instances": [{"path": BENCH}]},
    )
    monkeypatch.setattr(
        core_status.resolvers, "resolve_representative_site", lambda *a, **k: "dev.localhost"
    )
    return container
