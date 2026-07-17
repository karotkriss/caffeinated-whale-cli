"""``core.label`` - every branch, against faked container I/O and a temp DB.

`label` is built from the SAME primitives `backup`/`unlock` use, with none of its
own; these pin that the shared forks behave identically here, that the container
gate REFUSES rather than offering to start (label is the first production caller
of `resolve_container_state(offer_choice=False)`), and above all that the
two-store write ordering holds: the marker is cleared BEFORE the cache, and every
failure on that path fails closed. That ordering is the dangerous part of this
module - a cleared cache with a surviving marker would let a later full `inspect`
resurrect a label the user deleted.
"""

from __future__ import annotations

import pytest

from caffeinated_whale_cli.core import docker as core_docker
from caffeinated_whale_cli.core import label as core_label
from caffeinated_whale_cli.core.envelope import Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from caffeinated_whale_cli.utils import db_utils
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
    db_utils.db.init(str(orig_path))


def _seed(benches):
    db_utils.cache_project_data("proj", benches)


def _two_benches(labels=(None, None)):
    return [
        {"path": BENCH_A, "sites": [], "available_apps": [], "label": labels[0]},
        {"path": BENCH_B, "sites": [], "available_apps": [], "label": labels[1]},
    ]


def _wire(monkeypatch, container):
    monkeypatch.setattr(core_docker, "get_frappe_container", lambda name: container)


class TestListBenches:
    def test_lists_indices_labels_and_paths(self, temp_db, monkeypatch):
        _seed(_two_benches(labels=(None, "staging")))

        def _boom(name):
            raise AssertionError("list_benches must not touch the container")

        monkeypatch.setattr(core_docker, "get_frappe_container", _boom)

        result = core_label.list_benches("proj")
        assert result.status is Status.OK
        assert result.data is not None
        assert [(b.index, b.path, b.label) for b in result.data.benches] == [
            (0, BENCH_A, None),
            (1, BENCH_B, "staging"),
        ]

    def test_uninspected_project_is_not_found_not_an_empty_list(self, temp_db):
        # "never inspected" and "has zero benches" are different facts, and only
        # one of them has a remedy the caller can act on.
        with pytest.raises(CwcliError) as exc:
            core_label.list_benches("never-inspected")
        assert exc.value.kind is ErrorKind.NOT_FOUND
        assert "inspect" in (exc.value.hint or "")


