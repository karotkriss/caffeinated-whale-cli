"""The cwcli-owned backup store (core.backup_store): verification rules, adoption of a
removal's archive, the last-good delete guard, settings over defaults, key lookups."""

from __future__ import annotations

import gzip
import json
import tarfile
from pathlib import Path

import pytest

from caffeinated_whale_cli.core import backup_store as store
from caffeinated_whale_cli.core.envelope import Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CWCLI_HOME", str(tmp_path / ".cwcli"))
    monkeypatch.setattr(store, "cwcli_home", lambda: tmp_path / ".cwcli")
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path / ".cwcli" / "projects")
    return tmp_path


def _dump(path: Path, complete: bool = True) -> None:
    body = b"-- MariaDB dump\nCREATE TABLE `tabUser` (id int);\n" * 50
    if complete:
        body += b"-- Dump completed on 2026-10-27 12:00:00\n"
    with gzip.open(path, "wb") as f:
        f.write(body)


def _tar(path: Path) -> None:
    scratch = path.parent.parent / "scratch"
    scratch.mkdir(exist_ok=True)
    p = scratch / "f.txt"
    p.write_text("x")
    with tarfile.open(path, "w:gz") as tar:
        tar.add(p, arcname="files/f.txt")


def _archive_dir(tmp, ts="20261027_120000", site="shop_localhost", complete=True, with_files=True):
    d = tmp / "archive"
    d.mkdir(exist_ok=True)
    _dump(d / f"{ts}-{site}-database.sql.gz", complete=complete)
    if with_files:
        _tar(d / f"{ts}-{site}-files.tar")
        _tar(d / f"{ts}-{site}-private-files.tar")
    (d / f"{ts}-{site}-site_config_backup.json").write_text(
        json.dumps({"db_name": "x", "encryption_key": "k"})
    )
    return d


class TestChecks:
    def test_a_complete_dump_verifies_and_a_truncated_one_does_not(self, home):
        good = home / "good.sql.gz"
        _dump(good)
        assert store._check_database(good).ok
        bad = home / "bad.sql.gz"
        _dump(bad, complete=False)
        check = store._check_database(bad)
        assert not check.ok and "Dump completed" in check.detail

    def test_a_corrupt_gzip_fails_closed(self, home):
        bad = home / "corrupt.sql.gz"
        bad.write_bytes(b"not gzip at all")
        assert not store._check_database(bad).ok

    def test_tar_and_config_checks(self, home):
        t = home / "x-files.tar"
        _tar(t)
        assert store._check_tar("public_files", t).ok
        cfg = home / "c.json"
        cfg.write_text("{bad json")
        assert not store._check_config(cfg).ok


class TestAdoptAndList:
    def test_adopting_a_removal_archive_makes_a_verified_set_that_lists_and_resolves(self, home):
        d = _archive_dir(home)
        bset = store.adopt_dir(
            "p1", "shop.localhost", d, bench_path="/workspace/frappe-bench", reason="before-removal"
        )
        assert bset is not None and bset.verified and bset.reason == "before-removal"
        assert {p.kind for p in bset.parts} == {
            "database",
            "public_files",
            "private_files",
            "site_config",
        }
        assert not list(d.iterdir())  # moved, not copied
        listed = store.list_sets("p1", "shop.localhost")
        assert [b.key for b in listed] == [bset.key]
        assert store.find_set(bset.key) is not None
        assert store.last_good("p1", "shop.localhost").id == bset.id

    def test_an_incomplete_dump_adopts_as_unverified_and_is_never_last_good(self, home):
        d = _archive_dir(home, complete=False)
        bset = store.adopt_dir("p1", "shop.localhost", d, reason="before-removal")
        assert bset is not None and not bset.verified
        assert store.last_good("p1", "shop.localhost") is None

    def test_adopt_is_fail_soft_on_an_empty_or_missing_dir(self, home):
        assert store.adopt_dir("p1", "s.localhost", home / "nope") is None
        (home / "empty").mkdir()
        assert store.adopt_dir("p1", "s.localhost", home / "empty") is None

    def test_find_set_rejects_a_malformed_key(self, home):
        assert store.find_set("../../etc/passwd") is None
        assert store.find_set("p/s/not-a-set") is None


