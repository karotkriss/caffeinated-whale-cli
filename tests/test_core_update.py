"""``core.update`` - the app-update state machine on the logic core.

Covers what the envelope makes possible and what the migration must never lose:
the seven-way aggregation as RETURNED data, the load-bearing maintenance gate, the
unconditional ``finally``, the failed-vs-unknown distinction (design Decision 2),
the frappe fork, ``NEEDS_CHOICE``, and that no live Docker object rides the DTO.

The command-level behaviour lives in `test_apps.py` and
`test_update_characterization.py`; these drive the core directly.
"""

import contextlib
import dataclasses
import json
import shlex
import types

import pytest

from caffeinated_whale_cli.core import bench_ops, resolvers, update_preflight
from caffeinated_whale_cli.core import docker as core_docker
from caffeinated_whale_cli.core import exec_stream as exec_stream_mod
from caffeinated_whale_cli.core import update as core_update
from caffeinated_whale_cli.core.envelope import Result, Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from caffeinated_whale_cli.core.update import (
    UpdateAborted,
    UpdateOutput,
    UpdatePreflight,
    UpdateStepEnd,
    UpdateStepStart,
)

from .batched_probes import emulate_batched
from .test_apps import FakeFrappeContainer, _FakeAPI, _wire_stopped_bench

BENCH = "/workspace/frappe-bench"


def _fake_clock(monkeypatch):
    """Instant sleeps that advance a fake ``time.monotonic`` clock, so the interrupt
    cleanup's wait (``bench_ops._wait_marked_gone``'s real 10s+5s deadline, reached via
    ``core_update.time`` since it and ``bench_ops.time`` are the SAME module object) is
    satisfied by the clock rather than really waited. Patching only ``sleep`` would leave
    a real deadline: a still-alive orphan then spins the loop flat-out for 15s while the
    fake container records every /proc probe - the gigabyte spike tests/conftest.py's
    memory/slow guard exists to catch."""
    clock = [0.0]

    def _sleep(seconds=0.0, *_a, **_k):
        clock[0] += seconds or bench_ops._INTERRUPT_POLL_INTERVAL

    monkeypatch.setattr(core_update.time, "sleep", _sleep)
    monkeypatch.setattr(core_update.time, "monotonic", lambda: clock[0])


@pytest.fixture()
def wired(monkeypatch):
    """Container resolution + recache + sleep, faked. Returns the fake container."""
    container = FakeFrappeContainer(available_apps=["frappe", "payments"])
    monkeypatch.setattr(core_docker, "get_frappe_container", lambda name: container)
    monkeypatch.setattr(core_update.core_docker, "get_frappe_container", lambda name: container)
    monkeypatch.setattr(core_update.cache, "recache_project", lambda *a, **k: True)
    _fake_clock(monkeypatch)
    monkeypatch.setattr(core_update, "_sites_with_app", lambda *a, **k: ["a.localhost"])
    _wire_stopped_bench(monkeypatch)
    return container


def _update(**kwargs):
    kwargs.setdefault("bench_path", BENCH)
    return core_update.update("proj", kwargs.pop("apps", ["payments"]), **kwargs)


# ------------------------------------------------------------------ the envelope


class TestEnvelope:
    def test_ok_run_returns_an_ok_report(self, wired):
        result = _update()

        assert result.status is Status.OK
        report = result.data
        assert report.ok is True
        assert report.project == "proj"
        assert report.bench_path == BENCH
        assert report.apps == ["payments"]
        assert report.affected_sites == ["a.localhost"]
        assert report.migrated_sites == ["a.localhost"]
        assert report.aborted is False

    def test_the_post_pull_recache_reads_only_the_updated_bench(self, monkeypatch, wired):
        recaches = []
        monkeypatch.setattr(
            core_update.cache,
            "recache_project",
            lambda name, verbose=False, bench_path=None, warnings=None: bool(
                recaches.append((name, bench_path)) or True
            ),
        )

        _update()

        assert recaches == [("proj", BENCH)]

    def test_a_partial_failure_is_a_warning_envelope_carrying_ok_false(self, wired):
        # The closed Status set has no ERROR member by design: hard failures raise,
        # and update's partial failures must NOT raise (reporting them all is the
        # job). So the aggregate rides the DTO, and the frontends' exit code reads
        # report.ok - NOT result.status, which maps WARNING to 0 everywhere else.
        wired.fail_on = ["bench --site a.localhost migrate"]

        result = _update()

        assert result.status is Status.WARNING
        assert result.data.ok is False

    def test_report_holds_no_live_docker_object(self, wired):
        report = _update().data

        # dataclasses.asdict recurses; anything holding a live object would surface
        # here, and this DTO has to survive TOON/JSON serialization.
        flat = dataclasses.asdict(report)
        # `None` is in the set because `resync_error` is genuinely optional and
        # serializes fine; the property under test is "no live object", not "no None".
        assert all(
            isinstance(v, (str, bool, list, type(None))) for v in flat.values()
        ), f"non-builtin in report: {flat}"

    def test_no_apps_is_a_usage_error(self, wired):
        with pytest.raises(CwcliError) as exc:
            _update(apps=[])
        assert exc.value.kind is ErrorKind.USAGE

    def test_multibench_without_a_selector_is_a_needs_choice(self, monkeypatch, wired):
        monkeypatch.setattr(
            core_update.resolvers,
            "resolve_bench",
            lambda *a, **k: types.SimpleNamespace(
                status=Status.NEEDS_CHOICE,
                choice=types.SimpleNamespace(kind="select_bench"),
                data=None,
                warnings=[],
            ),
        )

        result = core_update.update("proj", ["payments"], bench_path=None)

        assert result.status is Status.NEEDS_CHOICE
        assert result.choice.kind == "select_bench"
        # Nothing was touched: a choice is asked BEFORE any work.
        assert not any("migrate" in c for c in wired.calls)

    def test_no_cached_bench_falls_back_to_the_default_with_a_warning(self, monkeypatch, wired):
        monkeypatch.setattr(core_update.resolvers, "resolve_bench", lambda *a, **k: None)

        result = core_update.update("proj", ["payments"], bench_path=None)

        assert result.data.bench_path == resolvers.DEFAULT_BENCH_PATH
        assert any(w.code == "bench.default_used" for w in result.warnings)


# ------------------------------------------------------- the two-directory probe


class TestBenchProbe:
    @pytest.mark.parametrize("missing", ["apps", "sites"])
    def test_either_missing_bench_directory_is_not_found(self, wired, missing):
        # update probes BOTH apps/ and sites/, which is why it keeps its own probe
        # instead of resolvers.require_bench_dir (that one checks sites/ only, so
        # reusing it would silently DROP the apps/ check).
        wired.fail_on = [f"test -d {BENCH}/{missing}"]

        with pytest.raises(CwcliError) as exc:
            _update()

        assert exc.value.kind is ErrorKind.NOT_FOUND
        assert "Bench directory not found" in exc.value.message

    def test_the_probe_uses_no_shell(self, wired):
        # ["test", "-d", path] rather than ["sh", "-c", "test -d " + quote(path)]:
        # no shell at all is strictly safer than a correctly quoted one.
        _update()
        assert f"test -d {BENCH}/apps" in wired.calls
        assert not any(c.startswith("sh -c") for c in wired.calls)


# -------------------------------------------------- failed vs unknown (Decision 2)


def _lose_stream_on(container, needle):
    """Make the exec matching ``needle`` look like a dropped connection."""
    api = container.client.api
    real_create = api.exec_create
    api._lost = False

    def exec_create(cid, cmd, workdir=None, tty=False, environment=None):
        cmd_str = cmd if isinstance(cmd, str) else " ".join(cmd)
        api._lost = needle in cmd_str
        return real_create(cid, cmd, workdir=workdir, tty=tty, environment=environment)

    api.exec_create = exec_create
    api.exec_inspect = lambda exec_id: (
        {"ExitCode": None, "Running": True} if api._lost else {"ExitCode": container._last_code}
    )


