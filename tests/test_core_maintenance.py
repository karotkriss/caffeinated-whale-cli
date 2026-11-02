"""``core.maintenance``: maintenance mode the way Frappe answers it, and the clear.

The model is pinned against the behaviour reproduced on a real v16 bench: a site
inherits the bench-wide flag unless its own file sets the key, ``bench serve``
answers every Host as the default site (so a site whose own file says 0 still
answers SessionStopped), and Frappe's config cache makes the live answer lag.
"""

from __future__ import annotations

import json

import pytest

from caffeinated_whale_cli.core import bench_ops, resolvers
from caffeinated_whale_cli.core import maintenance as m
from caffeinated_whale_cli.core.envelope import Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from tests import maintenance_fakes

BENCH = "/workspace/frappe-bench"


def _raw(common, sites, **kwargs):
    """A ``read_bench`` record built through the real parser from the fake output."""

    class _C:
        def exec_run(self, cmd, **_):
            return (0, maintenance_fakes.read_output(common=common, sites=sites, **kwargs))

    return m.read_bench(_C(), BENCH)


def _assess(common, sites, **kwargs):
    return m.assess(_raw(common, sites, **kwargs), bench_path=BENCH, command="CMD")


class TestTheRead:
    def test_only_the_three_keys_survive_the_parse(self):
        raw = _raw(
            {"maintenance_mode": 1, "db_host": "mariadb"},
            {"a.localhost": {"db_password": "secret", "maintenance_mode": 0}},
        )
        assert raw["common"] == {"value": {"maintenance_mode": 1}}
        assert raw["sites"]["a.localhost"] == {"value": {"maintenance_mode": 0}}
        assert "secret" not in json.dumps(raw)

    def test_absent_is_empty_but_unreadable_is_unknown(self):
        report = _assess(None, {"a.localhost": maintenance_fakes.UNREADABLE})
        assert report.bench_flags == []
        assert report.sites[0].on is None  # never "not in maintenance"
        assert report.down == []

    def test_an_unreadable_common_config_makes_every_site_unknown(self):
        report = _assess(maintenance_fakes.UNREADABLE, {"a.localhost": {}})
        assert [(s.site, s.on) for s in report.sites] == [("a.localhost", None)]

    def test_a_failed_exec_is_none(self):
        class _C:
            def exec_run(self, cmd, **_):
                return (3, b"")

        assert m.read_bench(_C(), BENCH) is None
        assert m.assess(None, bench_path=BENCH) is None


class TestMergeOrder:
    def test_a_site_inherits_the_bench_wide_flag(self):
        report = _assess({"maintenance_mode": 1, "pause_scheduler": 1}, {"a.localhost": {}})
        assert report.down == ["a.localhost"]
        assert report.sites[0].cause == "bench"
        assert report.bench_flags == ["maintenance_mode", "pause_scheduler"]

    def test_a_site_own_zero_overrides_the_bench(self):
        report = _assess({"maintenance_mode": 1}, {"a.localhost": {"maintenance_mode": 0}})
        assert report.down == []
        assert report.needs_clearing  # a new site would still inherit the 1

    def test_a_site_own_flag_is_its_own_cause(self):
        report = _assess({}, {"a.localhost": {"maintenance_mode": 1}, "b.localhost": {}})
        assert report.down == ["a.localhost"]
        assert report.sites[0].cause == "site"

    def test_truthiness_is_frappe_s(self):
        # `bench set-config -g maintenance_mode 0` without --parse stores the STRING
        # "0", which Frappe's `if conf.maintenance_mode:` reads as set.
        report = _assess({"maintenance_mode": "0"}, {"a.localhost": {}})
        assert report.down == ["a.localhost"]


