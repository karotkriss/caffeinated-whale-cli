"""App code added to a serving bench must be loaded by the bench's running processes.

The defect: an app reached a RUNNING bench and was installed on a site, and the site
answered HTTP 500 until ``cwcli restart``, because the web and worker processes booted
before the app existed and a Python process computes its import path once, at boot.

The rule pinned here, with no Docker (every container read is faked at the
supervisord boundary):

(a) only an operation that ADDED code (a fetch, a moved checkout) reloads; a site that
    installs apps already on the bench restarts nothing;
(b) a composed operation (``new_site``) reloads ONCE, at its end, after every install;
(c) the reload is judged by the restarted programs returning to RUNNING, and no site
    is ever probed, so an unrelated site in maintenance cannot fail the operation.
"""

from __future__ import annotations

from caffeinated_whale_cli.core import app_deps, supervision
from caffeinated_whale_cli.core import apps as core_apps
from caffeinated_whale_cli.core import init as core_init

from .test_app_deps_flows import _gets, _wire_new_site, world  # noqa: F401 - the fixture
from .test_core_app_deps import BOOKING
from .test_core_apps import BENCH, container  # noqa: F401 - the fixture
from .test_core_init import patched  # noqa: F401 - the fixture

LIVE = {
    "web": ("RUNNING", 1),
    "socketio": ("RUNNING", 2),
    "schedule": ("RUNNING", 3),
    "worker_default": ("RUNNING", 4),
    "worker_long": ("STOPPED", None),
    "redis_cache": ("RUNNING", 5),
}


def _supervised_bench(monkeypatch, *, after=None, supervised=True, manager_up=False):
    """A bench whose supervisord reports ``LIVE``; ``after`` is what each status read
    returns once a restart happened. Returns the shared event log."""
    log: list[str] = []
    reads = iter(after or [])
    monkeypatch.setattr(
        supervision,
        "discover_stack",
        lambda *a, **k: supervision.StackSnapshot(
            supervisor_up=supervised, supervisor_pid=100 if supervised else None, processes=[]
        ),
    )
    monkeypatch.setattr(
        supervision,
        "discover_unsupervised_stack",
        lambda *a, **k: supervision.UnsupervisedStack(manager_up=manager_up, processes=[]),
    )

    def states(*_a, **_k):
        if not any(e.startswith("restart ") for e in log):
            return dict(LIVE)
        return next(reads, dict(LIVE))

    def restart(_c, _p, program):
        log.append(f"restart {program}")
        return 0, ""

    def probe(*_a, **_k):
        raise AssertionError("a site was probed")

    monkeypatch.setattr(supervision, "supervisorctl_states", states)
    monkeypatch.setattr(supervision, "restart_program", restart)
    monkeypatch.setattr(supervision, "web_http_code", probe)
    monkeypatch.setattr(supervision.time, "sleep", lambda _s: None)
    return log


def _restarts(log):
    return [e.split(" ", 1)[1] for e in log if e.startswith("restart ")]


class TestReloadAfterFetch:
    """(a) and (c) at the shared step."""

    def test_no_added_code_restarts_nothing(self, monkeypatch):
        log = _supervised_bench(monkeypatch)
        outcome = supervision.reload_after_fetch(object(), BENCH, code_added=False, project="proj")
        assert outcome.ok and outcome.restarted == [] and log == []

    def test_added_code_restarts_only_the_live_python_programs(self, monkeypatch):
        log = _supervised_bench(monkeypatch)
        outcome = supervision.reload_after_fetch(object(), BENCH, code_added=True, project="proj")
        # socketio and redis import no app; a stopped worker stays stopped.
        assert _restarts(log) == ["web", "schedule", "worker_default"]
        assert outcome.ok and outcome.restarted == ["web", "schedule", "worker_default"]

    def test_a_program_still_starting_is_waited_for(self, monkeypatch):
        starting = dict(LIVE, web=("STARTING", 9))
        _supervised_bench(monkeypatch, after=[starting, dict(LIVE)])
        outcome = supervision.reload_after_fetch(object(), BENCH, code_added=True, project="proj")
        assert outcome.ok

    def test_a_program_that_does_not_return_to_running_fails_the_reload(self, monkeypatch):
        fatal = dict(LIVE, worker_default=("FATAL", None))
        _supervised_bench(monkeypatch, after=[fatal] * 5)
        outcome = supervision.reload_after_fetch(
            object(), BENCH, code_added=True, project="proj", timeout=0
        )
        assert not outcome.ok
        assert "worker_default" in outcome.error and "cwcli restart proj" in outcome.error

    def test_an_unsupervised_running_bench_is_reported_with_the_restart_remedy(self, monkeypatch):
        log = _supervised_bench(monkeypatch, supervised=False, manager_up=True)
        outcome = supervision.reload_after_fetch(object(), BENCH, code_added=True, project="proj")
        assert not outcome.ok and "cwcli restart proj" in outcome.error
        assert log == []

    def test_a_stopped_bench_stays_stopped(self, monkeypatch):
        log = _supervised_bench(monkeypatch, supervised=False, manager_up=False)
        outcome = supervision.reload_after_fetch(object(), BENCH, code_added=True, project="proj")
        assert outcome.ok and log == []