class TestUnknownIsNotFailed:
    @pytest.fixture(autouse=True)
    def _fast_poll(self, monkeypatch):
        monkeypatch.setattr(exec_stream_mod, "_EXIT_CODE_POLL_TIMEOUT", 0.01)

    def test_a_lost_migration_stream_is_unknown_and_the_fanout_continues(self, monkeypatch, wired):
        # The behaviour change this batch argues for: a lost stream used to ABORT
        # the fan-out while a non-zero exit code was recorded and the fan-out went
        # on - one real-world event, two behaviours, decided by whether Docker
        # happened to record a code. Now both continue.
        monkeypatch.setattr(
            core_update, "_sites_with_app", lambda *a, **k: ["a.localhost", "b.localhost"]
        )
        _lose_stream_on(wired, "bench --site a.localhost migrate")

        report = _update().data

        assert report.unknown_migrations == ["a.localhost"]
        assert report.failed_migrations == []  # NOT a failure: it may still be running
        assert report.ok is False  # ...but never a success either
        # The fan-out continued.
        assert any("bench --site b.localhost migrate" in c for c in wired.calls)
        assert report.aborted is False

    def test_a_lost_stream_carries_a_warning_saying_why(self, monkeypatch, wired):
        _lose_stream_on(wired, "bench --site a.localhost migrate")

        result = _update()

        assert any(w.code == "exec.stream_lost" for w in result.warnings)
        assert any("Lost track of migrate 'a.localhost'" in w.text for w in result.warnings)

    def test_a_lost_pull_stream_is_unknown_and_the_app_is_not_discovered(self, monkeypatch, wired):
        # An app whose outcome is unknown is not KNOWN to have updated, so it gets
        # the same treatment a failed pull gets: no discovery against it.
        _lose_stream_on(wired, "git pull")

        report = _update().data

        assert report.unknown_apps == ["payments"]
        assert report.failed_apps == []
        assert report.affected_sites == []
        assert not any("migrate" in c for c in wired.calls)

    def test_a_lost_build_stream_is_unknown(self, wired):
        _lose_stream_on(wired, "bench build")

        report = _update(build=True).data

        assert report.unknown_builds == ["payments"]
        assert report.failed_builds == []

    def test_exec_start_failure_is_unknown_not_failed(self, wired):
        # Deliberate: exec.start_failed arguably means "it never ran", which would be
        # safe to retry - but that is a confident claim derived from an API call whose
        # own outcome is uncertain, and the safe direction for a retry decision is
        # unknown. Every exec-stream error is unknown.
        from docker.errors import DockerException

        def boom(*a, **k):
            raise DockerException("daemon gone")

        wired.client.api.exec_create = boom

        report = _update().data

        assert report.unknown_apps == ["payments"]
        assert report.failed_apps == []
        assert report.ok is False

    def test_a_returned_nonzero_code_is_still_a_plain_failure(self, wired):
        # The other half of the distinction: a KNOWN non-zero code is a failure, and
        # is safe to retry.
        wired.fail_on = ["bench --site a.localhost migrate"]

        report = _update().data

        assert report.failed_migrations == ["a.localhost"]
        assert report.unknown_migrations == []


# ---------------------------------------------- the maintenance-mode lifecycle


class TestMaintenanceMode:
    def test_a_site_that_cannot_enter_maintenance_is_never_migrated(self, monkeypatch, wired):
        # THE load-bearing gate: migrate only what actually entered maintenance.
        monkeypatch.setattr(
            core_update, "_sites_with_app", lambda *a, **k: ["a.localhost", "b.localhost"]
        )
        wired.fail_on = ["--site b.localhost set-maintenance-mode on"]

        report = _update(clear_cache=True).data

        assert report.failed_maintenance_enable == ["b.localhost"]
        assert report.migrated_sites == ["a.localhost"]
        assert report.ok is False
        assert not any("bench --site b.localhost migrate" in c for c in wired.calls)
        # ...and it is not cache/lock-cleared either: it was never updated.
        assert not any("bench --site b.localhost clear-cache" in c for c in wired.calls)
        assert not any("b.localhost/locks" in c for c in wired.calls if c.startswith("rm -rf"))

    def test_maintenance_is_disabled_for_exactly_the_sites_enabled(self, monkeypatch, wired):
        monkeypatch.setattr(
            core_update, "_sites_with_app", lambda *a, **k: ["a.localhost", "b.localhost"]
        )
        wired.fail_on = ["--site b.localhost set-maintenance-mode on"]

        _update()

        assert any("bench --site a.localhost set-maintenance-mode off" in c for c in wired.calls)
        # b never entered maintenance, so it must not be taken back out of it.
        assert not any(
            "bench --site b.localhost set-maintenance-mode off" in c for c in wired.calls
        )

    def test_a_stuck_site_is_reported_and_never_ok(self, wired):
        wired.fail_on = ["set-maintenance-mode off"]

        report = _update().data

        assert report.failed_maintenance_disable == ["a.localhost"]
        assert report.ok is False

    def test_skip_maintenance_migrates_every_affected_site_and_touches_no_maintenance(
        self, monkeypatch, wired
    ):
        monkeypatch.setattr(
            core_update, "_sites_with_app", lambda *a, **k: ["a.localhost", "b.localhost"]
        )

        report = _update(skip_maintenance=True).data

        assert not any("set-maintenance-mode" in c for c in wired.calls)
        assert report.migrated_sites == ["a.localhost", "b.localhost"]

    def test_the_finally_runs_when_a_step_raises_mid_fanout(self, monkeypatch, wired):
        # The reason core.update is a plain function and not a generator: this
        # guarantee must not depend on a consumer resuming, closing, or dropping an
        # iterator. Re-verified by TEST, not by inspection - inspection is how the
        # reporting gap shipped past a review.
        # Raise during the MIGRATE, i.e. after maintenance mode is actually on -
        # raising during the pull would prove nothing, since there is nothing to
        # undo yet.
        def boom(container, cmd, **k):
            if "migrate" in cmd:
                raise KeyboardInterrupt
            return 0, None

        monkeypatch.setattr(core_update, "_stream_step", boom)

        with pytest.raises(KeyboardInterrupt):
            _update()

        # Maintenance was still taken back off, even though nothing was returned.
        assert any("bench --site a.localhost set-maintenance-mode on" in c for c in wired.calls)
        assert any("bench --site a.localhost set-maintenance-mode off" in c for c in wired.calls)

    def test_an_aborted_run_hands_its_report_to_the_callback(self, monkeypatch, wired):
        # A RETURNED report cannot survive an unwinding KeyboardInterrupt, and a
        # Ctrl-C mid-update must still surface a stuck site's remediation (the
        # property PR #81 restored). So the finally hands it over instead.
        wired.fail_on = ["set-maintenance-mode off"]  # ...and the site really is stuck

        def boom_after_maintenance(container, cmd, **k):
            if "migrate" in cmd:
                raise KeyboardInterrupt
            return 0, None

        monkeypatch.setattr(core_update, "_stream_step", boom_after_maintenance)
        events = []

        with pytest.raises(KeyboardInterrupt):
            _update(on_event=events.append)

        aborted = [e for e in events if isinstance(e, UpdateAborted)]
        assert len(aborted) == 1
        report = aborted[0].report
        assert report.aborted is True
        assert report.failed_maintenance_disable == ["a.localhost"]
        assert report.ok is False

    def test_an_abort_before_any_fanout_is_not_reported_as_an_interrupted_update(
        self, monkeypatch, wired
    ):
        # Raising before a site is selected leaves nothing half-done, so the raise's
        # own error stands alone rather than being dressed up as an interrupted run.
        def boom(*a, **k):
            raise KeyboardInterrupt

        monkeypatch.setattr(core_update, "_sites_with_app", boom)
        events = []

        with pytest.raises(KeyboardInterrupt):
            _update(on_event=events.append)

        aborted = [e for e in events if isinstance(e, UpdateAborted)]
        assert aborted and aborted[0].report.aborted is False


# --------------------------------------------------------------- the --site filter


class TestSiteFilter:
    def test_site_filter_narrows_the_fanout(self, monkeypatch, wired):
        monkeypatch.setattr(
            core_update, "_sites_with_app", lambda *a, **k: ["a.localhost", "b.localhost"]
        )

        report = _update(sites=["a.localhost"]).data

        assert report.migrated_sites == ["a.localhost"]
        assert not any("bench --site b.localhost migrate" in c for c in wired.calls)

    def test_site_filter_matching_nothing_is_a_usage_error(self, wired):
        # Distinct from "no site has the app at all", which exits 0: this is a typo,
        # and completing having migrated nothing would be a silent lie.
        with pytest.raises(CwcliError) as exc:
            _update(sites=["nope.localhost"])

        assert exc.value.kind is ErrorKind.USAGE
        assert "matched no affected site" in exc.value.message

    def test_no_affected_site_at_all_is_a_clean_ok(self, monkeypatch, wired):
        monkeypatch.setattr(core_update, "_sites_with_app", lambda *a, **k: [])

        result = _update()

        assert result.status is Status.OK
        assert result.data.ok is True
        assert result.data.migrated_sites == []