class TestServedAs:
    """``bench serve`` answers every request as one site when it starts with one."""

    FLAGS = {"maintenance_mode": 1, "default_site": "a.localhost"}
    SITES = {"a.localhost": {}, "b.localhost": {"maintenance_mode": 0}}

    def test_v15_plus_pins_to_default_site(self):
        report = _assess(self.FLAGS, self.SITES)
        assert report.served_as == "a.localhost"
        assert report.down == ["a.localhost", "b.localhost"]
        b = report.sites[1]
        assert (b.site, b.cause, b.served_as) == ("b.localhost", "served_as", "a.localhost")

    def test_v14_pins_to_currentsite_and_ignores_default_site(self):
        report = _assess(
            {"maintenance_mode": 1, "default_site": "b.localhost"},
            {"a.localhost": {}, "b.localhost": {"maintenance_mode": 0}},
            currentsite="a.localhost\n",
            version='__version__ = "14.82.1"',
        )
        assert report.served_as == "a.localhost"

    def test_v15_ignores_currentsite(self):
        report = _assess(
            {"maintenance_mode": 1},
            {"a.localhost": {}, "b.localhost": {"maintenance_mode": 0}},
            currentsite="a.localhost\n",
        )
        assert report.served_as is None
        assert report.down == ["a.localhost"]

    def test_an_explicit_site_flag_pins(self):
        report = _assess(
            {"maintenance_mode": 1},
            {"a.localhost": {}, "b.localhost": {"maintenance_mode": 0}},
            procfile="web: bench --site a.localhost serve --port 8000\n",
        )
        assert report.served_as == "a.localhost"

    def test_gunicorn_routes_by_host(self):
        report = _assess(
            self.FLAGS,
            self.SITES,
            procfile="web: env/bin/gunicorn -b 0.0.0.0:8000 frappe.app:application\n",
        )
        assert report.served_as is None
        assert report.down == ["a.localhost"]

    def test_a_default_site_that_does_not_exist_does_not_pin(self):
        report = _assess({"maintenance_mode": 1, "default_site": "gone.localhost"}, self.SITES)
        assert report.served_as is None


class TestPlan:
    def test_bench_wide_clears_both_flags_together(self):
        report = _assess({"maintenance_mode": 1, "pause_scheduler": 1}, {"a.localhost": {}})
        assert m.plan(report) == {
            "bench_flags": ["maintenance_mode", "pause_scheduler"],
            "sites": [],
            "scope": ["a.localhost"],
            "held": [],
        }

    def test_a_scheduler_paused_alone_is_not_maintenance(self):
        report = _assess({"pause_scheduler": 1}, {"a.localhost": {}})
        assert not report.needs_clearing
        assert m.plan(report)["bench_flags"] == []

    def test_a_site_own_flag_is_cleared_on_that_site_only(self):
        report = _assess({}, {"a.localhost": {"maintenance_mode": 1}, "b.localhost": {}})
        assert m.plan(report) == {
            "bench_flags": [],
            "sites": ["a.localhost"],
            "scope": ["a.localhost"],
            "held": [],
        }

    def test_served_as_a_default_site_in_its_own_maintenance_clears_the_default(self):
        report = _assess(
            {"default_site": "a.localhost"},
            {"a.localhost": {"maintenance_mode": 1}, "b.localhost": {}},
        )
        assert m.plan(report)["sites"] == ["a.localhost"]

    def test_a_site_scope_writes_only_that_site_and_names_what_holds_it(self):
        report = _assess(
            {"maintenance_mode": 1, "default_site": "a.localhost"},
            {"a.localhost": {"maintenance_mode": 1}, "b.localhost": {}},
        )
        assert m.plan(report, "b.localhost") == {
            "bench_flags": [],
            "sites": [],
            "scope": ["b.localhost"],
            "held": ["b.localhost"],
        }

    def test_site_scope_leaves_the_bench_flag_when_that_site_does_not_inherit_it(self):
        report = _assess(
            {"maintenance_mode": 1},
            {"a.localhost": {"maintenance_mode": 1}, "b.localhost": {"maintenance_mode": 0}},
        )
        assert m.plan(report, "a.localhost") == {
            "bench_flags": [],
            "sites": ["a.localhost"],
            "scope": ["a.localhost"],
            "held": [],
        }

    def test_the_question_says_the_scheduler_resumes(self):
        report = _assess({"maintenance_mode": 1, "pause_scheduler": 1}, {"a.localhost": {}})
        assert m.prompt_text(report) == (
            "Take a.localhost out of maintenance mode and resume the scheduler?"
        )


