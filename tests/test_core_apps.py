"""``core.apps`` - the branches the CLI frontend cannot reach.

`tests/test_apps.py` + `tests/test_apps_characterization.py` drive the three verbs
through `commands/apps.py`, which PRE-RESOLVES the bench and starts the container
itself (the `backup`/`update` frontend pattern). That is the right shape for the
CLI, but it means the core's own forks - a stopped container, an ambiguous
multi-bench project, the no-cache default fallback, and the destructive-consent
gate - are never exercised from there. They are what `axi` and a future GUI hit,
so they are pinned here.

Also pinned: the purity properties the whole rework rests on - the core prints
NOTHING, returns no live Docker object, and `dataclasses.asdict` on what it
returns is plain serializable data.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from caffeinated_whale_cli.core import apps as core_apps
from caffeinated_whale_cli.core import docker as core_docker
from caffeinated_whale_cli.core import resolvers
from caffeinated_whale_cli.core.envelope import Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind

BENCH = "/workspace/frappe-bench"


class FakeAPI:
    """Streaming exec surface (client.api), mirroring docker-py's shape."""

    def __init__(self, container):
        self.container = container
        self._pending = None

    def exec_create(self, cid, cmd, workdir=None, tty=False, environment=None):
        self._pending = (cmd, workdir)
        return {"Id": "exec-1"}

    def exec_start(self, exec_id, stream=True, demux=False):
        cmd, _workdir = self._pending
        code, out = self.container._run(cmd)
        self.container._last_code = code
        raw = out.encode() if isinstance(out, str) else out
        yield (raw, None) if demux else raw

    def exec_inspect(self, exec_id):
        return {"ExitCode": self.container._last_code}


class FakeContainer:
    labels = {"com.docker.compose.service": "frappe"}
    name = "proj-frappe-1"
    id = "cid"

    def __init__(self, *, status="running", available=("frappe",), installed=None, fail_on=()):
        self.status = status
        self.available = list(available)
        self.installed = installed or {}
        self.fail_on = list(fail_on)
        self.calls: list[str] = []
        self._last_code = 0
        import types

        self.client = types.SimpleNamespace(api=FakeAPI(self))

    def reload(self):
        pass

    def _run(self, cmd):
        cmd_str = cmd if isinstance(cmd, str) else " ".join(cmd)
        self.calls.append(cmd_str)
        for sub in self.fail_on:
            if sub in cmd_str:
                return 1, f"boom: {cmd_str}"
        if cmd_str.startswith("git status"):
            return 0, getattr(self, "dirty", "")
        if cmd_str.strip() == "git remote":
            return 0, "\n".join(getattr(self, "remotes", ["upstream"])) + "\n"
        if cmd_str.startswith("ls -1") and cmd_str.rstrip().endswith("apps"):
            return 0, "\n".join(self.available) + "\n"
        if "execute frappe.get_installed_apps" in cmd_str:
            import shlex

            parts = shlex.split(cmd_str)
            site = parts[parts.index("--site") + 1] if "--site" in parts else ""
            apps = [line.split()[0] for line in self.installed.get(site, [])]
            return 0, json.dumps(apps) + "\n"
        return 0, ""

    def exec_run(self, cmd, workdir=None, **kwargs):
        code, out = self._run(cmd)
        return code, out.encode() if isinstance(out, str) else out


@pytest.fixture()
def container(monkeypatch):
    c = FakeContainer(installed={"a.localhost": ["frappe 15.0.0 version-15"]})
    monkeypatch.setattr(core_docker, "get_frappe_container", lambda _p: c)
    monkeypatch.setattr(core_apps.bench_sites, "list_sites", lambda *a, **k: ["a.localhost"])
    monkeypatch.setattr(
        core_apps.supervision,
        "discover_stack",
        lambda *a, **k: core_apps.supervision.StackSnapshot(
            supervisor_up=False, supervisor_pid=None, processes=[]
        ),
    )
    monkeypatch.setattr(
        core_apps.supervision,
        "discover_unsupervised_stack",
        lambda *a, **k: core_apps.supervision.UnsupervisedStack(manager_up=False, processes=[]),
    )
    return c


def _cache(monkeypatch, benches):
    monkeypatch.setattr(resolvers, "cached_benches", lambda _p: benches)


# ------------------------------------------------------------------ the shared forks


def test_a_stopped_container_is_a_returned_choice_never_a_start(monkeypatch, container):
    """The core REPORTS the fork; performing the start is the frontend's job."""
    container.status = "exited"
    _cache(monkeypatch, [{"path": BENCH}])

    result = core_apps.list_apps("proj")

    assert result.status is Status.NEEDS_CHOICE
    assert result.choice.kind == "confirm_start"
    assert container.calls == []  # nothing ran against a stopped container


def test_multibench_with_no_selector_is_a_returned_choice(monkeypatch, container):
    _cache(monkeypatch, [{"path": BENCH}, {"path": "/workspace/other-bench"}])

    result = core_apps.list_apps("proj")

    assert result.status is Status.NEEDS_CHOICE
    assert result.choice.kind == "select_bench"
    assert result.choice.param == "bench"
    assert len(result.choice.options) == 2


def test_no_cached_bench_falls_back_to_the_historical_default(monkeypatch, container):
    """Matches `run` and the pre-migration `_resolve_bench`'s `or _DEFAULT_BENCH`."""
    _cache(monkeypatch, [])

    result = core_apps.list_apps("proj")

    assert result.data.bench_path == resolvers.DEFAULT_BENCH_PATH
    assert any(w.code == "bench.default_used" for w in result.warnings)


def test_a_missing_project_raises_rather_than_returning(monkeypatch):
    def _boom(_p):
        raise CwcliError(ErrorKind.NOT_FOUND, "project.not_found", "Project 'nope' not found.")

    monkeypatch.setattr(core_docker, "get_frappe_container", _boom)

    with pytest.raises(CwcliError) as exc:
        core_apps.list_apps("nope")
    assert exc.value.kind is ErrorKind.NOT_FOUND


