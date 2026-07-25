"""Tests for the label DB layer (schema migration, set/get) and the ``label``
command (list / set / clear, validation, stopped-container guard).

The DB tests use a throwaway SQLite file so they never touch the real cache; the
peewee ``db`` object is re-pointed for the fixture's lifetime and restored after.
"""

import pytest
import typer

from caffeinated_whale_cli.commands import label as label_mod
from caffeinated_whale_cli.core import docker as core_docker
from caffeinated_whale_cli.core import label as core_label
from caffeinated_whale_cli.core import resolvers
from caffeinated_whale_cli.core.errors import CwcliError
from caffeinated_whale_cli.utils import db_utils, docker_utils
from tests.bench_fakes import MarkerFakeContainer

BENCH_A = "/workspace/frappe-bench"
BENCH_B = "/workspace/frappe-bench-2"
BENCH_EARLIER = "/workspace/aaa-bench"
MARKER_A = f"{BENCH_A}/.cwcli/.bench-label"
MARKER_B = f"{BENCH_B}/.cwcli/.bench-label"


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """Point db_utils at a throwaway sqlite file, restoring the real one after."""
    orig_path = db_utils.DB_PATH
    dbfile = tmp_path / "cache.db"
    monkeypatch.setattr(db_utils, "DB_PATH", dbfile)
    if not db_utils.db.is_closed():
        db_utils.db.close()
    db_utils.db.init(str(dbfile))
    db_utils.initialize_database()
    yield dbfile
    if not db_utils.db.is_closed():
        db_utils.db.close()
    # Restore the real DB binding so later tests in the session are unaffected.
    db_utils.db.init(str(orig_path))


def _seed_two_benches(labels=(None, None)):
    db_utils.cache_project_data(
        "proj",
        [
            {"path": BENCH_A, "sites": [], "available_apps": [], "label": labels[0]},
            {"path": BENCH_B, "sites": [], "available_apps": [], "label": labels[1]},
        ],
    )


# --------------------------------------------------------------------------- DB