class _Bench:
    """A stateful fake bench for :func:`m.clear`: every write the clear makes lands in
    the configs, and the probe answers what Frappe would after ``lag`` probes."""

    def __init__(self, common, sites, *, lag=0, refuse=(), procs=None, owners=None, unserved=False):
        self.common = dict(common)
        self.sites = {k: dict(v) for k, v in sites.items()}
        self.lag = lag
        self.unserved = unserved
        self.probes = 0
        self.refuse = set(refuse)
        self.procs = procs or {}
        self.owners = dict(owners or {})
        self.commands: list = []
        self.removed: list = []

    def _on(self, site):
        own = self.sites[site]
        return (
            bool(own["maintenance_mode"])
            if "maintenance_mode" in own
            else bool(self.common.get("maintenance_mode"))
        )

    def exec_run(self, cmd, workdir=None, **_):
        if maintenance_fakes.is_read(cmd):
            return (
                0,
                maintenance_fakes.read_output(
                    common=self.common, sites=self.sites, owners=self.owners, procs=self.procs
                ),
            )
        if maintenance_fakes.is_probe(cmd):
            self.probes += 1
            if self.unserved:
                return (
                    0,
                    maintenance_fakes.probe_output(cmd, {s: ("000", False) for s in self.sites}),
                )
            if self.lag:
                self.lag -= 1
                return (
                    0,
                    maintenance_fakes.probe_output(cmd, {s: ("503", True) for s in self.sites}),
                )
            answers = {s: ("503", True) if self._on(s) else ("200", False) for s in self.sites}
            return (0, maintenance_fakes.probe_output(cmd, answers))
        if isinstance(cmd, list) and cmd[:2] == ["test", "-d"]:
            return (1, b"")  # no locks dir: nothing holds a migrate lock
        if isinstance(cmd, list) and cmd[:2] == ["rm", "-f"]:
            self.removed.append(cmd[2])
            return (0, b"")
        self.commands.append(cmd)
        if cmd.startswith("bench set-config --global --parse "):
            flag = cmd.split()[4]
            if flag in self.refuse:
                return (1, b"")
            self.common[flag] = 0
            return (0, b"")
        if cmd.endswith("set-maintenance-mode off"):
            site = cmd.split()[2]
            if site in self.refuse:
                return (1, b"")
            self.sites[site]["maintenance_mode"] = 0
            return (0, b"")
        return (1, b"")


@pytest.fixture
def wire(monkeypatch):
    def _wire(bench, *, port=8000):
        monkeypatch.setattr(
            resolvers, "resolve_container_and_bench", lambda *a, **k: (bench, BENCH, [])
        )
        monkeypatch.setattr(resolvers, "bench_cli_flag", lambda *a, **k: "")
        monkeypatch.setattr(
            resolvers, "resolve_web_ports", lambda c, paths: {BENCH: port} if port else {}
        )
        monkeypatch.setattr(m.time, "sleep", lambda s: None)
        return bench

    return _wire


def _events():
    seen: list = []
    return seen, seen.append


