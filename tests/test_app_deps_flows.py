"""The dependency check where it acts: ``apps install``, ``apps update``, and the
human and agent surfaces' consent (both modes, per the project's standard).

``core.app_deps``' own rules are pinned in ``test_core_app_deps.py``; here the plan is
the one a planner builds from an injected world, and what is asserted is what each
surface does with it: refuse before anything is fetched, ask (a terminal) or refuse
naming ``--with-deps`` (a script), and fetch and install required apps first, in
order, only with consent.
"""

from __future__ import annotations

import json
import types

import pytest
from typer.testing import CliRunner

from caffeinated_whale_cli.core import app_deps
from caffeinated_whale_cli.core import apps as core_apps
from caffeinated_whale_cli.core import update as core_update
from caffeinated_whale_cli.core.app_deps import Declarations
from caffeinated_whale_cli.core.envelope import Choice, Result, Status
from caffeinated_whale_cli.core.errors import CwcliError
from tests.test_core_app_deps import BOOKING, TOOLKIT, _booking_world
from tests.test_core_apps import BENCH, _cache, container  # noqa: F401 - the fixture

# ----------------------------------------------------------------------- install


@pytest.fixture()
def world(monkeypatch, container):  # noqa: F811 - the imported fixture
    """The booking/toolkit world behind ``install_apps``' plan."""
    w = _booking_world()
    _cache(monkeypatch, [{"path": BENCH}])

    def state(_container, path, *, sites, new_site=None):
        return app_deps.BenchState(
            bench_path=path,
            apps={
                name: Declarations(app=name, version="15.125.0" if name == "frappe" else "1.0")
                for name in container.available
            },
            sites=list(sites),
            installed={s: {"frappe"} for s in sites},
            frappe_version="15.125.0",
        )

    real_build = app_deps.build_plan
    monkeypatch.setattr(app_deps, "read_bench_state", state)
    monkeypatch.setattr(
        app_deps,
        "build_plan",
        lambda project, st, targets, **kw: real_build(
            project,
            st,
            targets,
            reader=w.read,
            brancher=w.branches,
            sourcer=w.sources,
            **kw,
        ),
    )
    # What landed on disk is what the plan read (no drift) unless a test says otherwise.
    w.landed = {}
    monkeypatch.setattr(
        app_deps,
        "read_bench_apps",
        lambda _c, _p: {
            "toolkit": w.landed.get("toolkit", w.repos[TOOLKIT]["master"]),
            "booking": w.landed.get("booking", w.repos[BOOKING]["master"]),
        },
    )
    # The fake bench names a fetched repository after its URL's last segment.
    return w


def _gets(c):
    return [x for x in c.calls if x.startswith("bench get-app")]


def _installs(c):
    return [x for x in c.calls if "install-app" in x]


def test_missing_required_apps_ask_before_anything_is_fetched(world, container):  # noqa: F811
    result = core_apps.install_apps("proj", [BOOKING], branch="master", sites=["a.localhost"])
    assert result.status is Status.NEEDS_CHOICE
    assert result.choice.kind == "confirm_required_apps"
    assert _gets(container) == [] and _installs(container) == []


def test_with_deps_fetches_and_installs_the_required_app_first(world, container):  # noqa: F811
    result = core_apps.install_apps(
        "proj", [BOOKING], branch="master", sites=["a.localhost"], with_deps=True
    )
    assert result.data.ok, result.data.results
    gets = _gets(container)
    assert len(gets) == 2
    assert TOOLKIT in gets[0] and "--branch master" in gets[0]
    assert BOOKING in gets[1]
    assert _installs(container) == [
        "bench --site a.localhost install-app toolkit",
        "bench --site a.localhost install-app booking",
    ]


def test_a_plain_name_found_in_the_catalog_is_fetched_from_where_the_plan_read_it(
    world, container  # noqa: F811
):
    """Not ``bench get-app <name>``: bench's own lookup lands that repository's default
    branch, which the plan did not read (a real run fetched payments' develop branch,
    built for the next Frappe major, when the plan had chosen version-16)."""
    result = core_apps.install_apps("proj", ["toolkit"], sites=["a.localhost"])
    assert result.data.ok, result.data.results
    (get,) = _gets(container)
    assert TOOLKIT in get and "--branch develop" in get


