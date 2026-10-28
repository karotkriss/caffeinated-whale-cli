"""Restore drills: the latest good set restored into a throwaway site, checked, dropped,
and the verdict written on the set. Composition only; the bench calls are stubbed."""

from __future__ import annotations

import pytest

from caffeinated_whale_cli.core import backup_drill as drill
from caffeinated_whale_cli.core import backup_store as store
from caffeinated_whale_cli.core.envelope import Result, Status
from tests.test_core_transfer import _set


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "cwcli_home", lambda: tmp_path)
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path / "projects")
    return tmp_path


def _restored(ok=True, checks=None):
    return store.RestoreFromSetReport(
        set_key="k",
        project="p",
        bench_path="/b",
        site="s",
        restored=True,
        migrate_ran=True,
        migrate_ok=ok,
        config_merged=True,
        config_backup_path=None,
        post_checks=[
            store.BackupCheck(**c)
            for c in (checks or [{"name": "ping", "ok": True, "detail": "pong"}])
        ],
        report={},
    )


def test_a_drill_restores_into_a_throwaway_site_drops_it_and_records_ok(home, monkeypatch):
    bset = _set(home, "p", "shop.localhost")
    calls = []
    monkeypatch.setattr(
        drill.core_new_site,
        "new_site",
        lambda project, **kw: (
            calls.append(("new", kw["site"], tuple(kw["apps"]))),
            Result(status=Status.OK, data=object()),
        )[1],
    )
    monkeypatch.setattr(
        store,
        "restore_set",
        lambda key, **kw: (
            calls.append(("restore", key, kw["site"])),
            Result(status=Status.OK, data=_restored()),
        )[1],
    )
    monkeypatch.setattr(
        drill.core_rm_site,
        "drop_site",
        lambda project, site, **kw: (
            calls.append(("drop", site)),
            Result(status=Status.OK, data=object()),
        )[1],
    )

    r = drill.run_drill("p", site="shop.localhost", mariadb_root_password="root")

    rep = r.data
    assert rep.ok and rep.dropped and rep.drill_site == "shop-drill.localhost"
    assert calls == [
        ("new", "shop-drill.localhost", ("erpnext",)),
        ("restore", bset.key, "shop-drill.localhost"),
        ("drop", "shop-drill.localhost"),
    ]
    recorded = store.find_set(bset.key).drill
    assert (
        recorded["ok"] is True
        and recorded["dropped"] is True
        and "answered every check" in recorded["detail"]
    )
    assert store.last_drill("p", "shop.localhost")["ok"] is True


def test_a_failed_check_is_a_failed_drill_and_the_throwaway_is_still_dropped(home, monkeypatch):
    bset = _set(home, "p", "shop.localhost")
    dropped = []
    monkeypatch.setattr(
        drill.core_new_site,
        "new_site",
        lambda project, **kw: Result(status=Status.OK, data=object()),
    )
    monkeypatch.setattr(
        store,
        "restore_set",
        lambda key, **kw: Result(
            status=Status.OK,
            data=_restored(checks=[{"name": "http", "ok": False, "detail": "502"}]),
        ),
    )
    monkeypatch.setattr(
        drill.core_rm_site,
        "drop_site",
        lambda project, site, **kw: (dropped.append(site), Result(status=Status.OK, data=object()))[
            1
        ],
    )

    r = drill.run_drill("p", site="shop.localhost", mariadb_root_password="root")

    assert r.status is Status.WARNING and not r.data.ok
    assert "http failed (502)" in r.data.detail
    assert dropped == ["shop-drill.localhost"]
    assert store.find_set(bset.key).drill["ok"] is False


def test_a_site_without_a_good_backup_cannot_be_drilled(home):
    r = drill.run_drill("p", site="none.localhost", mariadb_root_password="root")
    assert r.status is Status.WARNING and r.data.set_key is None
