"""``core.fleet`` - the streaming fleet model behind ``cwcli serve``.

Three properties carry real cost when they break, and each has its own class here:

* :class:`TestHealthKey` / :class:`TestDeltaSuppression` - the stable-field digest.
  A single volatile field inside it turns the quiet FAST tier into a firehose,
  which is exactly what happened in the design spike (8 deltas in 8 cycles of an
  instance that never changed).
* :class:`TestHonestUnknown` - "could not find out" never collapses into a healthy
  default, a zero, or a stale token.
* :class:`TestReBootstrap` - event-stream loss rebuilds the model instead of
  replaying with ``since=``, which Docker's 256-entry ring makes silently useless.
"""

from __future__ import annotations

import threading

import pytest

from caffeinated_whale_cli.core import fleet as core_fleet
from caffeinated_whale_cli.core import status as core_status
from caffeinated_whale_cli.core.envelope import Result, Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from caffeinated_whale_cli.core.list import InstanceDTO
from caffeinated_whale_cli.core.status import BenchStatus, StatusReport
from caffeinated_whale_cli.core.supervision import ProcessHealth


def _proc(label="web", **kw):
    base = dict(
        label=label, up=True, pid=101, uptime_s=499, cpu_pct=0.5, rss_kb=80000, state="RUNNING"
    )
    return ProcessHealth(**{**base, **kw})


def _bench(**kw):
    base = dict(
        index=0,
        bench_path="/workspace/frappe-bench",
        label=None,
        overall="running",
        supervisor_up=True,
        web_port=8000,
        web_port_verified=True,
        web_site="a.localhost",
        web_http_code="200",
        processes=[_proc()],
    )
    return BenchStatus(**{**base, **kw})


def _state(**kw):
    base = dict(
        project="p",
        docker_status="running",
        container_running=True,
        ports=["8000"],
        overall="running",
        web_probed=True,
        benches=[_bench()],
        probe_error=None,
        probed_at=1.0,
        probe_ms=250.0,
        probe_failed_at=None,
    )
    return core_fleet.InstanceState(**{**base, **kw})


class _Recorder:
    """Collects published deltas as ``(tier, project, state, cause)`` tuples."""

    def __init__(self):
        self.deltas = []

    def __call__(self, tier, project, state, cause):
        self.deltas.append((tier, project, state, cause))

    @property
    def tiers(self):
        return [d[0] for d in self.deltas]


@pytest.fixture
def listing(monkeypatch):
    """Drive ``core.list_instances`` from a mutable list of ``(project, status, ports)``."""
    rows = []

    def _set(*triples):
        rows[:] = [
            InstanceDTO(project_name=p, status=s, ports=list(ports)) for p, s, ports in triples
        ]

    monkeypatch.setattr(
        core_fleet, "list_instances", lambda: Result(status=Status.OK, data=list(rows))
    )
    return _set


@pytest.fixture
def probing(monkeypatch):
    """Drive ``core.status(..., fused=True)``; the callable receives every kwarg."""
    calls = []

    def _set(answer):
        def _status(project, **kwargs):
            calls.append((project, kwargs))
            if isinstance(answer, Exception):
                raise answer
            report = answer(project, **kwargs) if callable(answer) else answer
            return Result(status=Status.OK, data=report)

        monkeypatch.setattr(core_status, "status", _status)

    _set.calls = calls
    return _set


def _report(overall="running", benches=None):
    return StatusReport(
        overall=overall,
        project="p",
        container_running=True,
        benches=[_bench()] if benches is None else benches,
    )


