"""Apps installed on a site of a serving bench must be loaded by its running processes.

The defect: an app reached a RUNNING bench and was installed on a site, and the site
answered HTTP 500 until ``cwcli restart``, because the web and worker processes booted
before the app existed and a Python process computes its import path once, at boot.
cwcli cannot tell when an app already in ``apps/`` arrived (``cwcli run <p> get-app``,
an older cwcli), so the rule pinned here, with no Docker (every container read is
faked at the supervisord and HTTP boundary), is:

1. an operation that installed an app on a site reloads the bench ONCE, at its end,
   whether or not this call fetched the app; a fetch alone reloads nothing;
2. the reload is the shared ``resync_after_code_change`` step, proven by ONLY the
   sites whose code changed answering HTTP 200: the new site, and any existing site a
   new-site checkout moved an app under. An unrelated site is never probed.
"""

from __future__ import annotations

import types

import pytest

from caffeinated_whale_cli import jobs
from caffeinated_whale_cli.core import app_deps, supervision
from caffeinated_whale_cli.core import apps as core_apps
from caffeinated_whale_cli.core import init as core_init
from caffeinated_whale_cli.core.envelope import Result, Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind

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
PYTHON_PROGRAMS = ["web", "schedule", "worker_default"]


def _bench(monkeypatch, *, supervised=True, manager_up=False, serving=None):
    """A bench whose supervisord reports ``LIVE`` and whose sites answer ``serving``
    (any other site probed is a test failure). Returns the shared event log; probes
    are logged as ``probe <site>``."""
    log: list[str] = []
    answers = {"a.localhost": "200", "shop.localhost": "200", **(serving or {})}
    clock = [0.0]
    monkeypatch.setattr(
        supervision,
        "time",
        types.SimpleNamespace(
            monotonic=lambda: clock[0],
            sleep=lambda s: clock.__setitem__(0, clock[0] + s),
        ),
    )
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
        return dict(LIVE)

    def restart(_c, _p, program):
        log.append(f"restart {program}")
        return 0, ""

    def probe(_c, *, port, site, **_k):
        assert site in answers, f"an unrelated site was probed: {site}"
        log.append(f"probe {site}")
        return answers[site]

    monkeypatch.setattr(supervision, "supervisorctl_states", states)
    monkeypatch.setattr(supervision, "restart_program", restart)
    monkeypatch.setattr(supervision, "web_http_code", probe)
    monkeypatch.setattr(
        supervision.resolvers, "resolve_web_ports", lambda _c, paths: dict.fromkeys(paths, 8000)
    )
    return log


def _restarts(log):
    return [e.split(" ", 1)[1] for e in log if e.startswith("restart ")]


def _probes(log):
    return sorted({e.split(" ", 1)[1] for e in log if e.startswith("probe ")})


def _resync(**kwargs):
    return supervision.resync_after_code_change(object(), BENCH, **kwargs)


class TestTheSharedResync:
    """The one step every install path reloads through."""

    def test_no_changed_site_restarts_nothing(self, monkeypatch):
        log = _bench(monkeypatch)
        outcome = _resync(sites=[])
        assert outcome.ok and outcome.restarted == [] and log == []

    def test_restarts_the_live_python_programs_and_probes_only_the_changed_site(self, monkeypatch):
        log = _bench(monkeypatch, serving={"broken.localhost": "500"})
        outcome = _resync(sites=["a.localhost"])
        # socketio and redis import no app; a stopped worker stays stopped; the bench's
        # other site (answering 500) is never asked.
        assert _restarts(log) == PYTHON_PROGRAMS
        assert _probes(log) == ["a.localhost"]
        assert outcome.ok and outcome.restarted == PYTHON_PROGRAMS

    def test_a_changed_site_that_does_not_serve_fails_the_reload(self, monkeypatch):
        _bench(monkeypatch, serving={"a.localhost": "500"})
        outcome = _resync(sites=["a.localhost"], timeout=5)
        assert not outcome.ok and outcome.unserved_sites == ["a.localhost"]

    def test_an_unreadable_supervisord_state_fails_honestly(self, monkeypatch):
        log = _bench(monkeypatch)
        monkeypatch.setattr(supervision, "supervisorctl_states", lambda *_a, **_k: {})
        outcome = _resync(sites=["a.localhost"])
        assert not outcome.ok and "no supervised web process" in outcome.error
        assert log == []

    def test_an_unsupervised_bench_is_probed_without_a_restart(self, monkeypatch):
        log = _bench(monkeypatch, supervised=False, manager_up=True)
        outcome = _resync(sites=["a.localhost"])
        assert outcome.ok and _restarts(log) == [] and _probes(log) == ["a.localhost"]

    def test_a_stopped_bench_stays_stopped(self, monkeypatch):
        log = _bench(monkeypatch, supervised=False, manager_up=False)
        assert _resync(sites=["a.localhost"]).ok and log == []