def test_a_blocker_refuses_with_nothing_fetched(world, container):  # noqa: F811
    world.repos[BOOKING]["master"] = Declarations(
        app="booking", ranges={"frappe": ">=16.0.0,<17.0.0"}, branch="master"
    )
    with pytest.raises(CwcliError) as raised:
        core_apps.install_apps(
            "proj", [BOOKING], branch="master", sites=["a.localhost"], with_deps=True
        )
    assert raised.value.code == "app.frappe_range_mismatch"
    assert _gets(container) == [] and _installs(container) == []


def test_the_human_override_passes_a_frappe_range(world, container):  # noqa: F811
    world.repos[BOOKING]["master"] = Declarations(
        app="booking",
        ranges={"frappe": ">=16.0.0,<17.0.0"},
        required=["toolkit"],
        branch="master",
    )
    world.landed["booking"] = world.repos[BOOKING]["master"]
    result = core_apps.install_apps(
        "proj",
        [BOOKING],
        branch="master",
        sites=["a.localhost"],
        with_deps=True,
        ignore_frappe_range=True,
    )
    assert result.data.ok
    assert any(w.code == "app.frappe_range_mismatch" for w in result.warnings)


def test_a_remote_that_moved_since_the_check_stops_before_any_site_install(
    world, container  # noqa: F811
):
    world.landed["toolkit"] = Declarations(
        app="toolkit", required=["payments"], ranges={"frappe": ">=15.0.0,<17.0.0"}
    )
    result = core_apps.install_apps(
        "proj", [BOOKING], branch="master", sites=["a.localhost"], with_deps=True
    )
    assert not result.data.ok
    assert ("toolkit", "verify-dependencies", False) in [
        (r.app, r.action, r.ok) for r in result.data.results
    ]
    assert _installs(container) == []
    assert any(w.code == "app.dependencies_changed" for w in result.warnings)


def test_a_failed_required_app_fetch_installs_nothing(world, container):  # noqa: F811
    container.fail_on = [f"get-app --branch master {TOOLKIT}"]
    result = core_apps.install_apps(
        "proj", [BOOKING], branch="master", sites=["a.localhost"], with_deps=True
    )
    assert not result.data.ok and _installs(container) == []
    assert any(w.code == "app.required_app_fetch_failed" for w in result.warnings)


# ------------------------------------------------------------------------ update


class _UpdateBench:
    """A bench whose ``jobboard`` app's new tip adds ``required_apps = ["toolkit"]``."""

    def __init__(self, *, has_toolkit=False, tip_required=("toolkit",)):
        self.apps = {
            "frappe": {"init": '__version__ = "15.125.0"\n'},
            "jobboard": {"pyproject": "", "hooks": ""},
        }
        if has_toolkit:
            self.apps["toolkit"] = {"pyproject": '[project]\nversion = "0.23.3"\n'}
        self.tip = {
            "pyproject.toml": '[tool.bench.frappe-dependencies]\nfrappe = ">=15.0.0,<16.0.0"\n',
            "jobboard/hooks.py": f"required_apps = {list(tip_required)!r}\n",
        }
        self.calls: list = []

    def exec_run(self, cmd, workdir=None, **_kw):
        self.calls.append(cmd)
        if isinstance(cmd, list) and len(cmd) >= 6 and app_deps.SENTINEL in cmd[4]:
            record = {
                name: {
                    "pyproject": f.get("pyproject"),
                    "hooks": f.get("hooks"),
                    "init": f.get("init"),
                    "urls": [],
                }
                for name, f in self.apps.items()
            }
            return 0, ("\n" + app_deps.SENTINEL + json.dumps(record) + "\n").encode()
        text = cmd if isinstance(cmd, str) else " ".join(cmd)
        if text == "git remote":
            return 0, b"upstream\n"
        if text == "git rev-parse --abbrev-ref HEAD":
            return 0, b"master\n"
        if text.startswith("git fetch"):
            return 0, b""
        if text.startswith("git show FETCH_HEAD:"):
            body = self.tip.get(text.split("FETCH_HEAD:", 1)[1])
            return (0, body.encode()) if body is not None else (128, b"fatal: no such path")
        return 0, b""