# ------------------------------------------------------------------------ list_apps


def test_list_reads_the_authoritative_installed_app_names(monkeypatch, container):
    _cache(monkeypatch, [{"path": BENCH}])

    result = core_apps.list_apps("proj", installed=True)

    assert result.data.installed == {"a.localhost": ["frappe"]}
    assert any("execute frappe.get_installed_apps" in call for call in container.calls)
    assert result.status is Status.OK
    assert result.data.ok is True


def test_list_reports_a_failed_site_read_as_none_not_empty(monkeypatch, container):
    """ "no apps" and "could not tell" are different facts, and only one exits 1."""
    container.fail_on = ["execute frappe.get_installed_apps"]
    _cache(monkeypatch, [{"path": BENCH}])

    result = core_apps.list_apps("proj", installed=True)

    assert result.data.installed == {"a.localhost": None}
    assert result.data.ok is False
    assert result.status is Status.WARNING


def test_list_does_not_read_sites_unless_asked(monkeypatch, container):
    _cache(monkeypatch, [{"path": BENCH}])

    result = core_apps.list_apps("proj")

    assert result.data.installed == {}
    assert not any("frappe.get_installed_apps" in c for c in container.calls)


def test_installed_apps_rejects_the_stale_v14_list_surface(monkeypatch, container):
    """The safety read uses the global that install updates, not v14's stale singleton."""
    container.installed = {"a.localhost": ["frappe 14.0.0 version-14", "payments 1.0.0 version-14"]}
    _cache(monkeypatch, [{"path": BENCH}])

    with pytest.raises(CwcliError) as exc:
        core_apps.install_apps(
            "proj",
            ["payments"],
            sites=["a.localhost"],
            require_absent=True,
        )

    assert exc.value.code == "app.already_installed"
    assert any("execute frappe.get_installed_apps" in call for call in container.calls)
    assert not any(call.startswith("bench get-app") for call in container.calls)


# --------------------------------------------------------------------- install_apps


def test_install_fans_out_over_every_site_and_reports_each(monkeypatch, container):
    monkeypatch.setattr(
        core_apps.bench_sites, "list_sites", lambda *a, **k: ["b.localhost", "a.localhost"]
    )
    _cache(monkeypatch, [{"path": BENCH}])

    result = core_apps.install_apps("proj", ["payments"])

    assert result.data.ok is True
    # Sorted, so the fan-out order is deterministic rather than filesystem order.
    assert [(r.action, r.site) for r in result.data.results] == [
        ("get-app", None),
        ("install-app", "a.localhost"),
        ("install-app", "b.localhost"),
    ]


def test_install_skips_the_fetch_when_the_app_is_already_on_the_bench(monkeypatch, container):
    """A pre-warmed bench already carries the app: skip get-app (it would fail on the
    existing apps/ dir) and install it on the site anyway. Bench-presence, not
    site-install: the app is present under apps/ but not installed on a.localhost."""
    container.available = ["frappe", "payments"]
    container.fail_on = ["get-app"]  # prove get-app is never run: it would fail here.
    _cache(monkeypatch, [{"path": BENCH}])

    result = core_apps.install_apps("proj", ["payments"], sites=["a.localhost"])

    assert result.data.ok is True
    assert not any(c.startswith("bench get-app") for c in container.calls)
    assert any("install-app payments" in c for c in container.calls)
    assert [
        (r.action, r.site, r.ok) for r in result.data.results if r.action != "restart-processes"
    ] == [
        ("get-app", None, True),
        ("install-app", "a.localhost", True),
    ]


def test_install_still_fetches_when_the_app_is_not_on_the_bench(monkeypatch, container):
    """The unchanged path: an app absent from apps/ is fetched, then installed."""
    _cache(monkeypatch, [{"path": BENCH}])

    result = core_apps.install_apps("proj", ["payments"], sites=["a.localhost"])

    assert result.data.ok is True
    assert any(c.startswith("bench get-app") and "payments" in c for c in container.calls)


def test_install_partial_failure_is_a_warning_envelope_carrying_ok_false(monkeypatch, container):
    """The trap: WARNING maps to exit 0 everywhere else, so the frontend reads .ok."""
    container.fail_on = ["--site b.localhost install-app"]
    monkeypatch.setattr(
        core_apps.bench_sites, "list_sites", lambda *a, **k: ["a.localhost", "b.localhost"]
    )
    _cache(monkeypatch, [{"path": BENCH}])

    result = core_apps.install_apps("proj", ["payments"])

    assert result.status is Status.WARNING
    assert result.data.ok is False
    assert [r.ok for r in result.data.results] == [True, True, False]


def test_install_fetch_only_never_touches_a_site(monkeypatch, container):
    _cache(monkeypatch, [{"path": BENCH}])

    result = core_apps.install_apps("proj", ["payments"], fetch_only=True)

    assert not any("install-app" in c for c in container.calls)
    assert [r.action for r in result.data.results] == ["get-app"]


def test_install_shell_interpolations_are_shlex_quoted(monkeypatch, container):
    _cache(monkeypatch, [{"path": BENCH}])

    core_apps.install_apps("proj", ["a; rm -rf /"], branch="x; whoami", sites=["s; echo"])

    fetch = next(c for c in container.calls if c.startswith("bench get-app"))
    assert "'a; rm -rf /'" in fetch
    assert "'x; whoami'" in fetch


# ------------------------------------------------- install_apps(if_not_present=True)


