"""``cwcli schedule`` and ``cwcli axi schedule``: thin renderers over ``core.schedules``."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from caffeinated_whale_cli.core import schedules as sch
from caffeinated_whale_cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("CWCLI_HOME", str(tmp_path / "home"))


def test_add_list_disable_rm_on_the_human_surface():
    r = runner.invoke(app, ["schedule", "add", "Nightly", "--verb", "backup-sites", "-p", "p", "--daily", "02:00"])
    assert r.exit_code == 0, r.output
    sid = r.output.split()[1]
    assert sid.startswith("sch-") and "daily at 02:00" in r.output
    r = runner.invoke(app, ["schedule", "list"])
    assert "Nightly" in r.output and "(backup-sites) p" in r.output
    assert runner.invoke(app, ["schedule", "disable", sid]).exit_code == 0
    assert "off" in runner.invoke(app, ["schedule", "list"]).output.split("Nightly")[0]
    assert runner.invoke(app, ["schedule", "rm", sid]).exit_code == 0
    assert "No schedules" in runner.invoke(app, ["schedule", "list"]).output


def test_weekly_cron_and_args_and_a_removal_is_refused():
    r = runner.invoke(app, ["schedule", "add", "Core", "--verb", "apps-update", "-p", "p", "-b", "0", "--weekly", "sun 03:00", "--arg", 'apps=["frappe"]', "--allow-unattended", "--in-window"])
    assert r.exit_code == 0, r.output
    row = sch.list_schedules()["schedules"][0]
    assert row["cron"] == "0 3 * * sun" and row["args"] == {"apps": ["frappe"]}
    assert row["unattended"] is True and row["respect_window"] is True
    r = runner.invoke(app, ["schedule", "add", "Bad", "--verb", "rm", "-p", "p", "--daily", "02:00"])
    assert r.exit_code == 1 and "can never be scheduled" in r.output
    r = runner.invoke(app, ["schedule", "add", "NoTime", "--verb", "restart", "-p", "p"])
    assert r.exit_code == 2


def test_run_now_starts_the_job_with_the_schedule_in_its_origin(monkeypatch):
    from caffeinated_whale_cli import jobs as core_jobs

    made: list = []
    monkeypatch.setattr(core_jobs, "create_job", lambda verb, target, args, **kw: made.append((verb, target, kw)) or {"id": "j1", "verb": verb, "state": "queued"})
    r = runner.invoke(app, ["schedule", "add", "R", "--verb", "restart", "-p", "p", "--daily", "02:00"])
    sid = r.output.split()[1]
    r = runner.invoke(app, ["schedule", "run", sid])
    assert r.exit_code == 0 and "started job j1" in r.output
    assert made[0][0] == "restart" and made[0][2]["origin"]["schedule"] == sid


def test_the_agent_surface_is_toon():
    r = runner.invoke(app, ["axi", "schedule", "add", "Nightly", "--verb", "backup-sites", "--project", "p", "--cron", "0 2 * * *"])
    assert r.exit_code == 0, r.output
    assert "daily at 02:00" in r.output
    r = runner.invoke(app, ["axi", "schedule", "list"])
    assert r.exit_code == 0 and "schedules[1]" in r.output and "upcoming[" in r.output
    r = runner.invoke(app, ["axi", "schedule", "add", "Bad", "--verb", "rm-site", "--project", "p", "--cron", "0 2 * * *"])
    assert r.exit_code == 1 and "can never be scheduled" in r.output


def test_the_json_listing_is_the_core_view():
    runner.invoke(app, ["schedule", "add", "N", "--verb", "restart", "-p", "p", "--daily", "02:00"])
    r = runner.invoke(app, ["schedule", "list", "--json"])
    data = json.loads(r.output)
    assert data["schedules"][0]["name"] == "N" and "guards" in data and "upcoming" in data
