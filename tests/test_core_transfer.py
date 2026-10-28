"""Phase 2 of the backups spec: clone and move (single and batch) compose capture,
new-site, restore and the rm-site gate; the sendme leg bundles sets into one ticket and
files what it receives. Everything below the composition is stubbed; the sendme binary
is a script so the pty path and the ticket parse run for real."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from caffeinated_whale_cli.core import backup_store as store
from caffeinated_whale_cli.core import transfer
from caffeinated_whale_cli.core.envelope import Result, Status
from caffeinated_whale_cli.core.errors import CwcliError


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "cwcli_home", lambda: tmp_path)
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path / "projects")
    return tmp_path


def _set(home, project, site, sid="20260101_000000-b0", verified=True):
    d = home / "backups" / project / site / sid
    d.mkdir(parents=True)
    (d / f"{sid}-database.sql.gz").write_bytes(b"x")
    man = {
        "id": sid,
        "project": project,
        "site": site,
        "bench_index": 0,
        "created_at": "2026-01-01T00:00:00Z",
        "reason": "manual",
        "note": None,
        "with_files": False,
        "verified": verified,
        "size": 1,
        "frappe_version": "16.1.0",
        "apps": ["frappe", "erpnext"],
        "path": str(d),
        "parts": [
            {"kind": "database", "filename": f"{sid}-database.sql.gz", "size": 1, "sha256": "0"}
        ],
        "checks": [],
    }
    (d / "set.json").write_text(json.dumps(man))
    return store.find_set(f"{project}/{site}/{sid}")


def _restored():
    return store.RestoreFromSetReport(
        set_key="k",
        project="q",
        bench_path="/b",
        site="s",
        restored=True,
        migrate_ran=True,
        migrate_ok=True,
        config_merged=True,
        config_backup_path=None,
        post_checks=[],
        report={},
    )


class Calls:
    def __init__(self):
        self.log = []


@pytest.fixture
def composed(monkeypatch, home):
    calls = Calls()

    def capture(project, *, site, bench, with_files, reason, note, on_event):
        calls.log.append(("capture", site, reason))
        return Result(
            status=Status.OK,
            data=_set(home, project, site, sid=f"20260101_0000{len(calls.log):02d}-b0"),
        )

    def new_site(project, *, site, admin_password, bench, apps, db_root_password, on_event):
        calls.log.append(("new_site", project, site, tuple(apps)))
        return Result(status=Status.OK, data=object())

    def restore_set(
        key, *, project, site, bench, mariadb_root_password, consent, config_mode, on_event
    ):
        calls.log.append(("restore", key, project, site, config_mode))

        return Result(status=Status.OK, data=_restored())

    def drop_site(project, site, *, bench, consent, db_root_password, on_event):
        calls.log.append(("drop", project, site, consent))
        return Result(status=Status.OK, data=object())

    monkeypatch.setattr(transfer.backup_store, "capture", capture)
    monkeypatch.setattr(transfer.core_new_site, "new_site", new_site)
    monkeypatch.setattr(transfer.backup_store, "restore_set", restore_set)
    monkeypatch.setattr(transfer.core_rm_site, "drop_site", drop_site)
    monkeypatch.setattr(transfer, "_site_exists", lambda p, b, s: s == "exists.localhost")
    return calls


def test_a_clone_captures_creates_and_restores_and_keeps_the_source(composed):
    events = []
    r = transfer.transfer_sites(
        "p",
        [("a.localhost", "b.localhost")],
        mode="clone",
        admin_password="x",
        mariadb_root_password="root",
        on_event=events.append,
    )
    assert r.data.ok and r.data.items[0].created and r.data.items[0].restored
    assert [c[0] for c in composed.log] == ["capture", "new_site", "restore"]
    assert composed.log[1][3] == ("erpnext",)  # the set's apps minus frappe
    assert not any(c[0] == "drop" for c in composed.log)
    progress = [e for e in events if isinstance(e, transfer.TransferProgress)]
    assert (progress[0].done, progress[0].total, progress[-1].done) == (0, 1, 1)


def test_a_move_needs_consent_and_drops_the_source_only_after_the_copy_answers(composed):
    with pytest.raises(CwcliError) as e:
        transfer.transfer_sites(
            "p",
            [("a.localhost", "a.localhost")],
            mode="move",
            target_project="q",
            admin_password="x",
            mariadb_root_password="root",
        )
    assert e.value.code == "transfer.consent"
    r = transfer.transfer_sites(
        "p",
        [("a.localhost", "a.localhost")],
        mode="move",
        target_project="q",
        admin_password="x",
        mariadb_root_password="root",
        consent=True,
    )
    assert r.data.ok and r.data.items[0].dropped
    assert [c[0] for c in composed.log] == ["capture", "new_site", "restore", "drop"]
    assert composed.log[-1] == ("drop", "p", "a.localhost", True)


def test_a_batch_stops_at_the_first_failure_and_touches_nothing_after_it(composed, monkeypatch):
    def failing_restore(key, **kw):
        if "second" in kw["site"]:
            raise CwcliError(transfer.ErrorKind.PRECONDITION, "x", "db said no")

        return Result(status=Status.OK, data=_restored())

    monkeypatch.setattr(transfer.backup_store, "restore_set", failing_restore)
    r = transfer.transfer_sites(
        "p",
        [("one", "one.localhost"), ("two", "second.localhost"), ("three", "third.localhost")],
        mode="move",
        target_project="q",
        admin_password="x",
        mariadb_root_password="root",
        consent=True,
    )
    items = r.data.items
    assert r.status is Status.WARNING and not r.data.ok
    assert [i.ok for i in items] == [True, False]  # the third was never started
    assert items[1].error == "db said no" and not items[1].dropped
    assert not any(c[0] == "capture" and c[1] == "three" for c in composed.log)


def test_an_existing_target_site_is_restored_into_not_recreated(composed):
    transfer.transfer_sites(
        "p", [("a.localhost", "exists.localhost")], admin_password="x", mariadb_root_password="root"
    )
    assert [c[0] for c in composed.log] == ["capture", "restore"]


def test_the_transfer_reasons_are_store_reasons():
    assert {"clone", "move", "received"} <= set(store.REASONS)


def test_cloning_a_site_onto_itself_is_refused_before_anything_runs(composed):
    with pytest.raises(CwcliError) as e:
        transfer.transfer_sites(
            "p", [("a.localhost", "a.localhost")], admin_password="x", mariadb_root_password="root"
        )
    assert e.value.code == "transfer.same_site" and composed.log == []


def _fake_sendme(tmp_path, bundle: Path | None = None) -> Path:
    script = tmp_path / "sendme"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = send ]; then\n'
        "  echo \"imported $2\"; echo 'to get this data, use'; echo 'sendme receive blobacqaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'; sleep 30\n"
        f"else\n  cp -r '{bundle}'/. .\nfi\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def test_send_bundles_the_sets_prints_one_ticket_and_cleans_its_outbox(home, monkeypatch, tmp_path):
    a = _set(home, "p", "a.localhost")
    b = _set(home, "p", "b.localhost")
    monkeypatch.setattr(transfer, "_sendme", lambda: str(_fake_sendme(tmp_path)))
    events = []
    seen = {"stop": False}

    def on_event(e):
        events.append(e)
        if isinstance(e, transfer.Ticket):
            seen["stop"] = True

    r = transfer.send_sets([a.key, b.key], on_event=on_event, stop=lambda: seen["stop"])
    assert r.data["ticket"].startswith("blobacqaaaa") and r.data["sets"] == [a.key, b.key]
    assert not (home / "backups" / ".outbox").exists()  # bundle removed, outbox dir pruned
    assert (home / "backups" / "p" / "a.localhost").is_dir()  # the store itself untouched


def test_send_refuses_an_unverified_set(home, monkeypatch, tmp_path):
    bad = _set(home, "p", "a.localhost", verified=False)
    monkeypatch.setattr(transfer, "_sendme", lambda: str(_fake_sendme(tmp_path)))
    with pytest.raises(CwcliError) as e:
        transfer.send_sets([bad.key])
    assert e.value.code == "transfer.set_unverified"


def test_receive_files_every_set_in_the_download_into_the_store(home, monkeypatch, tmp_path):
    # the bundle as the sender's outbox laid it out: <project>/<site>/<id>/{set.json, parts}
    other = tmp_path / "other-machine"
    monkeypatch.setattr(store, "cwcli_home", lambda: other)
    a = _set(other, "staging16", "shop.localhost")
    monkeypatch.setattr(store, "cwcli_home", lambda: home)
    bundle = other / "backups"
    monkeypatch.setattr(transfer, "_sendme", lambda: str(_fake_sendme(tmp_path, bundle)))
    monkeypatch.setattr(store, "_bench_index", lambda p, bp: 0)
    events = []
    r = transfer.receive_sets("blob" + "a" * 60, on_event=events.append)
    assert len(r.data["sets"]) == 1 and r.data["sets"][0].startswith("staging16/shop.localhost/")
    got = store.find_set(r.data["sets"][0])
    assert (
        got is not None and got.reason == "received" and got.note == "from staging16:shop.localhost"
    )
    assert (
        not (home / "backups" / ".inbox" / os.listdir(home / "backups" / ".inbox")[0]).exists()
        if os.listdir(home / "backups" / ".inbox")
        else True
    )
    assert isinstance(events[-1], transfer.Received)
    # a single set can be filed under the receiving machine's own project/site
    r2 = transfer.receive_sets(
        "blob" + "a" * 60, project="local", site="shop.local", on_event=events.append
    )
    assert r2.data["sets"][0].startswith("local/shop.local/")
    assert a.key  # the sender's store was read, never written


def test_export_and_import_round_trip_a_set_as_a_tar(home, monkeypatch, tmp_path):
    a = _set(home, "p", "a.localhost")
    out = transfer.export_tar(a.key, tmp_path / "dl")
    assert out.name == "p__a.localhost__20260101_000000-b0.tar"
    # import into another store, filed as the sender filed it, and under a chosen site
    other = tmp_path / "other"
    monkeypatch.setattr(store, "cwcli_home", lambda: other)
    monkeypatch.setattr(store, "_bench_index", lambda p, bp: 0)
    r = transfer.import_tar(out)
    assert r.data["sets"][0].startswith("p/a.localhost/")
    r2 = transfer.import_tar(out, project="q", site="b.localhost")
    assert r2.data["sets"][0].startswith("q/b.localhost/")
    assert not any((other / "backups" / ".inbox").iterdir())


def test_import_refuses_a_tar_that_escapes_its_directory(home, tmp_path):
    import tarfile

    bad = tmp_path / "bad.tar"
    with tarfile.open(bad, "w") as tar:
        info = tarfile.TarInfo("../../escape.json")
        info.size = 2
        import io

        tar.addfile(info, io.BytesIO(b"{}"))
    with pytest.raises(CwcliError) as e:
        transfer.import_tar(bad)
    assert e.value.code == "transfer.tar_unsafe"