def test_install_if_not_present_skips_an_already_installed_app(monkeypatch, container):
    """The idempotent path: an app already on the site is reported skipped, its
    install hooks NEVER re-run, and the command succeeds (exit 0 upstream)."""
    container.available = ["frappe", "payments"]
    container.installed = {"a.localhost": ["frappe 15.0.0 version-15", "payments 1.2.3 version-15"]}
    _cache(monkeypatch, [{"path": BENCH}])

    result = core_apps.install_apps(
        "proj", ["payments"], sites=["a.localhost"], if_not_present=True
    )

    assert result.status is Status.OK
    assert result.data.ok is True
    # The whole point: install-app is NEVER run for the skipped app.
    assert not any("install-app" in c for c in container.calls)
    assert ("skip-install", "a.localhost", True) in [
        (r.action, r.site, r.ok) for r in result.data.results
    ]


def test_install_if_not_present_installs_an_app_absent_from_the_site(monkeypatch, container):
    """The normal path is unchanged: an app not on the site is fetched and installed
    even under --if-not-present."""
    _cache(monkeypatch, [{"path": BENCH}])  # container.installed has only frappe

    result = core_apps.install_apps(
        "proj", ["payments"], sites=["a.localhost"], if_not_present=True
    )

    assert result.data.ok is True
    assert any("install-app payments" in c for c in container.calls)
    assert not any(r.action == "skip-install" for r in result.data.results)


def test_install_if_not_present_skips_a_duplicate_after_installing(monkeypatch, container):
    container.available = ["frappe", "payments"]
    _cache(monkeypatch, [{"path": BENCH}])

    result = core_apps.install_apps(
        "proj", ["payments", "payments"], sites=["a.localhost"], if_not_present=True
    )

    install_calls = [call for call in container.calls if " install-app " in call]
    assert len(install_calls) == 1
    assert [
        (row.action, row.site, row.ok)
        for row in result.data.results
        if row.action in {"install-app", "skip-install"}
    ] == [
        ("install-app", "a.localhost", True),
        ("skip-install", "a.localhost", True),
    ]


def test_install_if_not_present_skips_only_the_sites_that_have_it(monkeypatch, container):
    """Multi-site sanity: skip the site that has it, install on the one that does not."""
    container.available = ["frappe", "payments"]
    container.installed = {
        "a.localhost": ["frappe 15.0.0 version-15", "payments 1.2.3 version-15"],
        "b.localhost": ["frappe 15.0.0 version-15"],
    }
    monkeypatch.setattr(
        core_apps.bench_sites, "list_sites", lambda *a, **k: ["a.localhost", "b.localhost"]
    )
    _cache(monkeypatch, [{"path": BENCH}])

    result = core_apps.install_apps("proj", ["payments"], if_not_present=True)

    assert result.data.ok is True
    outcomes = {(r.site, r.action) for r in result.data.results if r.site is not None}
    assert ("a.localhost", "skip-install") in outcomes
    assert ("b.localhost", "install-app") in outcomes
    # The skipped site's install-app is never issued; the absent one's is.
    assert any("--site b.localhost install-app payments" in c for c in container.calls)
    assert not any("--site a.localhost install-app payments" in c for c in container.calls)


def test_install_if_not_present_fails_closed_on_an_unreadable_site(monkeypatch, container):
    """An unreadable site cannot be confirmed clean, so it refuses (PRECONDITION)
    rather than degrading to "not installed" and (re)installing blindly."""
    container.available = ["frappe", "payments"]
    container.fail_on = ["execute frappe.get_installed_apps"]
    _cache(monkeypatch, [{"path": BENCH}])

    with pytest.raises(CwcliError) as exc:
        core_apps.install_apps("proj", ["payments"], sites=["a.localhost"], if_not_present=True)

    assert exc.value.code == "app.install_state_unknown"
    assert not any("install-app" in c for c in container.calls)


def test_install_if_not_present_still_fails_on_a_genuine_install_failure(monkeypatch, container):
    """The exit code stays trustworthy: a real install failure is NOT masked by the
    idempotent path (the whole reason the change exists)."""
    container.fail_on = ["install-app"]  # payments is absent from the site, so it installs
    _cache(monkeypatch, [{"path": BENCH}])

    result = core_apps.install_apps(
        "proj", ["payments"], sites=["a.localhost"], if_not_present=True
    )

    assert result.status is Status.WARNING
    assert result.data.ok is False


def _wire_running_bench(monkeypatch, *, restart_code=0, web_state="RUNNING", programs=None):
    """A live cwcli supervisord whose programs are all serving; restarts recorded.

    Patches the SHARED step's collaborators on ``core.supervision`` rather than
    anything private to ``core.apps``: the restart-and-verify logic lives there now,
    so a test that reached into ``core.apps`` for it would pin a seam that no longer
    decides anything.
    """
    programs = programs or ["web", "socketio", "watch", "schedule", "worker_default"]
    restarted: list[str] = []
    monkeypatch.setattr(
        core_apps.supervision,
        "discover_stack",
        lambda *a, **k: core_apps.supervision.StackSnapshot(
            supervisor_up=True, supervisor_pid=100, processes=[]
        ),
    )
    monkeypatch.setattr(
        core_apps.supervision,
        "supervisorctl_states",
        lambda *a, **k: {
            program: (web_state if program == "web" else "RUNNING", 101 + i)
            for i, program in enumerate(programs)
        },
    )

    def restart(_container, _path, program):
        restarted.append(program)
        return restart_code, f"{program}: stopped\n{program}: started"

    monkeypatch.setattr(core_apps.supervision, "restart_program", restart)
    monkeypatch.setattr(
        core_apps.supervision.resolvers,
        "resolve_assigned_ports",
        lambda *a, **k: {BENCH: (8000, 9000)},
    )
    monkeypatch.setattr(
        core_apps.supervision,
        "_wait_for_serving_sites",
        lambda *a, **k: ([], {site: "200" for site in k["sites"]}),
    )
    return restarted


def _unserved(monkeypatch, sites, code="500"):
    monkeypatch.setattr(
        core_apps.supervision,
        "_wait_for_serving_sites",
        lambda *a, **k: (list(sites), dict.fromkeys(sites, code)),
    )