# ------------------------------------------------------------------ the frappe fork


class TestFrappeFork:
    """The core update (frappe, erpnext) is scoped to those apps: a fetch and a reset
    to the remote branch for EACH named core app, never bench's bench-wide
    `bench update --reset` (captain, staging16 2026-10-28: it hard-reset every app on
    the bench), with every site's locks cleared first, the requirements reinstalled,
    then the ordinary per-site migrate under cwcli's own maintenance lifecycle."""

    def test_frappe_is_reset_to_its_remote_branch_and_no_other_app_is_touched(self, wired):
        report = _update(apps=["frappe"]).data

        calls = [c if isinstance(c, str) else " ".join(c) for c in wired.calls]
        assert not any("bench update" in c for c in calls)
        assert "git fetch upstream version-16" in calls
        assert "git reset --hard upstream/version-16" in calls
        assert not any("git pull" in c for c in calls)
        assert "bench setup requirements" in calls
        # payments is on the bench, and nothing ran against it
        assert not any("apps/payments" in str(wd) for wd in wired.workdirs)
        assert report.frappe_reset is True
        assert report.ok is True

    def test_every_site_s_locks_are_cleared_first_and_listed(self, wired):
        events = []
        wired.unlock_output = f"{BENCH}/sites/a.localhost/locks/Module Profile-x.lock\n"

        _update(apps=["frappe"], on_event=events.append)

        ends = [e for e in events if isinstance(e, UpdateStepEnd) and e.phase == "unlock_all"]
        assert ends and ends[0].status == "ok"
        assert "cleared 1 lock" in ends[0].message and "Module Profile" in ends[0].message
        order = [e.phase for e in events if isinstance(e, UpdateStepStart)]
        assert order.index("unlock_all") < order.index("reset")

    def test_the_core_path_migrates_every_affected_site_under_maintenance(self, wired):
        report = _update(apps=["frappe"]).data

        calls = [c if isinstance(c, str) else " ".join(c) for c in wired.calls]
        assert any("set-maintenance-mode on" in c for c in calls)
        assert any("set-maintenance-mode off" in c for c in calls)
        assert report.migrated_sites == ["a.localhost"]

    def test_a_failed_reset_rides_failed_apps_as_frappe_and_migrates_nothing(self, wired):
        wired.fail_on = ["git reset --hard"]

        report = _update(apps=["frappe"]).data

        assert report.failed_apps == ["frappe"]
        assert report.migrated_sites == [] and report.frappe_reset is True
        assert report.ok is False
        assert report.bench_left_in_maintenance is False

    def test_a_failed_migrate_names_the_site_and_clears_its_maintenance(self, wired):
        wired.fail_on = ["migrate"]

        report = _update(apps=["frappe"]).data

        assert report.failed_migrations == ["a.localhost"]
        assert report.failed_apps == []  # the reset itself worked; the stage is named
        assert report.failed_maintenance_disable == []
        assert report.ok is False

    def test_failed_requirements_fail_the_core_apps_before_any_migrate(self, wired):
        wired.fail_on = ["bench setup requirements"]

        result = _update(apps=["frappe"])
        report = result.data

        assert report.failed_apps == ["frappe"] and report.migrated_sites == []
        assert any(w.code == "requirements.failed" for w in result.warnings)

    def test_erpnext_is_a_core_app_too_and_payments_rides_the_plain_pull(self, wired):
        _update(apps=["erpnext", "payments"])

        calls = [c if isinstance(c, str) else " ".join(c) for c in wired.calls]
        assert "git reset --hard upstream/version-16" in calls and "git pull" in calls

    def test_a_site_that_cannot_be_opened_fails_the_run_and_is_named(self, monkeypatch, wired):
        # The staging16 shape with the preflight off: the only site's database login is
        # rejected, discovery cannot read it, the core code has already moved. That is
        # a failed update naming the site, never "successfully updated, 0 sites".
        def unreadable(project, bench_path, app, container, warnings, unreadable=None):
            if unreadable is not None:
                unreadable.append("a.localhost")
            return []

        monkeypatch.setattr(core_update, "_sites_with_app", unreadable)

        report = _update(apps=["frappe"]).data

        assert report.unreadable_sites == ["a.localhost"]
        assert report.migrated_sites == [] and report.ok is False

    def test_discovery_names_the_site_it_could_not_read(self, monkeypatch):
        # No `wired` here: that fixture fakes _sites_with_app itself, and this test
        # drives the real one.
        container = FakeFrappeContainer(available_apps=["frappe"])
        monkeypatch.setattr(core_update.db_utils, "get_cached_project_data", lambda p: {})
        site = types.SimpleNamespace(
            name="broken.localhost", list_apps=None, error="OperationalError"
        )
        bench = types.SimpleNamespace(sites=[site])
        monkeypatch.setattr(
            core_update.bench_read,
            "read_benches",
            lambda c, paths, list_apps=True: types.SimpleNamespace(
                benches={BENCH: bench}, errors={}
            ),
        )
        warnings, unreadable = [], []

        found = core_update._sites_with_app(
            "proj", BENCH, "frappe", container, warnings, unreadable
        )

        assert found == [] and unreadable == ["broken.localhost"]
        assert any(w.code == "discover.site_unreadable" for w in warnings)

    def test_site_and_cache_options_apply_to_a_core_update(self, wired):
        result = _update(apps=["frappe"], clear_cache=True, sites=["a.localhost"])

        assert not any(w.code == "frappe_reset.options_ignored" for w in result.warnings)
        calls = [c if isinstance(c, str) else " ".join(c) for c in wired.calls]
        assert any("clear-cache" in c for c in calls)


# ------------------------------------------------------------- the frappe preflight


def _finding(severity, code, app="someapp"):
    return update_preflight.PreflightFinding(
        severity=severity, code=code, app=app, site=None, message=code, remedy=None
    )


@pytest.fixture()
def preflight_finds(monkeypatch):
    """Make the preflight report ``findings``; return the recorded calls."""
    calls = []

    def arm(*findings):
        def fake(container, bench_path, *, project, bench=None, path=None, local_only=False):
            calls.append((bench_path, project, bench, path))
            if local_only:
                calls.append("local_only")
            return update_preflight.PreflightOutcome(
                findings=list(findings), apps=2, sites=1, elapsed=0.1, bench_path=bench_path
            )

        monkeypatch.setattr(core_update.update_preflight, "run_preflight", fake)
        return calls

    return arm