class TestSetLabel:
    def test_writes_marker_and_cache(self, temp_db, monkeypatch):
        _seed(_two_benches())
        container = MarkerFakeContainer(bench_path=BENCH_B)
        _wire(monkeypatch, container)

        result = core_label.set_label("proj", bench="1", label="staging")

        assert result.status is Status.OK
        assert result.data is not None
        assert result.data.label == "staging"
        assert result.data.previous_label is None
        assert result.data.cleared is False
        assert result.data.marker_path == MARKER_B
        assert MARKER_B in container.fs
        assert db_utils.get_cached_project_data("proj")["bench_instances"][1]["label"] == "staging"

    def test_rename_reports_the_previous_label(self, temp_db, monkeypatch):
        # The one fact the caller cannot recover afterward.
        _seed(_two_benches(labels=(None, "staging")))
        _wire(monkeypatch, MarkerFakeContainer(bench_path=BENCH_B))

        result = core_label.set_label("proj", bench="staging", label="prod")

        assert result.data is not None
        assert result.data.previous_label == "staging"
        assert result.data.label == "prod"

    def test_numeric_label_rejected_before_any_write(self, temp_db, monkeypatch):
        _seed(_two_benches())
        container = MarkerFakeContainer(bench_path=BENCH_B)
        _wire(monkeypatch, container)

        with pytest.raises(CwcliError) as exc:
            core_label.set_label("proj", bench="1", label="7")

        assert exc.value.kind is ErrorKind.USAGE
        assert container.fs == {}  # nothing written

    def test_duplicate_label_rejected_before_any_write(self, temp_db, monkeypatch):
        _seed(_two_benches(labels=(None, "staging")))
        container = MarkerFakeContainer(bench_path=BENCH_A)
        _wire(monkeypatch, container)

        with pytest.raises(CwcliError) as exc:
            core_label.set_label("proj", bench="0", label="staging")

        assert exc.value.kind is ErrorKind.USAGE
        assert container.fs == {}

    def test_relabelling_a_bench_to_its_own_label_is_not_a_duplicate(self, temp_db, monkeypatch):
        # The duplicate check keys on PATH, so a bench does not collide with itself.
        _seed(_two_benches(labels=(None, "staging")))
        _wire(monkeypatch, MarkerFakeContainer(bench_path=BENCH_B))

        result = core_label.set_label("proj", bench="1", label="staging")

        assert result.status is Status.OK

    def test_marker_write_failure_raises(self, temp_db, monkeypatch):
        _seed(_two_benches())
        _wire(monkeypatch, MarkerFakeContainer(bench_path=BENCH_B))
        monkeypatch.setattr(core_label.bench_labels, "write_label_marker", lambda *a, **k: False)

        with pytest.raises(CwcliError) as exc:
            core_label.set_label("proj", bench="1", label="staging")

        assert exc.value.kind is ErrorKind.PRECONDITION

    def test_cache_write_failure_is_tolerated_because_marker_is_source_of_truth(
        self, temp_db, monkeypatch
    ):
        # The documented asymmetry with clear_label (cwcli-label-setpath-db-check-a3):
        # a missed cache write on SET must NOT raise. The marker is written and
        # verified, and `inspect` recovers the label from the marker, so the cache
        # self-heals toward the user's intent. Compare the clear path's
        # test_cache_failure_after_marker_removal_is_surfaced, which DOES raise.
        _seed(_two_benches())
        container = MarkerFakeContainer(bench_path=BENCH_B)
        _wire(monkeypatch, container)
        monkeypatch.setattr(core_label.db_utils, "set_bench_label", lambda *a, **k: False)

        result = core_label.set_label("proj", bench="1", label="staging")

        assert result.status is Status.OK
        assert result.data is not None
        assert result.data.label == "staging"
        assert MARKER_B in container.fs  # the marker really was written


class TestClearLabel:
    def test_clears_marker_and_cache(self, temp_db, monkeypatch):
        _seed(_two_benches(labels=(None, "staging")))
        container = MarkerFakeContainer(
            bench_path=BENCH_B, fs={MARKER_B: b'{"schema":1,"label":"staging"}'}
        )
        _wire(monkeypatch, container)

        result = core_label.clear_label("proj", bench="staging")

        assert result.status is Status.OK
        assert result.data is not None
        assert result.data.cleared is True
        assert result.data.label is None
        assert result.data.previous_label == "staging"
        assert MARKER_B not in container.fs
        assert "label" not in db_utils.get_cached_project_data("proj")["bench_instances"][1]

    def test_marker_failure_leaves_cache_label_intact(self, temp_db, monkeypatch):
        # The invariant this whole module is careful about: a cleared cache with a
        # surviving marker would let a later full inspect resurrect the label.
        _seed(_two_benches(labels=(None, "staging")))
        container = MarkerFakeContainer(
            bench_path=BENCH_B, fs={MARKER_B: b'{"schema":1,"label":"staging"}'}
        )
        _wire(monkeypatch, container)
        monkeypatch.setattr(core_label.bench_labels, "clear_label_marker", lambda *a, **k: False)

        with pytest.raises(CwcliError) as exc:
            core_label.clear_label("proj", bench="staging")

        assert exc.value.kind is ErrorKind.PRECONDITION
        assert db_utils.get_cached_project_data("proj")["bench_instances"][1]["label"] == "staging"
        assert MARKER_B in container.fs

    def test_cache_failure_after_marker_removal_is_surfaced(self, temp_db, monkeypatch):
        _seed(_two_benches(labels=(None, "staging")))
        container = MarkerFakeContainer(
            bench_path=BENCH_B, fs={MARKER_B: b'{"schema":1,"label":"staging"}'}
        )
        _wire(monkeypatch, container)
        monkeypatch.setattr(core_label.db_utils, "set_bench_label", lambda *a, **k: False)

        with pytest.raises(CwcliError) as exc:
            core_label.clear_label("proj", bench="staging")

        assert exc.value.kind is ErrorKind.INTERNAL
        assert MARKER_B not in container.fs  # the marker really was removed first

    def test_marker_is_cleared_before_the_cache(self, temp_db, monkeypatch):
        """Pin the ORDER directly, not just its failure modes."""
        _seed(_two_benches(labels=(None, "staging")))
        container = MarkerFakeContainer(
            bench_path=BENCH_B, fs={MARKER_B: b'{"schema":1,"label":"staging"}'}
        )
        _wire(monkeypatch, container)

        order: list[str] = []
        real_clear = core_label.bench_labels.clear_label_marker
        real_set = core_label.db_utils.set_bench_label

        def _clear(*a, **k):
            order.append("marker")
            return real_clear(*a, **k)

        def _set(*a, **k):
            order.append("cache")
            return real_set(*a, **k)

        monkeypatch.setattr(core_label.bench_labels, "clear_label_marker", _clear)
        monkeypatch.setattr(core_label.db_utils, "set_bench_label", _set)

        core_label.clear_label("proj", bench="staging")

        assert order == ["marker", "cache"]