@pytest.mark.parametrize("web_state", ["RUNNING", "STARTING"])
def test_install_resynchronises_a_running_bench_and_reports_the_verified_step(
    monkeypatch, container, web_state
):
    _cache(monkeypatch, [{"path": BENCH}])
    restarted = _wire_running_bench(monkeypatch, web_state=web_state)

    result = core_apps.install_apps("proj", ["payments"], sites=["a.localhost"])

    assert restarted == ["web", "schedule", "worker_default"]
    row = [(r.action, r.ok) for r in result.data.results][-1]
    assert row == ("restart-processes", True)
    assert result.data.ok is True


def test_the_scheduler_and_workers_are_cycled_too_not_only_web(monkeypatch, container):
    """The residual the first fix left: a worker keeps the pre-mutation interpreter.

    Cycling only ``web`` fixes the page a human loads and leaves every background
    job running code that no longer matches the disk - after an install it cannot
    import the new app, after an uninstall it writes to tables that are gone. The
    node (``socketio``, ``watch``) and redis programs import no Frappe app, so they
    are deliberately NOT cycled: restarting redis would drop the cache and the job
    queue for nothing.
    """
    _cache(monkeypatch, [{"path": BENCH}])
    restarted = _wire_running_bench(
        monkeypatch,
        programs=[
            "web",
            "socketio",
            "watch",
            "schedule",
            "worker_short",
            "worker_long",
            "worker_default",
            "redis_cache",
        ],
    )

    core_apps.install_apps("proj", ["payments"], sites=["a.localhost"])

    assert restarted == ["web", "schedule", "worker_short", "worker_long", "worker_default"]
    assert "socketio" not in restarted
    assert "watch" not in restarted
    assert "redis_cache" not in restarted


def test_install_does_not_claim_success_when_the_restarted_bench_cannot_serve(
    monkeypatch, container
):
    _cache(monkeypatch, [{"path": BENCH}])
    _wire_running_bench(monkeypatch)
    _unserved(monkeypatch, ["a.localhost"])

    result = core_apps.install_apps("proj", ["payments"], sites=["a.localhost"])

    assert result.status is Status.WARNING
    assert result.data.ok is False
    assert [(r.action, r.ok) for r in result.data.results][-1] == ("restart-processes", False)
    assert any("HTTP 200" in warning.text for warning in result.warnings)


def test_each_restart_is_announced_and_not_only_reported_afterwards(monkeypatch, container):
    """The disturbance is disclosed AS IT HAPPENS, and names the process.

    This defect exists because a bench was mutated underneath a running process
    silently. A fix that restarts silently and mentions it only in the trailing
    table repeats the habit on a command that can sit in the site-probe wait for
    up to a minute - and a worker restart can interrupt a job in flight, which the
    caller has a right to see named as it happens.
    """
    _cache(monkeypatch, [{"path": BENCH}])
    _wire_running_bench(monkeypatch)
    events = []

    core_apps.install_apps("proj", ["payments"], sites=["a.localhost"], on_event=events.append)

    announced = [(e.phase, e.app) for e in events if isinstance(e, core_apps.AppsAnnounce)]
    assert ("restart-processes", "web") in announced
    assert ("restart-processes", "worker_default") in announced


def test_a_restart_reporting_no_exit_code_is_not_treated_as_a_failure(monkeypatch, container):
    """``None`` is "docker recorded no code", which is not evidence of failure.

    Every other container read in this module reads ``not in (0, None)``. Treating
    a bare ``None`` as a failed restart would fail an install whose restart in fact
    worked, and would skip the site probe that is the real gate.
    """
    _cache(monkeypatch, [{"path": BENCH}])
    _wire_running_bench(monkeypatch, restart_code=None)

    result = core_apps.install_apps("proj", ["payments"], sites=["a.localhost"])

    assert [(r.action, r.ok) for r in result.data.results][-1] == ("restart-processes", True)
    assert result.data.ok is True


def test_an_unreadable_process_state_fails_closed_rather_than_assuming_nothing_runs(
    monkeypatch, container
):
    """Fail-honest (``core.where``'s rule): "I could not tell" is never "nothing is running".

    Degrading here would silently skip the restart on exactly the bench that needed
    it and hand back the plain success this whole change exists to stop.
    """
    _cache(monkeypatch, [{"path": BENCH}])

    def boom(*_a, **_k):
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "supervisor.process_state_unknown",
            "Could not verify the supervisord process state.",
        )

    monkeypatch.setattr(core_apps.supervision, "discover_stack", boom)

    result = core_apps.install_apps("proj", ["payments"], sites=["a.localhost"])

    assert result.data.ok is False
    assert [(r.action, r.ok) for r in result.data.results][-1] == ("restart-processes", False)
    # The install itself still reports as landed: it did, and a caller that retried
    # it because the restart check failed would install over a live site.
    assert ("install-app", True) in [(r.action, r.ok) for r in result.data.results]


def test_a_stopped_bench_is_not_started_by_an_install(monkeypatch, container):
    """A bench nobody started stays stopped, with no restart row to explain.

    ``container`` wires ``discover_stack`` to report no supervisord, so this is the
    ordinary "installed into a bench that is not serving" case: there is no running
    process serving stale code, so there is nothing to cycle.
    """
    _cache(monkeypatch, [{"path": BENCH}])
    started = []
    monkeypatch.setattr(
        core_apps.supervision, "restart_program", lambda *a, **k: started.append(a) or (0, "")
    )

    result = core_apps.install_apps("proj", ["payments"], sites=["a.localhost"])

    assert started == []
    assert "restart-processes" not in [r.action for r in result.data.results]
    assert result.data.ok is True