class TestDeleteGuard:
    def test_the_last_good_set_is_never_deleted(self, home):
        d = _archive_dir(home)
        bset = store.adopt_dir("p1", "shop.localhost", d)
        with pytest.raises(CwcliError) as exc:
            store.delete_set(bset, consent=True)
        assert exc.value.kind is ErrorKind.CONFLICT and exc.value.code == "backups.last_good"
        assert store.find_set(bset.key) is not None

    def test_delete_needs_consent_and_removes_an_older_set(self, home):
        older = store.adopt_dir("p1", "shop.localhost", _archive_dir(home, ts="20261026_120000"))
        newer = store.adopt_dir("p1", "shop.localhost", _archive_dir(home, ts="20261027_120000"))
        assert older and newer
        assert store.delete_set(older, consent=False).choice.kind == "confirm_delete_backup"
        result = store.delete_set(older, consent=True)
        assert result.data["deleted"] == older.key
        assert [b.id for b in store.list_sets("p1")] == [newer.id]


class TestSettings:
    def test_defaults_then_per_instance_values(self, home):
        s = store.settings("p1")
        assert s["local"]["daily"] == 7 and s["s3"]["monthly"] == 12 and s["min_free_gb"] == 20
        saved = store.save_settings("p1", {"local": {"daily": 3}, "stale_after_days": 2})
        assert saved["local"] == {"daily": 3, "weekly": 4, "monthly": 0}
        assert store.settings("p1")["stale_after_days"] == 2
        assert store.settings("p2")["local"]["daily"] == 7
        with pytest.raises(CwcliError):
            store.save_settings("p1", {"nope": 1})


# -------------------------------------------------------------- phase 3: retention


def _manifest(home, project, site, sid, created_at, reason="manual", verified=True, offsite=None):
    d = home / "backups" / project / site / sid
    d.mkdir(parents=True)
    (d / f"{sid}-database.sql.gz").write_bytes(b"x")
    man = {
        "id": sid,
        "project": project,
        "site": site,
        "bench_index": 0,
        "created_at": created_at,
        "reason": reason,
        "note": None,
        "with_files": False,
        "verified": verified,
        "size": 10,
        "frappe_version": "16.1.0",
        "apps": ["frappe"],
        "path": str(d),
        "offsite": offsite,
        "parts": [
            {"kind": "database", "filename": f"{sid}-database.sql.gz", "size": 10, "sha256": "0"}
        ],
        "checks": [],
    }
    (d / "set.json").write_text(json.dumps(man))