class TestHealthKey:
    """The stable-fields-only digest. Volatile in = firehose out."""

    @pytest.mark.parametrize(
        "field,value",
        [
            ("probe_ms", 999.9),
            ("probed_at", 123456.0),
            ("probe_failed_at", 123456.0),
        ],
    )
    def test_volatile_instance_fields_are_excluded(self, field, value):
        assert core_fleet.health_key(_state()) == core_fleet.health_key(_state(**{field: value}))

    @pytest.mark.parametrize(
        "field,value", [("uptime_s", 99999), ("cpu_pct", 88.8), ("rss_kb", 123456)]
    )
    def test_volatile_process_fields_are_excluded(self, field, value):
        moved = _state(benches=[_bench(processes=[_proc(**{field: value})])])
        assert core_fleet.health_key(_state()) == core_fleet.health_key(moved)

    def test_every_volatile_field_moving_at_once_is_still_no_change(self):
        # The realistic shape of a steady instance's next probe: nothing a human
        # would call a change, every counter different.
        churn = _state(
            probe_ms=311.4,
            probed_at=99.0,
            benches=[_bench(processes=[_proc(uptime_s=502, cpu_pct=0.7, rss_kb=80512)])],
        )
        assert core_fleet.health_key(_state()) == core_fleet.health_key(churn)

    @pytest.mark.parametrize(
        "field,value",
        [
            ("docker_status", "exited"),
            ("container_running", False),
            ("ports", ["8000", "9000"]),
            ("overall", "degraded"),
            ("web_probed", False),
            ("probe_error", "docker.unreachable: nope"),
        ],
    )
    def test_stable_instance_fields_are_included(self, field, value):
        assert core_fleet.health_key(_state()) != core_fleet.health_key(_state(**{field: value}))

    @pytest.mark.parametrize(
        "field,value",
        [
            ("overall", "degraded"),
            ("supervisor_up", False),
            ("web_port", 8001),
            ("web_port_verified", False),
            ("web_site", "b.localhost"),
            ("web_http_code", "500"),
            ("not_cwcli_supervised", True),
            ("label", "staging"),
            ("index", 1),
        ],
    )
    def test_stable_bench_fields_are_included(self, field, value):
        moved = _state(benches=[_bench(**{field: value})])
        assert core_fleet.health_key(_state()) != core_fleet.health_key(moved)

    def test_a_changed_pid_is_a_change_even_when_the_state_token_is_not(self):
        # A process that died and was restarted between two probes reads RUNNING
        # both times. The pid is the only evidence, so it is a STABLE field.
        restarted = _state(benches=[_bench(processes=[_proc(pid=999)])])
        assert core_fleet.health_key(_state()) != core_fleet.health_key(restarted)

    def test_a_process_going_down_is_a_change(self):
        down = _state(benches=[_bench(processes=[_proc(up=False, state="FATAL")])])
        assert core_fleet.health_key(_state()) != core_fleet.health_key(down)


class TestDeltaSuppression:
    def test_a_steady_instance_publishes_nothing_after_the_first_probe(self, listing, probing):
        listing(("p", "running", ["8000"]))
        tick = {"n": 0}

        def _answer(project, **kwargs):
            tick["n"] += 1  # every cycle moves the volatile counters, as a real one does
            return StatusReport(
                overall="running",
                project=project,
                container_running=True,
                benches=[
                    _bench(processes=[_proc(uptime_s=500 + tick["n"], cpu_pct=tick["n"] / 10)])
                ],
            )

        probing(_answer)
        rec = _Recorder()
        f = core_fleet.Fleet(publish=rec)
        f.bootstrap()
        rec.deltas.clear()

        for _ in range(8):
            f.probe("p")

        assert len(rec.deltas) == 1, "only the first probe changes anything"
        assert rec.deltas[0][0] == "fast"

    def test_a_real_change_still_publishes(self, listing, probing):
        listing(("p", "running", ["8000"]))
        overall = {"v": "running"}
        probing(lambda project, **kw: _report(overall=overall["v"]))
        rec = _Recorder()
        f = core_fleet.Fleet(publish=rec)
        f.bootstrap()
        f.probe("p")
        rec.deltas.clear()

        overall["v"] = "degraded"
        f.probe("p")

        assert [d[0] for d in rec.deltas] == ["fast"]
        assert rec.deltas[0][2].overall == "degraded"


