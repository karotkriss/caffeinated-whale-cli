"""Tests for the label DB layer (schema migration, set/get) and the ``label``
command (list / set / clear, validation, stopped-container guard).

The DB tests use a throwaway SQLite file so they never touch the real cache; the
peewee ``db`` object is re-pointed for the fixture's lifetime and restored after.
"""

import pytest
import typer

from caffeinated_whale_cli.commands import label as label_mod
from caffeinated_whale_cli.utils import db_utils, docker_utils
from tests.bench_fakes import MarkerFakeContainer

BENCH_A = "/workspace/frappe-bench"
BENCH_B = "/workspace/frappe-bench-2"
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
    monkeypatch.setattr(label_mod, "get_frappe_container", lambda name: container)

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
    def test_list_mode_no_container_needed(self, temp_db, monkeypatch, capsys):
        _seed_two_benches(labels=(None, "staging"))

        # get_frappe_container should NOT be called in list mode; make it explode.
        def _boom(name):
            raise AssertionError("list mode must not touch the container")

        monkeypatch.setattr(docker_utils.shutil, "which", lambda _n: "/usr/bin/docker")
        monkeypatch.setattr(
            docker_utils.docker, "from_env", lambda: type("C", (), {"ping": lambda s: True})()
        )
        monkeypatch.setattr(label_mod, "get_frappe_container", _boom)
        label_mod.label(
            project_name="proj",
            bench_selector=None,
            new_label=None,
            clear=False,
            verbose=False,
        )
        out = capsys.readouterr().out
        assert "[0]" in out and "[1]" in out and "staging" in out

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
        monkeypatch.setattr(label_mod.bench_labels, "clear_label_marker", lambda *a, **k: False)
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
        monkeypatch.setattr(label_mod.db_utils, "set_bench_label", lambda *a, **k: False)
        with pytest.raises(typer.Exit) as exc:
            _run_label(monkeypatch, container, bench_selector="staging", clear=True)
        assert exc.value.exit_code == 1
        err = capsys.readouterr().err
        assert "Error:" in err
        # The marker was actually removed before the DB step ran.
        assert MARKER_B not in container.fs

    def test_reject_numeric_label(self, temp_db, monkeypatch):
        _seed_two_benches()
        container = MarkerFakeContainer(bench_path=BENCH_B)
        with pytest.raises(typer.Exit) as exc:
            _run_label(monkeypatch, container, bench_selector="1", new_label="2")
        assert exc.value.exit_code == 1
        # Nothing written.
        assert MARKER_B not in container.fs

    def test_reject_duplicate_label(self, temp_db, monkeypatch):
        _seed_two_benches(labels=("staging", None))
        container = MarkerFakeContainer(bench_path=BENCH_B)
        with pytest.raises(typer.Exit) as exc:
            _run_label(monkeypatch, container, bench_selector="1", new_label="staging")
        assert exc.value.exit_code == 1

    def test_unknown_selector_errors(self, temp_db, monkeypatch):
        _seed_two_benches()
        container = MarkerFakeContainer(bench_path=BENCH_B)
        with pytest.raises(typer.Exit) as exc:
            _run_label(monkeypatch, container, bench_selector="99", new_label="x")
        assert exc.value.exit_code == 1

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