class TestReloadForInstalledApps:
    """The report-shaping step ``new_site`` and ``init`` share."""

    def test_a_failed_reload_is_a_warning_and_not_ok(self, monkeypatch):
        _bench(monkeypatch, serving={"a.localhost": "500"})
        warnings: list = []
        events: list = []
        outcome = core_init.reload_for_installed_apps(
            object(), BENCH, sites=["a.localhost"], warnings=warnings, emit=events.append
        )
        assert not outcome.ok
        assert [w.code for w in warnings] == ["site.reload_failed"]
        assert events[-1].code == "site.reload_failed"

    def test_restarts_are_announced(self, monkeypatch):
        _bench(monkeypatch)
        events: list = []
        outcome = core_init.reload_for_installed_apps(
            object(), BENCH, sites=["a.localhost"], warnings=[], emit=events.append
        )
        assert outcome.ok and outcome.restarted == PYTHON_PROGRAMS
        assert [e.item for e in events if e.phase == "restart_processes"] == PYTHON_PROGRAMS


def _cached(monkeypatch):
    monkeypatch.setattr(core_apps.resolvers, "cached_benches", lambda _p: [{"path": BENCH}])


class TestAppsInstall:
    def test_an_app_already_on_the_bench_installed_on_a_site_reloads(
        self, monkeypatch, container  # noqa: F811
    ):
        """The staging16 sequence: the app reached apps/ outside this call."""
        container.available = ["frappe", "payments"]
        log = _bench(monkeypatch)
        _cached(monkeypatch)

        result = core_apps.install_apps("proj", ["payments"], sites=["a.localhost"])

        assert _restarts(log) == PYTHON_PROGRAMS and _probes(log) == ["a.localhost"]
        assert [(r.action, r.ok) for r in result.data.results][-1] == ("restart-processes", True)
        assert result.data.ok

    def test_fetch_only_reloads_nothing(self, monkeypatch, container):  # noqa: F811
        log = _bench(monkeypatch)
        _cached(monkeypatch)

        result = core_apps.install_apps("proj", ["payments"], fetch_only=True)

        assert log == [] and [r.action for r in result.data.results] == ["get-app"]

    def test_a_failed_install_is_an_install_failure_not_a_restart_failure(
        self, monkeypatch, container  # noqa: F811
    ):
        container.fail_on = ["install-app"]
        log = _bench(monkeypatch)
        _cached(monkeypatch)

        result = core_apps.install_apps("proj", ["payments"], sites=["a.localhost"])

        rows = [(r.action, r.ok) for r in result.data.results]
        assert ("install-app", False) in rows
        assert not [r for r in rows if r[0] == "restart-processes"]
        assert log == []


def _run_new_site(monkeypatch, container, new_site, log, fail_phase=None, **kwargs):  # noqa: F811
    from caffeinated_whale_cli.core import db_access

    monkeypatch.setattr(db_access, "verify_root_password", lambda *_a, **_k: None)
    monkeypatch.setattr(db_access, "widen_after_create", lambda *_a, **_k: (False, None))
    monkeypatch.setattr(new_site.db_password, "resolve", lambda *_a, **_k: "root")
    monkeypatch.setattr(new_site, "frappe_major", lambda *_a: "15")

    def run(_c, cmd, *, phase, **_k):
        log.append(f"{phase} {cmd}")
        if phase == fail_phase:
            raise CwcliError(ErrorKind.DOCKER, "init.command_failed", f"{phase} failed")

    monkeypatch.setattr(new_site, "_run_exec", run)
    return new_site.new_site("proj", site="shop.localhost", admin_password="a", **kwargs)


def _fetching_new_site(monkeypatch, world, container):  # noqa: F811
    new_site = _wire_new_site(monkeypatch, container)
    monkeypatch.setattr(
        app_deps, "find_sources", lambda module, pin=None: world.sources(module, pin)
    )
    monkeypatch.setattr(
        new_site,
        "_directory_exists",
        lambda _c, path: "/apps/" in path and bool(_gets(container)),
    )
    return new_site


