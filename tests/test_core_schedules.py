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


HUB_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAoLvGEvGjS97I/0qCfGZ07VM9w5bY4Gd4/g9hbWOwYm"


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


class TestAdminOnlySchedules:
    """A prune deletes backups, so a schedule of one runs only on a current Admin's rights."""

    def _prune(self, by, source, **over):
        return sch.save(
            nightly(name="prune", verb="prune-backups", **over), by=by, by_source=source, now=T0
        )

    def test_it_runs_while_its_owner_is_still_an_admin(self, monkeypatch):
        from caffeinated_whale_cli.core import accounts, directory

        accounts.ensure_default()
        monkeypatch.setattr(directory, "current_role", lambda u, source: "admin")
        fleet = Fleet()
        for i, (by, source) in enumerate([("admin", "local"), ("ana", "ldap"), ("me", "host")]):
            self._prune(by, source, target={"project": f"p{i}"})
        made = run_tick(fleet, now=T0.replace(hour=2))
        assert sorted(j["target"]["project"] for j in made) == ["p0", "p1", "p2"]

    @pytest.mark.parametrize(
        "lookup,why",
        [
            (lambda u, source: "operator", "no longer an Admin"),
            (lambda u, source: None, "no longer an Admin"),
            (
                lambda u, source: (_ for _ in ()).throw(
                    CwcliError(sch.ErrorKind.PRECONDITION, "ldap.lookup", "unreachable")
                ),
                "could not be confirmed",
            ),
        ],
    )
    def test_a_run_on_lost_rights_is_skipped_and_reported(self, monkeypatch, lookup, why):
        from caffeinated_whale_cli.core import directory

        monkeypatch.setattr(directory, "current_role", lookup)
        fleet = Fleet()
        row = self._prune("ana", "ldap")
        assert run_tick(fleet, now=T0.replace(hour=2)) == []
        row = sch.list_schedules(now=T0.replace(hour=2))["schedules"][0]
        assert row["last_run"]["state"] == "skipped" and why in row["last_run"]["reason"]
        assert "ana" in row["last_skip"]["reason"]

    @staticmethod
    def _grant(tmp_path, cap, **kw):
        """`cwcli remote allow` on this machine, recorded in the cwcli home the tick reads."""
        from caffeinated_whale_cli.core import remote_access

        remote_access.allow(
            hub="hub-x", key=HUB_KEY, cap=cap, python="/opt/py/bin/python3", state_path=str(tmp_path / "ui.json"),
            host_key_file=str(tmp_path / "none"), proc_root=tmp_path, **kw,
        )  # fmt: skip

    @staticmethod
    def _last_run(day):
        return sch.list_schedules(now=T0 + timedelta(days=day, hours=2))["schedules"][0]["last_run"]

    def test_a_hub_users_schedule_runs_only_while_this_machine_grants_that_hub_admin(self, tmp_path, monkeypatch):
        """A hub's user acts here at most at the cap this machine's grant gives the hub, so
        the remote, never the hub, decides whether that owner is still an Admin: no grant
        or a lower re-grant skips the run, with the reason."""
        monkeypatch.setenv("HOME", str(tmp_path / "remote-user"))
        fleet = Fleet()
        self._prune("alice@hub-x", "hub")
        assert run_tick(fleet, now=T0.replace(hour=2)) == []
        assert self._last_run(0)["state"] == "skipped" and "hub hub-x" in self._last_run(0)["reason"]
        self._grant(tmp_path, "admin")
        (made,) = run_tick(fleet, now=T0 + timedelta(days=1, hours=2))
        fleet.finish(made)
        self._grant(tmp_path, "operator")
        assert run_tick(fleet, now=T0 + timedelta(days=2, hours=2)) == []
        assert self._last_run(2)["state"] == "skipped" and "no longer lets act as an Admin" in self._last_run(2)["reason"]

    def test_a_grant_written_to_a_chosen_authorized_keys_file_is_the_one_checked(self, tmp_path, monkeypatch):
        """`cwcli remote allow --authorized-keys <file>`: the bridge reads that file, so the
        tick does too, never the ~/.ssh/authorized_keys of whoever runs it."""
        from caffeinated_whale_cli.core import remote_access

        monkeypatch.setenv("HOME", str(tmp_path / "remote-user"))
        chosen = tmp_path / "keys" / "authorized_keys"
        fleet = Fleet()
        self._prune("alice@hub-x", "hub")
        self._grant(tmp_path, "admin", keys_path=chosen)
        assert not (tmp_path / "remote-user" / ".ssh" / "authorized_keys").exists()
        (made,) = run_tick(fleet, now=T0.replace(hour=2))
        fleet.finish(made)
        self._grant(tmp_path, "operator", keys_path=chosen)
        assert run_tick(fleet, now=T0 + timedelta(days=1, hours=2)) == []
        assert "no longer lets act as an Admin" in self._last_run(1)["reason"]
        remote_access.revoke(hub="hub-x", proc_root=tmp_path)
        assert "cwcli-hub:hub-x" not in chosen.read_text() and remote_access.recorded_grants() == {}

    def test_a_grant_line_this_home_never_recorded_still_counts(self, tmp_path, monkeypatch):
        """The bridge honours a grant line in the default authorized_keys whether or not this
        cwcli home recorded it (a reset home), so the schedule check counts it while the line's
        bridge serves this home, until a revoke removes it."""
        from caffeinated_whale_cli.core import remote_access
        from caffeinated_whale_cli.utils import config_utils

        monkeypatch.setenv("HOME", str(tmp_path / "remote-user"))
        self._grant(tmp_path, "admin")
        remote_access.grants_record().unlink()
        assert remote_access.recorded_grants() == {}
        assert [g.home for g in remote_access.list_grants()] == [str(config_utils.cwcli_home())]
        fleet = Fleet()
        self._prune("alice@hub-x", "hub")
        (made,) = run_tick(fleet, now=T0.replace(hour=2))
        fleet.finish(made)
        assert remote_access.revoke(hub="hub-x", proc_root=tmp_path).removed == 1
        assert run_tick(fleet, now=T0 + timedelta(days=1, hours=2)) == []
        assert "no longer lets act as an Admin" in self._last_run(1)["reason"]

    def test_a_grant_line_for_another_home_does_not_keep_an_admin_only_schedule_running(self, tmp_path, monkeypatch):
        """A line's bridge reaches only the cwcli home its command names: once the hub is
        granted for another home instead, it has no way into this one, so this home's
        Admin-only schedule stops, though that line still caps the hub at Admin."""
        monkeypatch.setenv("HOME", str(tmp_path / "remote-user"))
        fleet = Fleet()
        self._prune("alice@hub-x", "hub")
        self._grant(tmp_path, "admin")
        (made,) = run_tick(fleet, now=T0.replace(hour=2))
        fleet.finish(made)
        self._grant(tmp_path, "admin", home=tmp_path / "another-home")
        assert run_tick(fleet, now=T0 + timedelta(days=1, hours=2)) == []
        assert "no longer lets act as an Admin" in self._last_run(1)["reason"]

    @pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads any file")
    def test_on_a_shared_install_a_members_grant_is_confirmed_by_them_and_named_elsewhere(self, tmp_path, monkeypatch):
        """A group member ran `cwcli remote allow` (their own 0700 ~/.ssh/authorized_keys,
        naming the shared home). Their own daemon's tick reads that grant and runs the
        schedule; the shared scheduler, another account, cannot read the file, so it skips
        the run naming the file and that member, never a traceback and never that the hub
        lost its Admin cap."""
        from caffeinated_whale_cli.core import remote_access

        marker = tmp_path / "shared.toml"
        marker.write_text(f'enabled = true\nstate_dir = "{tmp_path / "shared"}"\n')
        monkeypatch.setenv("CWCLI_SHARED_MARKER", str(marker))
        monkeypatch.delenv("CWCLI_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path / "carol"))
        monkeypatch.setattr(remote_access.getpass, "getuser", lambda: "carol")
        self._grant(tmp_path, "admin")
        carols = tmp_path / "carol" / ".ssh" / "authorized_keys"
        fleet = Fleet()
        self._prune("alice@hub-x", "hub")
        (made,) = run_tick(fleet, now=T0.replace(hour=2))
        fleet.finish(made)
        monkeypatch.setenv("HOME", str(tmp_path / "shared"))  # the service account's home
        os.chmod(carols.parent, 0)
        try:
            assert run_tick(fleet, now=T0 + timedelta(days=1, hours=2)) == []
        finally:
            os.chmod(carols.parent, 0o700)
        reason = self._last_run(1)["reason"]
        assert str(carols) in reason and "written by carol" in reason and "could not be confirmed" in reason
        assert "no longer lets act as an Admin" not in reason

    def test_an_owner_saved_before_roles_keeps_running(self):
        """No ``owner_source``: the local account of that name, else this host's CLI."""
        from caffeinated_whale_cli.core import accounts

        accounts.ensure_default()
        fleet = Fleet()
        for i, owner in enumerate(("admin", "cmckay")):
            self._prune(owner, "local", target={"project": f"p{i}"})
        data = sch._load()
        for rec in data["schedules"].values():
            rec.pop("owner_source")
        sch._save(data)
        made = run_tick(fleet, now=T0.replace(hour=2))
        assert sorted(j["target"]["project"] for j in made) == ["p0", "p1"]

    def test_a_schedule_with_no_owner_says_so(self):
        fleet = Fleet()
        self._prune("", "local")
        assert run_tick(fleet, now=T0.replace(hour=2)) == []
        row = sch.list_schedules(now=T0.replace(hour=2))["schedules"][0]
        assert row["last_run"]["reason"].startswith("it has no recorded owner")

    def test_a_deleted_local_owner_is_no_longer_an_admin(self):
        fleet = Fleet()
        self._prune("gone", "local")
        assert run_tick(fleet, now=T0.replace(hour=2)) == []
        row = sch.list_schedules(now=T0.replace(hour=2))["schedules"][0]
        assert "its owner gone is no longer an Admin" in row["last_run"]["reason"]

    def test_an_admin_saving_it_takes_it_over_and_others_keep_their_owner(self):
        first = self._prune("ana", "ldap")
        again = sch.save({**first, "cron": "0 3 * * *"}, by="admin", by_source="local", now=T0)
        assert (again["owner"], again["owner_source"]) == ("admin", "local")
        plain = sch.save(nightly(), by="otto", by_source="ldap", now=T0)
        plain = sch.save({**plain, "cron": "0 4 * * *"}, by="ana", by_source="ldap", now=T0)
        assert plain["owner"] == "otto"


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
        assert run_tick(fleet, now=T0, running_projects=set()) == []
        finished = [{"id": "x", "verb": "apps-update", "state": "succeeded", "target": {"project": "p"}, "origin": {}}]
        made = run_tick(fleet, now=T0, finished_jobs=finished, running_projects={"q"})
        assert sorted(j["verb"] for j in made) == ["backup-sites", "restart"]
        # a second scheduler offered the same job and the same start fires nothing
        assert run_tick(fleet, now=T0, finished_jobs=finished, running_projects={"q"}) == []
        # a failed update or another instance's update does not fire it
        finished = [{"id": "y", "verb": "apps-update", "state": "failed", "target": {"project": "p"}, "origin": {}}]
        assert run_tick(fleet, now=T0, finished_jobs=finished) == []

    def test_an_event_that_meets_a_busy_instance_fires_once_it_is_free(self):
        fleet = Fleet()
        sch.save({"name": "after", "verb": "restart", "target": {"project": "p"}, "kind": "event", "trigger": {"type": "after_job", "verb": "apps-update"}}, now=T0)
        sch.save({"name": "boot", "verb": "backup-sites", "target": {"project": "q"}, "kind": "event", "trigger": {"type": "on_start"}}, now=T0)
        assert run_tick(fleet, now=T0, running_projects=set()) == []
        finished = [{"id": "x", "verb": "apps-update", "state": "succeeded", "target": {"project": "p"}, "origin": {}}]
        fleet.busy = True
        assert run_tick(fleet, now=T0, finished_jobs=finished, running_projects={"q"}) == []
        fleet.busy = False
        made = run_tick(fleet, now=T0 + timedelta(minutes=1), running_projects={"q"})
        assert sorted(j["verb"] for j in made) == ["backup-sites", "restart"]
        assert run_tick(fleet, now=T0 + timedelta(minutes=2), running_projects={"q"}) == []

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