def _days_ago(n, hour=3):
    from datetime import datetime, timedelta, timezone

    return (
        (datetime.now(timezone.utc) - timedelta(days=n))
        .replace(hour=hour, minute=0, second=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


@pytest.fixture
def retention_home(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "cwcli_home", lambda: tmp_path)
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(store, "disk_budget", lambda min_free_gb=None: {"warn": False})
    return tmp_path


def test_retention_keeps_the_newest_per_day_week_and_month_and_the_last_good(retention_home):
    home = retention_home
    store.save_settings("p", {"local": {"daily": 2, "weekly": 1, "monthly": 0}})
    for n in range(5):
        _manifest(home, "p", "s.localhost", f"2026010{n + 1}_000000-b0", _days_ago(n))
    # two sets on the newest day: only the newest of that day fills the daily slot
    _manifest(home, "p", "s.localhost", "20260109_000000-b0", _days_ago(0, hour=1))
    plan = {d.key.split("/")[-1]: d for d in store.retention_plan("p")}
    kept = sorted(k for k, d in plan.items() if d.keep)
    # newest two days (today, yesterday) + the newest ISO-week slot (today's set already covers it)
    assert kept == ["20260101_000000-b0", "20260102_000000-b0"]
    assert plan["20260101_000000-b0"].why == "the site's last good backup"
    assert all(not plan[k].keep for k in ("20260103_000000-b0", "20260109_000000-b0"))


def test_safety_sets_follow_their_own_window_and_cap(retention_home):
    home = retention_home
    store.save_settings(
        "p",
        {
            "local": {"daily": 0, "weekly": 0, "monthly": 0},
            "safety": {"days": 14, "max_per_site": 2},
        },
    )
    for n in range(4):
        _manifest(
            home,
            "p",
            "s.localhost",
            f"2026020{n + 1}_000000-b0",
            _days_ago(n),
            reason="before-core-update",
        )
    _manifest(
        home, "p", "s.localhost", "20260101_000000-b0", _days_ago(30), reason="before-migrate"
    )
    plan = {d.key.split("/")[-1]: d for d in store.retention_plan("p")}
    assert [k for k, d in sorted(plan.items()) if d.keep] == [
        "20260201_000000-b0",
        "20260202_000000-b0",
    ]
    assert "safety backup" in plan["20260202_000000-b0"].why
    assert not plan["20260101_000000-b0"].keep  # older than the window


def test_under_the_disk_floor_only_sets_with_an_offsite_copy_lean_on_it(
    retention_home, monkeypatch
):
    home = retention_home
    monkeypatch.setattr(store, "disk_budget", lambda min_free_gb=None: {"warn": True})
    store.save_settings("p", {"local": {"daily": 7, "weekly": 0, "monthly": 0}})
    _manifest(home, "p", "s.localhost", "20260103_000000-b0", _days_ago(0), offsite={"bucket": "b"})
    _manifest(home, "p", "s.localhost", "20260102_000000-b0", _days_ago(1), offsite={"bucket": "b"})
    _manifest(home, "p", "s.localhost", "20260101_000000-b0", _days_ago(2))  # no copy: policy only
    plan = {d.key.split("/")[-1]: d for d in store.retention_plan("p")}
    assert plan["20260103_000000-b0"].keep  # the last good stays whatever happens
    assert not plan["20260102_000000-b0"].keep and "off-site copy" in plan["20260102_000000-b0"].why
    assert plan["20260101_000000-b0"].keep  # never pruned harder than the policy


def test_prune_needs_consent_lists_first_and_never_removes_the_last_good(retention_home):
    home = retention_home
    store.save_settings("p", {"local": {"daily": 1, "weekly": 0, "monthly": 0}})
    _manifest(home, "p", "s.localhost", "20260102_000000-b0", _days_ago(0))
    _manifest(home, "p", "s.localhost", "20260101_000000-b0", _days_ago(1))
    dry = store.prune("p", dry_run=True)
    assert [d["key"] for d in dry.data["would_remove"]] == ["p/s.localhost/20260101_000000-b0"]
    assert (home / "backups" / "p" / "s.localhost" / "20260101_000000-b0").exists()
    assert store.prune("p").status is Status.NEEDS_CHOICE
    events = []
    done = store.prune("p", consent=True, on_event=events.append)
    assert [d["key"] for d in done.data["removed"]] == ["p/s.localhost/20260101_000000-b0"]
    assert not (home / "backups" / "p" / "s.localhost" / "20260101_000000-b0").exists()
    assert (home / "backups" / "p" / "s.localhost" / "20260102_000000-b0").exists()
    assert any(isinstance(e, store.StoreStep) and e.phase == "prune" for e in events)


def test_schedule_is_due_once_a_day_after_its_time_and_runs_once_on_wake(retention_home):
    from datetime import datetime

    store.save_settings("p", {"schedule": {"enabled": True, "daily_at": "02:00"}})
    today = datetime.now().astimezone()
    before = today.replace(hour=1, minute=30)
    after = today.replace(hour=9, minute=0)  # the machine was off at 02:00: still due now
    assert store.schedule_due("p", before) is False
    assert store.schedule_due("p", after) is True
    store.mark_schedule_run("p", after)
    assert store.schedule_due("p", after.replace(hour=23)) is False
    store.save_settings("p", {"schedule": {"enabled": False}})
    assert store.schedule_due("p", after) is False


def test_a_set_filed_with_a_collision_suffix_is_still_addressable(retention_home):
    home = retention_home
    _manifest(home, "p", "s.localhost", "20260101_000000-b0-48439", _days_ago(0))
    assert store.find_set("p/s.localhost/20260101_000000-b0-48439") is not None
    assert store.find_set("p/s.localhost/junk") is None