class TestNewSite:
    def test_apps_already_on_the_bench_reload_once_after_the_installs(
        self, world, container, monkeypatch  # noqa: F811
    ):
        new_site = _wire_new_site(monkeypatch, container)
        container.available.extend(["toolkit", "booking"])
        monkeypatch.setattr(new_site, "_directory_exists", lambda _c, path: "/apps/" in path)
        log = _bench(monkeypatch)

        result = _run_new_site(monkeypatch, container, new_site, log, apps=["toolkit"])

        assert _gets(container) == []
        assert _restarts(log) == PYTHON_PROGRAMS and _probes(log) == ["shop.localhost"]
        last_install = max(i for i, e in enumerate(log) if e.startswith("install_app "))
        assert min(i for i, e in enumerate(log) if e.startswith("restart ")) > last_install
        assert result.data.ok and result.data.processes_restarted == PYTHON_PROGRAMS

    def test_two_fetched_repositories_reload_exactly_once_at_the_end(
        self, world, container, monkeypatch  # noqa: F811
    ):
        new_site = _fetching_new_site(monkeypatch, world, container)
        log = _bench(monkeypatch)

        result = _run_new_site(
            monkeypatch, container, new_site, log, apps=["booking"], fetch=[BOOKING], with_deps=True
        )

        assert len(_gets(container)) == 2
        assert _restarts(log) == PYTHON_PROGRAMS
        last_install = max(i for i, e in enumerate(log) if e.startswith("install_app "))
        assert min(i for i, e in enumerate(log) if e.startswith("restart ")) > last_install
        assert result.data.ok

    def test_a_site_with_no_app_restarts_nothing(self, world, container, monkeypatch):  # noqa: F811
        new_site = _wire_new_site(monkeypatch, container)
        log = _bench(monkeypatch)

        result = _run_new_site(monkeypatch, container, new_site, log)

        assert _restarts(log) == [] and result.data.processes_restarted == []

    def test_a_fetch_alone_reloads_nothing_when_creating_the_site_fails(
        self, world, container, monkeypatch  # noqa: F811
    ):
        new_site = _fetching_new_site(monkeypatch, world, container)
        log = _bench(monkeypatch)

        with pytest.raises(CwcliError) as raised:
            _run_new_site(
                monkeypatch,
                container,
                new_site,
                log,
                fail_phase="new_site",
                apps=["booking"],
                fetch=[BOOKING],
                with_deps=True,
            )

        assert raised.value.code == "init.command_failed"
        assert len(_gets(container)) == 2 and _restarts(log) == []

    @pytest.mark.parametrize("serves", [True, False])
    def test_installed_apps_are_reloaded_when_a_later_step_fails(
        self, world, container, monkeypatch, serves  # noqa: F811
    ):
        new_site = _wire_new_site(monkeypatch, container)
        container.available.extend(["toolkit", "booking"])
        monkeypatch.setattr(new_site, "_directory_exists", lambda _c, path: "/apps/" in path)
        log = _bench(monkeypatch, serving={"shop.localhost": "200" if serves else "500"})

        with pytest.raises(CwcliError) as raised:
            _run_new_site(
                monkeypatch,
                container,
                new_site,
                log,
                fail_phase="use",
                apps=["toolkit"],
                default=True,
            )

        assert _restarts(log) == PYTHON_PROGRAMS and _probes(log) == ["shop.localhost"]
        assert raised.value.message.startswith("use failed")
        # A reload that did not serve rides the error every surface shows.
        assert ("shop.localhost=500" in raised.value.message) is not serves


class TestInitErpnextOnAReusedBench:
    def test_erpnext_already_on_the_bench_still_reloads(self, monkeypatch, patched):  # noqa: F811
        from .test_core_init import (
            BENCH_PATH,
            PROJECT,
            SITE,
            FakeContainer,
            bench_kwargs,
            use_container,
        )

        log = _bench(monkeypatch, serving={SITE: "200"})
        use_container(
            monkeypatch, FakeContainer(dirs_exist={BENCH_PATH, f"{BENCH_PATH}/apps/erpnext"})
        )

        report = core_init.init_bench(
            PROJECT, **bench_kwargs(reuse_bench=True, install_erpnext=True)
        ).data

        assert report.erpnext_installed and report.ok
        assert _restarts(log) == PYTHON_PROGRAMS and _probes(log) == [SITE]
        assert report.processes_restarted == PYTHON_PROGRAMS


def test_the_web_ui_init_job_fails_when_the_reload_failed(monkeypatch):
    report = core_init.InitReport(
        project="p",
        bench_name="frappe-bench",
        bench_path=BENCH,
        site_name="p.localhost",
        bench_created=False,
        site_created=True,
        erpnext_installed=True,
        ok=False,
    )
    monkeypatch.setattr(jobs.core_init, "init_instance", lambda *_a, **_k: Result(status=Status.OK))
    monkeypatch.setattr(
        jobs.core_init, "init_bench", lambda *_a, **_k: Result(status=Status.WARNING, data=report)
    )
    monkeypatch.setattr(jobs.cache, "recache_project", lambda *_a, **_k: True)

    result = jobs._run_init(
        {"project": "p"}, {"start": False}, {"admin_password": "x"}, lambda _e: None
    )

    assert jobs._result_ok(result) is False