def _wire_update(monkeypatch, bench, installed):
    monkeypatch.setattr(core_update.bench_sites, "list_sites", lambda *a, **k: list(installed))
    monkeypatch.setattr(
        core_update.core_apps,
        "_read_installed_apps",
        lambda _c, _p, sites, emit: {s: (installed[s], None) for s in sites},
    )


def test_the_tier_names_a_new_required_app_missing_from_the_bench(monkeypatch):
    bench = _UpdateBench()
    _wire_update(monkeypatch, bench, {"jobs.localhost": ["frappe", "jobboard"]})
    facts = core_update._dependency_facts(
        bench, bench_path=BENCH, apps=["jobboard"], sites_filter=None, ranges=True
    )
    (fact,) = facts
    assert fact["code"] == "app.required_app_missing"
    assert fact["dep"] == "toolkit" and fact["sites"] == ["jobs.localhost"]
    assert fact["ref"] == "upstream/master"


def test_the_tier_names_a_site_that_lacks_a_bench_present_required_app(monkeypatch):
    bench = _UpdateBench(has_toolkit=True)
    _wire_update(monkeypatch, bench, {"jobs.localhost": ["frappe", "jobboard"]})
    (fact,) = core_update._dependency_facts(
        bench, bench_path=BENCH, apps=["jobboard"], sites_filter=None, ranges=True
    )
    assert fact["code"] == "app.required_app_not_installed" and fact["site"] == "jobs.localhost"


def test_the_tier_finds_nothing_when_the_tip_requires_nothing_new(monkeypatch):
    bench = _UpdateBench(tip_required=())
    _wire_update(monkeypatch, bench, {"jobs.localhost": ["frappe", "jobboard"]})
    assert (
        core_update._dependency_facts(
            bench, bench_path=BENCH, apps=["jobboard"], sites_filter=None, ranges=True
        )
        == []
    )


def test_a_frappe_range_the_new_tip_breaks_is_the_existing_blocker_for_any_app(monkeypatch):
    bench = _UpdateBench(tip_required=())
    bench.tip["pyproject.toml"] = '[tool.bench.frappe-dependencies]\nfrappe = ">=16.0.0"\n'
    _wire_update(monkeypatch, bench, {"jobs.localhost": ["frappe", "jobboard"]})
    (fact,) = core_update._dependency_facts(
        bench, bench_path=BENCH, apps=["jobboard"], sites_filter=None, ranges=True
    )
    assert fact["code"] == "app.frappe_dependency_unsatisfied"
    assert fact["fix_app"] == "jobboard" and fact["fix_branch"] == "version-15"


def _wire_update_verb(monkeypatch, bench):
    monkeypatch.setattr(core_update.core_docker, "get_frappe_container", lambda _p: bench)
    monkeypatch.setattr(
        core_update.resolvers,
        "resolve_container_state",
        lambda *a, **k: Result(status=Status.OK),
    )
    monkeypatch.setattr(
        core_update.resolvers, "resolve_bench", lambda *a, **k: Result(status=Status.OK, data=BENCH)
    )
    monkeypatch.setattr(core_update, "_require_bench_dirs", lambda *a: None)

    class _Bridge:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(core_update.credbridge, "credential_bridge", lambda *a, **k: _Bridge())