class TestFrappePreflight:
    def test_a_blocker_refuses_with_nothing_run(self, monkeypatch, wired, preflight_finds):
        recaches = []
        monkeypatch.setattr(
            core_update.cache, "recache_project", lambda *a, **k: recaches.append(a) or True
        )
        preflight_finds(
            _finding(update_preflight.BLOCKER, "git.branch_not_on_remote"),
            _finding(update_preflight.DATA_LOSS, "git.uncommitted", "other"),
        )

        report = _update(apps=["frappe"]).data

        assert not any("git reset --hard" in str(c) for c in wired.calls)
        assert recaches == []  # nothing changed, so nothing to refresh
        assert report.preflight_blocked is True
        assert report.ok is False
        assert report.failed_apps == []  # bench never ran, so nothing "failed"
        # The refusal carries EVERY finding, not just the blocker.
        assert [f.code for f in report.preflight] == ["git.branch_not_on_remote", "git.uncommitted"]

    def test_notices_and_unverified_predictions_do_not_block(self, wired, preflight_finds):
        preflight_finds(
            _finding(update_preflight.NOTICE, "git.no_remote"),
            _finding(update_preflight.UNVERIFIED, "git.remote_unverified"),
        )

        report = _update(apps=["frappe"]).data

        assert any("git reset --hard" in str(c) for c in wired.calls)
        assert report.preflight_blocked is False and report.data_loss_refused is False
        assert report.ok is True
        assert len(report.preflight) == 2

    def test_it_runs_inside_the_credential_bridge_against_the_resolved_bench(
        self, monkeypatch, wired, preflight_finds
    ):
        inside = []
        calls = preflight_finds()

        @contextlib.contextmanager
        def bridge(container, bench_path):
            inside.append(True)
            yield
            inside.append(False)

        monkeypatch.setattr(core_update.credbridge, "credential_bridge", bridge)
        real = core_update.update_preflight.run_preflight
        monkeypatch.setattr(
            core_update.update_preflight,
            "run_preflight",
            lambda *a, **k: (calls.append(inside[-1]), real(*a, **k))[1],
        )

        monkeypatch.setattr(
            core_update.resolvers,
            "resolve_bench",
            lambda *a, **k: core_update.Result(status=Status.OK, data=BENCH, warnings=[]),
        )
        _update(apps=["frappe"], bench="1", bench_path=None)

        # The bridge was open while the preflight probed, against the resolved bench.
        assert calls == [True, (BENCH, "proj", "1", None)]

    def test_it_carries_an_explicit_path_for_remedies(self, wired, preflight_finds):
        calls = preflight_finds()

        _update(apps=["frappe"], path_selector="/workspace/chosen bench")

        assert calls == [(BENCH, "proj", None, "/workspace/chosen bench")]

    def test_no_preflight_runs_only_the_local_tier_and_never_blocks_on_it(
        self, wired, preflight_finds
    ):
        # The local tier still finds the detached HEAD; only the guard acts on it.
        calls = preflight_finds(_finding(update_preflight.BLOCKER, "git.detached_head"))

        report = _update(apps=["frappe"], preflight=False).data

        assert calls == [(BENCH, "proj", None, None), "local_only"]
        assert any("git reset --hard" in str(c) for c in wired.calls)
        assert report.preflight == [] and report.preflight_blocked is False
        assert report.ok is True

    def test_the_per_app_path_never_runs_it(self, wired, preflight_finds):
        calls = preflight_finds(_finding(update_preflight.BLOCKER, "git.detached_head"))

        report = _update(apps=["payments"]).data

        assert calls == []
        assert report.ok is True

    def test_its_findings_are_emitted_before_the_reset_starts(self, wired, preflight_finds):
        preflight_finds(_finding(update_preflight.NOTICE, "git.no_remote"))
        events = []

        _update(apps=["frappe"], on_event=events.append)

        phases = [
            (type(e).__name__, getattr(e, "phase", None))
            for e in events
            if isinstance(e, (UpdateStepStart, UpdatePreflight))
        ]
        assert phases[:3] == [
            ("UpdateStepStart", "preflight"),
            ("UpdatePreflight", None),
            ("UpdateStepStart", "unlock_all"),
        ]
        preflight_event = next(e for e in events if isinstance(e, UpdatePreflight))
        assert preflight_event.blocked is False
        assert preflight_event.summary.startswith("2 app(s), 1 site(s)")

    def test_a_preflight_that_could_not_run_fails_the_guard_closed(self, wired):
        # No record at all: nothing says whether an app holds work the reset discards.
        wired.preflight_output = ""

        refused = _update(apps=["frappe"]).data

        assert [f.code for f in refused.preflight] == ["preflight.unverified"]
        assert refused.preflight_blocked is False  # not a blocker: a prediction failed
        assert refused.data_loss_refused is True and refused.ok is False
        assert not any("git reset --hard" in str(c) for c in wired.calls)

        forced = _update(apps=["frappe"], force=True).data

        assert forced.ok is True
        assert any("git reset --hard" in str(c) for c in wired.calls)


# --------------------------------------------------- the frappe data-loss guard


class TestFrappeDataLossGuard:
    """The core reset hard-resets the named core apps, discarding their uncommitted
    changes and unpushed commits (other apps are not touched, so their local work is
    not at risk). Without ``--force`` that refuses with nothing run - under
    ``--no-preflight`` too, since the guard is not a prediction - and ``--force``
    accepts it, re-owning any app repo git refuses for dubious ownership first.
    """

    @pytest.mark.parametrize("preflight", [True, False])
    @pytest.mark.parametrize(
        "code", ["git.uncommitted", "git.unpushed", "git.local_work_unverified"]
    )
    def test_local_work_refuses_with_nothing_run(
        self, monkeypatch, wired, preflight_finds, preflight, code
    ):
        recaches = []
        monkeypatch.setattr(
            core_update.cache, "recache_project", lambda *a, **k: recaches.append(a) or True
        )
        severity = (
            update_preflight.UNVERIFIED if "unverified" in code else update_preflight.DATA_LOSS
        )
        preflight_finds(
            _finding(severity, code, "frappe"),
            _finding(update_preflight.NOTICE, "git.no_remote", "noremote"),
        )
        events = []

        report = _update(apps=["frappe"], preflight=preflight, on_event=events.append).data

        assert not any("git reset --hard" in str(c) for c in wired.calls)
        assert recaches == []
        assert report.data_loss_refused is True and report.preflight_blocked is False
        assert report.data_loss_apps == ["frappe"]
        assert report.ok is False and report.failed_apps == []
        # Without --no-preflight every finding rides along; with it, only the guard's.
        assert [f.code for f in report.preflight] == (
            [code, "git.no_remote"] if preflight else [code]
        )
        verdict = next(e for e in events if isinstance(e, UpdatePreflight))
        assert verdict.blocked is True and verdict.local_only is (not preflight)

    @pytest.mark.parametrize("preflight", [True, False])
    def test_force_accepts_the_loss_and_the_report_names_the_app(
        self, wired, preflight_finds, preflight
    ):
        preflight_finds(_finding(update_preflight.DATA_LOSS, "git.unpushed", "frappe"))
        events = []

        result = _update(apps=["frappe"], preflight=preflight, force=True, on_event=events.append)

        assert any("git reset --hard" in str(c) for c in wired.calls)
        report = result.data
        assert report.ok is True and report.data_loss_refused is False
        assert report.data_loss_apps == ["frappe"]
        warning = next(w for w in result.warnings if w.code == "frappe_reset.discarding_local_work")
        assert warning.detail == {"apps": ["frappe"], "all_apps_unchecked": False}
        # Said before the reset runs, where it can still be acted on.
        start = next(
            e for e in events if isinstance(e, UpdateStepStart) and e.phase == "core_reset"
        )
        assert "will discard any uncommitted or unpushed work in frappe" in start.message
        # --force is no longer an option the frappe path ignores.
        assert not any(w.code == "frappe_reset.options_ignored" for w in result.warnings)

    @pytest.mark.parametrize("preflight", [True, False])
    def test_force_past_checks_that_never_ran_still_warns_bench_wide(
        self, wired, preflight_finds, preflight
    ):
        preflight_finds(_finding(update_preflight.UNVERIFIED, "preflight.unverified", None))

        refused = _update(apps=["frappe"], preflight=preflight).data
        assert refused.data_loss_refused is True
        assert not any("git reset --hard" in str(c) for c in wired.calls)

        result = _update(apps=["frappe"], preflight=preflight, force=True)

        assert any("git reset --hard" in str(c) for c in wired.calls)
        assert result.data.data_loss_apps == []
        warning = next(w for w in result.warnings if w.code == "frappe_reset.discarding_local_work")
        assert warning.detail == {"apps": [], "all_apps_unchecked": True}
        assert "the core apps, none of whose local work could be checked" in warning.text

    def test_another_app_s_local_work_is_not_at_risk_so_it_does_not_refuse(
        self, wired, preflight_finds
    ):
        preflight_finds(_finding(update_preflight.DATA_LOSS, "git.uncommitted", "payments"))

        report = _update(apps=["frappe"]).data

        assert report.data_loss_refused is False and report.ok is True
        assert any("git reset --hard" in str(c) for c in wired.calls)
        # The fact is still shown, as a notice that says it is not touched.
        finding = next(f for f in report.preflight if f.app == "payments")
        assert finding.severity == update_preflight.NOTICE
        assert "not touched by a core update" in finding.message

    def test_force_never_passes_a_blocker(self, wired, preflight_finds):
        preflight_finds(
            _finding(update_preflight.BLOCKER, "git.branch_not_on_remote", "feature"),
            _finding(update_preflight.DATA_LOSS, "git.uncommitted", "dirty"),
        )

        report = _update(apps=["frappe"], force=True).data

        assert not any("git reset --hard" in str(c) for c in wired.calls)
        assert report.preflight_blocked is True and report.data_loss_refused is False
        assert report.ok is False

    def test_a_blocker_and_local_work_are_both_reported(self, wired, preflight_finds):
        preflight_finds(
            _finding(update_preflight.BLOCKER, "git.branch_not_on_remote", "feature"),
            _finding(update_preflight.DATA_LOSS, "git.uncommitted", "frappe"),
        )

        report = _update(apps=["frappe"]).data

        assert report.preflight_blocked is True and report.data_loss_refused is True
        assert report.data_loss_apps == ["frappe"]

    def test_a_clean_bench_runs_without_force(self, wired, preflight_finds):
        preflight_finds()

        report = _update(apps=["frappe"]).data

        assert any("git reset --hard" in str(c) for c in wired.calls)
        assert report.ok is True and report.data_loss_apps == []

    def test_the_per_app_path_has_no_such_guard(self, wired, preflight_finds):
        calls = preflight_finds(_finding(update_preflight.DATA_LOSS, "git.uncommitted"))

        report = _update(apps=["payments"]).data

        assert calls == [] and report.ok is True and report.data_loss_refused is False