def test_a_stopped_supervised_web_process_is_not_started_by_an_install(monkeypatch, container):
    _cache(monkeypatch, [{"path": BENCH}])
    restarted = _wire_running_bench(monkeypatch, web_state="STOPPED")
    probed = []
    monkeypatch.setattr(
        core_apps.supervision,
        "_wait_for_serving_sites",
        lambda *a, **k: probed.append(k["sites"]) or ([], {}),
    )

    result = core_apps.install_apps("proj", ["payments"], sites=["a.localhost"])

    assert restarted == []
    assert probed == []
    assert "restart-processes" not in [r.action for r in result.data.results]
    assert result.data.ok is True


def test_a_stopped_worker_is_not_started_by_an_install(monkeypatch, container):
    """Only programs already serving are cycled: a deliberately-stopped worker stays down."""
    _cache(monkeypatch, [{"path": BENCH}])
    restarted = _wire_running_bench(monkeypatch, programs=["web", "schedule", "worker_default"])
    monkeypatch.setattr(
        core_apps.supervision,
        "supervisorctl_states",
        lambda *a, **k: {
            "web": ("RUNNING", 101),
            "schedule": ("RUNNING", 102),
            "worker_default": ("FATAL", None),
        },
    )

    core_apps.install_apps("proj", ["payments"], sites=["a.localhost"])

    assert restarted == ["web", "schedule"]


def test_an_unsupervised_running_bench_is_verified_without_being_restarted(monkeypatch, container):
    _cache(monkeypatch, [{"path": BENCH}])
    monkeypatch.setattr(
        core_apps.supervision,
        "discover_unsupervised_stack",
        lambda *a, **k: core_apps.supervision.UnsupervisedStack(
            manager_up=True,
            processes=[core_apps.supervision.ProcessHealth(label="web", up=True, pid=101)],
        ),
    )
    monkeypatch.setattr(
        core_apps.supervision.resolvers,
        "resolve_assigned_ports",
        lambda *a, **k: {BENCH: (8000, 9000)},
    )
    probed = []
    monkeypatch.setattr(
        core_apps.supervision,
        "_wait_for_serving_sites",
        lambda *a, **k: probed.append(k["sites"]) or ([], {"a.localhost": "200"}),
    )
    restarted = []
    monkeypatch.setattr(
        core_apps.supervision,
        "restart_program",
        lambda *a, **k: restarted.append(a) or (0, ""),
    )

    result = core_apps.install_apps("proj", ["payments"], sites=["a.localhost"])

    assert restarted == []
    assert probed == [["a.localhost"]]
    assert "restart-processes" not in [r.action for r in result.data.results]
    assert result.data.ok is True


def test_an_unhealthy_unsupervised_bench_fails_with_a_manual_restart_remedy(monkeypatch, container):
    _cache(monkeypatch, [{"path": BENCH}])
    monkeypatch.setattr(
        core_apps.supervision,
        "discover_unsupervised_stack",
        lambda *a, **k: core_apps.supervision.UnsupervisedStack(
            manager_up=True,
            processes=[core_apps.supervision.ProcessHealth(label="web", up=True, pid=101)],
        ),
    )
    monkeypatch.setattr(
        core_apps.supervision.resolvers,
        "resolve_assigned_ports",
        lambda *a, **k: {BENCH: (8000, 9000)},
    )
    _unserved(monkeypatch, ["a.localhost"])

    result = core_apps.install_apps("proj", ["payments"], sites=["a.localhost"])

    assert result.data.ok is False
    assert [(r.action, r.ok) for r in result.data.results][-1] == ("restart-processes", False)
    assert any("Restart the bench manually" in warning.text for warning in result.warnings)


@pytest.mark.parametrize(
    "stage",
    [
        "supervisorctl_states",
        "restart_program",
        "_wait_for_serving_sites",
    ],
)
def test_post_mutation_failures_are_reported_without_erasing_the_landed_change(
    monkeypatch, container, stage
):
    _cache(monkeypatch, [{"path": BENCH}])
    _wire_running_bench(monkeypatch)

    def boom(*_a, **_k):
        raise RuntimeError(f"{stage} failed")

    monkeypatch.setattr(core_apps.supervision, stage, boom)

    result = core_apps.install_apps("proj", ["payments"], sites=["a.localhost"])

    assert ("install-app", True) in [(r.action, r.ok) for r in result.data.results]
    assert [(r.action, r.ok) for r in result.data.results][-1] == ("restart-processes", False)
    assert result.data.ok is False
    assert any(f"{stage} failed" in warning.text for warning in result.warnings)


def test_an_unreadable_port_is_reported_rather_than_guessed(monkeypatch, container):
    _cache(monkeypatch, [{"path": BENCH}])
    _wire_running_bench(monkeypatch)
    monkeypatch.setattr(
        core_apps.supervision.resolvers, "resolve_assigned_ports", lambda *a, **k: {}
    )

    result = core_apps.install_apps("proj", ["payments"], sites=["a.localhost"])

    assert result.data.ok is False
    assert any("assigned port could not be read" in w.text for w in result.warnings)


# ------------------------------------------------------------------- uninstall_apps


def test_uninstall_without_consent_is_a_choice_and_runs_nothing(monkeypatch, container):
    _cache(monkeypatch, [{"path": BENCH}])

    result = core_apps.uninstall_apps("proj", ["payments"])

    assert result.status is Status.NEEDS_CHOICE
    assert result.choice.kind == "confirm_uninstall"
    assert result.choice.param == "consent"
    assert "a.localhost" in result.choice.prompt
    assert not any("uninstall-app" in c for c in container.calls)


def test_uninstall_with_consent_fans_out(monkeypatch, container):
    _cache(monkeypatch, [{"path": BENCH}])

    result = core_apps.uninstall_apps("proj", ["payments"], consent=True)

    assert result.data.ok is True
    assert any("uninstall-app payments --yes" in c for c in container.calls)