class TestSharedForks:
    def test_stopped_container_raises_and_never_offers_to_start(self, temp_db, monkeypatch):
        # label is the first production caller of offer_choice=False: a label change
        # must not spin up a stopped project, so this is a hard refusal rather than
        # a confirm_start choice.
        _seed(_two_benches())
        container = MarkerFakeContainer(bench_path=BENCH_B)
        container.status = "exited"
        _wire(monkeypatch, container)

        with pytest.raises(CwcliError) as exc:
            core_label.set_label("proj", bench="1", label="staging")

        assert exc.value.kind is ErrorKind.NOT_RUNNING
        assert container.fs == {}

    def test_not_running_hint_does_not_name_a_flag_label_lacks(self, temp_db, monkeypatch):
        # The disclosed primitive widening, verified at its point of use: the
        # resolver's default hint names --yes, which `label` has no such flag for.
        _seed(_two_benches())
        container = MarkerFakeContainer(bench_path=BENCH_B)
        container.status = "exited"
        _wire(monkeypatch, container)

        with pytest.raises(CwcliError) as exc:
            core_label.clear_label("proj", bench="1")

        assert "--yes" not in (exc.value.hint or "")
        assert "Start the project first" in (exc.value.hint or "")

    def test_unknown_selector_is_not_found_without_presentation_data(self, temp_db, monkeypatch):
        _seed(_two_benches())
        _wire(monkeypatch, MarkerFakeContainer(bench_path=BENCH_B))

        with pytest.raises(CwcliError) as exc:
            core_label.set_label("proj", bench="nope", label="staging")

        assert exc.value.kind is ErrorKind.NOT_FOUND
        # The bench list is presentation; the frontend fetches it via list_benches.
        assert exc.value.detail is None

    def test_multi_bench_without_selector_is_select_bench(self, temp_db, monkeypatch):
        _seed(_two_benches())
        container = MarkerFakeContainer(bench_path=BENCH_B)
        _wire(monkeypatch, container)

        result = core_label.set_label("proj", label="staging")

        assert result.status is Status.NEEDS_CHOICE
        assert result.choice is not None
        assert result.choice.kind == "select_bench"
        assert container.fs == {}  # writes nothing

    def test_single_bench_resolves_without_a_selector(self, temp_db, monkeypatch):
        _seed([{"path": BENCH_A, "sites": [], "available_apps": [], "label": None}])
        _wire(monkeypatch, MarkerFakeContainer(bench_path=BENCH_A))

        result = core_label.set_label("proj", label="only")

        assert result.status is Status.OK
        assert [w.code for w in result.warnings] == ["bench.sole"]

    def test_uninspected_project_raises_on_mutations_too(self, temp_db, monkeypatch):
        _wire(monkeypatch, MarkerFakeContainer(bench_path=BENCH_A))
        with pytest.raises(CwcliError) as exc:
            core_label.set_label("nope", bench="0", label="x")
        assert exc.value.kind is ErrorKind.NOT_FOUND