class TestClear:
    def test_the_bench_left_down_by_bench_update_comes_back_in_one_go(self, wire):
        bench = wire(
            _Bench(
                {"maintenance_mode": 1, "pause_scheduler": 1},
                {"a.localhost": {}, "b.localhost": {}},
            )
        )
        seen, emit = _events()

        result = m.clear("proj", consent=True, on_event=emit)

        out = result.data
        assert out.ok and out.verified
        assert out.cleared_bench_flags == ["maintenance_mode", "pause_scheduler"]
        assert out.scheduler_resumed is True
        assert out.answering == ["a.localhost", "b.localhost"]
        assert bench.commands == [
            "bench set-config --global --parse maintenance_mode 0",
            "bench set-config --global --parse pause_scheduler 0",
        ]
        phases = [(type(e).__name__, e.phase) for e in seen]
        assert phases == [
            ("MaintenanceStepStart", "check"),
            ("MaintenanceStepEnd", "check"),
            ("MaintenanceStepStart", "bench_flags"),
            ("MaintenanceStepEnd", "bench_flags"),
            ("MaintenanceStepStart", "verify"),
            ("MaintenanceStepEnd", "verify"),
        ]
        assert "the scheduler resumes" in seen[3].message

    def test_both_levels_are_cleared_when_a_site_also_sets_its_own(self, wire):
        bench = wire(
            _Bench(
                {"maintenance_mode": 1}, {"a.localhost": {"maintenance_mode": 1}, "b.localhost": {}}
            )
        )

        out = m.clear("proj", consent=True).data

        assert out.ok
        assert out.cleared_bench_flags == ["maintenance_mode"]
        assert out.cleared_sites == ["a.localhost"]
        assert "bench --site a.localhost set-maintenance-mode off" in bench.commands

    def test_it_waits_for_frappe_s_config_cache(self, wire):
        wire(_Bench({"maintenance_mode": 1}, {"a.localhost": {}}, lag=4))

        out = m.clear("proj", consent=True).data

        assert out.ok and out.answering == ["a.localhost"]

    def test_a_site_that_keeps_answering_maintenance_fails_the_clear(self, wire):
        wire(_Bench({"maintenance_mode": 1}, {"a.localhost": {}}, lag=10_000))

        out = m.clear("proj", consent=True, verify_timeout=0).data

        assert out.ok is False
        assert out.still_down == ["a.localhost"]

    def test_a_bench_whose_web_server_is_down_is_cleared_but_unverified(self, wire):
        bench = wire(_Bench({"maintenance_mode": 1}, {"a.localhost": {}}, unserved=True))
        seen, emit = _events()

        result = m.clear("proj", consent=True, on_event=emit)
        out = result.data

        assert out.ok is True and out.verified is False
        assert out.still_down == [] and out.answering == []
        assert out.cleared_bench_flags == ["maintenance_mode"]
        assert bench.probes == 2  # the check's probe, then one verify probe: no wait
        assert any(w.code == "maintenance.unverified" for w in result.warnings)
        verify = [e for e in seen if isinstance(e, m.MaintenanceStepEnd) and e.phase == "verify"]
        assert verify[0].status == "warning"

    def test_a_refused_write_fails_the_clear(self, wire):
        wire(_Bench({"maintenance_mode": 1}, {"a.localhost": {}}, refuse={"maintenance_mode"}))

        out = m.clear("proj", consent=True, verify_timeout=0).data

        assert out.ok is False and out.cleared_bench_flags == []

    def test_without_consent_it_asks_the_same_question(self, wire):
        bench = wire(_Bench({"maintenance_mode": 1, "pause_scheduler": 1}, {"a.localhost": {}}))

        result = m.clear("proj")

        assert result.status is Status.NEEDS_CHOICE
        assert result.choice.kind == "confirm_clear_maintenance"
        assert result.choice.param == "consent"
        assert "resume the scheduler" in result.choice.prompt
        assert bench.commands == []  # nothing written before consent

    def test_a_running_migrate_is_refused_before_consent_is_asked(self, wire):
        procs = {
            77: [
                f"{BENCH}/env/bin/python",
                "-m",
                "frappe.utils.bench_helper",
                "frappe",
                "--site",
                "a.localhost",
                "migrate",
            ]
        }
        bench = wire(_Bench({"maintenance_mode": 1}, {"a.localhost": {}}, procs=procs))

        with pytest.raises(CwcliError) as e:
            m.clear("proj")

        assert e.value.kind is ErrorKind.CONFLICT
        assert e.value.code == "maintenance.busy"
        assert "'bench migrate' is still running (pid 77)" in e.value.message
        assert bench.commands == []

    def test_a_held_migrate_lock_is_refused(self, wire, monkeypatch):
        wire(_Bench({"maintenance_mode": 1}, {"a.localhost": {}}))
        monkeypatch.setattr(m.bench_ops, "migrate_lock_held", lambda c, b, s: True)

        with pytest.raises(CwcliError, match="holds the migrate lock on 'a.localhost'"):
            m.clear("proj", consent=True)

    def test_an_unreadable_lock_is_refused_not_assumed_free(self, wire, monkeypatch):
        wire(_Bench({"maintenance_mode": 1}, {"a.localhost": {}}))

        def unreadable(*_a):
            raise CwcliError(ErrorKind.PRECONDITION, "migrate.lock_probe_failed", "x")

        monkeypatch.setattr(m.bench_ops, "migrate_lock_held", unreadable)

        with pytest.raises(CwcliError, match="could not tell whether the migrate lock"):
            m.clear("proj", consent=True)

    def test_nothing_in_maintenance_is_a_no_op(self, wire):
        bench = wire(_Bench({}, {"a.localhost": {}}))

        result = m.clear("proj", consent=True)

        assert result.status is Status.OK and result.data.ok and result.data.sites == []
        assert bench.commands == []

    def test_one_site_scope_clears_only_what_holds_that_site(self, wire):
        bench = wire(
            _Bench(
                {"maintenance_mode": 1},
                {"a.localhost": {"maintenance_mode": 1}, "b.localhost": {"maintenance_mode": 0}},
            )
        )

        out = m.clear("proj", site="a.localhost", consent=True).data

        assert out.ok and out.cleared_sites == ["a.localhost"]
        assert bench.commands == ["bench --site a.localhost set-maintenance-mode off"]

    def test_a_site_scope_never_clears_the_bench_wide_flag(self, wire):
        bench = wire(
            _Bench(
                {"maintenance_mode": 1, "pause_scheduler": 1},
                {"a.localhost": {}, "b.localhost": {}},
            )
        )

        result = m.clear("proj", site="a.localhost", consent=True)
        out = result.data

        assert out.ok is False and out.still_down == ["a.localhost"]
        assert out.cleared_bench_flags == [] and out.scheduler_resumed is False
        assert bench.commands == []
        assert bench.common == {"maintenance_mode": 1, "pause_scheduler": 1}
        (warning,) = [w for w in result.warnings if w.code == "maintenance.bench_wide"]
        assert "cwcli status proj --clear-maintenance" in warning.text

    def test_a_site_answered_as_the_default_site_is_not_cleared_through_it(self, wire):
        bench = wire(
            _Bench(
                {"default_site": "a.localhost"},
                {"a.localhost": {"maintenance_mode": 1}, "b.localhost": {}},
                lag=1,
            )
        )

        result = m.clear("proj", site="b.localhost", consent=True)

        assert result.data.ok is False and result.data.still_down == ["b.localhost"]
        assert bench.commands == []
        assert bench.sites["a.localhost"]["maintenance_mode"] == 1
        (warning,) = [w for w in result.warnings if w.code == "maintenance.bench_wide"]
        assert "default site 'a.localhost'" in warning.text

    def test_records_of_the_freed_sites_are_removed(self, wire):
        owners = {"a.localhost": {"verb": "apps update", "pid": 1, "host": "elsewhere"}}
        bench = wire(_Bench({"maintenance_mode": 1}, {"a.localhost": {}}, owners=owners))

        out = m.clear("proj", consent=True).data

        assert out.owners[0].verb == "apps update"
        assert out.owners[0].running is None  # another machine: cannot be checked
        assert bench.removed == [f"{BENCH}/{bench_ops.MAINTENANCE_RECORD_DIR}/a.localhost.json"]

    def test_an_unknown_port_is_cleared_but_reported_unverified(self, wire):
        wire(_Bench({"maintenance_mode": 1}, {"a.localhost": {}}), port=None)

        result = m.clear("proj", consent=True)

        assert result.data.verified is False
        assert any(w.code == "maintenance.unverified" for w in result.warnings)