def test_uninstall_resynchronises_a_running_bench_and_reports_the_verified_step(
    monkeypatch, container
):
    _cache(monkeypatch, [{"path": BENCH}])
    restarted = _wire_running_bench(monkeypatch)

    result = core_apps.uninstall_apps("proj", ["payments"], sites=["a.localhost"], consent=True)

    assert restarted == ["web", "schedule", "worker_default"]
    assert [(r.action, r.ok) for r in result.data.results][-1] == ("restart-processes", True)
    assert result.data.ok is True


def test_uninstall_with_no_sites_is_a_clean_noop_before_the_gate(monkeypatch, container):
    """No sites is decided BEFORE consent: there is nothing to consent to."""
    monkeypatch.setattr(core_apps.bench_sites, "list_sites", lambda *a, **k: [])
    _cache(monkeypatch, [{"path": BENCH}])

    result = core_apps.uninstall_apps("proj", ["payments"])

    assert result.status is Status.OK
    assert result.data.results == []
    assert any(w.code == "sites.none" for w in result.warnings)


def test_uninstall_consent_is_independent_of_auto_start(monkeypatch, container):
    """The CLI fuses them behind --yes; the core must not.

    auto_start=True must NOT imply destructive consent - that fusion is a UX
    choice of one frontend, and a core that serves axi and a GUI models the two
    as what they are.
    """
    _cache(monkeypatch, [{"path": BENCH}])

    result = core_apps.uninstall_apps("proj", ["payments"], auto_start=True)

    assert result.status is Status.NEEDS_CHOICE
    assert result.choice.kind == "confirm_uninstall"


# ---------------------------------------------------------------------- the contract


def test_the_core_prints_nothing_at_all(monkeypatch, container, capsys):
    _cache(monkeypatch, [{"path": BENCH}])

    core_apps.list_apps("proj", installed=True)
    core_apps.install_apps("proj", ["payments"])
    core_apps.uninstall_apps("proj", ["payments"], consent=True)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


@pytest.mark.parametrize("verb", ["listing", "report"])
def test_what_the_core_returns_is_plain_serializable_data(monkeypatch, container, verb):
    """No live Docker object may cross a core.<verb> return boundary."""
    _cache(monkeypatch, [{"path": BENCH}])

    if verb == "listing":
        data = core_apps.list_apps("proj", installed=True).data
    else:
        data = core_apps.install_apps("proj", ["payments"]).data

    plain = dataclasses.asdict(data)
    assert isinstance(plain, dict)

    def _plain(value):
        if isinstance(value, dict):
            return all(_plain(v) for v in value.values())
        if isinstance(value, list):
            return all(_plain(v) for v in value)
        return value is None or isinstance(value, (str, int, float, bool))

    assert _plain(plain), plain


# ---------------------------------------------------------------------- checkout_app


def _bridge_spy(monkeypatch):
    """Record whether the credential bridge wrapped the git ops."""
    import contextlib

    entered = {"count": 0}

    @contextlib.contextmanager
    def _fake_bridge(_container, _path):
        entered["count"] += 1
        yield

    monkeypatch.setattr(core_apps.credbridge, "credential_bridge", _fake_bridge)
    return entered


def test_checkout_fetches_then_checks_out_the_ref_in_the_app_dir(monkeypatch, container):
    _cache(monkeypatch, [{"path": BENCH}])
    entered = _bridge_spy(monkeypatch)

    result = core_apps.checkout_app("proj", "payments", "feature/x")

    assert result.status is Status.OK
    assert result.data.ok is True
    assert [(r.action, r.ok) for r in result.data.results] == [
        ("fetch", True),
        ("checkout", True),
    ]
    # The fetch is authed through the bridge, and both git ops run in apps/<app>.
    assert entered["count"] == 1
    # bench's get-app names the remote `upstream`, so the fetch auto-detects it.
    assert "git fetch upstream -- feature/x" in container.calls
    assert "git checkout -B feature/x FETCH_HEAD" in container.calls
    # No reset step without --reset.
    assert not any("reset --hard" in c for c in container.calls)


def test_checkout_resynchronises_the_sites_that_have_the_app_installed(monkeypatch, container):
    """The residual that kept ``checkout`` out of the first fix, closed.

    "checkout has no target site" was true of its ARGUMENTS and false of its
    effect: the sites it changes are the ones with the app installed, which is the
    inverse of a question this module already answers per site. Without this the
    bench happily keeps serving the branch the developer just moved off - the
    quietest form of the defect, because nothing errors at all.
    """
    container.installed = {"a.localhost": ["frappe 15.0.0 version-15", "payments 1.0.0 main"]}
    _cache(monkeypatch, [{"path": BENCH}])
    _bridge_spy(monkeypatch)
    restarted = _wire_running_bench(monkeypatch)

    result = core_apps.checkout_app("proj", "payments", "feature/x")

    assert restarted == ["web", "schedule", "worker_default"]
    assert [(r.action, r.ok) for r in result.data.results][-1] == ("restart-processes", True)
    assert result.data.ok is True


def test_checkout_does_not_claim_success_when_the_bench_cannot_serve_the_new_ref(
    monkeypatch, container
):
    container.installed = {"a.localhost": ["payments 1.0.0 main"]}
    _cache(monkeypatch, [{"path": BENCH}])
    _bridge_spy(monkeypatch)
    _wire_running_bench(monkeypatch)
    _unserved(monkeypatch, ["a.localhost"])

    result = core_apps.checkout_app("proj", "payments", "feature/x")

    assert result.data.ok is False
    assert [(r.action, r.ok) for r in result.data.results][-1] == ("restart-processes", False)
    assert ("checkout", True) in [(r.action, r.ok) for r in result.data.results]