class TestStaging16PreflightFixes:
    """040: an app on a branch its remote does not fetch is widened in place and logged;
    an app whose declared frappe-dependencies the bench cannot satisfy refuses with the
    switch named, and --fix-branches performs it through `cwcli apps checkout`."""

    def _arm(self, monkeypatch, *rounds):
        calls = []

        def fake(container, bench_path, *, project, bench=None, path=None, local_only=False):
            calls.append(local_only)
            facts = list(rounds[min(len(calls), len(rounds)) - 1])
            return update_preflight.PreflightOutcome(
                findings=update_preflight.classify(facts, project=project, bench=bench, path=path),
                apps=2,
                sites=1,
                elapsed=0.1,
                bench_path=bench_path,
                facts=facts,
            )

        monkeypatch.setattr(core_update.update_preflight, "run_preflight", fake)
        return calls

    _MISMATCH = {
        "code": "app.frappe_dependency_unsatisfied",
        "app": "payments",
        "dep": "frappe",
        "spec": ">=17.0.0-dev,<18.0.0",
        "version": "16.54.0",
        "fix_app": "payments",
        "fix_branch": "version-16",
    }

    def test_an_untracked_branch_is_widened_and_the_update_proceeds(self, monkeypatch, wired):
        untracked = {
            "code": "git.branch_not_tracked",
            "app": "frappe",
            "remote": "upstream",
            "branch": "version-16",
            "shallow_repo": True,
        }
        calls = self._arm(monkeypatch, [untracked], [])
        events = []

        result = _update(apps=["frappe"], on_event=events.append)

        fix = [
            c for c in wired.calls if "git remote set-branches --add upstream version-16" in str(c)
        ]
        assert fix and "git fetch --depth=1 upstream version-16" in fix[0]
        assert calls == [False, False]  # checked again after the fix
        assert any(w.code == "preflight.refspec_widened" for w in result.warnings)
        assert any(
            isinstance(e, UpdateStepEnd) and e.phase == "fix_refspec" and e.status == "ok"
            for e in events
        )
        assert result.data.preflight_blocked is False and result.data.ok is True

    def test_a_dependency_mismatch_refuses_and_names_the_switch(self, monkeypatch, wired):
        self._arm(monkeypatch, [self._MISMATCH])

        report = _update(apps=["frappe"]).data

        assert report.preflight_blocked is True and report.ok is False
        finding = report.preflight[0]
        assert finding.severity == update_preflight.BLOCKER
        assert "switch payments to version-16" in finding.message
        assert "cwcli apps checkout proj payments version-16" in finding.remedy
        assert "--fix-branches" in finding.remedy
        assert not any("git reset --hard" in str(c) for c in wired.calls)

    def test_fix_branches_switches_the_app_then_checks_again_and_updates(self, monkeypatch, wired):
        calls = self._arm(monkeypatch, [self._MISMATCH], [])
        switched = []

        def checkout(project, app, ref, *, bench_path=None, on_event=None, **kw):
            switched.append((project, app, ref, bench_path))
            return Result(status=Status.OK, data=types.SimpleNamespace(ok=True), warnings=[])

        monkeypatch.setattr(core_update.core_apps, "checkout_app", checkout)

        result = _update(apps=["frappe"], fix_branches=True)

        assert switched == [("proj", "payments", "version-16", BENCH)]
        assert calls == [False, False]
        assert any(w.code == "preflight.branch_switched" for w in result.warnings)
        assert result.data.preflight_blocked is False and result.data.ok is True

    def test_a_failed_switch_keeps_the_refusal(self, monkeypatch, wired):
        calls = self._arm(monkeypatch, [self._MISMATCH])

        def refuse(*a, **k):
            raise CwcliError(ErrorKind.CONFLICT, "app.dirty_tree", "dirty")

        monkeypatch.setattr(core_update.core_apps, "checkout_app", refuse)

        result = _update(apps=["frappe"], fix_branches=True)

        assert calls == [False]  # nothing changed, nothing to re-check
        assert any(w.code == "preflight.branch_switch_failed" for w in result.warnings)
        assert result.data.preflight_blocked is True


class TestFrappeForceReownsDubiousRepos:
    """``--force`` re-owns exactly the app repos git refused for dubious ownership
    (an owner mismatch), then checks again so their local work is seen too."""

    def _arm(self, monkeypatch, *rounds):
        """Each preflight call returns the next round's findings; return the calls."""
        calls = []

        def fake(container, bench_path, *, project, bench=None, path=None, local_only=False):
            calls.append(local_only)
            return update_preflight.PreflightOutcome(
                findings=list(rounds[min(len(calls), len(rounds)) - 1]),
                apps=2,
                sites=1,
                elapsed=0.1,
                bench_path=bench_path,
            )

        monkeypatch.setattr(core_update.update_preflight, "run_preflight", fake)
        return calls

    def _record_reown(self, monkeypatch, outcome=(True, None)):
        seen = []
        monkeypatch.setattr(
            core_update.core_docker,
            "reown_app_repo_to_frappe",
            lambda container, app_path: seen.append(app_path) or outcome,
        )
        return seen

    @pytest.mark.parametrize("preflight", [True, False])
    def test_force_reowns_the_mismatched_repo_then_checks_again(
        self, monkeypatch, wired, preflight
    ):
        dubious = _finding(update_preflight.BLOCKER, "git.dubious_ownership", "frappe")
        calls = self._arm(
            monkeypatch, [dubious], [_finding(update_preflight.DATA_LOSS, "git.unpushed", "frappe")]
        )
        seen = self._record_reown(monkeypatch)

        result = _update(apps=["frappe"], preflight=preflight, force=True)

        # Once for the preflight's finding, once more before the reset (the per-app
        # --force re-own every pulled app gets).
        assert seen == [f"{BENCH}/apps/frappe", f"{BENCH}/apps/frappe"]
        assert calls == [not preflight, not preflight]  # checked again, same tier
        # The re-owned repo's work is now visible, and --force accepted it.
        assert result.data.data_loss_apps == ["frappe"]
        assert result.data.ok is True
        assert any(w.code == "app.force_reown" for w in result.warnings)
        assert any("git reset --hard" in str(c) for c in wired.calls)

    def test_no_mismatch_means_no_reown_and_one_check(self, monkeypatch, wired):
        calls = self._arm(monkeypatch, [_finding(update_preflight.NOTICE, "git.no_remote")])
        seen = self._record_reown(monkeypatch)

        _update(apps=["frappe"], force=True)

        # No dubious finding, so the preflight re-owns nothing; the pull pass's own
        # --force re-own of the app being reset is the one call left.
        assert seen == [f"{BENCH}/apps/frappe"] and calls == [False]

    def test_without_force_nothing_is_reowned(self, monkeypatch, wired):
        dubious = _finding(update_preflight.BLOCKER, "git.dubious_ownership", "shared")
        self._arm(monkeypatch, [dubious])
        seen = self._record_reown(monkeypatch)

        report = _update(apps=["frappe"]).data

        assert seen == [] and report.preflight_blocked is True

    def test_a_failed_reown_is_reported_and_its_blocker_stands(self, monkeypatch, wired):
        dubious = _finding(update_preflight.BLOCKER, "git.dubious_ownership", "shared")
        calls = self._arm(monkeypatch, [dubious])
        self._record_reown(monkeypatch, (False, "could not re-own the app repo: denied"))

        result = _update(apps=["frappe"], force=True)

        assert calls == [False]  # nothing changed, so nothing to check again
        assert any(w.code == "app.force_reown_failed" for w in result.warnings)
        assert result.data.preflight_blocked is True
        assert not any("git reset --hard" in str(c) for c in wired.calls)


# ----------------------------------------------------------------- the callback


