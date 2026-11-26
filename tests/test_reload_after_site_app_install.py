"""A change to a serving bench's app set must reload the bench's running processes.

The defect: ``new-site --app X`` (and init's ERPNext on a reused bench, and the web
UI's New site) ran ``bench install-app`` on a bench whose web and worker processes
booted BEFORE app X reached the bench. A Python process computes its import path at
boot, so the site answered HTTP 500 until someone ran ``cwcli restart`` by hand.
The fix reloads the bench whenever an app was installed (or fetched with
``--fetch-only``), through the shared ``resync_after_code_change`` step, which
restarts only running code-bearing programs. Pure: no Docker, every read is faked.
"""

from __future__ import annotations

from caffeinated_whale_cli.core import apps as core_apps
from caffeinated_whale_cli.core import init as core_init
from caffeinated_whale_cli.core import supervision

from .test_core_apps import BENCH, _cache, wire_fake_bench


def _record_resync(monkeypatch, outcome=None):
    calls: list[dict] = []

    def resync(_c, _p, **kwargs):
        calls.append(kwargs)
        if not kwargs["sites"]:
            return supervision.ResyncOutcome(
                attempted=False, restarted=[], unserved_sites=[], error=None
            )
        if kwargs.get("on_restart"):
            kwargs["on_restart"]("web")
        return outcome or supervision.ResyncOutcome(
            attempted=True, restarted=["web", "worker_default"], unserved_sites=[], error=None
        )

    monkeypatch.setattr(supervision, "resync_after_code_change", resync)
    return calls


class TestReloadForInstalledApps:
    """The step ``new_site`` and ``init`` share after installing apps on a site."""

    def test_an_installed_app_reloads_the_bench_and_verifies_the_new_site(self, monkeypatch):
        calls = _record_resync(monkeypatch)
        events: list = []
        restarted, ok = core_init.reload_for_installed_apps(
            object(), BENCH, site="ners.localhost", apps=["srms"], warnings=[], emit=events.append
        )
        assert (restarted, ok) == (["web", "worker_default"], True)
        assert [c["sites"] for c in calls] == [["ners.localhost"]]
        assert events[0].phase == "restart_processes" and events[0].item == "web"

    def test_no_installed_app_touches_nothing(self, monkeypatch):
        calls = _record_resync(monkeypatch)
        warnings: list = []
        assert core_init.reload_for_installed_apps(
            object(), BENCH, site="s", apps=[], warnings=warnings, emit=lambda _e: None
        ) == ([], True)
        assert calls == [] and warnings == []

    def test_a_failed_reload_is_a_warning_and_not_ok(self, monkeypatch):
        _record_resync(
            monkeypatch,
            supervision.ResyncOutcome(
                attempted=True, restarted=[], unserved_sites=["s"], error="s answered 500"
            ),
        )
        warnings: list = []
        events: list = []
        _restarted, ok = core_init.reload_for_installed_apps(
            object(), BENCH, site="s", apps=["srms"], warnings=warnings, emit=events.append
        )
        assert ok is False
        assert [w.code for w in warnings] == ["site.reload_failed"]
        assert "s answered 500" in warnings[0].text
        assert events[-1].code == "site.reload_failed"


class TestFetchOnlyReloadsAServingBench:
    """``apps install --fetch-only`` changes the bench's app set with no site install."""

    def test_a_fetched_app_reloads_and_verifies_every_bench_site(self, monkeypatch):
        wire_fake_bench(monkeypatch)
        monkeypatch.setattr(
            core_apps.bench_sites, "list_sites", lambda *a, **k: ["b.localhost", "a.localhost"]
        )
        _cache(monkeypatch, [{"path": BENCH}])
        calls = _record_resync(monkeypatch)

        result = core_apps.install_apps("proj", ["payments"], fetch_only=True)

        assert [c["sites"] for c in calls] == [["a.localhost", "b.localhost"]]
        assert [(r.action, r.ok) for r in result.data.results] == [
            ("get-app", True),
            ("restart-processes", True),
        ]
        assert result.data.ok is True

    def test_an_unlistable_site_set_fails_the_resync_rather_than_skipping_it(self, monkeypatch):
        wire_fake_bench(monkeypatch)
        monkeypatch.setattr(core_apps.bench_sites, "list_sites", lambda *a, **k: None)
        _cache(monkeypatch, [{"path": BENCH}])
        calls = _record_resync(monkeypatch)

        result = core_apps.install_apps("proj", ["payments"], fetch_only=True)

        assert calls == []
        assert [(r.action, r.ok) for r in result.data.results][-1] == ("restart-processes", False)
        assert "app.resync_failed" in [w.code for w in result.warnings]
        assert result.data.ok is False

    def test_an_app_already_on_the_bench_changes_nothing_and_reloads_nothing(self, monkeypatch):
        container = wire_fake_bench(monkeypatch)
        container.available = ["frappe", "payments"]
        _cache(monkeypatch, [{"path": BENCH}])
        calls = _record_resync(monkeypatch)

        result = core_apps.install_apps("proj", ["payments"], fetch_only=True)

        assert all(not c["sites"] for c in calls)
        assert [r.action for r in result.data.results] == ["get-app"]