class TestHonestUnknown:
    def test_a_running_instance_nobody_probed_yet_is_unknown_not_healthy(self, listing):
        listing(("p", "running", ["8000"]))
        f = core_fleet.Fleet()
        f.bootstrap()

        state = f.get("p")
        assert state.overall == core_fleet.UNKNOWN
        assert state.benches == []
        assert state.probed_at is None and state.probe_ms is None

    def test_a_failed_probe_is_unknown_and_says_why(self, listing, probing):
        listing(("p", "running", ["8000"]))
        probing(lambda project, **kw: _report())
        f = core_fleet.Fleet()
        f.bootstrap()
        f.probe("p")
        assert f.get("p").overall == "running"

        probing(CwcliError(ErrorKind.DOCKER, "docker.unreachable", "no daemon"))
        f.probe("p")

        state = f.get("p")
        assert state.overall == core_fleet.UNKNOWN, "a dead read must not keep the last green token"
        assert state.probe_error == "docker.unreachable: no daemon"

    def test_a_failed_probe_keeps_success_evidence_at_its_success_time(self, listing, probing):
        listing(("p", "running", ["8000"]))
        probing(lambda project, **kw: _report())
        f = core_fleet.Fleet()
        f.bootstrap()
        f.set_focus({"p"})
        f.probe("p")
        successful = f.get("p")

        f.set_focus(set())
        probing(CwcliError(ErrorKind.DOCKER, "docker.unreachable", "no daemon"))
        f.probe("p")

        failed = f.get("p")
        assert failed.overall == core_fleet.UNKNOWN
        assert failed.benches == successful.benches
        assert failed.web_probed is successful.web_probed is True
        assert failed.probed_at == successful.probed_at
        assert failed.probe_ms == successful.probe_ms
        assert failed.probe_failed_at is not None
        assert successful.probe_failed_at is None

    def test_a_stopped_instance_is_offline_with_no_bench_claims(self, listing, probing):
        listing(("p", "running", ["8000"]))
        probing(lambda project, **kw: _report())
        f = core_fleet.Fleet()
        f.bootstrap()
        f.probe("p")

        listing(("p", "exited", []))
        f.bootstrap()

        state = f.get("p")
        assert state.overall == core_status.OFFLINE
        assert state.benches == [], "processes that are provably gone must not be reported"
        assert state.web_probed is False
        # A probe timing describes a health read; a stopped instance has none.
        assert state.probed_at is None and state.probe_ms is None

    def test_a_container_coming_up_is_unknown_not_its_old_health(self, listing, probing):
        # A start event means the CONTAINER is up; the bench needs seconds more.
        # Inheriting the token from before the stop would paint a green pill over
        # a bench that is not serving yet.
        listing(("p", "running", ["8000"]))
        probing(lambda project, **kw: _report())
        f = core_fleet.Fleet()
        f.bootstrap()
        f.probe("p")
        listing(("p", "exited", []))
        f.bootstrap()

        listing(("p", "running", ["8000"]))
        f.bootstrap()

        assert f.get("p").overall == core_fleet.UNKNOWN

    def test_a_stopped_instance_is_never_probed(self, listing, probing):
        listing(("p", "exited", []))
        probing(lambda project, **kw: _report())
        f = core_fleet.Fleet()
        f.bootstrap()
        f.probe("p")
        assert probing.calls == []

    def test_an_unresolved_web_port_stays_null_and_never_defaults(self, listing, probing):
        listing(("p", "running", ["8000"]))
        probing(
            lambda project, **kw: _report(
                benches=[_bench(web_port=None, web_port_verified=False, web_http_code=None)]
            )
        )
        f = core_fleet.Fleet()
        f.bootstrap()
        f.probe("p")

        bench = core_fleet.as_json(f.get("p"))["benches"][0]
        assert bench["web_port"] is None and bench["web_port_verified"] is False
        assert bench["web_http_code"] is None


