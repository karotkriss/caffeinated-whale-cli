"""``core.update`` - the app-update state machine on the logic core.

Covers what the envelope makes possible and what the migration must never lose:
the seven-way aggregation as RETURNED data, the load-bearing maintenance gate, the
unconditional ``finally``, the failed-vs-unknown distinction (design Decision 2),
the frappe fork, ``NEEDS_CHOICE``, and that no live Docker object rides the DTO.

The command-level behaviour lives in `test_apps.py` and
`test_update_characterization.py`; these drive the core directly.
"""

import dataclasses
import shlex
import types

import pytest

from caffeinated_whale_cli.core import docker as core_docker
from caffeinated_whale_cli.core import exec_stream as exec_stream_mod
from caffeinated_whale_cli.core import resolvers
from caffeinated_whale_cli.core import update as core_update
from caffeinated_whale_cli.core.envelope import Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from caffeinated_whale_cli.core.update import (
    UpdateAborted,
    UpdateOutput,
    UpdateStepEnd,
    UpdateStepStart,
)

from .test_apps import FakeFrappeContainer, _wire_stopped_bench

BENCH = "/workspace/frappe-bench"


@pytest.fixture()
def wired(monkeypatch):
    """Container resolution + recache + sleep, faked. Returns the fake container."""
    container = FakeFrappeContainer(available_apps=["frappe", "payments"])
    monkeypatch.setattr(core_docker, "get_frappe_container", lambda name: container)
    monkeypatch.setattr(core_update.core_docker, "get_frappe_container", lambda name: container)
    monkeypatch.setattr(core_update.cache, "recache_project", lambda *a, **k: True)
    monkeypatch.setattr(core_update.time, "sleep", lambda *a, **k: None)
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
    def test_frappe_runs_the_bench_wide_reset_and_no_git_pull(self, wired):
        report = _update(apps=["frappe"]).data

        assert any("bench update --reset" in c for c in wired.calls)
        assert not any("git pull" in c for c in wired.calls)
        assert report.frappe_reset is True
        assert report.ok is True

    def test_the_frappe_path_enters_no_maintenance_mode(self, wired):
        # It returns BEFORE the state machine: bench update --reset manages its own.
        _update(apps=["frappe"])

        assert not any("set-maintenance-mode" in c for c in wired.calls)

    def test_a_failed_reset_rides_failed_apps_as_frappe(self, wired):
        wired.fail_on = ["bench update --reset"]

        report = _update(apps=["frappe"]).data

        assert report.failed_apps == ["frappe"]
        assert report.frappe_reset is True
        assert report.ok is False

    def test_the_reset_recaches_before_checking_the_exit_code(self, monkeypatch, wired):
        # Preserved deliberately, not "fixed" in a migration: a partially-applied
        # reset genuinely changes the cache, so a FAILED reset still warrants a
        # refresh. Defensible and previously untested either way.
        wired.fail_on = ["bench update --reset"]
        recaches = []
        monkeypatch.setattr(
            core_update.cache,
            "recache_project",
            lambda name, verbose=False: (recaches.append(name), True)[1],
        )

        report = _update(apps=["frappe"]).data

        assert recaches == ["proj"]
        assert report.ok is False

    def test_ignored_options_are_announced_not_silently_dropped(self, wired):
        result = _update(apps=["frappe"], clear_cache=True, sites=["a.localhost"])

        note = [w for w in result.warnings if w.code == "frappe_reset.options_ignored"]
        assert note
        assert "--clear-cache" in note[0].text
        assert "--site" in note[0].text


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
    ``frappe 16.26.3``, exactly as ``bench list-apps`` prints them)."""

    def __init__(self, *, sites, installed, fail_on=None):
        self.sites = sites  # names under <bench>/sites
        self.installed = installed  # site -> [versioned "app x.y.z" lines]
        self.fail_on = fail_on or []  # substrings that make an exec fail
        self.calls = []

    def exec_run(self, cmd, workdir=None, **kwargs):
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
    """The live-query fallback of ``_sites_with_app`` (update.py:295-321).

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

        found = core_update._sites_with_app("proj", BENCH, "payments", container)

        # The live query - not the cache - produced this, proving the fallback ran.
        assert found == ["a.localhost"]
        assert any(c.startswith("ls -1") and c.endswith("/sites") for c in container.calls)
        assert any("--site a.localhost list-apps" in c for c in container.calls)

    def test_empty_cache_also_uses_the_live_query(self, monkeypatch):
        _seed_cache(monkeypatch, None)
        container = _SiteQueryContainer(
            sites=["a.localhost"], installed={"a.localhost": ["frappe 16.26.3"]}
        )

        assert core_update._sites_with_app("proj", BENCH, "frappe", container) == ["a.localhost"]

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
        assert core_update._sites_with_app("proj", BENCH, "frappe", None) == []

    def test_live_fallback_skips_a_site_whose_list_apps_fails(self, monkeypatch):
        _seed_cache(monkeypatch, None)
        container = _SiteQueryContainer(
            sites=["a.localhost", "b.localhost"],
            installed={"a.localhost": ["frappe 16.26.3"], "b.localhost": ["frappe 16.26.3"]},
            fail_on=["--site b.localhost list-apps"],
        )
        assert core_update._sites_with_app("proj", BENCH, "frappe", container) == ["a.localhost"]

    def test_live_fallback_returns_empty_when_site_listing_fails(self, monkeypatch):
        _seed_cache(monkeypatch, None)
        container = _SiteQueryContainer(sites=[], installed={}, fail_on=["/sites"])
        assert core_update._sites_with_app("proj", BENCH, "frappe", container) == []