class TestOwnershipRecord:
    """``set_maintenance`` records who turned maintenance on, BEFORE the flag, so a
    process killed outright still leaves its name behind."""

    class _C:
        def __init__(self):
            self.calls: list = []

        def exec_run(self, cmd, workdir=None, **_):
            self.calls.append(cmd)
            return (0, b"")

    def test_enable_writes_the_record_first(self, monkeypatch):
        monkeypatch.setenv(bench_ops.JOB_ID_ENV, "job-1")
        c = self._C()

        assert bench_ops.set_maintenance(c, BENCH, "a.localhost", enable=True, owner="migrate")

        write, flag = c.calls
        assert write[:2] == ["python3", "-c"]
        assert write[3] == f"{BENCH}/logs/.cwcli-maintenance/a.localhost.json"
        record = json.loads(write[4])
        assert record["verb"] == "migrate" and record["job_id"] == "job-1"
        assert record["pid"] > 0 and record["host"]
        assert flag == "bench --site a.localhost set-maintenance-mode on"

    def test_a_successful_disable_forgets_it(self):
        c = self._C()

        bench_ops.set_maintenance(c, BENCH, "a.localhost", enable=False)

        assert c.calls[-1] == ["rm", "-f", f"{BENCH}/logs/.cwcli-maintenance/a.localhost.json"]

    def test_a_refused_enable_forgets_the_record_it_wrote(self):
        c = self._C()
        c.exec_run = lambda cmd, workdir=None, **_: c.calls.append(cmd) or (
            (1, b"") if isinstance(cmd, str) else (0, b"")
        )

        assert not bench_ops.set_maintenance(c, BENCH, "a.localhost", enable=True, owner="migrate")

        assert c.calls[-1] == ["rm", "-f", f"{BENCH}/logs/.cwcli-maintenance/a.localhost.json"]

    def test_no_owner_writes_no_record(self):
        c = self._C()

        bench_ops.set_maintenance(c, BENCH, "a.localhost", enable=True)

        assert c.calls == ["bench --site a.localhost set-maintenance-mode on"]

    def test_both_maintenance_owning_verbs_name_themselves(self, monkeypatch):
        from caffeinated_whale_cli.core import update as core_update

        seen = []
        monkeypatch.setattr(
            bench_ops, "set_maintenance", lambda *a, **k: seen.append(k.get("owner")) or True
        )
        core_update._set_maintenance(object(), BENCH, "a.localhost", enable=True)
        assert seen == ["apps update"]