class TestWebProbeCadence:
    """Decision B: process health on the normal cadence, the web check only when
    a client is looking or a lifecycle event just landed."""

    def test_web_is_not_probed_by_default(self, listing, probing):
        listing(("p", "running", ["8000"]))
        probing(lambda project, **kw: _report())
        f = core_fleet.Fleet()
        f.bootstrap()
        f.probe("p")

        assert probing.calls[0][1]["probe_web"] is False
        assert probing.calls[0][1]["fused"] is True
        assert f.get("p").web_probed is False

    def test_web_is_probed_while_a_client_has_the_instance_open(self, listing, probing):
        listing(("p", "running", ["8000"]))
        probing(lambda project, **kw: _report())
        f = core_fleet.Fleet()
        f.bootstrap()
        f.set_focus({"p"})
        f.probe("p")

        assert probing.calls[-1][1]["probe_web"] is True
        assert f.get("p").web_probed is True

    def test_focus_is_scoped_to_the_instance_that_is_open(self, listing, probing):
        listing(("p", "running", ["8000"]), ("q", "running", ["8100"]))
        probing(lambda project, **kw: _report())
        f = core_fleet.Fleet()
        f.bootstrap()
        f.set_focus({"p"})

        assert f.should_probe_web("p") is True
        assert f.should_probe_web("q") is False

    def test_a_lifecycle_event_opens_a_window_that_expires(self, listing):
        listing(("p", "running", ["8000"]))
        f = core_fleet.Fleet(web_probe_window_s=0.0)
        f.bootstrap()
        f.note_lifecycle("p")
        assert f.should_probe_web("p") is False  # a zero window is already past

        f = core_fleet.Fleet(web_probe_window_s=60.0)
        f.bootstrap()
        f.note_lifecycle("p")
        assert f.should_probe_web("p") is True


class TestReBootstrap:
    def test_concurrent_bootstraps_commit_observations_in_read_order(self, monkeypatch):
        first_read = threading.Event()
        second_read = threading.Event()
        release_first = threading.Event()
        call_lock = threading.Lock()
        calls = 0

        def _listing():
            nonlocal calls
            with call_lock:
                calls += 1
                call = calls
            if call == 1:
                first_read.set()
                assert release_first.wait(2)
                status = "running"
            else:
                second_read.set()
                status = "exited"
            return Result(
                status=Status.OK,
                data=[InstanceDTO(project_name="p", status=status, ports=[])],
            )

        monkeypatch.setattr(core_fleet, "list_instances", _listing)
        f = core_fleet.Fleet()
        older = threading.Thread(target=f.bootstrap)
        newer = threading.Thread(target=f.bootstrap)
        older.start()
        assert first_read.wait(2)
        newer.start()
        observations_overlapped = second_read.wait(0.1)
        release_first.set()
        older.join(2)
        newer.join(2)

        assert not older.is_alive()
        assert not newer.is_alive()
        assert not observations_overlapped
        assert f.get("p").docker_status == "exited"

    def test_an_unchanged_re_bootstrap_publishes_nothing(self, listing, probing):
        listing(("p", "running", ["8000"]))
        probing(lambda project, **kw: _report())
        rec = _Recorder()
        f = core_fleet.Fleet(publish=rec)
        f.bootstrap()
        f.probe("p")
        rec.deltas.clear()

        f.bootstrap()  # what an event-stream reconnect does

        assert rec.deltas == []

    def test_a_re_bootstrap_keeps_health_it_already_probed(self, listing, probing):
        listing(("p", "running", ["8000"]))
        probing(lambda project, **kw: _report())
        f = core_fleet.Fleet()
        f.bootstrap()
        f.probe("p")

        f.bootstrap()

        assert f.get("p").overall == "running"
        assert f.get("p").benches, "a reconnect must not blank health it already has"

    def test_a_state_change_missed_while_disconnected_is_caught_by_the_rebuild(
        self, listing, probing
    ):
        listing(("p", "running", ["8000"]))
        probing(lambda project, **kw: _report())
        rec = _Recorder()
        f = core_fleet.Fleet(publish=rec)
        f.bootstrap()
        f.probe("p")
        rec.deltas.clear()

        # The stop happened while the stream was down; no event will ever arrive.
        listing(("p", "exited", []))
        f.bootstrap()

        assert [(d[0], d[2].overall) for d in rec.deltas] == [("instant", core_status.OFFLINE)]

    def test_a_removed_instance_publishes_a_removal(self, listing):
        listing(("p", "running", ["8000"]), ("q", "running", ["8100"]))
        rec = _Recorder()
        f = core_fleet.Fleet(publish=rec)
        f.bootstrap()
        rec.deltas.clear()

        listing(("p", "running", ["8000"]))
        f.bootstrap()

        assert [(d[1], d[2]) for d in rec.deltas] == [("q", None)]
        assert f.get("q") is None

    def test_a_new_instance_publishes_an_instant_delta(self, listing):
        listing(("p", "running", ["8000"]))
        rec = _Recorder()
        f = core_fleet.Fleet(publish=rec)
        f.bootstrap()
        rec.deltas.clear()

        listing(("p", "running", ["8000"]), ("new", "running", ["8100"]))
        f.bootstrap()

        assert [(d[0], d[1]) for d in rec.deltas] == [("instant", "new")]


