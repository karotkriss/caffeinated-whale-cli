"""A new site's apps must be importable by the bench processes already serving.

The defect: ``new-site --app X`` (and init's ERPNext on a reused bench, and the web
UI's New site) ran ``bench install-app`` on a bench whose web and worker processes
booted BEFORE app X reached the bench. A Python process computes its import path at
boot, so the site answered HTTP 500 until someone ran ``cwcli restart`` by hand.
The fix reloads those processes, but only when they predate the app's arrival, so a
scheduled restore drill that recreates a site from apps the bench already served
does not cycle a live bench. Pure: no Docker, every container read is faked.
"""

from __future__ import annotations

from caffeinated_whale_cli.core import init as core_init
from caffeinated_whale_cli.core import supervision

BENCH = "/workspace/frappe-bench"
QUIET = supervision.ResyncOutcome(attempted=False, restarted=[], unserved_sites=[], error=None)


class TestCodePredatesLanding:
    """The decision: may a live code-bearing process have booted before the code landed?"""

    def test_a_process_older_than_the_app_reloads(self):
        # Booted at 1000 (now 2000, up 1000s); the app landed at 1500.
        assert supervision.code_predates_landing(2000, [1000], [1500]) is True

    def test_a_process_younger_than_the_app_does_not(self):
        # Booted at 1900; the app was on the bench since 1500, so it imports fine.
        assert supervision.code_predates_landing(2000, [100], [1500]) is False

    def test_the_oldest_process_decides(self):
        # A recycled young worker does not hide the long-lived web process.
        assert supervision.code_predates_landing(2000, [10, 1000], [1500]) is True

    def test_the_newest_landing_decides(self):
        assert supervision.code_predates_landing(2000, [300], [100, 1800]) is True

    def test_a_boot_within_the_clock_slack_is_ambiguous_and_reloads(self):
        assert supervision.code_predates_landing(2000, [500], [1499]) is True

    def test_nothing_running_needs_no_reload(self):
        assert supervision.code_predates_landing(2000, [], [1500]) is False

    def test_unknown_reads_reload(self):
        assert supervision.code_predates_landing(None, [100], [1500]) is True
        assert supervision.code_predates_landing(2000, [None], [1500]) is True
        assert supervision.code_predates_landing(2000, [100], [None]) is True


class _ClockFake:
    """Answers the one ``date; stat`` exec with fixed lines."""

    def __init__(self, text: str):
        self.text = text
        self.calls: list = []

    def exec_run(self, cmd, **_k):
        self.calls.append(cmd)
        return 0, self.text.encode()


class TestLandingTimes:
    def test_reads_the_clock_then_one_mtime_per_path(self):
        fake = _ClockFake("2000\n1500\n-\n")
        now, times = supervision._landing_times(fake, ["a", "b"])
        assert (now, times) == (2000, [1500, None])
        assert fake.calls[0][-2:] == ["a", "b"]

    def test_short_output_pads_with_unknown(self):
        assert supervision._landing_times(_ClockFake(""), ["a"]) == (None, [None])


def _wire(monkeypatch, *, uptimes, now=2000, landed=(1500,), raises=False):
    calls: list[dict] = []

    def uptimes_fn(_c, _p):
        if raises:
            raise RuntimeError("ps failed")
        return list(uptimes)

    monkeypatch.setattr(supervision, "_code_bearing_uptimes", uptimes_fn)
    monkeypatch.setattr(supervision, "_landing_times", lambda _c, paths: (now, list(landed)))

    def resync(_c, _p, **kwargs):
        calls.append(kwargs)
        return supervision.ResyncOutcome(
            attempted=True, restarted=["web", "worker_default"], unserved_sites=[], error=None
        )

    monkeypatch.setattr(supervision, "resync_after_code_change", resync)
    return calls


class TestResyncIfCodeLanded:
    def test_a_stale_bench_is_reloaded_and_the_new_site_verified(self, monkeypatch):
        calls = _wire(monkeypatch, uptimes=[1000])
        outcome = supervision.resync_if_code_landed(
            object(), BENCH, sites=["new.localhost"], landed_paths=["x"]
        )
        assert outcome.restarted == ["web", "worker_default"]
        assert calls[0]["sites"] == ["new.localhost"]

    def test_a_bench_that_booted_after_the_app_is_left_running(self, monkeypatch):
        calls = _wire(monkeypatch, uptimes=[100])
        outcome = supervision.resync_if_code_landed(
            object(), BENCH, sites=["drill.localhost"], landed_paths=["x"]
        )
        assert outcome == QUIET and calls == []

    def test_a_stopped_bench_is_not_started(self, monkeypatch):
        calls = _wire(monkeypatch, uptimes=[])
        outcome = supervision.resync_if_code_landed(
            object(), BENCH, sites=["new.localhost"], landed_paths=["x"]
        )
        assert outcome == QUIET and calls == []

    def test_an_unreadable_process_list_reloads(self, monkeypatch):
        calls = _wire(monkeypatch, uptimes=[], raises=True)
        supervision.resync_if_code_landed(
            object(), BENCH, sites=["new.localhost"], landed_paths=["x"]
        )
        assert len(calls) == 1


class TestReloadForInstalledApps:
    """The report-shaping step ``new_site`` and ``init`` share."""

    def test_no_installed_app_touches_nothing(self, monkeypatch):
        monkeypatch.setattr(
            supervision,
            "resync_if_code_landed",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")),
        )
        warnings: list = []
        assert core_init.reload_for_installed_apps(
            object(), BENCH, site="s", apps=[], warnings=warnings, emit=lambda _e: None
        ) == ([], True)
        assert warnings == []

    def test_it_checks_the_registry_and_each_installed_app(self, monkeypatch):
        seen: dict = {}

        def fake(_c, _p, **kwargs):
            seen.update(kwargs)
            kwargs["on_restart"]("web")
            return supervision.ResyncOutcome(
                attempted=True, restarted=["web"], unserved_sites=[], error=None
            )

        monkeypatch.setattr(supervision, "resync_if_code_landed", fake)
        events: list = []
        restarted, ok = core_init.reload_for_installed_apps(
            object(), BENCH, site="s", apps=["srms"], warnings=[], emit=events.append
        )
        assert (restarted, ok) == (["web"], True)
        assert seen["sites"] == ["s"]
        assert seen["landed_paths"] == [f"{BENCH}/sites/apps.txt", f"{BENCH}/apps/srms"]
        assert events[0].phase == "restart_processes" and events[0].item == "web"

    def test_a_failed_reload_is_a_warning_and_not_ok(self, monkeypatch):
        monkeypatch.setattr(
            supervision,
            "resync_if_code_landed",
            lambda *a, **k: supervision.ResyncOutcome(
                attempted=True, restarted=[], unserved_sites=["s"], error="s answered 500"
            ),
        )
        warnings: list = []
        events: list = []
        restarted, ok = core_init.reload_for_installed_apps(
            object(), BENCH, site="s", apps=["srms"], warnings=warnings, emit=events.append
        )
        assert ok is False
        assert [w.code for w in warnings] == ["site.reload_failed"]
        assert "s answered 500" in warnings[0].text
        assert events[-1].code == "site.reload_failed"
