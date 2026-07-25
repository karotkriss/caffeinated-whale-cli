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
from tests.test_core_supervision import (
    _CTL_SINGLE,
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

    def test_a_config_that_omits_the_port_is_unresolved_not_defaulted_to_8000(self, wire):
        # THE tightening. core.scale reads the same file and DOES default an omitted
        # key to 8000, correctly - it is computing which host ports to publish. Here
        # the same answer becomes a PROBE TARGET, so carrying that default across
        # would silently re-create the defect inside the change that removes it: a
        # bench past the first would report web_port 8000 / verified true and measure
        # bench 0's server.
        c = FakeContainer(marker=_MARKER, web_code="200", configs={BENCH: {"some_other_key": 1}})
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
        assert b_fast.web_http_code == b_slow.web_http_code == "200"
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