class TestApplyEvent:
    def test_a_start_event_invalidates_healthy_state_even_when_docker_already_says_running(
        self, listing, probing
    ):
        listing(("p", "running", ["8000"]))
        probing(lambda project, **kw: _report())
        rec = _Recorder()
        f = core_fleet.Fleet(publish=rec)
        f.bootstrap()
        f.probe("p")
        rec.deltas.clear()

        f.apply_event(
            {
                "Action": "start",
                "Actor": {
                    "Attributes": {
                        "com.docker.compose.project": "p",
                        "com.docker.compose.service": "frappe",
                    }
                },
            }
        )

        assert f.get("p").overall == core_fleet.UNKNOWN
        assert f.get("p").benches == []
        assert [(tier, state.overall) for tier, _project, state, _cause in rec.deltas] == [
            ("instant", core_fleet.UNKNOWN)
        ]

    def test_a_restart_event_invalidates_healthy_state_without_a_stopped_observation(
        self, listing, probing
    ):
        listing(("p", "running", ["8000"]))
        probing(lambda project, **kw: _report())
        rec = _Recorder()
        f = core_fleet.Fleet(publish=rec)
        f.bootstrap()
        f.probe("p")
        rec.deltas.clear()

        f.apply_event(
            {
                "Action": "restart",
                "Actor": {
                    "Attributes": {
                        "com.docker.compose.project": "p",
                        "com.docker.compose.service": "frappe",
                    }
                },
            }
        )

        assert f.get("p").overall == core_fleet.UNKNOWN
        assert f.get("p").benches == []
        assert [(tier, state.overall) for tier, _project, state, _cause in rec.deltas] == [
            ("instant", core_fleet.UNKNOWN)
        ]

    def test_a_service_event_burst_publishes_one_unknown_delta(self, listing, probing):
        listing(("p", "running", ["8000"]))
        probing(lambda project, **kw: _report())
        rec = _Recorder()
        f = core_fleet.Fleet(publish=rec)
        f.bootstrap()
        f.probe("p")
        rec.deltas.clear()

        for service in ("frappe", "mariadb", "redis-cache", "redis-queue"):
            f.apply_event(
                {
                    "Action": "start",
                    "Actor": {
                        "Attributes": {
                            "com.docker.compose.project": "p",
                            "com.docker.compose.service": service,
                        }
                    },
                }
            )

        assert f.get("p").overall == core_fleet.UNKNOWN
        assert f.get("p").benches == []
        assert [(tier, state.overall) for tier, _project, state, _cause in rec.deltas] == [
            ("instant", core_fleet.UNKNOWN)
        ]

    def test_an_event_rebuilds_the_model_and_carries_its_cause(self, listing):
        listing(("p", "exited", []))
        rec = _Recorder()
        f = core_fleet.Fleet(publish=rec)
        f.bootstrap()
        rec.deltas.clear()

        listing(("p", "running", ["8000"]))
        f.apply_event(
            {
                "Action": "start",
                "Actor": {
                    "Attributes": {
                        "com.docker.compose.project": "p",
                        "com.docker.compose.service": "frappe",
                    }
                },
            }
        )

        tier, project, state, cause = rec.deltas[0]
        assert (tier, project) == ("instant", "p")
        assert state.overall == core_fleet.UNKNOWN, "container up is not bench healthy"
        assert cause == {"project": "p", "action": "start", "service": "frappe"}

    def test_an_event_cause_is_attached_only_to_its_project(self, listing):
        listing(("p", "exited", []), ("q", "exited", []))
        rec = _Recorder()
        f = core_fleet.Fleet(publish=rec)
        f.bootstrap()
        rec.deltas.clear()

        listing(("p", "running", ["8000"]), ("q", "running", ["8100"]))
        f.apply_event(
            {
                "Action": "start",
                "Actor": {
                    "Attributes": {
                        "com.docker.compose.project": "p",
                        "com.docker.compose.service": "frappe",
                    }
                },
            }
        )

        assert [(project, cause) for _tier, project, _state, cause in rec.deltas] == [
            ("p", {"project": "p", "action": "start", "service": "frappe"}),
            ("q", None),
        ]

    def test_an_event_opens_the_web_probe_window(self, listing):
        listing(("p", "running", ["8000"]))
        f = core_fleet.Fleet()
        f.bootstrap()
        assert f.should_probe_web("p") is False

        f.apply_event(
            {"Action": "start", "Actor": {"Attributes": {"com.docker.compose.project": "p"}}}
        )

        assert f.should_probe_web("p") is True

    def test_the_event_window_is_folded_by_the_event_bootstrap(self, listing, monkeypatch):
        listing(("p", "exited", []))
        f = core_fleet.Fleet()
        f.bootstrap()
        listing(("p", "running", ["8000"]))
        monkeypatch.setattr(
            f,
            "note_lifecycle",
            lambda project: pytest.fail("event window was recorded outside bootstrap"),
        )

        f.apply_event(
            {"Action": "start", "Actor": {"Attributes": {"com.docker.compose.project": "p"}}}
        )

        assert f.should_probe_web("p") is True

    def test_an_event_without_a_compose_project_is_ignored(self, listing):
        listing(("p", "running", ["8000"]))
        rec = _Recorder()
        f = core_fleet.Fleet(publish=rec)
        f.bootstrap()
        rec.deltas.clear()

        f.apply_event({"Action": "start", "Actor": {"Attributes": {}}})

        assert rec.deltas == []