def test_an_update_whose_new_code_needs_another_app_stops_before_the_pull(monkeypatch):
    bench = _UpdateBench()
    _wire_update(monkeypatch, bench, {"jobs.localhost": ["frappe", "jobboard"]})
    _wire_update_verb(monkeypatch, bench)
    pulled = []
    monkeypatch.setattr(core_update, "_update_apps", lambda *a, **k: pulled.append(1))
    events = []
    result = core_update.update("proj", ["jobboard"], on_event=events.append)
    report = result.data
    assert report.preflight_blocked and not report.ok and pulled == []
    blocker = next(f for f in report.preflight if f.code == "app.required_app_missing")
    assert "--with-deps" in blocker.remedy
    assert any(isinstance(e, core_update.UpdatePreflight) and e.blocked for e in events)


def test_with_deps_installs_the_required_app_then_updates(monkeypatch):
    bench = _UpdateBench()
    _wire_update(monkeypatch, bench, {"jobs.localhost": ["frappe", "jobboard"]})
    _wire_update_verb(monkeypatch, bench)
    installs = []

    def install(project, apps, **kw):
        installs.append((apps, kw["sites"], kw["prefer_ref"], kw["with_deps"]))
        bench.apps["toolkit"] = {}
        return Result(
            status=Status.OK,
            data=core_apps.AppsReport(project=project, bench_path=BENCH, results=[], ok=True),
        )

    monkeypatch.setattr(core_update.core_apps, "install_apps", install)
    monkeypatch.setattr(
        core_update.core_apps,
        "_read_installed_apps",
        lambda _c, _p, sites, emit: {
            s: (["frappe", "jobboard"] + (["toolkit"] if installs else []), None) for s in sites
        },
    )
    ran = []
    monkeypatch.setattr(core_update, "_update_apps", lambda *a, **k: ran.append(k) or "updated")
    assert core_update.update("proj", ["jobboard"], with_deps=True) == "updated"
    assert installs == [(["toolkit"], ["jobs.localhost"], "master", True)]
    assert ran


# ---------------------------------------------------------------------- surfaces


def _asked():
    return Result(
        status=Status.NEEDS_CHOICE,
        choice=Choice(
            kind="confirm_required_apps",
            param="with_deps",
            prompt="booking requires toolkit. Fetch and install toolkit "
            "first? (order on a.localhost: toolkit, booking)",
            options=[
                {
                    "order": 1,
                    "app": "toolkit",
                    "action": "fetch+install",
                    "required_by": "booking",
                    "source": "gitlab.example.com/tools/toolkit",
                    "ref": "master",
                    "value": "toolkit",
                    "label": "toolkit",
                },
                {
                    "order": 2,
                    "app": "booking",
                    "action": "fetch+install",
                    "required_by": "",
                    "source": "gitlab.example.com/team/booking",
                    "ref": "master",
                    "value": "booking",
                    "label": "booking",
                },
            ],
        ),
    )


@pytest.fixture()
def recorded_install(monkeypatch):
    calls = []

    def install(project, apps, **kw):
        calls.append(kw)
        if not kw.get("with_deps"):
            return _asked()
        return Result(
            status=Status.OK,
            data=core_apps.AppsReport(
                project=project,
                bench_path=BENCH,
                results=[
                    core_apps.AppResult(app="x", site="a.localhost", action="install-app", ok=True)
                ],
                ok=True,
            ),
        )

    monkeypatch.setattr(core_apps, "install_apps", install)
    return calls


def test_axi_install_without_with_deps_is_exit_2_naming_the_flag(monkeypatch, recorded_install):
    from caffeinated_whale_cli.commands import axi

    result = CliRunner().invoke(
        axi.app, ["apps", "install", "proj", BOOKING, "--site", "a.localhost"]
    )
    assert result.exit_code == 2
    assert "app.required_apps_missing" in result.output and "--with-deps" in result.output
    assert "toolkit" in result.output


def test_axi_install_with_deps_consents(monkeypatch, recorded_install):
    from caffeinated_whale_cli.commands import axi
    from caffeinated_whale_cli.utils import cache

    monkeypatch.setattr(cache, "recache_project", lambda *a, **k: True)
    result = CliRunner().invoke(
        axi.app, ["apps", "install", "proj", BOOKING, "--site", "a.localhost", "--with-deps"]
    )
    assert result.exit_code == 0, result.output
    assert recorded_install[-1]["with_deps"] is True