class TestSetLabels:
    """The batched verb behind `inspect -i`: the SAME validate/uniqueness/marker/
    cache rule as `set_label`, but per-assignment outcomes (never batch-aborting),
    a stopped-project degrade to cache-only, and first-wins uniqueness within the
    batch. These pin the nuances the interactive loop used to own inline."""

    def test_applies_several_labels_writing_markers_and_cache(self, temp_db, monkeypatch):
        _seed(_two_benches())
        container = MarkerFakeContainer(bench_path=BENCH_A)
        _wire(monkeypatch, container)

        result = core_label.set_labels("proj", [(BENCH_A, "web"), (BENCH_B, "worker")])

        assert result.status is Status.OK
        assert result.data is not None
        assert [(r.bench_path, r.label, r.applied, r.marker_written) for r in result.data] == [
            (BENCH_A, "web", True, True),
            (BENCH_B, "worker", True, True),
        ]
        assert MARKER_A in container.fs and MARKER_B in container.fs
        cached = db_utils.get_cached_project_data("proj")["bench_instances"]
        assert cached[0]["label"] == "web"
        assert cached[1]["label"] == "worker"

    def test_label_is_stripped_and_blank_never_reaches_here(self, temp_db, monkeypatch):
        # The frontend drops blank answers; a whitespace-padded label is trimmed.
        _seed(_two_benches())
        _wire(monkeypatch, MarkerFakeContainer(bench_path=BENCH_A))

        result = core_label.set_labels("proj", [(BENCH_A, "  web  ")])

        assert result.data is not None
        assert result.data[0].label == "web"
        assert result.data[0].applied is True

    def test_first_wins_on_a_within_batch_duplicate(self, temp_db, monkeypatch):
        # Two benches given the same label in one pass: the first is accepted, the
        # second is rejected (matching the old in-memory cross-bench check), and the
        # batch does NOT abort.
        _seed(_two_benches())
        _wire(monkeypatch, MarkerFakeContainer(bench_path=BENCH_A))

        result = core_label.set_labels("proj", [(BENCH_A, "dup"), (BENCH_B, "dup")])

        assert result.data is not None
        first, second = result.data
        assert (first.applied, first.error) == (True, None)
        assert second.applied is False
        assert "already used" in (second.error or "")
        cached = db_utils.get_cached_project_data("proj")["bench_instances"]
        assert cached[0]["label"] == "dup"
        assert "label" not in cached[1]

    def test_duplicate_of_an_untouched_bench_is_rejected(self, temp_db, monkeypatch):
        _seed(_two_benches(labels=(None, "staging")))
        _wire(monkeypatch, MarkerFakeContainer(bench_path=BENCH_A))

        result = core_label.set_labels("proj", [(BENCH_A, "staging")])

        assert result.data is not None
        assert result.data[0].applied is False
        assert "already used" in (result.data[0].error or "")

    def test_invalid_label_is_rejected_but_siblings_still_apply(self, temp_db, monkeypatch):
        _seed(_two_benches())
        _wire(monkeypatch, MarkerFakeContainer(bench_path=BENCH_A))

        result = core_label.set_labels("proj", [(BENCH_A, "7"), (BENCH_B, "ok")])

        assert result.data is not None
        assert result.data[0].applied is False  # purely-numeric label
        assert result.data[1].applied is True
        cached = db_utils.get_cached_project_data("proj")["bench_instances"]
        assert "label" not in cached[0]
        assert cached[1]["label"] == "ok"

    def test_stopped_project_degrades_to_cache_only(self, temp_db, monkeypatch):
        # `inspect -i` on a stopped instance still records labels (to the cache),
        # rather than the hard NOT_RUNNING `set_label` raises - the interactive
        # affordance the frontend used to own.
        _seed(_two_benches())
        container = MarkerFakeContainer(bench_path=BENCH_A)
        container.status = "exited"
        _wire(monkeypatch, container)

        result = core_label.set_labels("proj", [(BENCH_A, "web")])

        assert result.status is Status.OK
        assert [w.code for w in result.warnings] == ["label.marker_skipped"]
        assert result.data is not None
        assert result.data[0].applied is True
        assert result.data[0].marker_written is False
        assert container.fs == {}  # no marker written
        cached = db_utils.get_cached_project_data("proj")["bench_instances"]
        assert cached[0]["label"] == "web"

    def test_container_not_found_degrades_to_cache_only(self, temp_db, monkeypatch):
        # A project whose containers were removed since the last inspect must
        # degrade like a stopped project, not raise - the pre-migration frontend
        # caught ANY container-resolution failure this broadly.
        _seed(_two_benches())

        def _boom(name):
            raise CwcliError(ErrorKind.NOT_FOUND, "project.not_found", "gone")

        monkeypatch.setattr(core_docker, "get_frappe_container", _boom)

        result = core_label.set_labels("proj", [(BENCH_A, "web")])

        assert result.status is Status.OK
        assert [w.code for w in result.warnings] == ["label.marker_skipped"]
        assert result.data is not None
        assert result.data[0].applied is True
        assert result.data[0].marker_written is False
        cached = db_utils.get_cached_project_data("proj")["bench_instances"]
        assert cached[0]["label"] == "web"

    def test_docker_unreachable_degrades_to_cache_only(self, temp_db, monkeypatch):
        # A daemon hiccup mid-session must degrade the same way, not crash the
        # interactive session with a raw CwcliError.
        _seed(_two_benches())

        def _boom(name):
            raise CwcliError(ErrorKind.DOCKER, "docker.unreachable", "Could not connect")

        monkeypatch.setattr(core_docker, "get_frappe_container", _boom)

        result = core_label.set_labels("proj", [(BENCH_A, "web")])

        assert result.status is Status.OK
        assert [w.code for w in result.warnings] == ["label.marker_skipped"]
        assert result.data is not None
        assert result.data[0].applied is True
        assert result.data[0].marker_written is False
        cached = db_utils.get_cached_project_data("proj")["bench_instances"]
        assert cached[0]["label"] == "web"

    def test_unknown_bench_path_is_reported_not_raised(self, temp_db, monkeypatch):
        _seed(_two_benches())
        _wire(monkeypatch, MarkerFakeContainer(bench_path=BENCH_A))

        result = core_label.set_labels("proj", [("/workspace/ghost", "x")])

        assert result.data is not None
        assert result.data[0].applied is False
        assert "No cached bench" in (result.data[0].error or "")

    def test_empty_assignments_is_a_noop(self, temp_db, monkeypatch):
        _seed(_two_benches(labels=(None, "staging")))
        _wire(monkeypatch, MarkerFakeContainer(bench_path=BENCH_A))

        result = core_label.set_labels("proj", [])

        assert result.status is Status.OK
        assert result.data == []
        # Existing labels are preserved through the cache round-trip.
        cached = db_utils.get_cached_project_data("proj")["bench_instances"]
        assert cached[1]["label"] == "staging"

    def test_uninspected_project_raises(self, temp_db, monkeypatch):
        _wire(monkeypatch, MarkerFakeContainer(bench_path=BENCH_A))
        with pytest.raises(CwcliError) as exc:
            core_label.set_labels("nope", [(BENCH_A, "x")])
        assert exc.value.kind is ErrorKind.NOT_FOUND