class TestLabelDB:
    def test_cache_and_get_roundtrips_label(self, temp_db):
        _seed_two_benches(labels=(None, "staging"))
        data = db_utils.get_cached_project_data("proj")
        benches = data["bench_instances"]
        # Order is stable (by id / sorted discovery order) and label surfaces only
        # when set.
        assert benches[0]["path"] == BENCH_A and "label" not in benches[0]
        assert benches[1]["label"] == "staging"

    def test_set_bench_label_updates_one_bench(self, temp_db):
        _seed_two_benches()
        assert db_utils.set_bench_label("proj", BENCH_B, "prod") is True
        data = db_utils.get_cached_project_data("proj")
        assert data["bench_instances"][1]["label"] == "prod"
        assert "label" not in data["bench_instances"][0]

    def test_set_bench_label_clear(self, temp_db):
        _seed_two_benches(labels=(None, "staging"))
        assert db_utils.set_bench_label("proj", BENCH_B, None) is True
        data = db_utils.get_cached_project_data("proj")
        assert "label" not in data["bench_instances"][1]

    def test_set_bench_label_unknown_returns_false(self, temp_db):
        _seed_two_benches()
        assert db_utils.set_bench_label("proj", "/nope", "x") is False

    def test_prior_index_still_resolves_the_same_bench_after_an_earlier_add(self, temp_db):
        """A stored numeric bench identity must never silently change targets."""
        db_utils.cache_project_data(
            "proj",
            [{"path": BENCH_A, "sites": [], "available_apps": []}],
        )
        before = core_label.list_benches("proj", verify=False)
        assert before.data is not None
        prior_index = before.data.benches[0].index

        # This is the ordering that caused the live defect: full discovery returns
        # sorted paths, so a newly-added earlier path is written before the bench
        # whose numeric identity a caller already holds.
        db_utils.cache_project_data(
            "proj",
            [
                {"path": BENCH_EARLIER, "sites": [], "available_apps": []},
                {"path": BENCH_A, "sites": [], "available_apps": []},
            ],
        )

        # Positive first: the prior identity still selects the original bench.
        resolved = resolvers.resolve_bench("proj", str(prior_index), None)
        assert resolved is not None
        assert resolved.data == BENCH_A

        # The earlier path is really there, but it owns a different identity.
        after = core_label.list_benches("proj", verify=False)
        assert after.data is not None
        assert {b.path for b in after.data.benches} == {BENCH_A, BENCH_EARLIER}
        earlier = next(b for b in after.data.benches if b.path == BENCH_EARLIER)
        assert earlier.index != prior_index

    def test_list_position_is_also_stable_across_an_earlier_add(self, temp_db):
        """A held LIST POSITION must not retarget either.

        ``inspect --json`` and the TOON bench rows are arrays, so ``benches[0]`` is
        itself a reference a caller can hold. Serving in durable-identity order
        (rather than sorted-path discovery order) makes a new bench append instead
        of displacing the rows in front of it.
        """
        db_utils.cache_project_data(
            "proj",
            [{"path": BENCH_A, "sites": [], "available_apps": []}],
        )

        db_utils.cache_project_data(
            "proj",
            [
                {"path": BENCH_EARLIER, "sites": [], "available_apps": []},
                {"path": BENCH_A, "sites": [], "available_apps": []},
            ],
        )

        # Positive first: position 0 is still the bench it was before the add.
        served = db_utils.get_cached_project_data("proj")
        assert served is not None
        assert served["bench_instances"][0]["path"] == BENCH_A
        # The earlier-sorting bench appended rather than displacing it.
        assert served["bench_instances"][1]["path"] == BENCH_EARLIER
        assert [b["index"] for b in served["bench_instances"]] == [0, 1]

        listed = core_label.list_benches("proj", verify=False).data
        assert listed is not None
        assert [b.path for b in listed.benches] == [BENCH_A, BENCH_EARLIER]

    def test_removed_identity_is_never_reused_for_another_path(self, temp_db):
        _seed_two_benches()
        original = core_label.list_benches("proj", verify=False).data
        assert original is not None
        removed_index = next(b.index for b in original.benches if b.path == BENCH_B)

        db_utils.cache_project_data(
            "proj",
            [
                {"path": BENCH_EARLIER, "sites": [], "available_apps": []},
                {"path": BENCH_A, "sites": [], "available_apps": []},
            ],
        )

        current = core_label.list_benches("proj", verify=False).data
        assert current is not None
        earlier = next(b for b in current.benches if b.path == BENCH_EARLIER)
        assert earlier.index != removed_index
        with pytest.raises(CwcliError) as exc:
            resolvers.resolve_bench("proj", str(removed_index), None)
        assert "No bench" in str(exc.value)

    @pytest.mark.parametrize("scope", ["project", "all"])
    def test_identity_survives_explicit_cache_clear(self, temp_db, scope):
        db_utils.cache_project_data(
            "proj",
            [{"path": BENCH_A, "sites": [], "available_apps": []}],
        )
        prior = core_label.list_benches("proj", verify=False).data
        assert prior is not None
        prior_index = prior.benches[0].index

        if scope == "project":
            db_utils.clear_cache_for_project("proj")
        else:
            db_utils.clear_all_cache()
        db_utils.cache_project_data(
            "proj",
            [
                {"path": BENCH_EARLIER, "sites": [], "available_apps": []},
                {"path": BENCH_A, "sites": [], "available_apps": []},
            ],
        )

        resolved = resolvers.resolve_bench("proj", str(prior_index), None)
        assert resolved is not None
        assert resolved.data == BENCH_A

    def test_upgrade_backfill_preserves_the_old_positional_meaning(self, temp_db):
        _seed_two_benches()
        db_utils.BenchIdentity.delete().execute()

        db_utils._backfill_bench_identities()

        identities = {row.path: row.numeric_id for row in db_utils.BenchIdentity.select()}
        assert identities == {BENCH_A: 0, BENCH_B: 1}

    def test_migration_adds_label_column_to_old_cache(self, temp_db):
        # Simulate a pre-label cache: drop the column by recreating an old table.
        db_utils.db.execute_sql("DROP TABLE bench")
        db_utils.db.execute_sql(
            "CREATE TABLE bench (id INTEGER PRIMARY KEY, project_id INTEGER, path VARCHAR)"
        )
        cols = {r[1] for r in db_utils.db.execute_sql("PRAGMA table_info(bench)").fetchall()}
        assert "label" not in cols

        db_utils._migrate_bench_label_column()

        cols = {r[1] for r in db_utils.db.execute_sql("PRAGMA table_info(bench)").fetchall()}
        assert "label" in cols
        # Idempotent: a second run is a no-op (no exception).
        db_utils._migrate_bench_label_column()


# ---------------------------------------------------------------------- command


def _run_label(monkeypatch, container, **kwargs):
    """Invoke the label command with docker checks + container wired to fakes."""
    monkeypatch.setattr(docker_utils.shutil, "which", lambda _name: "/usr/bin/docker")

    class _Client:
        def ping(self):
            return True

    monkeypatch.setattr(docker_utils.docker, "from_env", lambda: _Client())
    monkeypatch.setattr(core_docker, "get_frappe_container", lambda name: container)

    params = dict(
        project_name="proj",
        bench_selector=None,
        new_label=None,
        clear=False,
        verbose=False,
    )
    params.update(kwargs)
    return label_mod.label(**params)