class TestEvents:
    def test_the_callback_is_optional_and_the_run_is_silent_without_one(self, wired, capsys):
        # The drain-and-discard consumption mode: what axi and --json pass. Nothing
        # the core does may reach stdout.
        _update()
        assert capsys.readouterr().out == ""

    def test_events_narrate_the_run_in_order(self, wired):
        events = []
        _update(on_event=events.append)

        phases = [e.phase for e in events if isinstance(e, UpdateStepStart)]
        assert phases.index("pull") < phases.index("migrate")
        assert "maintenance_enable" in phases
        assert phases.index("migrate") < phases.index("maintenance_disable")

    def test_step_end_carries_the_honest_three_way_status(self, wired):
        wired.fail_on = ["bench --site a.localhost migrate"]
        events = []
        _update(on_event=events.append)

        migrate_ends = [e for e in events if isinstance(e, UpdateStepEnd) and e.phase == "migrate"]
        assert [e.status for e in migrate_ends] == ["failed"]

    def test_bench_output_rides_the_callback_tagged_with_its_stream(self, monkeypatch, wired):
        class _NoisyContainer(FakeFrappeContainer):
            def _run(self, cmd, workdir=None):
                cmd_str = cmd if isinstance(cmd, str) else " ".join(cmd)
                if cmd_str == "git pull":
                    self.calls.append(cmd_str)
                    return 0, "Already up to date.\n"
                return super()._run(cmd, workdir)

        noisy = _NoisyContainer(available_apps=["frappe", "payments"])
        monkeypatch.setattr(core_update.core_docker, "get_frappe_container", lambda name: noisy)
        events = []

        _update(on_event=events.append)

        output = [e for e in events if isinstance(e, UpdateOutput) and e.phase == "pull"]
        assert output
        assert output[0].stream == "stdout"
        assert "Already up to date." in "".join(e.text for e in output)

    def test_a_callback_that_raises_does_not_strand_a_site_in_maintenance(self, wired):
        # A frontend's renderer is not trusted to be well-behaved: if it blows up
        # mid-run, the finally still runs because it is in a plain function.
        def boom(event):
            if isinstance(event, UpdateStepStart) and event.phase == "migrate":
                raise RuntimeError("renderer exploded")

        with pytest.raises(RuntimeError):
            _update(on_event=boom)

        assert any("bench --site a.localhost set-maintenance-mode off" in c for c in wired.calls)


# --------------------------------------------------- _sites_with_app live fallback


class _SiteQueryContainer:
    """Serves the live-fallback execs of ``_sites_with_app``: the ``ls -1 .../sites``
    directory listing and per-site ``bench ... list-apps`` (REALISTIC versioned lines,
    ``frappe 16.26.3``, exactly as ``bench list-apps`` prints them), from which
    ``emulate_batched`` answers ``core.bench_read``'s one-exec read."""

    def __init__(self, *, sites, installed, fail_on=None):
        self.sites = sites  # names under <bench>/sites
        self.installed = installed  # site -> [versioned "app x.y.z" lines]
        self.fail_on = fail_on or []  # substrings that make an exec fail
        self.calls = []

    def exec_run(self, cmd, workdir=None, **kwargs):
        if isinstance(cmd, list):
            return emulate_batched(self, cmd) or (1, b"")
        self.calls.append(cmd)
        for sub in self.fail_on:
            if sub in cmd:
                return 1, b"boom"
        if cmd.startswith("ls -1") and cmd.rstrip().endswith("/sites"):
            return 0, ("\n".join(self.sites) + "\n").encode()
        if "list-apps" in cmd:
            parts = shlex.split(cmd)
            site = parts[parts.index("--site") + 1]
            return 0, ("\n".join(self.installed.get(site, [])) + "\n").encode()
        return 0, b""


def _seed_cache(monkeypatch, cached):
    monkeypatch.setattr(
        core_update.db_utils, "get_cached_project_data", lambda project_name: cached
    )


class TestSitesWithAppLiveFallback:
    """The live-query fallback of ``_sites_with_app``.

    Production ALWAYS lands here: the cache stores RAW ``bench list-apps`` lines
    (``frappe 16.26.3``) and the cache branch does exact membership against a bare
    app name, so it never matches on a real bench and falls through here. These
    exercise that path directly with realistic versioned cached data."""

    def test_versioned_cache_misses_and_the_live_query_answers(self, monkeypatch):
        # Cache holds the REAL shape: "payments 16.1.0", not "payments". The bare
        # `app in installed_apps` membership fails, forcing the live query.
        _seed_cache(
            monkeypatch,
            {
                "bench_instances": [
                    {
                        "path": BENCH,
                        "sites": [
                            {
                                "name": "a.localhost",
                                "installed_apps": ["frappe 16.26.3", "payments 16.1.0"],
                            },
                            {"name": "b.localhost", "installed_apps": ["frappe 16.26.3"]},
                        ],
                    }
                ]
            },
        )
        container = _SiteQueryContainer(
            sites=["a.localhost", "b.localhost", "apps.txt", "assets"],
            installed={
                "a.localhost": ["frappe 16.26.3", "payments 16.1.0"],
                "b.localhost": ["frappe 16.26.3"],
            },
        )

        found = core_update._sites_with_app("proj", BENCH, "payments", container, [])

        # The live query - not the cache - produced this, proving the fallback ran.
        assert found == ["a.localhost"]
        assert any(c.startswith("ls -1") and c.endswith("/sites") for c in container.calls)
        assert any("--site a.localhost list-apps" in c for c in container.calls)

    def test_empty_cache_also_uses_the_live_query(self, monkeypatch):
        _seed_cache(monkeypatch, None)
        container = _SiteQueryContainer(
            sites=["a.localhost"], installed={"a.localhost": ["frappe 16.26.3"]}
        )

        assert core_update._sites_with_app("proj", BENCH, "frappe", container, []) == [
            "a.localhost"
        ]

    def test_live_fallback_without_a_container_returns_empty(self, monkeypatch):
        # Versioned cache misses AND no container to query -> honestly empty.
        _seed_cache(
            monkeypatch,
            {
                "bench_instances": [
                    {"path": BENCH, "sites": [{"name": "a", "installed_apps": ["frappe 16.26.3"]}]}
                ]
            },
        )
        assert core_update._sites_with_app("proj", BENCH, "frappe", None, []) == []

    def test_live_fallback_skips_a_site_whose_list_apps_fails(self, monkeypatch):
        _seed_cache(monkeypatch, None)
        container = _SiteQueryContainer(
            sites=["a.localhost", "b.localhost"],
            installed={"a.localhost": ["frappe 16.26.3"], "b.localhost": ["frappe 16.26.3"]},
            fail_on=["--site b.localhost list-apps"],
        )
        warnings: list = []
        found = core_update._sites_with_app("proj", BENCH, "frappe", container, warnings)

        assert found == ["a.localhost"]
        [warning] = warnings
        assert warning.code == "discover.site_unreadable"
        assert "'b.localhost'" in warning.text
        assert f"{BENCH}: list-apps failed (RuntimeError)" in warning.text

    def test_live_fallback_returns_empty_when_site_listing_fails(self, monkeypatch):
        _seed_cache(monkeypatch, None)
        container = _SiteQueryContainer(sites=[], installed={}, fail_on=["/sites"])
        warnings: list = []
        assert core_update._sites_with_app("proj", BENCH, "frappe", container, warnings) == []
        assert [w.code for w in warnings] == ["discover.bench_unreadable"]
        assert BENCH in warnings[0].text

    def test_a_bench_with_no_record_is_reported_with_its_cause(self, monkeypatch):
        _seed_cache(monkeypatch, None)
        container = _SiteQueryContainer(sites=[], installed={})
        container.exec_run = lambda cmd, workdir=None, **k: (
            0,
            f"\n{core_update.bench_read.FAILED}139\t{BENCH}\n".encode(),
        )
        warnings: list = []

        assert core_update._sites_with_app("proj", BENCH, "frappe", container, warnings) == []
        [warning] = warnings
        assert warning.code == "discover.bench_unreadable"
        assert BENCH in warning.text
        assert "its read process exited with code 139" in warning.text


# ---------------------------------------------------------- the --force conflict reset


