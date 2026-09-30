"""The post-mutation recache re-reads only the bench the verb changed.

``core.inspect.refresh_bench`` (reached through ``cache.recache_project(...,
bench_path=...)``) splices one freshly read bench into the cached project: replaced
when it is still a bench, dropped when it is gone, appended when it is new. These
run against a real throwaway SQLite cache, so the durable ``BenchIdentity``
numbering and the redacting write are the production ones, and against
``MultiBenchContainer`` so every read the recache issues is recorded.
"""

from __future__ import annotations

import json

import pytest

from caffeinated_whale_cli.core import bench_read
from caffeinated_whale_cli.core import docker as core_docker
from caffeinated_whale_cli.core import inspect as core_inspect
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from caffeinated_whale_cli.utils import cache, config_utils, db_utils
from tests.test_inspect_characterization import BENCH_A, BENCH_B, ROOT, MultiBenchContainer

BENCH_C = f"{ROOT}/bench-c"


@pytest.fixture()
def container(tmp_path, monkeypatch):
    """A running two-bench instance over a throwaway cache DB."""
    orig_path = db_utils.DB_PATH
    dbfile = tmp_path / "cache.db"
    monkeypatch.setattr(db_utils, "DB_PATH", dbfile)
    if not db_utils.db.is_closed():
        db_utils.db.close()
    db_utils.db.init(str(dbfile))
    db_utils.initialize_database()
    monkeypatch.setattr(config_utils, "load_config", lambda: {})

    fake = MultiBenchContainer()
    monkeypatch.setattr(core_docker, "get_project_containers", lambda name: [fake])
    yield fake
    if not db_utils.db.is_closed():
        db_utils.db.close()
    db_utils.db.init(str(orig_path))


def _cached() -> dict[str, dict]:
    data = db_utils.get_cached_project_data("proj") or {}
    return {b["path"]: b for b in data.get("bench_instances", [])}


def _read_paths(fake) -> list[list[str]]:
    """The bench paths each batched full read was asked for."""
    return [list(call[6:]) for call in fake.batched_reads]


def _record_batched_reads(fake, monkeypatch):
    fake.batched_reads = []
    real = fake.exec_run

    def exec_run(cmd, workdir=None, environment=None):
        if isinstance(cmd, list) and cmd[2:3] == [bench_read._READ_SH]:
            fake.batched_reads.append(cmd)
        return real(cmd, workdir=workdir, environment=environment)

    monkeypatch.setattr(fake, "exec_run", exec_run)


def _full_inspect_then_record(fake, monkeypatch):
    core_inspect.inspect("proj", refresh="full", offer_choice=False)
    _record_batched_reads(fake, monkeypatch)


def test_only_the_touched_bench_is_read_and_the_others_keep_their_cache(container, monkeypatch):
    _full_inspect_then_record(container, monkeypatch)
    identities = {path: b["index"] for path, b in _cached().items()}
    # Both benches change live; only bench A is the one the verb touched.
    container.benches[BENCH_A]["apps"].append("hrms")
    container.benches[BENCH_B]["apps"].append("payments")

    result = core_inspect.refresh_bench("proj", BENCH_A)

    assert result.data == "bench"
    assert _read_paths(container) == [[BENCH_A]]
    cached = _cached()
    assert cached[BENCH_A]["available_apps"] == ["erpnext", "frappe", "hrms"]
    assert cached[BENCH_B]["available_apps"] == ["frappe"]  # not re-read
    assert cached[BENCH_B]["label"] == "staging"
    assert {path: b["index"] for path, b in cached.items()} == identities


def test_a_spliced_bench_is_byte_identical_to_a_full_inspect_of_it(container, monkeypatch):
    _full_inspect_then_record(container, monkeypatch)
    container.benches[BENCH_A]["sites"]["c.localhost"] = ["frappe 15.0.0 version-15"]
    container.benches[BENCH_A]["site_configs"]["c.localhost"] = {"db_name": "cdb"}

    core_inspect.refresh_bench("proj", BENCH_A)
    spliced = _cached()

    core_inspect.inspect("proj", refresh="full", offer_choice=False)
    assert json.dumps(_cached(), sort_keys=False) == json.dumps(spliced, sort_keys=False)