def test_checkout_skips_a_site_that_does_not_have_the_app(monkeypatch, container):
    """A site without the app cannot be affected by its checkout, so it is not probed."""
    monkeypatch.setattr(
        core_apps.bench_sites, "list_sites", lambda *a, **k: ["a.localhost", "b.localhost"]
    )
    container.installed = {
        "a.localhost": ["payments 1.0.0 main"],
        "b.localhost": ["frappe 15.0.0 version-15"],
    }
    _cache(monkeypatch, [{"path": BENCH}])
    _bridge_spy(monkeypatch)
    _wire_running_bench(monkeypatch)
    probed = []
    monkeypatch.setattr(
        core_apps.supervision,
        "_wait_for_serving_sites",
        lambda *a, **k: probed.append(k["sites"]) or ([], {}),
    )

    core_apps.checkout_app("proj", "payments", "feature/x")

    assert probed == [["a.localhost"]]


def test_checkout_reports_a_site_whose_installed_apps_it_could_not_read(monkeypatch, container):
    """Unreadable is REPORTED, never silently folded into "not affected"."""
    container.fail_on = ["execute frappe.get_installed_apps"]
    _cache(monkeypatch, [{"path": BENCH}])
    _bridge_spy(monkeypatch)
    _wire_running_bench(monkeypatch)

    result = core_apps.checkout_app("proj", "payments", "feature/x")

    assert any(w.code == "app.site_scope_unknown" for w in result.warnings)


def test_checkout_preserves_its_report_when_a_site_read_raises(monkeypatch, container):
    _cache(monkeypatch, [{"path": BENCH}])
    _bridge_spy(monkeypatch)
    _wire_running_bench(monkeypatch)
    monkeypatch.setattr(
        core_apps,
        "_installed_apps",
        lambda *a, **k: (_ for _ in ()).throw(
            CwcliError(ErrorKind.DOCKER, "exec.lost", "the exec stream was lost")
        ),
    )

    result = core_apps.checkout_app("proj", "payments", "feature/x")

    assert ("checkout", True) in [(r.action, r.ok) for r in result.data.results]
    assert any(w.code == "app.site_scope_unknown" for w in result.warnings)


def test_checkout_fails_resync_when_the_bench_site_set_is_unknown(monkeypatch, container):
    _cache(monkeypatch, [{"path": BENCH}])
    _bridge_spy(monkeypatch)
    _wire_running_bench(monkeypatch)
    monkeypatch.setattr(core_apps.bench_sites, "list_sites", lambda *a, **k: None)

    result = core_apps.checkout_app("proj", "payments", "feature/x")

    assert ("checkout", True) in [(r.action, r.ok) for r in result.data.results]
    assert result.data.results[-1].action == "restart-processes"
    assert result.data.results[-1].ok is False
    assert result.data.ok is False
    assert any(w.code == "app.site_scope_unknown" for w in result.warnings)


def test_checkout_preserves_its_report_when_site_enumeration_raises(monkeypatch, container):
    _cache(monkeypatch, [{"path": BENCH}])
    _bridge_spy(monkeypatch)
    _wire_running_bench(monkeypatch)
    monkeypatch.setattr(
        core_apps.bench_sites,
        "list_sites",
        lambda *a, **k: (_ for _ in ()).throw(
            CwcliError(ErrorKind.DOCKER, "sites.unreadable", "the site list was lost")
        ),
    )

    result = core_apps.checkout_app("proj", "payments", "feature/x")

    assert ("checkout", True) in [(r.action, r.ok) for r in result.data.results]
    assert result.data.results[-1].action == "restart-processes"
    assert result.data.results[-1].ok is False
    assert result.data.ok is False
    assert any(w.code == "app.site_scope_unknown" for w in result.warnings)


def test_a_checkout_that_never_moved_the_tree_resynchronises_nothing(monkeypatch, container):
    """A failed ``git fetch`` writes only into ``.git``: no process is serving it."""
    container.fail_on = ["git fetch"]
    container.installed = {"a.localhost": ["payments 1.0.0 main"]}
    _cache(monkeypatch, [{"path": BENCH}])
    _bridge_spy(monkeypatch)
    restarted = _wire_running_bench(monkeypatch)

    result = core_apps.checkout_app("proj", "payments", "feature/x")

    assert restarted == []
    assert "restart-processes" not in [r.action for r in result.data.results]


def test_checkout_falls_back_to_origin_when_no_upstream(monkeypatch, container):
    """A hand-cloned checkout (remote `origin`, no `upstream`) still works."""
    container.remotes = ["origin"]
    _cache(monkeypatch, [{"path": BENCH}])
    _bridge_spy(monkeypatch)

    core_apps.checkout_app("proj", "payments", "feature/x")

    assert "git fetch origin -- feature/x" in container.calls


def test_checkout_a_dir_that_is_not_a_git_checkout_raises(monkeypatch, container):
    container.fail_on = ["git remote"]
    _cache(monkeypatch, [{"path": BENCH}])
    _bridge_spy(monkeypatch)

    with pytest.raises(CwcliError) as exc:
        core_apps.checkout_app("proj", "ghost", "feature/x")
    assert exc.value.kind is ErrorKind.NOT_FOUND
    assert not any("git fetch" in c for c in container.calls)


@pytest.mark.parametrize("app", ["../other", "nested/app", ".", "..", " ", "bad\0name"])
def test_checkout_rejects_non_child_app_names_before_container_access(monkeypatch, container, app):
    _cache(monkeypatch, [{"path": BENCH}])
    _bridge_spy(monkeypatch)

    with pytest.raises(CwcliError) as exc:
        core_apps.checkout_app("proj", app, "feature/x")

    assert exc.value.kind is ErrorKind.USAGE
    assert exc.value.code == "app.invalid_component"
    assert container.calls == []


def test_checkout_reset_adds_a_hard_reset_step(monkeypatch, container):
    _cache(monkeypatch, [{"path": BENCH}])
    _bridge_spy(monkeypatch)

    result = core_apps.checkout_app("proj", "payments", "v1.2.0", reset=True)

    assert result.data.ok is True
    assert [r.action for r in result.data.results] == ["fetch", "checkout", "reset"]
    assert "git reset --hard FETCH_HEAD" in container.calls