class TestEventLoop:
    def test_it_never_asks_docker_for_history(self, monkeypatch, listing):
        """Docker's event ring is 256 entries and this daemon's own execs flush it
        in ~51s, so a ``since=`` replay silently returns nothing. The reconnect
        path must rebuild instead - asserted on the CALL, so adding ``since=``
        later has to edit this test."""
        listing(("p", "running", ["8000"]))
        seen = []

        class _Client:
            def events(self, **kwargs):
                seen.append(kwargs)
                return iter(())  # an immediately-ended stream = a dropped connection

        monkeypatch.setattr(core_fleet.docker, "from_env", lambda: _Client())
        f = core_fleet.Fleet()
        stop = threading.Event()

        # Let it connect twice, then stop it inside the reconnect wait.
        original_wait = stop.wait
        rounds = {"n": 0}

        def _wait(timeout=None):
            rounds["n"] += 1
            if rounds["n"] >= 2:
                stop.set()
            return original_wait(0)

        monkeypatch.setattr(stop, "wait", _wait)
        core_fleet.event_loop(f, stop)

        assert len(seen) == 2, "a lost stream reconnects"
        assert all("since" not in kwargs and "until" not in kwargs for kwargs in seen)
        assert all(kwargs["filters"]["type"] == "container" for kwargs in seen)
        assert all(
            "exec_start" not in kwargs["filters"]["event"] for kwargs in seen
        ), "the daemon's own probe execs must never wake it"

    def test_every_reconnect_rebuilds_the_model(self, monkeypatch, listing):
        listing(("p", "running", ["8000"]))
        bootstraps = {"n": 0}

        class _Client:
            def events(self, **kwargs):
                return iter(())

        monkeypatch.setattr(core_fleet.docker, "from_env", lambda: _Client())
        f = core_fleet.Fleet()
        real = f.bootstrap

        def _counted(**kwargs):
            bootstraps["n"] += 1
            return real(**kwargs)

        monkeypatch.setattr(f, "bootstrap", _counted)
        stop = threading.Event()
        rounds = {"n": 0}

        def _wait(timeout=None):
            rounds["n"] += 1
            if rounds["n"] >= 3:
                stop.set()
            return False

        monkeypatch.setattr(stop, "wait", _wait)
        core_fleet.event_loop(f, stop)

        assert bootstraps["n"] == 3, "one full rebuild per (re)connect"

    def test_a_docker_failure_is_reported_and_retried_not_fatal(self, monkeypatch, listing):
        listing(("p", "running", ["8000"]))
        errors = []

        def _boom():
            raise core_fleet.DockerException("daemon gone")

        monkeypatch.setattr(core_fleet.docker, "from_env", _boom)
        f = core_fleet.Fleet()
        stop = threading.Event()

        def _wait(timeout=None):
            stop.set()
            return True

        monkeypatch.setattr(stop, "wait", _wait)
        core_fleet.event_loop(f, stop, on_error=errors.append)

        assert len(errors) == 1


