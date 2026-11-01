"""Two cache writers at once both land (the web UI serves detail reads on threads).

``cache_project_data`` reads the bench identities and then writes. In a DEFERRED
transaction two overlapping writers each hold a read lock and then both need the write
lock, which SQLite resolves by failing one with "database is locked" at once: a cold
cache's instance page and bench page then raced, and the bench page showed "not in the
cache" instead of its tabs.
"""

from __future__ import annotations

import threading

import pytest

from caffeinated_whale_cli.utils import db_utils


@pytest.fixture()
def disk_db(tmp_path, monkeypatch):
    monkeypatch.setenv("CWCLI_HOME", str(tmp_path))
    monkeypatch.delenv("CWCLI_NO_CACHE", raising=False)
    dbfile = tmp_path / "cache" / "cwc-cache.db"
    dbfile.parent.mkdir(parents=True)
    orig = db_utils.DB_PATH
    monkeypatch.setattr(db_utils, "DB_PATH", dbfile)
    if not db_utils.db.is_closed():
        db_utils.db.close()
    db_utils.db.init(str(dbfile))
    db_utils.initialize_database()
    yield
    if not db_utils.db.is_closed():
        db_utils.db.close()
    db_utils.db.init(str(orig))


def test_overlapping_cache_writes_both_succeed(disk_db, monkeypatch):
    # Hold each writer just after its identity read until the other has read too. With
    # the write lock taken up front the second writer waits at BEGIN instead, so the
    # barrier times out and the first writer goes on alone.
    barrier = threading.Barrier(2, timeout=1)
    read = db_utils._ensure_bench_identities

    def held(*args):
        got = read(*args)
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return got

    monkeypatch.setattr(db_utils, "_ensure_bench_identities", held)
    errors: list[BaseException] = []

    def write():
        try:
            db_utils.cache_project_data(
                "proj", [{"path": "/workspace/frappe-bench", "available_apps": [], "sites": []}]
            )
        except BaseException as e:  # noqa: BLE001 - collected for the assertion
            errors.append(e)
        finally:
            db_utils.db.close()

    threads = [threading.Thread(target=write) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors, errors
    assert db_utils.get_cached_project_data("proj") is not None