class TestReloadForAddedCode:
    """The report-shaping step ``new_site`` and ``init`` share."""

    def test_a_failed_reload_is_a_warning_and_not_ok(self, monkeypatch):
        _supervised_bench(monkeypatch, supervised=False, manager_up=True)
        warnings: list = []
        events: list = []
        _restarted, ok = core_init.reload_for_added_code(
            object(), BENCH, project="proj", code_added=True, warnings=warnings, emit=events.append
        )
        assert ok is False
        assert [w.code for w in warnings] == ["site.reload_failed"]
        assert events[-1].code == "site.reload_failed"

    def test_restarts_are_announced(self, monkeypatch):
        _supervised_bench(monkeypatch)
        events: list = []
        restarted, ok = core_init.reload_for_added_code(
            object(), BENCH, project="proj", code_added=True, warnings=[], emit=events.append
        )
        assert ok and restarted == ["web", "schedule", "worker_default"]
        assert [e.item for e in events if e.phase == "restart_processes"] == restarted


class TestAppsInstall:
    def test_fetch_only_reloads_once_without_probing_a_site(
        self, monkeypatch, container  # noqa: F811
    ):
        log = _supervised_bench(monkeypatch)
        monkeypatch.setattr(core_apps.resolvers, "cached_benches", lambda _p: [{"path": BENCH}])

        result = core_apps.install_apps("proj", ["payments"], fetch_only=True)

        assert _restarts(log) == ["web", "schedule", "worker_default"]
        assert [(r.action, r.ok) for r in result.data.results] == [
            ("get-app", True),
            ("restart-processes", True),
        ]
        assert result.data.ok

    def test_an_app_already_on_the_bench_is_installed_without_a_reload(
        self, monkeypatch, container  # noqa: F811
    ):
        container.available = ["frappe", "payments"]
        log = _supervised_bench(monkeypatch)
        monkeypatch.setattr(core_apps.resolvers, "cached_benches", lambda _p: [{"path": BENCH}])

        result = core_apps.install_apps("proj", ["payments"], sites=["a.localhost"])

        assert log == []
        assert [r.action for r in result.data.results] == ["get-app", "install-app"]
        assert result.data.ok

    def test_a_failed_install_is_an_install_failure_not_a_restart_failure(
        self, monkeypatch, container  # noqa: F811
    ):
        container.fail_on = ["install-app"]
        log = _supervised_bench(monkeypatch)
        monkeypatch.setattr(core_apps.resolvers, "cached_benches", lambda _p: [{"path": BENCH}])

        result = core_apps.install_apps("proj", ["payments"], sites=["a.localhost"])

        rows = [(r.action, r.ok) for r in result.data.results]
        assert ("install-app", False) in rows
        assert ("restart-processes", True) in rows and ("restart-processes", False) not in rows
        assert _restarts(log) == ["web", "schedule", "worker_default"]

    def test_reload_false_leaves_the_reload_to_the_caller(
        self, monkeypatch, container  # noqa: F811
    ):
        log = _supervised_bench(monkeypatch)
        monkeypatch.setattr(core_apps.resolvers, "cached_benches", lambda _p: [{"path": BENCH}])

        result = core_apps.install_apps("proj", ["payments"], fetch_only=True, reload=False)

        assert log == [] and [r.action for r in result.data.results] == ["get-app"]