class TestProbeLoop:
    def test_it_probes_only_running_instances(self, listing, probing):
        listing(("up", "running", ["8000"]), ("down", "exited", []))
        probing(lambda project, **kw: _report())
        f = core_fleet.Fleet()
        f.bootstrap()
        stop = threading.Event()

        def _wait(timeout=None):
            stop.set()
            return True

        stop.wait = _wait  # type: ignore[method-assign]
        core_fleet.probe_loop(f, stop, 0.01)

        assert [c[0] for c in probing.calls] == ["up"]


class TestProbeAndModelStayConsistent:
    """A probe that outruns the event stream must not publish a self-contradicting row."""

    def test_publications_follow_the_order_the_model_commits_changes(self, listing, probing):
        listing(("p", "running", ["8000"]))
        probing(lambda project, **kw: _report())
        fast_publishing = threading.Event()
        release_fast = threading.Event()
        instant_published = threading.Event()
        published = []

        def _publish(tier, project, state, cause):
            if tier == "fast":
                fast_publishing.set()
                assert release_fast.wait(2)
            else:
                instant_published.set()
            published.append((tier, state.overall))

        f = core_fleet.Fleet(publish=_publish)
        f.bootstrap()
        published.clear()
        instant_published.clear()
        probe = threading.Thread(target=f.probe, args=("p",))
        probe.start()
        assert fast_publishing.wait(2)

        listing(("p", "exited", []))
        lifecycle = threading.Thread(target=f.bootstrap)
        lifecycle.start()
        assert not instant_published.wait(0.1)

        release_fast.set()
        probe.join(2)
        lifecycle.join(2)

        assert not probe.is_alive()
        assert not lifecycle.is_alive()
        assert published == [("fast", "running"), ("instant", core_status.OFFLINE)]

    def test_a_probe_that_finds_the_container_gone_publishes_one_consistent_row(
        self, listing, probing
    ):
        listing(("p", "running", ["8000"]))
        probing(lambda project, **kw: _report())
        rec = _Recorder()
        f = core_fleet.Fleet(publish=rec)
        f.bootstrap()
        f.probe("p")
        rec.deltas.clear()

        # Docker has stopped it; the probe sees that before the event lands
        # (measured 54ms ahead of it on a real instance).
        listing(("p", "exited", []))
        probing(
            lambda project, **kw: StatusReport(
                overall=core_status.OFFLINE, project=project, container_running=False, benches=[]
            )
        )
        f.probe("p")

        assert len(rec.deltas) == 1
        state = rec.deltas[0][2]
        assert state.overall == core_status.OFFLINE
        assert state.container_running is False, "offline with container_running true is a lie"
        assert state.docker_status == "exited"
        assert state.benches == []

    def test_a_probe_is_discarded_when_lifecycle_changes_while_it_runs(
        self, listing, probing
    ):
        listing(("p", "running", ["8000"]))
        probe_started = threading.Event()
        release_probe = threading.Event()

        def _answer(project, **kwargs):
            probe_started.set()
            assert release_probe.wait(2)
            return _report()

        probing(_answer)
        rec = _Recorder()
        f = core_fleet.Fleet(publish=rec)
        f.bootstrap()
        rec.deltas.clear()
        thread = threading.Thread(target=f.probe, args=("p",))
        thread.start()
        assert probe_started.wait(2)

        listing(("p", "exited", []))
        f.bootstrap()
        release_probe.set()
        thread.join(2)

        assert not thread.is_alive()
        assert f.get("p").overall == core_status.OFFLINE
        assert f.get("p").container_running is False
        assert f.get("p").benches == []
        assert [(tier, state.overall) for tier, _project, state, _cause in rec.deltas] == [
            ("instant", core_status.OFFLINE)
        ]

    def test_the_event_that_follows_is_then_suppressed_as_no_change(self, listing, probing):
        listing(("p", "running", ["8000"]))
        probing(lambda project, **kw: _report())
        rec = _Recorder()
        f = core_fleet.Fleet(publish=rec)
        f.bootstrap()
        f.probe("p")
        listing(("p", "exited", []))
        probing(
            lambda project, **kw: StatusReport(
                overall=core_status.OFFLINE, project=project, container_running=False, benches=[]
            )
        )
        f.probe("p")
        rec.deltas.clear()

        f.apply_event(
            {"Action": "die", "Actor": {"Attributes": {"com.docker.compose.project": "p"}}}
        )

        assert rec.deltas == [], "the stop was already reported; saying it twice is noise"

    def test_a_vanished_project_is_dropped_rather_than_left_as_a_phantom(self, listing, probing):
        listing(("p", "running", ["8000"]))
        probing(lambda project, **kw: _report())
        rec = _Recorder()
        f = core_fleet.Fleet(publish=rec)
        f.bootstrap()
        f.probe("p")
        rec.deltas.clear()

        listing()
        probing(CwcliError(ErrorKind.NOT_FOUND, "project.not_found", "gone"))
        f.probe("p")

        assert f.get("p") is None
        assert [(d[1], d[2]) for d in rec.deltas] == [("p", None)]

    def test_an_unreadable_probe_is_still_an_honest_unknown_not_a_removal(self, listing, probing):
        # Only NOT_FOUND means "this instance is not there". Every other failure
        # means "could not find out", and dropping the row would be a claim.
        listing(("p", "running", ["8000"]))
        probing(lambda project, **kw: _report())
        f = core_fleet.Fleet()
        f.bootstrap()
        f.probe("p")

        probing(CwcliError(ErrorKind.DOCKER, "docker.unreachable", "no daemon"))
        f.probe("p")

        assert f.get("p") is not None
        assert f.get("p").overall == core_fleet.UNKNOWN
