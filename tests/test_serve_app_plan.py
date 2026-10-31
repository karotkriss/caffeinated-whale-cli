"""``GET /api/v1/apps/plan``: the read-only dependency plan the dialogs preview."""

from __future__ import annotations

import urllib.parse

from caffeinated_whale_cli.commands import serve as serve_cmd
from caffeinated_whale_cli.core import app_deps
from caffeinated_whale_cli.core.envelope import Result, Status
from tests.test_core_app_deps import BOOKING, _booking_world
from tests.test_serve_hosts import _req, daemon  # noqa: F401 - the fixture


def _wire(monkeypatch):
    world = _booking_world()
    monkeypatch.setattr(app_deps, "read_remote", world.read)
    monkeypatch.setattr(app_deps, "list_branches", world.branches)
    monkeypatch.setattr(app_deps, "find_sources", world.sources)
    return world


def test_a_new_bench_preview_plans_against_the_bench_it_will_build(
    daemon, monkeypatch  # noqa: F811
):
    _wire(monkeypatch)
    q = urllib.parse.urlencode(
        {"new_bench": "1", "version": "15", "site": "x.localhost", "target": BOOKING}
    )
    status, body, _ = _req(daemon, f"/api/v1/apps/plan?{q}", bearer=True)
    assert status == 200, body
    plan = body["data"]
    assert plan["order"] == ["toolkit", "booking"]
    assert plan["needs_consent"] is True and plan["ok"] is True
    assert plan["frappe_version"] == "15.x"


def test_an_instance_plan_rides_the_core_and_its_warnings(daemon, monkeypatch):  # noqa: F811
    world = _wire(monkeypatch)
    state = app_deps.BenchState(
        bench_path="/workspace/frappe-bench",
        apps={"frappe": app_deps.Declarations(app="frappe", version="15.125.0")},
        sites=["a.localhost"],
        installed={"a.localhost": {"frappe"}},
        frappe_version="15.125.0",
    )
    seen = {}

    def plan(project, targets, **kw):
        seen.update(project=project, targets=targets, **kw)
        return Result(status=Status.OK, data=world.plan(state, targets))

    monkeypatch.setattr(serve_cmd.core_app_deps, "plan", plan)
    q = urllib.parse.urlencode(
        [
            ("project", "acme"),
            ("bench", "0"),
            ("target", BOOKING),
            ("ref", "master"),
            ("site", "a.localhost"),
            ("dep_source", "toolkit=https://example.com/c"),
        ]
    )
    status, body, _ = _req(daemon, f"/api/v1/apps/plan?{q}", bearer=True)
    assert status == 200, body
    assert seen["targets"] == [(BOOKING, "master")] and seen["sites"] == ["a.localhost"]
    assert seen["dep_sources"] == {"toolkit": "https://example.com/c"}
    assert body["data"]["order"] == ["toolkit", "booking"]
    assert body["data"]["warnings"] == []


def test_a_plan_without_a_target_is_a_usage_error(daemon):  # noqa: F811
    status, body, _ = _req(daemon, "/api/v1/apps/plan?project=acme", bearer=True)
    assert status == 400 and body["errors"][0]["title"] == "plan.target_required"