def _run_new_site(monkeypatch, container, new_site, log, **kwargs):  # noqa: F811
    from caffeinated_whale_cli.core import db_access

    monkeypatch.setattr(db_access, "verify_root_password", lambda *_a, **_k: None)
    monkeypatch.setattr(db_access, "widen_after_create", lambda *_a, **_k: (False, None))
    monkeypatch.setattr(new_site.db_password, "resolve", lambda *_a, **_k: "root")
    monkeypatch.setattr(new_site, "frappe_major", lambda *_a: "15")

    def run(_c, cmd, *, phase, **_k):
        log.append(f"{phase} {cmd}")

    monkeypatch.setattr(new_site, "_run_exec", run)
    return new_site.new_site("proj", site="shop.localhost", admin_password="a", **kwargs)


class TestNewSite:
    def test_two_fetched_repositories_reload_once_after_the_installs(
        self, world, container, monkeypatch  # noqa: F811
    ):
        new_site = _wire_new_site(monkeypatch, container)
        monkeypatch.setattr(
            app_deps, "find_sources", lambda module, pin=None: world.sources(module, pin)
        )
        monkeypatch.setattr(
            new_site,
            "_directory_exists",
            lambda _c, path: "/apps/" in path and bool(_gets(container)),
        )
        log = _supervised_bench(monkeypatch)

        result = _run_new_site(
            monkeypatch,
            container,
            new_site,
            log,
            apps=["booking"],
            fetch=[BOOKING],
            with_deps=True,
        )

        assert len(_gets(container)) == 2
        assert _restarts(log) == ["web", "schedule", "worker_default"]
        last_install = max(i for i, e in enumerate(log) if e.startswith("install_app "))
        first_restart = min(i for i, e in enumerate(log) if e.startswith("restart "))
        assert first_restart > last_install
        assert result.data.ok and result.data.processes_restarted == _restarts(log)

    def test_a_site_with_only_bench_apps_restarts_nothing(
        self, world, container, monkeypatch  # noqa: F811
    ):
        new_site = _wire_new_site(monkeypatch, container)
        container.available.extend(["toolkit", "booking"])
        monkeypatch.setattr(new_site, "_directory_exists", lambda _c, path: "/apps/" in path)
        log = _supervised_bench(monkeypatch)

        result = _run_new_site(monkeypatch, container, new_site, log, apps=["toolkit"])

        assert _gets(container) == []
        assert any(e.startswith("install_app ") for e in log)
        assert _restarts(log) == []
        assert result.data.ok and result.data.processes_restarted == []


class TestInitErpnextOnAReusedBench:
    """Init's ERPNext reloads a reused bench only when apps/erpnext was not there."""

    def _init(self, monkeypatch, dirs):
        from .test_core_init import (
            BENCH_PATH,
            PROJECT,
            FakeContainer,
            bench_kwargs,
            use_container,
        )

        log = _supervised_bench(monkeypatch)
        use_container(monkeypatch, FakeContainer(dirs_exist={BENCH_PATH, *dirs}))
        result = core_init.init_bench(
            PROJECT, **bench_kwargs(reuse_bench=True, install_erpnext=True)
        )
        return result.data, _restarts(log)

    def test_a_newly_fetched_erpnext_reloads_the_bench(self, monkeypatch, patched):  # noqa: F811
        report, restarts = self._init(monkeypatch, dirs=())
        assert report.erpnext_installed and report.ok
        assert restarts == ["web", "schedule", "worker_default"]
        assert report.processes_restarted == restarts

    def test_an_erpnext_already_on_the_bench_restarts_nothing(
        self, monkeypatch, patched  # noqa: F811
    ):
        report, restarts = self._init(monkeypatch, dirs=(f"{BENCH}/apps/erpnext",))
        assert report.erpnext_installed and report.ok
        assert restarts == [] and report.processes_restarted == []
