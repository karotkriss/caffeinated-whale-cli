"""Scheduled jobs: the cron, the store, the guards and the tick (a controlled clock)."""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta

import pytest

from caffeinated_whale_cli.core import schedules as sch
from caffeinated_whale_cli.core.errors import CwcliError

TZ = datetime.now().astimezone().tzinfo
T0 = datetime(2026, 11, 2, 1, 0, tzinfo=TZ)  # a Monday, 01:00 local


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("CWCLI_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(sch, "_free_disk_gb", lambda: 100.0)
    monkeypatch.setattr(sch, "_free_mem_mb", lambda: 4096.0)


class Fleet:
    """A fake job creator + instance status, recording every job it was asked for."""

    def __init__(self, status="running"):
        self.jobs: list[dict] = []
        self.status = status
        self.busy = False

    def create(self, verb, target, args, *, consent=False, origin=None):
        if self.busy:
            raise CwcliError(sch.ErrorKind.CONFLICT, "job.project_busy", "busy")
        rec = {"id": f"j{len(self.jobs) + 1}", "verb": verb, "target": target, "args": args, "consent": consent, "origin": origin, "state": "queued"}
        self.jobs.append(rec)
        return rec

    def instance_status(self, project):
        return self.status

    def read(self, job_id):
        found = [j for j in self.jobs if j["id"] == job_id]
        if not found:
            raise CwcliError(sch.ErrorKind.NOT_FOUND, "job.not_found", "gone")
        return found[0]

    def finish(self, job, state="succeeded"):
        job["state"] = state


def run_tick(fleet, **kw):
    return sch.tick(fleet.create, fleet.instance_status, read_job=fleet.read, **kw)


def nightly(**over):
    base = {"name": "Nightly backup", "verb": "backup-sites", "target": {"project": "p"}, "cron": "0 2 * * *"}
    base.update(over)
    return base


class TestCron:
    def test_daily_weekly_and_steps(self):
        assert sch.next_cron("0 2 * * *", T0) == T0.replace(hour=2)
        # Sunday 03:00 from Monday 01:00 -> next Sunday
        nxt = sch.next_cron("0 3 * * 0", T0)
        assert nxt.weekday() == 6 and nxt.hour == 3 and nxt > T0
        assert sch.next_cron("*/15 * * * *", T0) == T0.replace(minute=15)
        assert sch.next_cron("0 7 * * 1-5", T0.replace(hour=8)) == T0.replace(day=3, hour=7)  # Tuesday
        assert sch.next_cron("0 0 1 * *", T0).day == 1 and sch.next_cron("0 0 1 * *", T0).month == 12

    @pytest.mark.parametrize("bad", ["", "0 2 * *", "60 2 * * *", "0 25 * * *", "a b c d e", "0 2 * * 8"])
    def test_bad_expressions_raise(self, bad):
        with pytest.raises(ValueError):
            sch.parse_cron(bad)

    def test_sunday_is_zero_or_seven_and_names_work(self):
        assert sch.parse_cron("0 0 * * 7")["dow"] == {0}
        assert sch.parse_cron("0 0 * * sun,mon")["dow"] == {0, 1}

    def test_friendly_descriptions(self):
        assert sch.describe_cron("0 2 * * *") == "daily at 02:00"
        assert sch.describe_cron("0 3 * * 0") == "weekly on Sun at 03:00"
        assert sch.describe_cron("0 7 * * 1-5") == "weekly on Mon-Fri at 07:00"
        assert sch.describe_cron("30 * * * *") == "hourly at :30"
        assert sch.describe_cron("0 0 1 * *") == "monthly on day 1 at 00:00"


class TestStore:
    def test_save_lists_with_next_run_and_history(self):
        row = sch.save(nightly(), by="admin", now=T0)
        assert row["id"].startswith("sch-") and row["next_run"].startswith("2026-11-02T02:00")
        assert row["when_text"] == "daily at 02:00"
        assert row["owner"] == "admin" and row["history"][0]["action"] == "created"
        listing = sch.list_schedules(now=T0)
        assert [r["name"] for r in listing["schedules"]] == ["Nightly backup"]
        assert listing["upcoming"][0]["at"] == row["next_run"]

    @pytest.mark.parametrize(
        ("over", "code"),
        [
            ({"verb": "rm"}, "schedule.never"),
            ({"verb": "init"}, "schedule.verb_unsupported"),
            ({"target": {}}, "schedule.project_required"),
            ({"verb": "migrate"}, "schedule.site_required"),
            ({"cron": "nope"}, "schedule.cron_invalid"),
            ({"kind": "once", "at": "yesterday"}, "schedule.at_invalid"),
            ({"missed": "ignore"}, "schedule.missed_invalid"),
        ],
    )
    def test_invalid_schedules_are_refused(self, over, code):
        with pytest.raises(CwcliError) as e:
            sch.save(nightly(**over), now=T0)
        assert e.value.code == code

    def test_update_delete_enable(self):
        row = sch.save(nightly(), now=T0)
        sch.save({**nightly(cron="0 4 * * *"), "id": row["id"]}, by="admin", now=T0)
        again = sch.list_schedules(now=T0)["schedules"][0]
        assert again["cron"] == "0 4 * * *" and again["history"][-1]["action"] == "updated"
        sch.set_enabled(row["id"], False, now=T0)
        assert sch.list_schedules(now=T0)["schedules"][0]["next_run"] is None
        sch.delete(row["id"])
        assert sch.list_schedules(now=T0)["schedules"] == []
        with pytest.raises(CwcliError):
            sch.delete(row["id"])

    def test_settings_windows_quiet_hours_guards(self):
        sch.save_settings(windows={"p": {"start": "02:00", "end": "05:00", "days": ["sun"]}}, quiet_hours={"start": "22:00", "end": "06:00"}, guards={"min_free_disk_gb": 20}, digest="daily")
        got = sch.list_schedules(now=T0)
        assert got["windows"]["p"]["days"] == ["sun"] and got["quiet_hours"]["start"] == "22:00"
        assert got["guards"]["min_free_disk_gb"] == 20 and got["guards"]["min_free_mem_mb"] == 512
        assert got["digest"] == "daily"
        with pytest.raises(CwcliError):
            sch.save_settings(quiet_hours={"start": "25:00", "end": "06:00"})


class TestTick:
    def test_fires_at_the_slot_once_then_waits_for_the_next(self):
        fleet = Fleet()
        sch.save(nightly(), now=T0)
        assert run_tick(fleet, now=T0.replace(hour=1, minute=59)) == []
        made = run_tick(fleet, now=T0.replace(hour=2))
        assert [j["verb"] for j in made] == ["backup-sites"]
        assert made[0]["origin"]["account"] == "schedule" and made[0]["consent"] is True
        assert run_tick(fleet, now=T0.replace(hour=2, minute=1)) == []
        row = sch.list_schedules(now=T0.replace(hour=2, minute=1))["schedules"][0]
        assert row["last_run"]["job_id"] == "j1" and row["next_run"].startswith("2026-11-03T02:00")
        made = run_tick(fleet, now=T0 + timedelta(days=1, hours=1))
        assert len(made) == 1

    def test_a_new_schedule_does_not_fire_for_a_slot_that_already_passed(self):
        fleet = Fleet()
        sch.save(nightly(cron="0 0 * * *"), now=T0)  # 00:00 passed an hour ago
        assert run_tick(fleet, now=T0) == []

    def test_missed_runs_run_once_on_wake_or_skip(self):
        fleet = Fleet()
        sch.save(nightly(name="run"), now=T0)
        sch.save(nightly(name="skip", missed="skip"), now=T0)
        made = run_tick(fleet, now=T0 + timedelta(hours=9))  # machine was off at 02:00
        assert len(made) == 1
        rows = {r["name"]: r for r in sch.list_schedules(now=T0 + timedelta(hours=9))["schedules"]}
        assert rows["skip"]["last_run"]["state"] == "skipped"
        assert rows["skip"]["next_run"].startswith("2026-11-03T02:00")

    def test_updates_and_migrate_never_start_without_the_unattended_flag(self):
        fleet = Fleet()
        upd = {"name": "u", "verb": "apps-update", "target": {"project": "p", "bench": "0"}, "cron": "0 2 * * *", "args": {"apps": ["frappe"]}}
        sch.save(upd, now=T0)
        sch.save({**upd, "name": "u2", "unattended": True}, now=T0)
        sch.save({"name": "m", "verb": "migrate", "target": {"project": "p", "site": "s"}, "cron": "0 2 * * *", "start_if_stopped": True}, now=T0)
        sch.save({"name": "r", "verb": "recipe", "target": {"project": "p", "site": "s"}, "cron": "0 2 * * *", "steps": [{"verb": "backup-sites"}, {"verb": "migrate"}]}, now=T0)
        made = run_tick(fleet, now=T0.replace(hour=2))
        assert [(j["verb"], j["origin"]["schedule"]) for j in made] == [("apps-update", made[0]["origin"]["schedule"])]
        rows = {r["name"]: r for r in sch.list_schedules(now=T0.replace(hour=2))["schedules"]}
        assert rows["u2"]["id"] == made[0]["origin"]["schedule"]
        for name in ("u", "m", "r"):
            assert "unattended" in rows[name]["last_skip"]["reason"]
            assert rows[name]["last_run"]["state"] == "skipped"
            assert rows[name]["next_run"].startswith("2026-11-03T02:00")
        fleet.status = "stopped"
        assert run_tick(fleet, now=T0 + timedelta(days=1, hours=1)) == []

    def test_a_busy_instance_is_retried_next_minute(self):
        fleet = Fleet()
        fleet.busy = True
        sch.save(nightly(), now=T0)
        assert run_tick(fleet, now=T0.replace(hour=2)) == []
        fleet.busy = False
        assert len(run_tick(fleet, now=T0.replace(hour=2, minute=1))) == 1

    def test_a_stopped_instance_is_skipped_or_started_first(self):
        fleet = Fleet(status="stopped")
        sch.save(nightly(name="skip"), now=T0)
        sch.save(nightly(name="start", start_if_stopped=True), now=T0)
        made = run_tick(fleet, now=T0.replace(hour=2))
        assert [j["verb"] for j in made] == ["start"]
        rows = {r["name"]: r for r in sch.list_schedules(now=T0)["schedules"]}
        assert rows["skip"]["last_skip"]["reason"] == "the instance is stopped"
        fleet.status = "running"
        made = run_tick(fleet, now=T0.replace(hour=2, minute=1))
        assert [j["verb"] for j in made] == ["backup-sites"]

    def test_the_maintenance_window_and_quiet_hours_hold_a_run_until_they_allow_it(self):
        fleet = Fleet()
        sch.save_settings(windows={"p": {"start": "03:00", "end": "05:00"}})
        sch.save(nightly(name="windowed", respect_window=True), now=T0)
        assert run_tick(fleet, now=T0.replace(hour=2)) == []
        assert len(run_tick(fleet, now=T0.replace(hour=3))) == 1
        sch.save_settings(quiet_hours={"start": "22:00", "end": "06:00"})
        sch.save(nightly(name="quiet"), now=T0.replace(hour=4))
        assert run_tick(fleet, now=T0 + timedelta(days=1, hours=1)) == []
        assert len(run_tick(fleet, now=T0 + timedelta(days=1, hours=5, minutes=1))) == 1

    def test_low_disk_or_memory_holds_everything_but_a_stop(self, monkeypatch):
        fleet = Fleet()
        monkeypatch.setattr(sch, "_free_disk_gb", lambda: 1.0)
        sch.save(nightly(name="backup"), now=T0)
        sch.save(nightly(name="stop", verb="stop"), now=T0)
        made = run_tick(fleet, now=T0.replace(hour=2))
        assert [j["verb"] for j in made] == ["stop"]
        assert "GB free" in sch.list_schedules(now=T0)["schedules"][0]["last_skip"]["reason"]

    def test_a_one_off_that_is_skipped_retires_instead_of_retrying_every_minute(self):
        fleet = Fleet(status="stopped")
        sch.save({"name": "once", "verb": "restart", "target": {"project": "p"}, "kind": "once", "at": T0.replace(hour=2).strftime("%Y-%m-%dT%H:%M:%S%z")}, now=T0)
        assert run_tick(fleet, now=T0.replace(hour=2)) == []
        fleet.status = "running"
        assert run_tick(fleet, now=T0.replace(hour=2, minute=1)) == []
        row = sch.list_schedules(now=T0.replace(hour=3))["schedules"][0]
        assert row["enabled"] is False and row["last_run"]["state"] == "skipped"

    def test_re_enabling_or_editing_waits_for_the_next_slot_not_a_stale_one(self):
        fleet = Fleet()
        row = sch.save(nightly(), now=T0)
        assert len(run_tick(fleet, now=T0.replace(hour=2))) == 1
        sch.set_enabled(row["id"], False, now=T0.replace(hour=3))
        sch.set_enabled(row["id"], True, now=T0 + timedelta(days=3, hours=9))  # Thursday 10:00
        assert run_tick(fleet, now=T0 + timedelta(days=3, hours=9, minutes=1)) == []
        assert len(run_tick(fleet, now=T0 + timedelta(days=4, hours=1))) == 1
        sch.save({**nightly(cron="0 4 * * *"), "id": row["id"]}, now=T0 + timedelta(days=6, hours=9))
        assert run_tick(fleet, now=T0 + timedelta(days=6, hours=9, minutes=1)) == []
        assert len(run_tick(fleet, now=T0 + timedelta(days=7, hours=3))) == 1

    def test_the_last_run_outcome_comes_from_the_job_record(self):
        fleet = Fleet()
        sch.save(nightly(), now=T0)
        made = run_tick(fleet, now=T0.replace(hour=2))
        fleet.finish(made[0], "failed")
        run_tick(fleet, now=T0.replace(hour=2, minute=1))
        assert sch.list_schedules(now=T0)["schedules"][0]["last_run"]["state"] == "failed"

    def test_a_one_off_fires_once_and_disables_itself(self):
        fleet = Fleet()
        sch.save({"name": "once", "verb": "restart", "target": {"project": "p"}, "kind": "once", "at": T0.replace(hour=2).strftime("%Y-%m-%dT%H:%M:%S%z")}, now=T0)
        assert len(run_tick(fleet, now=T0.replace(hour=2))) == 1
        row = sch.list_schedules(now=T0.replace(hour=3))["schedules"][0]
        assert row["enabled"] is False
        assert run_tick(fleet, now=T0.replace(hour=3)) == []


class TestRecipesAndTriggers:
    def test_a_recipe_runs_its_steps_one_after_another_and_stops_on_failure(self):
        fleet = Fleet()
        recipe = {
            "name": "maintenance",
            "verb": "recipe",
            "target": {"project": "p", "bench": "0", "site": "s"},
            "cron": "0 2 * * *",
            "steps": [{"verb": "backup-sites"}, {"verb": "migrate"}, {"verb": "restart"}],
            "unattended": True,
        }
        sch.save(recipe, now=T0)
        made = run_tick(fleet, now=T0.replace(hour=2))
        assert [j["verb"] for j in made] == ["backup-sites"] and made[0]["origin"]["recipe_step"] == 0
        assert run_tick(fleet, now=T0.replace(hour=2, minute=1)) == []  # still running
        fleet.finish(made[0])
        made = run_tick(fleet, now=T0.replace(hour=2, minute=5))  # no finished_jobs: read from the records
        assert [j["verb"] for j in made] == ["migrate"] and made[0]["consent"] is True
        # its own slot coming round again does not start a second copy mid-recipe
        assert run_tick(fleet, now=T0 + timedelta(days=1, hours=1)) == []
        fleet.finish(made[0], "failed")
        made = run_tick(fleet, now=T0.replace(hour=2, minute=9))
        assert made == []
        assert sch.list_schedules(now=T0)["schedules"][0]["last_run"]["state"] == "failed"

    def test_a_later_recipe_step_retries_a_busy_instance_and_obeys_the_guards(self):
        fleet = Fleet()
        sch.save({"name": "r", "verb": "recipe", "target": {"project": "p"}, "cron": "0 2 * * *", "steps": [{"verb": "backup-sites"}, {"verb": "restart"}]}, now=T0)
        first = run_tick(fleet, now=T0.replace(hour=2))[0]
        fleet.finish(first)
        fleet.busy = True
        assert run_tick(fleet, now=T0.replace(hour=2, minute=1)) == []
        fleet.busy = False
        sch.save_settings(quiet_hours={"start": "02:00", "end": "03:00"})
        assert run_tick(fleet, now=T0.replace(hour=2, minute=2)) == []
        made = run_tick(fleet, now=T0.replace(hour=3))
        assert [j["verb"] for j in made] == ["restart"]
        fleet.finish(made[0])
        fleet.status = "stopped"
        run_tick(fleet, now=T0.replace(hour=3, minute=1))
        assert sch.list_schedules(now=T0)["schedules"][0].get("recipe") is None

    def test_after_job_and_on_start_triggers(self):
        fleet = Fleet()
        sch.save({"name": "after", "verb": "restart", "target": {"project": "p"}, "kind": "event", "trigger": {"type": "after_job", "verb": "apps-update"}}, now=T0)
        sch.save({"name": "boot", "verb": "backup-sites", "target": {"project": "q"}, "kind": "event", "trigger": {"type": "on_start"}}, now=T0)
        assert run_tick(fleet, now=T0) == []
        finished = [{"id": "x", "verb": "apps-update", "state": "succeeded", "target": {"project": "p"}, "origin": {}}]
        made = run_tick(fleet, now=T0, finished_jobs=finished, started_projects={"q"})
        assert sorted(j["verb"] for j in made) == ["backup-sites", "restart"]
        # a failed update or another instance's update does not fire it
        finished = [{"id": "y", "verb": "apps-update", "state": "failed", "target": {"project": "p"}, "origin": {}}]
        assert run_tick(fleet, now=T0, finished_jobs=finished) == []

    def test_templates_are_valid_once_a_target_is_added(self):
        for t in sch.templates():
            body = {k: v for k, v in t.items() if k not in {"key", "note", "process"}}
            body["target"] = {"project": "p", "bench": "0", "process": t.get("process", "")}
            sch.validate(body)


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="needs time.tzset")
def test_a_daily_slot_keeps_its_wall_clock_time_across_daylight_saving(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        before = datetime(2026, 10, 31, 12, 0).astimezone()  # EDT; the clocks go back on Nov 1
        nxt = sch.next_cron("0 2 * * *", before)
        assert nxt.astimezone().replace(tzinfo=None) == datetime(2026, 11, 1, 2, 0)
        assert sch.next_cron("0 2 * * *", nxt).astimezone().replace(tzinfo=None) == datetime(2026, 11, 2, 2, 0)
    finally:
        monkeypatch.undo()
        time.tzset()


class TestStoreLock:
    def test_an_edit_waits_for_the_lock_another_scheduler_holds(self):
        fcntl = pytest.importorskip("fcntl")
        row = sch.save(nightly(), now=T0)
        fd = os.open(sch.store_path().with_suffix(".lock"), os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        t = threading.Thread(target=sch.delete, args=(row["id"],))
        t.start()
        t.join(0.3)
        try:
            assert t.is_alive() and row["id"] in sch.store_path().read_text()
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        t.join(5)
        assert sch.list_schedules(now=T0)["schedules"] == []

    def test_a_lock_that_cannot_be_taken_refuses_the_write(self):
        pytest.importorskip("fcntl")
        sch.store_path().with_suffix(".lock").mkdir(parents=True)
        with pytest.raises(CwcliError) as e:
            sch.save(nightly(), now=T0)
        assert e.value.code == "schedules.lock_unavailable"