def test_axi_has_no_range_override():
    """The agent surface gets the consent and the read, never the override (no named
    beneficiary); the human verb is the escape hatch."""
    import typer

    from caffeinated_whale_cli.commands import axi

    root = typer.main.get_command(axi.app)
    apps_group = root.commands["apps"]
    for path in (("apps", "install"), ("apps", "update"), ("apps", "plan"), ("new-site",)):
        command = apps_group.commands[path[1]] if path[0] == "apps" else root.commands[path[0]]
        flags = {opt for p in command.params for opt in getattr(p, "opts", [])}
        assert "--ignore-frappe-range" not in flags, path
    install = apps_group.commands["install"]
    assert "--with-deps" in {opt for p in install.params for opt in getattr(p, "opts", [])}


def _human_install(monkeypatch, *args):
    from caffeinated_whale_cli.commands import apps as apps_cmd

    monkeypatch.setattr(apps_cmd, "ensure_containers_running", lambda *a, **k: None)
    monkeypatch.setattr(apps_cmd, "_resolve_bench", lambda *a: BENCH)
    monkeypatch.setattr(apps_cmd, "_refresh_cache", lambda *a: None)
    return CliRunner().invoke(apps_cmd.app, ["install", "proj", BOOKING, *args])


def test_a_script_without_with_deps_refuses_exit_1_naming_the_flag(monkeypatch, recorded_install):
    result = _human_install(monkeypatch, "--site", "a.localhost")
    assert result.exit_code == 1
    assert "--with-deps" in result.output
    assert len(recorded_install) == 1  # asked once, never re-invoked with consent


def test_a_terminal_shows_the_plan_and_asks_once(monkeypatch, recorded_install):
    from caffeinated_whale_cli.commands import deps_ui

    monkeypatch.setattr(deps_ui, "interactive", lambda json_output=False: True)
    asked = []
    monkeypatch.setattr(
        deps_ui.questionary,
        "confirm",
        lambda prompt, default=False: asked.append(prompt)
        or types.SimpleNamespace(ask=lambda: True),
    )
    result = _human_install(monkeypatch, "--site", "a.localhost")
    assert result.exit_code == 0, result.output
    assert len(asked) == 1 and "toolkit" in asked[0]
    assert [c.get("with_deps") for c in recorded_install] == [False, True]
    assert "toolkit" in result.output  # the plan was shown


def test_a_declined_terminal_answer_installs_nothing(monkeypatch, recorded_install):
    from caffeinated_whale_cli.commands import deps_ui

    monkeypatch.setattr(deps_ui, "interactive", lambda json_output=False: True)
    monkeypatch.setattr(
        deps_ui.questionary,
        "confirm",
        lambda prompt, default=False: types.SimpleNamespace(ask=lambda: False),
    )
    result = _human_install(monkeypatch, "--site", "a.localhost")
    assert result.exit_code == 1 and len(recorded_install) == 1


def test_axi_apps_plan_is_a_read_with_an_honest_exit(monkeypatch):
    from caffeinated_whale_cli.commands import axi

    plan = _booking_world().plan(
        app_deps.BenchState(
            bench_path=BENCH,
            apps={"frappe": Declarations(app="frappe", version="15.125.0")},
            sites=["a.localhost"],
            installed={"a.localhost": {"frappe"}},
            frappe_version="15.125.0",
        ),
        [(BOOKING, "master")],
    )
    monkeypatch.setattr(app_deps, "plan", lambda *a, **k: Result(status=Status.OK, data=plan))
    result = CliRunner().invoke(
        axi.app, ["apps", "plan", "proj", BOOKING, "--site", "a.localhost", "--branch", "master"]
    )
    assert result.exit_code == 0, result.output
    assert "needs_consent: true" in result.output
    assert "plan[2]{" in result.output
    assert "--with-deps" in result.output  # the help line names how to apply it
