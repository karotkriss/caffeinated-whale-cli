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


def _kill_bench_read(fake, monkeypatch, dead: str) -> None:
    """Make ``dead``'s read process exit without a record, as a crash or OOM kill
    would, while every other bench in the same batched read is answered."""
    real = fake.exec_run

    def exec_run(cmd, workdir=None, environment=None):
        if isinstance(cmd, list) and cmd[2:3] == [bench_read._READ_SH] and dead in cmd[6:]:
            alive = [path for path in cmd[6:] if path != dead]
            _code, out = real(cmd[:6] + alive, workdir=workdir) if alive else (0, b"")
            return (0, out + f"\n{bench_read.FAILED}137\t{dead}\n".encode())
        return real(cmd, workdir=workdir, environment=environment)

    monkeypatch.setattr(fake, "exec_run", exec_run)


def _unread(bench: str) -> str:
    return f"Could not read bench {bench} (its read process exited with code 137)."


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


class TestABenchWithNoRecordNeverTakesDownTheOthers:
    """One bench whose read process dies is named with its cause; every other bench
    is still served and cached, and the dead bench's cached row is never
    overwritten (nor invented when it had none), on every full-read path."""

    def test_inspect_update(self, container, monkeypatch):
        core_inspect.inspect("proj", refresh="full", offer_choice=False)
        before = _cached()
        container.benches[BENCH_A]["apps"].append("hrms")
        _kill_bench_read(container, monkeypatch, BENCH_B)

        result = core_inspect.inspect("proj", refresh="full", offer_choice=False)

        assert result.data.served_from == "full"
        assert [w.text for w in result.warnings][0] == _unread(BENCH_B)
        assert "inspect.apps_unverified" in [w.code for w in result.warnings]
        verified = {
            b.path: {s.installed_apps_verified for s in b.sites} for b in result.data.benches
        }
        assert verified == {BENCH_A: {True}, BENCH_B: {False}}
        cached = _cached()
        assert cached[BENCH_A]["available_apps"] == ["erpnext", "frappe", "hrms"]
        assert cached[BENCH_B] == before[BENCH_B]

    def test_an_uncached_bench_is_not_cached(self, container, monkeypatch):
        _kill_bench_read(container, monkeypatch, BENCH_B)

        result = core_inspect.inspect("proj", refresh="full", offer_choice=False)

        assert [w.text for w in result.warnings] == [_unread(BENCH_B)]
        assert list(_cached()) == [BENCH_A]

    def test_a_drift_escalation(self, container, monkeypatch):
        core_inspect.inspect("proj", refresh="full", offer_choice=False)
        before = _cached()
        container.benches[BENCH_A]["apps"].append("hrms")
        _kill_bench_read(container, monkeypatch, BENCH_B)

        result = core_inspect.inspect("proj", offer_choice=False)

        assert result.data.served_from == "full"
        assert [w.code for w in result.warnings] == [
            "inspect.bench_unread",
            "inspect.apps_unverified",
        ]
        assert result.warnings[0].text == _unread(BENCH_B)
        assert _cached()[BENCH_A]["available_apps"] == ["erpnext", "frappe", "hrms"]
        assert _cached()[BENCH_B] == before[BENCH_B]

    def test_with_caching_disabled(self, container, monkeypatch):
        monkeypatch.setenv("CWCLI_NO_CACHE", "1")
        _kill_bench_read(container, monkeypatch, BENCH_B)

        cached = _cached()

        assert cached[BENCH_A]["available_apps"] == ["erpnext", "frappe"]
        assert BENCH_B not in cached

    def test_a_recache_that_falls_back_to_the_full_read(self, container, monkeypatch):
        core_inspect.inspect("proj", refresh="full", offer_choice=False)
        before = _cached()
        _kill_bench_read(container, monkeypatch, BENCH_B)
        warnings: list = []

        assert cache.recache_project("proj", warnings=warnings) is True
        assert [w.text for w in warnings] == [_unread(BENCH_B)]
        cached = _cached()
        assert cached[BENCH_A]["available_apps"] == ["erpnext", "frappe"]
        assert cached[BENCH_B] == before[BENCH_B]

    def test_refresh_bench_of_the_failing_bench(self, container, monkeypatch):
        _full_inspect_then_record(container, monkeypatch)
        before = _cached()
        _kill_bench_read(container, monkeypatch, BENCH_A)

        result = core_inspect.refresh_bench("proj", BENCH_A)

        assert result.data == "bench"
        assert [w.text for w in result.warnings] == [_unread(BENCH_A)]
        assert _cached() == before

    def test_apps_install_by_label_keeps_the_label_and_prints_the_warning(
        self, container, monkeypatch, capsys
    ):
        """``apps install --bench staging``; the touched bench's read then dies in the
        post-mutation recache. Its cached row (and so its label) survives, and the
        verb prints the warning naming the bench and the cause."""
        from caffeinated_whale_cli.commands import apps as apps_mod
        from caffeinated_whale_cli.core import apps as core_apps
        from caffeinated_whale_cli.core import resolvers
        from caffeinated_whale_cli.core.envelope import Result, Status

        core_inspect.inspect("proj", refresh="full", offer_choice=False)
        before = _cached()
        assert before[BENCH_B]["label"] == "staging"
        installed_on = []

        def fake_install(project, apps, *, bench_path, **_k):
            installed_on.append(bench_path)
            report = core_apps.AppsReport(
                project=project,
                bench_path=bench_path,
                results=[core_apps.AppResult(app="hrms", site=None, action="get-app", ok=True)],
                ok=True,
            )
            return Result(status=Status.OK, data=report)

        monkeypatch.setattr(apps_mod, "ensure_containers_running", lambda *a, **k: True)
        monkeypatch.setattr(apps_mod.core_apps, "install_apps", fake_install)
        _kill_bench_read(container, monkeypatch, BENCH_B)

        apps_mod.install_apps(
            "proj",
            ["hrms"],
            bench="staging",
            bench_path=None,
            sites=None,
            branch=None,
            fetch_only=True,
            if_not_present=False,
            json_output=False,
            yes=False,
            verbose=False,
        )

        assert installed_on == [BENCH_B]
        assert _cached()[BENCH_B] == before[BENCH_B]
        assert resolvers.resolve_bench("proj", "staging", None).data == BENCH_B
        err = " ".join(capsys.readouterr().err.split())
        assert f"Warning: {_unread(BENCH_B)}" in err