class TestLabelCommand:
    def _list_mode(self, monkeypatch, container):
        monkeypatch.setattr(docker_utils.shutil, "which", lambda _n: "/usr/bin/docker")
        monkeypatch.setattr(
            docker_utils.docker, "from_env", lambda: type("C", (), {"ping": lambda s: True})()
        )
        monkeypatch.setattr(core_docker, "get_frappe_container", lambda name: container)
        label_mod.label(
            project_name="proj",
            bench_selector=None,
            new_label=None,
            clear=False,
            verbose=False,
        )

    def test_list_mode_reports_live_benches(self, temp_db, monkeypatch, capsys):
        # This REVERSES the old "list mode must not touch the container" assertion.
        # Touching no container is precisely what let this listing keep vouching
        # for a bench whose directory had been removed; the cheap probe is the fix,
        # and the positive case comes first - a live bench is still listed, in full,
        # and carries no scary marker.
        _seed_two_benches(labels=(None, "staging"))
        self._list_mode(monkeypatch, MarkerFakeContainer())

        out = capsys.readouterr().out
        assert "[0]" in out and "[1]" in out and "staging" in out
        assert "GONE" not in out

    def test_list_mode_marks_a_removed_bench_gone(self, temp_db, monkeypatch, capsys):
        _seed_two_benches(labels=(None, "staging"))
        self._list_mode(monkeypatch, MarkerFakeContainer(present_paths={BENCH_A}))

        out = capsys.readouterr().out
        # Still listed (never silently pruned), but no longer vouched for.
        assert BENCH_B in out
        assert "GONE" in out

    def test_list_mode_still_answers_with_no_container_to_ask(self, temp_db, monkeypatch, capsys):
        # The verb that tells you which --bench to pass to `cwcli start` must keep
        # working on a stopped project; it just cannot claim `present`.
        _seed_two_benches(labels=(None, "staging"))
        stopped = MarkerFakeContainer()
        stopped.status = "exited"
        self._list_mode(monkeypatch, stopped)

        out = capsys.readouterr().out
        assert "[0]" in out and "[1]" in out and "staging" in out
        assert "not verified" in out

    def test_set_label_writes_db_and_marker(self, temp_db, monkeypatch):
        _seed_two_benches()
        container = MarkerFakeContainer(bench_path=BENCH_B)
        _run_label(monkeypatch, container, bench_selector="1", new_label="staging")
        # DB updated...
        data = db_utils.get_cached_project_data("proj")
        assert data["bench_instances"][1]["label"] == "staging"
        # ...and the marker file was written inside the container.
        assert MARKER_B in container.fs
        assert bench_label_from_marker(container, MARKER_B) == "staging"

    def test_set_label_by_existing_label_selector(self, temp_db, monkeypatch):
        _seed_two_benches(labels=(None, "staging"))
        container = MarkerFakeContainer(bench_path=BENCH_B)
        _run_label(monkeypatch, container, bench_selector="staging", new_label="prod")
        data = db_utils.get_cached_project_data("proj")
        assert data["bench_instances"][1]["label"] == "prod"

    def test_clear_label_removes_db_and_marker(self, temp_db, monkeypatch):
        _seed_two_benches(labels=(None, "staging"))
        container = MarkerFakeContainer(
            bench_path=BENCH_B, fs={MARKER_B: b'{"schema":1,"label":"staging"}'}
        )
        _run_label(monkeypatch, container, bench_selector="staging", clear=True)
        data = db_utils.get_cached_project_data("proj")
        assert "label" not in data["bench_instances"][1]
        assert MARKER_B not in container.fs

    def test_clear_marker_failure_leaves_db_label_intact(self, temp_db, monkeypatch, capsys):
        # If the marker removal fails, the DB label must NOT be cleared: the marker
        # is the source of truth, so a cleared DB + surviving marker would let a
        # later full inspect resurrect the "cleared" label. Command must exit non-zero.
        _seed_two_benches(labels=(None, "staging"))
        container = MarkerFakeContainer(
            bench_path=BENCH_B, fs={MARKER_B: b'{"schema":1,"label":"staging"}'}
        )
        monkeypatch.setattr(core_label.bench_labels, "clear_label_marker", lambda *a, **k: False)
        with pytest.raises(typer.Exit) as exc:
            _run_label(monkeypatch, container, bench_selector="staging", clear=True)
        assert exc.value.exit_code == 1
        # A clear error message is surfaced (not a silent exit).
        assert "Error:" in capsys.readouterr().err
        # DB label preserved (not cleared) and the marker still present.
        data = db_utils.get_cached_project_data("proj")
        assert data["bench_instances"][1]["label"] == "staging"
        assert MARKER_B in container.fs

    def test_clear_db_failure_prints_error(self, temp_db, monkeypatch, capsys):
        # Marker removal succeeds but the DB update returns False: the command must
        # not exit silently - it prints an Error explaining the marker was removed
        # but the cache label could not be cleared, then exits non-zero.
        _seed_two_benches(labels=(None, "staging"))
        container = MarkerFakeContainer(
            bench_path=BENCH_B, fs={MARKER_B: b'{"schema":1,"label":"staging"}'}
        )
        monkeypatch.setattr(core_label.db_utils, "set_bench_label", lambda *a, **k: False)
        with pytest.raises(typer.Exit) as exc:
            _run_label(monkeypatch, container, bench_selector="staging", clear=True)
        assert exc.value.exit_code == 1
        err = capsys.readouterr().err
        assert "Error:" in err
        # The marker was actually removed before the DB step ran.
        assert MARKER_B not in container.fs

    def test_reject_numeric_label(self, temp_db, monkeypatch):
        """`label` refuses a numeric label and writes nothing."""
        _seed_two_benches()
        container = MarkerFakeContainer(bench_path=BENCH_B)
        with pytest.raises(typer.Exit) as exc:
            _run_label(monkeypatch, container, bench_selector="1", new_label="2")
        assert exc.value.exit_code == 1
        # Nothing written.
        assert MARKER_B not in container.fs

    def test_reject_duplicate_label(self, temp_db, monkeypatch):
        """`label` refuses a label already used by another bench."""
        _seed_two_benches(labels=("staging", None))
        container = MarkerFakeContainer(bench_path=BENCH_B)
        with pytest.raises(typer.Exit) as exc:
            _run_label(monkeypatch, container, bench_selector="1", new_label="staging")
        assert exc.value.exit_code == 1

    def test_unknown_selector_errors(self, temp_db, monkeypatch):
        """`label` exits 1 on an unknown bench selector."""
        _seed_two_benches()
        container = MarkerFakeContainer(bench_path=BENCH_B)
        with pytest.raises(typer.Exit) as exc:
            _run_label(monkeypatch, container, bench_selector="99", new_label="x")
        assert exc.value.exit_code == 1

    @pytest.mark.parametrize(
        ("container", "marker"),
        [
            (MarkerFakeContainer(present_paths={BENCH_A}), "GONE"),
            pytest.param(
                MarkerFakeContainer(),
                "not verified",
                id="stopped-container",
            ),
        ],
    )
    def test_unknown_selector_available_benches_preserve_state(
        self, temp_db, monkeypatch, capsys, container, marker
    ):
        _seed_two_benches()
        if marker == "not verified":
            container.status = "exited"

        with pytest.raises(typer.Exit) as exc:
            _run_label(monkeypatch, container, bench_selector="99", new_label="x")

        assert exc.value.exit_code == 1
        err = capsys.readouterr().err
        assert "Available benches" in err
        assert BENCH_B in err
        assert marker in err

    def test_unknown_selector_without_new_label_reports_unknown_selector(
        self, temp_db, monkeypatch, capsys
    ):
        """An unresolved selector wins over the missing-new-label error, not the
        reverse: the real problem is the selector, so that is what must surface."""
        _seed_two_benches()
        container = MarkerFakeContainer(bench_path=BENCH_B)
        with pytest.raises(typer.Exit) as exc:
            _run_label(monkeypatch, container, bench_selector="99")
        assert exc.value.exit_code == 1
        err = capsys.readouterr().err
        assert "No bench '99' in project 'proj'" in err
        assert "Available benches" in err
        assert "Provide a new label" not in err

    def test_stopped_container_refuses_set(self, temp_db, monkeypatch):
        _seed_two_benches()
        container = MarkerFakeContainer(bench_path=BENCH_B)
        container.status = "exited"
        with pytest.raises(typer.Exit) as exc:
            _run_label(monkeypatch, container, bench_selector="1", new_label="staging")
        assert exc.value.exit_code == 1
        # No DB change, no marker write.
        data = db_utils.get_cached_project_data("proj")
        assert "label" not in data["bench_instances"][1]
        assert MARKER_B not in container.fs


def bench_label_from_marker(container, marker_path):
    import json

    return json.loads(container.fs[marker_path].decode())["label"]