def test_a_removed_bench_is_dropped_and_its_number_is_never_reused(container, monkeypatch):
    _full_inspect_then_record(container, monkeypatch)
    index_b = _cached()[BENCH_B]["index"]
    del container.benches[BENCH_A]

    result = core_inspect.refresh_bench("proj", BENCH_A)

    assert result.data == "bench"
    assert _read_paths(container) == []  # a gone bench needs no read
    assert list(_cached()) == [BENCH_B]
    assert _cached()[BENCH_B]["index"] == index_b

    container.benches[BENCH_C] = dict(container.benches[BENCH_B], apps=["frappe"])
    core_inspect.refresh_bench("proj", BENCH_C)
    # Bench A held 0 and B holds 1: A's number stays a tombstone, so C takes 2.
    assert (index_b, _cached()[BENCH_C]["index"]) == (1, 2)


def test_a_new_bench_is_appended_with_the_next_number(container, monkeypatch):
    _full_inspect_then_record(container, monkeypatch)
    before = _cached()
    container.benches[BENCH_C] = {
        "apps": ["frappe"],
        "sites": {"c.localhost": ["frappe 15.0.0 version-15"]},
        "site_configs": {},
        "common_site_config": None,
        "current_site": None,
    }

    result = core_inspect.refresh_bench("proj", BENCH_C)

    assert result.data == "bench"
    assert _read_paths(container) == [[BENCH_C]]
    after = _cached()
    assert after[BENCH_C]["index"] == max(b["index"] for b in before.values()) + 1
    assert {p: after[p] for p in before} == before


def test_a_bench_set_change_elsewhere_escalates_to_a_full_inspect(container, monkeypatch):
    _full_inspect_then_record(container, monkeypatch)
    del container.benches[BENCH_B]  # gone, but the verb touched bench A

    result = core_inspect.refresh_bench("proj", BENCH_A)

    assert result.data == "full"
    assert list(_cached()) == [BENCH_A]


def test_a_path_discovery_never_produces_runs_the_full_inspect(container, monkeypatch):
    """A trailing slash (say, from --path) names bench A but matches no cached row;
    splicing it would refresh nothing and leave bench A stale."""
    _full_inspect_then_record(container, monkeypatch)

    result = core_inspect.refresh_bench("proj", BENCH_A + "/")

    assert result.data == "full"
    assert _read_paths(container) == [[BENCH_A, BENCH_B]]


def test_no_cached_project_runs_the_full_inspect(container, monkeypatch):
    _record_batched_reads(container, monkeypatch)

    result = core_inspect.refresh_bench("proj", BENCH_A)

    assert result.data == "full"
    assert _read_paths(container) == [[BENCH_A, BENCH_B]]
    assert set(_cached()) == {BENCH_A, BENCH_B}


def test_a_stopped_project_raises_and_leaves_the_cache_untouched(container, monkeypatch):
    _full_inspect_then_record(container, monkeypatch)
    before = _cached()
    container.status = "exited"

    with pytest.raises(CwcliError) as exc:
        core_inspect.refresh_bench("proj", BENCH_A)

    assert exc.value.kind is ErrorKind.NOT_RUNNING
    assert container.start_calls == 0
    assert _cached() == before


class TestRecacheProject:
    def test_bench_path_routes_to_the_scoped_recache(self, container, monkeypatch):
        _full_inspect_then_record(container, monkeypatch)

        assert cache.recache_project("proj", bench_path=BENCH_B) is True
        assert _read_paths(container) == [[BENCH_B]]

    def test_without_a_bench_path_the_whole_project_is_re_read(self, container, monkeypatch):
        _full_inspect_then_record(container, monkeypatch)

        assert cache.recache_project("proj") is True
        assert _read_paths(container) == [[BENCH_A, BENCH_B]]

    def test_a_failed_scoped_recache_clears_the_stale_cache(self, container, monkeypatch):
        _full_inspect_then_record(container, monkeypatch)
        container.status = "exited"

        assert cache.recache_project("proj", bench_path=BENCH_A) is False
        assert db_utils.get_cached_project_data("proj") is None