def test_checkout_stops_at_a_failed_fetch(monkeypatch, container):
    """A failed fetch makes checkout meaningless, so it never runs."""
    container.fail_on = ["git fetch"]
    _cache(monkeypatch, [{"path": BENCH}])
    _bridge_spy(monkeypatch)

    result = core_apps.checkout_app("proj", "payments", "feature/x")

    assert result.status is Status.WARNING
    assert result.data.ok is False
    assert [(r.action, r.ok) for r in result.data.results] == [("fetch", False)]
    assert not any("git checkout" in c for c in container.calls)


class TestCheckoutRefusesADirtyTree:
    """The guarantee the docs make: uncommitted work is never carried across a ref.

    git's own refusal covers only a checkout that would OVERWRITE a modified file,
    so a NON-CONFLICTING dirty file used to ride through at exit 0 while the docs
    promised a dirty tree would fail. The guard lives in the core, so both the human
    verb and `axi apps checkout` route through it.
    """

    def test_an_unstaged_modification_refuses_before_any_fetch(self, monkeypatch, container):
        container.dirty = " M payments/hooks.py\n"
        _cache(monkeypatch, [{"path": BENCH}])
        _bridge_spy(monkeypatch)

        with pytest.raises(CwcliError) as exc:
            core_apps.checkout_app("proj", "payments", "feature/x")

        assert exc.value.kind is ErrorKind.CONFLICT
        assert exc.value.code == "app.dirty_tree"
        # Actionable: names WHAT is dirty and --reset as the way through.
        assert "payments/hooks.py" in exc.value.message
        assert "--reset" in (exc.value.hint or "")
        # Refused BEFORE the network op, not after.
        assert not any("git fetch" in c for c in container.calls)

    def test_a_staged_change_refuses_too(self, monkeypatch, container):
        container.dirty = "M  payments/hooks.py\n"
        _cache(monkeypatch, [{"path": BENCH}])
        _bridge_spy(monkeypatch)

        with pytest.raises(CwcliError) as exc:
            core_apps.checkout_app("proj", "payments", "feature/x")
        assert exc.value.code == "app.dirty_tree"

    def test_an_untracked_file_refuses_too(self, monkeypatch, container):
        """Captain's ruling: untracked files count as dirty.

        A brand-new module written but not yet `git add`ed is uncommitted work in
        the plainest sense, and it is exactly the case where the tool must not
        decide for the user that the file is worthless. Build residue does not
        trip this because .gitignore keeps it out of `git status` entirely.
        """
        container.dirty = "?? payments/new_module.py\n"
        _cache(monkeypatch, [{"path": BENCH}])
        _bridge_spy(monkeypatch)

        with pytest.raises(CwcliError) as exc:
            core_apps.checkout_app("proj", "payments", "feature/x")

        assert exc.value.code == "app.dirty_tree"
        assert "payments/new_module.py" in exc.value.message

    def test_the_status_read_is_not_narrowed_to_tracked_files(self, monkeypatch, container):
        """Asserted on the COMMAND: `--untracked-files=no` must not come back.

        Re-adding it would silently drop untracked files out of the guard, which
        is the exact narrowing the captain reversed.
        """
        _cache(monkeypatch, [{"path": BENCH}])
        _bridge_spy(monkeypatch)

        result = core_apps.checkout_app("proj", "payments", "feature/x")

        assert result.data.ok is True
        assert "git status --porcelain" in container.calls
        assert not any("--untracked-files=no" in c for c in container.calls)

    def test_reset_is_the_way_through_and_skips_the_check(self, monkeypatch, container):
        """--reset skips the check entirely. Note the honest limit documented on
        the hint: it hard-resets TRACKED changes and leaves untracked files in
        place - cwcli never runs `git clean`, so it cannot delete them."""
        container.dirty = " M payments/hooks.py\n?? payments/new_module.py\n"
        _cache(monkeypatch, [{"path": BENCH}])
        _bridge_spy(monkeypatch)

        result = core_apps.checkout_app("proj", "payments", "feature/x", reset=True)

        assert result.data.ok is True
        assert [r.action for r in result.data.results] == ["fetch", "checkout", "reset"]
        assert not any(c.startswith("git status") for c in container.calls)

    def test_an_unreadable_status_fails_closed(self, monkeypatch, container):
        """core.where's fail-honest rule: unknown must never degrade to "clean"."""
        container.fail_on = ["git status"]
        _cache(monkeypatch, [{"path": BENCH}])
        _bridge_spy(monkeypatch)

        with pytest.raises(CwcliError) as exc:
            core_apps.checkout_app("proj", "payments", "feature/x")

        assert exc.value.kind is ErrorKind.PRECONDITION
        assert exc.value.code == "app.dirty_state_unknown"
        assert not any("git fetch" in c for c in container.calls)


def test_checkout_quotes_a_hostile_ref(monkeypatch, container):
    """The ref is shell-quoted so it cannot break out of the git command."""
    _cache(monkeypatch, [{"path": BENCH}])
    _bridge_spy(monkeypatch)

    core_apps.checkout_app("proj", "payments", "x; rm -rf /")

    assert "git fetch upstream -- 'x; rm -rf /'" in container.calls


def test_checkout_guards_a_dash_prefixed_ref_from_being_parsed_as_an_option(monkeypatch, container):
    """A `--upload-pack=...`-style ref must not be parsed as a fetch option."""
    _cache(monkeypatch, [{"path": BENCH}])
    _bridge_spy(monkeypatch)

    core_apps.checkout_app("proj", "payments", "--upload-pack=touch pwned")

    assert "git fetch upstream -- '--upload-pack=touch pwned'" in container.calls