class TestForceResetOnConflict:
    """``--force`` hard-resets an app to upstream ONLY when its pull hit a conflict.

    A conflict is signalled by a DIRTY working tree after a failed ``git pull`` (local
    changes that block the merge, or a conflicted merge): ``git status --porcelain``
    is non-empty. A pull that failed on the network/auth leaves a clean tree, so
    ``--force`` must NOT discard work there. And a clean update is untouched by the
    flag entirely.
    """

    def test_force_hard_resets_on_conflict_then_completes_clean(self, wired):
        wired.fail_on = ["git pull"]  # the pull hits a conflict
        wired.porcelain = " M apps/payments/hooks.py\n"  # dirty tree = the conflict signal

        result = _update(force=True)
        report = result.data

        # The update completed: the app is un-failed, reported as force-reset, and its
        # site migrated.
        assert report.ok is True
        assert report.force_reset_apps == ["payments"]
        assert report.failed_apps == []
        assert report.migrated_sites == ["a.localhost"]
        # The destructive recovery actually ran: fetch + hard reset to the upstream.
        assert "git fetch" in wired.calls
        assert "git reset --hard @{u}" in wired.calls
        # A clear warning was emitted at the reset.
        assert any(w.code == "app.force_reset" for w in result.warnings)

    def test_without_force_a_conflict_fails_safe_and_discards_nothing(self, wired):
        wired.fail_on = ["git pull"]
        wired.porcelain = " M apps/payments/hooks.py\n"

        result = _update()  # no --force: current behavior preserved

        report = result.data
        assert report.ok is False
        assert report.failed_apps == ["payments"]
        assert report.force_reset_apps == []
        # NOTHING destructive ran - not even a status probe for the reset decision.
        assert not any("git reset" in c for c in wired.calls)

    def test_a_clean_update_is_untouched_by_force(self, wired):
        # git pull succeeds, so --force is a pure no-op.
        result = _update(force=True)

        report = result.data
        assert report.ok is True
        assert report.force_reset_apps == []
        assert "git pull" in wired.calls
        assert not any("git reset" in c for c in wired.calls)

    def test_force_does_not_reset_a_non_conflict_failure(self, wired):
        # The pull fails but the tree is CLEAN: a network/auth failure a reset cannot
        # fix, so --force leaves it as the honest failure it is.
        wired.fail_on = ["git pull"]
        wired.porcelain = ""  # clean

        result = _update(force=True)

        report = result.data
        assert report.ok is False
        assert report.failed_apps == ["payments"]
        assert report.force_reset_apps == []
        assert not any("git reset" in c for c in wired.calls)

    def test_force_fails_closed_when_the_working_tree_state_is_unreadable(self, wired):
        # An unreadable `git status` must NOT be guessed as dirty: never discard work
        # on an unknown (the safe direction for a destructive action).
        wired.fail_on = ["git pull", "git status --porcelain"]

        result = _update(force=True)

        report = result.data
        assert report.force_reset_apps == []
        assert report.failed_apps == ["payments"]
        assert not any("git reset" in c for c in wired.calls)


class TestForceReownsADubiousOwnedAppRepo:
    """``--force`` clears a git "dubious ownership" refusal at its root by re-owning
    the app repo to the container user BEFORE the pull.

    On a shared-mode/migrated instance the app source (often symlinked out of the
    bench to a shared tree like ``/workspace/.hdsrc/<app>``) is owned by the
    pre-shared uid, so git run as ``frappe`` refuses it and NEITHER the pull nor the
    ``git status`` the conflict path relies on can run - so plain ``--force`` was
    powerless. The fix re-owns the repo up front, which is what lets the pull proceed.
    """

    def test_force_reowns_each_app_before_pulling(self, wired, monkeypatch):
        seen = []
        monkeypatch.setattr(
            core_update.core_docker,
            "reown_app_repo_to_frappe",
            lambda container, app_path: seen.append(app_path) or (True, None),
        )
        result = _update(force=True)
        assert result.data.ok is True
        # re-owned the exact app repo path, and the pull still ran
        assert seen == [f"{BENCH}/apps/payments"]
        assert "git pull" in wired.calls
        # a re-own reports itself so the operator sees why ownership changed
        assert any(w.code == "app.force_reown" for w in result.warnings)

    def test_without_force_no_repo_is_reowned(self, wired, monkeypatch):
        seen = []
        monkeypatch.setattr(
            core_update.core_docker,
            "reown_app_repo_to_frappe",
            lambda container, app_path: seen.append(app_path) or (True, None),
        )
        _update()  # no --force
        assert seen == []

    def test_a_failed_reown_is_a_warning_and_the_pull_still_runs(self, wired, monkeypatch):
        monkeypatch.setattr(
            core_update.core_docker,
            "reown_app_repo_to_frappe",
            lambda container, app_path: (False, "could not re-own the app repo: denied"),
        )
        result = _update(force=True)
        assert "git pull" in wired.calls
        assert any(w.code == "app.force_reown_failed" for w in result.warnings)


# ------------------------------------------------------ BUG-10: wrong-copy detection


class TestWrongCopyDetection:
    """`git pull` lands on apps/<app>, but the bench may import a different copy."""

    def _patch_diverged(self, monkeypatch, imported="/workspace/.hdsrc/payments"):
        def _fake(_container, bench, apps):
            return {
                a: core_update.resolvers.AppImport(
                    app=a,
                    entry=f"{bench}/apps/{a}",
                    is_symlink=False,
                    resolved_path=f"{bench}/apps/{a}",
                    imported_path=imported,
                    diverged=True,
                    checked=True,
                )
                for a in apps
            }

        monkeypatch.setattr(core_update.resolvers, "resolve_app_imports", _fake)

    def test_a_wrong_copy_pull_is_not_ok_and_names_both_paths(self, monkeypatch, wired):
        self._patch_diverged(monkeypatch)

        result = _update()

        assert result.data.ok is False
        assert result.data.app_imports[0].diverged is True
        assert result.data.app_imports[0].imported_path == "/workspace/.hdsrc/payments"
        warning = next(w for w in result.warnings if w.code == "app.wrong_copy")
        assert f"{BENCH}/apps/payments" in warning.text
        assert "/workspace/.hdsrc/payments" in warning.text

    def test_a_skipped_app_is_not_checked_for_divergence(self, monkeypatch, wired):
        """A failed pull skips the app, so there is no updated copy to verify."""
        wired.fail_on = ["git pull"]
        self._patch_diverged(monkeypatch)

        result = _update()

        # The pull failed (already not-ok); the app was skipped, so no divergence row.
        assert result.data.app_imports == []


# ------------------------------------ interrupt cleanup: end the orphan, clear last
#
# The v16 race BUG-11 fix, wired into the apps-update fan-out. The shared mechanism
# lives in core.bench_ops (proven in test_core_bench_ops); here what matters is that
# `_update_apps` routes its interrupt cleanup through that ONE shared place: the
# migrate exec carries the marker, the orphan is ended BEFORE maintenance is cleared,
# and the clear re-checks on the abort path.


class _OrphanAPI(_FakeAPI):
    """The migrate exec raises KeyboardInterrupt when read (the interrupt), leaving an
    orphan alive; every other exec (git pull, ...) streams normally."""

    def exec_create(self, cid, cmd, workdir=None, tty=False, environment=None):
        if "migrate" in cmd:
            self.container.migrate_envs.append(environment)
        return super().exec_create(cid, cmd, workdir=workdir, tty=tty, environment=environment)

    def exec_start(self, exec_id, stream=True, demux=False):
        cmd, _ = self._pending
        if "migrate" in cmd:
            self.container.orphan_alive = True
            # Model a v15+ bench migrate that self-manages maintenance: the migrate
            # process, once running, has put its site INTO maintenance mode. Under
            # --skip-maintenance cwcli enabled none, so this is the ONLY thing that set
            # it - which is exactly why an interrupt that ends the orphan must restore
            # the pre-state rather than leave the site stuck at 503.
            site = _site_of(cmd)
            if site:
                self.container.maintenance[site] = True

            def _boom():
                raise KeyboardInterrupt
                yield  # pragma: no cover - only makes this a generator

            return _boom()
        return super().exec_start(exec_id, stream=stream, demux=demux)