def _checkout_returning(monkeypatch, new_site, rows, *, ok):
    results = [core_apps.AppResult(app="toolkit", site=None, action=a, ok=r) for a, r in rows]
    monkeypatch.setattr(
        new_site.core_apps,
        "checkout_app",
        lambda project, *_a, **_k: Result(
            status=Status.OK if ok else Status.WARNING,
            data=core_apps.AppsReport(project=project, bench_path=BENCH, results=results, ok=ok),
        ),
    )


def _toolkit_on_a_not_b(monkeypatch, container, world):  # noqa: F811
    """a.localhost runs toolkit, b.localhost does not; the bench holds toolkit."""
    from .test_app_deps_flows import _bench_holding_toolkit

    new_site = _wire_new_site(monkeypatch, container)
    container.available.append("toolkit")
    container.installed = {
        "a.localhost": ["frappe 15.0.0 version-15", "toolkit 0.23.3 master"],
        "b.localhost": ["frappe 15.0.0 version-15"],
    }
    monkeypatch.setattr(
        core_apps.bench_sites, "list_sites", lambda *a, **k: ["a.localhost", "b.localhost"]
    )
    monkeypatch.setattr(new_site, "_directory_exists", lambda _c, path: "/apps/" in path)
    _bench_holding_toolkit(monkeypatch)
    return new_site


class TestComposedCheckout:
    """``checkout_app(reload=False)`` skips only the reload; ``new_site`` reloads on
    failure only for a checkout that actually moved the tree."""

    def test_reload_false_still_fails_a_checkout_the_bench_does_not_import(
        self, monkeypatch, container  # noqa: F811
    ):
        from .test_core_apps import _bridge_spy, _patch_app_import

        _cached(monkeypatch)
        _bridge_spy(monkeypatch)
        _patch_app_import(monkeypatch, diverged=True, imported="/workspace/.hdsrc/payments")
        log = _bench(monkeypatch)

        result = core_apps.checkout_app("proj", "payments", "feature/x", reload=False)

        assert ("verify-import", False) in [(r.action, r.ok) for r in result.data.results]
        assert result.data.ok is False
        assert log == []

    @pytest.mark.parametrize(
        "rows, restarts",
        [
            ([("fetch", False)], []),
            ([("fetch", True), ("checkout", True), ("verify-import", False)], PYTHON_PROGRAMS),
        ],
    )
    def test_a_failed_checkout_reloads_only_when_it_moved_the_tree(
        self, world, container, monkeypatch, rows, restarts  # noqa: F811
    ):
        new_site = _toolkit_on_a_not_b(monkeypatch, container, world)
        _checkout_returning(monkeypatch, new_site, rows, ok=False)
        log = _bench(monkeypatch, serving={"a.localhost": "200"})

        with pytest.raises(CwcliError) as raised:
            _run_new_site(
                monkeypatch, container, new_site, log, apps=["toolkit"], refs={"toolkit": "develop"}
            )

        assert raised.value.code == "new_site.checkout_failed"
        assert _restarts(log) == restarts
        assert _probes(log) == (["a.localhost"] if restarts else [])

    @pytest.mark.parametrize("a_serves", [True, False])
    def test_a_checkout_proves_the_new_site_and_the_sites_it_moved_code_under(
        self, world, container, monkeypatch, a_serves  # noqa: F811
    ):
        new_site = _toolkit_on_a_not_b(monkeypatch, container, world)
        _checkout_returning(monkeypatch, new_site, [("fetch", True), ("checkout", True)], ok=True)
        log = _bench(monkeypatch, serving={"a.localhost": "200" if a_serves else "500"})

        result = _run_new_site(
            monkeypatch, container, new_site, log, apps=["toolkit"], refs={"toolkit": "develop"}
        )

        assert _restarts(log) == PYTHON_PROGRAMS
        # b.localhost does not run toolkit, so the checkout did not touch it.
        assert _probes(log) == ["a.localhost", "shop.localhost"]
        assert result.data.ok is a_serves
        broke = [w for w in result.warnings if w.code == "new_site.checkout_broke_site"]
        if a_serves:
            assert broke == []
        else:
            assert len(broke) == 1
            assert "a.localhost" in broke[0].text and "toolkit" in broke[0].text
