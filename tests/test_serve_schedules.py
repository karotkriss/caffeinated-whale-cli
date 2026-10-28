"""The daemon's schedules routes: the view, the writes with the account recorded, run now."""

from __future__ import annotations

import json
import secrets
import threading
import urllib.error
import urllib.request

import pytest

from caffeinated_whale_cli import jobs as core_jobs
from caffeinated_whale_cli.commands import serve as serve_cmd
from caffeinated_whale_cli.core import fleet as core_fleet
from caffeinated_whale_cli.core import schedules as sch
from caffeinated_whale_cli.core.envelope import Result, Status

_TOKEN = "s3cr3t-token-value"
_TIMEOUT = 5.0


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("CWCLI_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(core_fleet, "list_instances", lambda: Result(status=Status.OK, data=[]))


@pytest.fixture
def daemon():
    fleet = core_fleet.Fleet()
    hub = serve_cmd._Hub(fleet.set_focus)
    fleet.set_publish(hub.publish)
    fleet.bootstrap()
    httpd = serve_cmd.make_server("127.0.0.1", 0, fleet, hub, _TOKEN)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _call(base, path, payload=None, *, headers=None):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bearer {_TOKEN}", "Content-Type": "application/json", **(headers or {})},
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:  # noqa: S310 - fixed localhost
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_the_view_carries_templates_windows_and_upcoming(daemon):
    status, body = _call(daemon, "/api/v1/schedules")
    assert status == 200
    d = body["data"]
    assert d["schedules"] == [] and d["upcoming"] == [] and d["guards"]["min_free_disk_gb"] == 5
    assert [t["key"] for t in d["templates"]][:2] == ["nightly-backup", "weekly-core-update"]


def test_save_enable_delete_record_the_account(daemon):
    status, body = _call(daemon, "/api/v1/schedules", {"op": "save", "schedule": {"name": "N", "verb": "restart", "target": {"project": "p"}, "cron": "0 2 * * *"}})
    assert status == 200, body
    row = body["data"]
    assert row["owner"] == "api" and row["history"][0]["by"] == "api"
    status, body = _call(daemon, "/api/v1/schedules", {"op": "enable", "id": row["id"], "enabled": False})
    assert status == 200 and sch.list_schedules()["schedules"][0]["enabled"] is False
    status, body = _call(daemon, "/api/v1/schedules", {"op": "delete", "id": row["id"]})
    assert status == 200 and sch.list_schedules()["schedules"] == []


def test_a_removal_cannot_be_scheduled_over_the_api(daemon):
    status, body = _call(daemon, "/api/v1/schedules", {"op": "save", "schedule": {"name": "x", "verb": "rm", "target": {"project": "p"}, "cron": "0 2 * * *"}})
    assert status == 400 and body["errors"][0]["title"] == "schedule.never"


def test_run_now_starts_the_job_with_the_schedule_and_the_account(daemon, monkeypatch):
    made: list = []
    monkeypatch.setattr(core_jobs, "create_job", lambda verb, target, args, **kw: made.append((verb, kw)) or {"id": "j1", "verb": verb, "state": "queued"})
    _, body = _call(daemon, "/api/v1/schedules", {"op": "save", "schedule": {"name": "N", "verb": "restart", "target": {"project": "p"}, "cron": "0 2 * * *"}})
    sid = body["data"]["id"]
    status, body = _call(daemon, "/api/v1/schedules", {"op": "run", "id": sid})
    assert status == 202 and body["data"]["id"] == "j1"
    origin = made[0][1]["origin"]
    assert origin["schedule"] == sid and origin["account"] == "api" and origin["manual"] is True


def test_settings_write_windows_quiet_hours_and_guards(daemon):
    status, body = _call(daemon, "/api/v1/schedules", {"op": "settings", "windows": {"p": {"start": "02:00", "end": "05:00", "days": ["sun"]}}, "quiet_hours": {"start": "22:00", "end": "06:00"}, "guards": {"min_free_disk_gb": 20}, "digest": "weekly"})
    assert status == 200, body
    d = body["data"]
    assert d["windows"]["p"]["days"] == ["sun"] and d["quiet_hours"]["end"] == "06:00"
    assert d["guards"]["min_free_disk_gb"] == 20 and d["digest"] == "weekly"


def test_the_routes_need_a_credential(daemon):
    req = urllib.request.Request(daemon + "/api/v1/schedules")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=_TIMEOUT)  # noqa: S310 - fixed localhost
    assert e.value.code == 401