class _OrphanMigrateContainer(FakeFrappeContainer):
    """Models the v16 race in the fan-out: an orphan re-asserts maintenance after cwcli
    clears it, until cwcli kills it by its marker (see test_core_bench_ops for the same
    model on the standalone migrate verb)."""

    def __init__(self, *, killable=True, unreadable_sites=None, **kw):
        super().__init__(**kw)
        self.maintenance: dict[str, bool] = {}
        # Sites whose site_config.json reads as unreadable, so maintenance_mode_on
        # returns None (the "pre-state unknown" review-1 case).
        self.unreadable_sites = set(unreadable_sites or ())
        self.orphan_alive = False
        self.killable = killable
        self.migrate_envs: list = []
        self.client = types.SimpleNamespace(api=_OrphanAPI(self))

    def _run(self, cmd, workdir=None):
        s = cmd if isinstance(cmd, str) else " ".join(cmd)
        if "set-maintenance-mode on" in s:
            self.calls.append(s)
            self.maintenance[_site_of(s)] = True
            return 0, ""
        if "set-maintenance-mode off" in s:
            self.calls.append(s)
            site = _site_of(s)
            self.maintenance[site] = False
            # THE RACE: the still-running orphan re-asserts maintenance right after the
            # clear, then dies without clearing it again.
            if self.orphan_alive:
                self.maintenance[site] = True
            return 0, ""
        if s.startswith("cat sites/") and s.endswith("site_config.json"):
            self.calls.append(s)
            site = s.split("sites/")[1].split("/")[0]
            if site in self.unreadable_sites:
                return 1, ""  # unreadable -> supervision.maintenance_mode_on returns None
            return 0, json.dumps({"maintenance_mode": 1 if self.maintenance.get(site) else 0})
        if "/proc/[0-9]*" in s and "kill -" in s:  # _signal_marked
            self.calls.append(s)
            if self.killable:
                self.orphan_alive = False
            return 0, ""
        if "/proc/[0-9]*" in s:  # _marked_process_alive probe
            self.calls.append(s)
            return (0, "") if self.orphan_alive else (1, "")
        return super()._run(cmd, workdir)


def _site_of(cmd: str) -> str:
    parts = cmd.split()
    return parts[parts.index("--site") + 1] if "--site" in parts else ""


def _wire_orphan(monkeypatch, container):
    monkeypatch.setattr(core_docker, "get_frappe_container", lambda name: container)
    monkeypatch.setattr(core_update.core_docker, "get_frappe_container", lambda name: container)
    monkeypatch.setattr(core_update.cache, "recache_project", lambda *a, **k: True)
    # core_update.time and bench_ops.time are the SAME module; _fake_clock makes sleeps
    # instant AND advances a fake monotonic clock, so the interrupt-cleanup deadline is
    # reached by the clock rather than really waited or spun (see _fake_clock).
    _fake_clock(monkeypatch)
    monkeypatch.setattr(core_update, "_sites_with_app", lambda *a, **k: ["a.localhost"])
    _wire_stopped_bench(monkeypatch)


def test_apps_update_interrupt_ends_the_orphan_and_leaves_the_site_out_of_maintenance(
    monkeypatch,
):
    """DISCRIMINATING: on the pre-fix single-disable finally the orphan re-asserts and
    the site stays stuck in maintenance; with the fix the orphan is ended first, so the
    clear is the last write and the site is out of maintenance."""
    container = _OrphanMigrateContainer(available_apps=["frappe", "payments"], killable=True)
    _wire_orphan(monkeypatch, container)

    with pytest.raises(KeyboardInterrupt):
        core_update.update("proj", ["payments"], bench_path=BENCH)

    assert container.orphan_alive is False
    assert container.maintenance.get("a.localhost") is False
    # The migrate exec carried the interrupt marker the cleanup used.
    assert container.migrate_envs and container.migrate_envs[0]
    assert bench_ops._MIGRATE_MARKER_ENV in container.migrate_envs[0]


def test_apps_update_interrupt_terminates_before_it_clears_maintenance(monkeypatch):
    container = _OrphanMigrateContainer(available_apps=["frappe", "payments"], killable=True)
    _wire_orphan(monkeypatch, container)

    with pytest.raises(KeyboardInterrupt):
        core_update.update("proj", ["payments"], bench_path=BENCH)

    first_kill = next(i for i, c in enumerate(container.calls) if "kill -" in c)
    first_clear = next(i for i, c in enumerate(container.calls) if "set-maintenance-mode off" in c)
    assert first_kill < first_clear


def test_apps_update_interrupt_with_an_unkillable_orphan_still_clears_and_says_so(monkeypatch):
    container = _OrphanMigrateContainer(available_apps=["frappe", "payments"], killable=False)
    _wire_orphan(monkeypatch, container)
    events: list = []

    with pytest.raises(KeyboardInterrupt):
        core_update.update("proj", ["payments"], bench_path=BENCH, on_event=events.append)

    # Escalated SIGTERM -> SIGKILL and still attempted the clear.
    assert any("kill -TERM" in c for c in container.calls)
    assert any("kill -KILL" in c for c in container.calls)
    assert any("set-maintenance-mode off" in c for c in container.calls)
    # Said so plainly, naming the re-check + clear commands.
    notice = next(
        e for e in events if isinstance(e, UpdateStepEnd) and e.phase == "interrupt_cleanup"
    )
    assert "may still be running" in notice.message
    assert "set-maintenance-mode off" in notice.message


# ------------------- review-1: --skip-maintenance abort restores the pre-migrate state
#
# Under --skip-maintenance cwcli enables no maintenance, but the interrupt cleanup still
# ENDS the orphaned migrate that on v15+ would have cleared maintenance as it finished.
# Rather than strand the site at 503 (a regression) or override an operator who manages
# maintenance themselves, the abort restores each migrated site's PRE-migrate state.


def _off_calls(container, site: str) -> list[str]:
    return [c for c in container.calls if "set-maintenance-mode off" in c and site in c]


def _interrupt_notes(events) -> list[str]:
    return [
        e.message
        for e in events
        if isinstance(e, UpdateStepEnd) and e.phase == "interrupt_cleanup" and e.message
    ]


def test_skip_maintenance_interrupt_clears_a_site_that_was_off_before(monkeypatch):
    """was-OFF -> cleared. The site was not in maintenance before the migrate, so after
    cwcli ends the orphan the site is taken back out of maintenance (last write)."""
    container = _OrphanMigrateContainer(available_apps=["frappe", "payments"], killable=True)
    _wire_orphan(monkeypatch, container)
    # a.localhost starts OFF (maintenance dict empty), so pre-state is False.

    with pytest.raises(KeyboardInterrupt):
        core_update.update("proj", ["payments"], bench_path=BENCH, skip_maintenance=True)

    # cwcli enabled no maintenance (skip), the orphan (v15+) set it ON, cwcli ended the
    # orphan and then CLEARED it because it was off before.
    assert _off_calls(container, "a.localhost"), "a was-off site must be cleared on abort"
    assert container.maintenance.get("a.localhost") is False


def test_skip_maintenance_interrupt_leaves_a_site_that_was_on_before(monkeypatch):
    """was-ON -> left on with a one-line note. The operator had already put the site
    into maintenance, so --skip-maintenance must not override that."""
    container = _OrphanMigrateContainer(available_apps=["frappe", "payments"], killable=True)
    _wire_orphan(monkeypatch, container)
    container.maintenance["a.localhost"] = True  # operator-managed: ON before the migrate
    events: list = []

    with pytest.raises(KeyboardInterrupt):
        core_update.update(
            "proj", ["payments"], bench_path=BENCH, skip_maintenance=True, on_event=events.append
        )

    # Left in maintenance: no clear was issued for it, and it says so in one line.
    assert not _off_calls(container, "a.localhost"), "an operator-managed site must be left on"
    assert container.maintenance.get("a.localhost") is True
    notes = _interrupt_notes(events)
    assert any(
        "a.localhost" in m and "already in maintenance" in m for m in notes
    ), f"expected an 'already in maintenance' note; got {notes}"


def test_skip_maintenance_interrupt_clears_with_a_note_when_pre_state_unknown(monkeypatch):
    """unknown -> cleared, with a one-line note naming how to turn it back on. The
    pre-state could not be read, so fall back to clearing (never leave it stuck)."""
    container = _OrphanMigrateContainer(
        available_apps=["frappe", "payments"], killable=True, unreadable_sites={"a.localhost"}
    )
    _wire_orphan(monkeypatch, container)
    events: list = []

    with pytest.raises(KeyboardInterrupt):
        core_update.update(
            "proj", ["payments"], bench_path=BENCH, skip_maintenance=True, on_event=events.append
        )

    # Fell back to clearing, and named the re-enable command in one line.
    assert _off_calls(container, "a.localhost"), "unknown pre-state must fall back to clearing"
    notes = _interrupt_notes(events)
    assert any(
        "a.localhost" in m and "set-maintenance-mode on" in m for m in notes
    ), f"expected a note naming the re-enable command; got {notes}"
