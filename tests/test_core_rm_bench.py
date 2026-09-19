"""``core.remove_bench`` - every branch it owns, against a faked container.

``remove_bench`` COMPOSES the already-proven ``core.rm_site.drop_site`` (tested in
``test_core_rm_site.py``) once per site, so these tests monkeypatch ``drop_site``
to return controllable outcomes and pin only what this module itself owns: the
"refuse while running" gate, the consent gate, the fail-closed directory deletion
(deleted only when EVERY site's backup archive is confirmed on the host), the
outcome aggregation, and the path-safety guard on the ``rm -rf`` target.
"""

from __future__ import annotations

import pytest

from caffeinated_whale_cli.core import docker as core_docker
from caffeinated_whale_cli.core import resolvers, supervision
from caffeinated_whale_cli.core import rm_bench as core_rm_bench
from caffeinated_whale_cli.core import rm_site as core_rm_site
from caffeinated_whale_cli.core.envelope import Message, Result, Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from caffeinated_whale_cli.utils import bench_sites

BENCH = "/workspace/frappe-bench-2"


class FakeContainer:
    labels = {"com.docker.compose.service": "frappe"}
    name = "proj-frappe-1"
    id = "cid"

    def __init__(self, *, status="running", bench_dir_ok=True, rm_ok=True):
        self.status = status
        self.bench_dir_ok = bench_dir_ok
        self.rm_ok = rm_ok
        self.calls: list = []

    def reload(self):
        pass

    def exec_run(self, cmd, workdir=None, **kwargs):
        self.calls.append(cmd)
        if cmd[:2] == ["test", "-d"]:
            # require_bench_dir probes "{path}/sites".
            return (0 if self.bench_dir_ok else 1, b"")
        if cmd[:2] == ["rm", "-rf"]:
            return (0 if self.rm_ok else 1, b"could not remove")
        return (0, b"")

    def rm_calls(self):
        return [c for c in self.calls if isinstance(c, list) and c[:2] == ["rm", "-rf"]]


def _drop_outcome(site, *, archived="/host/archive/proj_dropped_sites/x.tar"):
    return Result(
        status=Status.OK if archived else Status.WARNING,
        data=core_rm_site.DropSiteOutcome(
            project="proj",
            site=site,
            bench_path=BENCH,
            archived_host_path=archived,
            archive_pruned_in_container=archived is not None,
            ok=archived is not None,
        ),
    )


@pytest.fixture()
def container(monkeypatch):
    c = FakeContainer()
    monkeypatch.setattr(core_docker, "get_frappe_container", lambda _p: c)
    monkeypatch.setattr(resolvers, "cached_benches", lambda _p: [{"path": BENCH, "index": 2}])
    # Not running by default; each test overrides as needed.
    monkeypatch.setattr(
        supervision, "discover_stack", lambda *_a, **_k: _StackSnap(supervisor_up=False)
    )
    monkeypatch.setattr(
        supervision,
        "discover_unsupervised_stack",
        lambda *_a, **_k: _Unsup(manager_up=False),
    )
    return c


class _StackSnap:
    def __init__(self, supervisor_up):
        self.supervisor_up = supervisor_up


class _Unsup:
    def __init__(self, manager_up):
        self.manager_up = manager_up


def _patch_sites(monkeypatch, sites):
    monkeypatch.setattr(bench_sites, "list_sites", lambda *_a, **_k: sites)


def _patch_drops(monkeypatch, outcomes_by_site):
    seen = []

    def _drop(project, site, **kwargs):
        seen.append((site, kwargs))
        return outcomes_by_site[site]

    monkeypatch.setattr(core_rm_site, "drop_site", _drop)
    return seen


# --------------------------------------------------------------------------- consent


class TestConsent:
    def test_no_consent_is_a_confirm_remove_bench_choice_and_nothing_runs(
        self, container, monkeypatch
    ):
        _patch_sites(monkeypatch, ["a.localhost"])
        seen = _patch_drops(monkeypatch, {})

        result = core_rm_bench.remove_bench("proj", bench="2")

        assert result.status is Status.NEEDS_CHOICE
        assert result.choice.kind == "confirm_remove_bench"
        assert result.choice.param == "consent"
        assert BENCH in result.choice.prompt
        assert seen == []  # no site dropped
        assert container.rm_calls() == []  # nothing deleted

    def test_consent_true_drops_each_site_and_removes_the_directory(self, container, monkeypatch):
        _patch_sites(monkeypatch, ["a.localhost", "b.localhost"])
        seen = _patch_drops(
            monkeypatch,
            {
                "a.localhost": _drop_outcome("a.localhost"),
                "b.localhost": _drop_outcome("b.localhost"),
            },
        )

        result = core_rm_bench.remove_bench("proj", bench="2", consent=True)

        assert result.status is Status.OK
        outcome = result.data
        assert outcome.ok is True
        assert outcome.dir_removed is True
        assert outcome.sites_dropped == ["a.localhost", "b.localhost"]
        assert outcome.sites_failed == []
        assert len(outcome.archived_host_paths) == 2
        # Each drop was consented and targeted at the resolved bench path.
        assert [s for s, _ in seen] == ["a.localhost", "b.localhost"]
        assert all(kw.get("consent") is True and kw.get("bench_path") == BENCH for _, kw in seen)
        assert container.rm_calls() == [["rm", "-rf", BENCH]]


# --------------------------------------------------------------------------- running gate


class TestRefuseWhileRunning:
    def test_supervisord_up_refuses_with_conflict_and_names_stop(self, container, monkeypatch):
        monkeypatch.setattr(
            supervision, "discover_stack", lambda *_a, **_k: _StackSnap(supervisor_up=True)
        )
        _patch_sites(monkeypatch, ["a.localhost"])
        seen = _patch_drops(monkeypatch, {})

        with pytest.raises(CwcliError) as exc:
            core_rm_bench.remove_bench("proj", bench="2", consent=True)

        assert exc.value.kind is ErrorKind.CONFLICT
        assert exc.value.code == "bench.running"
        assert "cwcli stop proj --bench 2" in (exc.value.hint or "")
        assert seen == []
        assert container.rm_calls() == []

    def test_unsupervised_manager_up_also_refuses(self, container, monkeypatch):
        monkeypatch.setattr(
            supervision,
            "discover_unsupervised_stack",
            lambda *_a, **_k: _Unsup(manager_up=True),
        )
        _patch_sites(monkeypatch, ["a.localhost"])
        _patch_drops(monkeypatch, {})

        with pytest.raises(CwcliError) as exc:
            core_rm_bench.remove_bench("proj", bench="2", consent=True)

        assert exc.value.kind is ErrorKind.CONFLICT
        assert container.rm_calls() == []


# --------------------------------------------------------------------- fail-closed deletion


class TestFailClosedDeletion:
    def test_a_trapped_archive_keeps_the_directory_and_reports_not_ok(self, container, monkeypatch):
        _patch_sites(monkeypatch, ["a.localhost", "b.localhost"])
        _patch_drops(
            monkeypatch,
            {
                "a.localhost": _drop_outcome("a.localhost"),
                "b.localhost": _drop_outcome("b.localhost", archived=None),  # copy-out failed
            },
        )

        result = core_rm_bench.remove_bench("proj", bench="2", consent=True)

        assert result.status is Status.WARNING
        outcome = result.data
        assert outcome.ok is False
        assert outcome.dir_removed is False
        assert outcome.sites_dropped == ["a.localhost"]
        assert outcome.sites_failed == ["b.localhost"]
        assert outcome.failures  # names the trapped site
        # The directory is NEVER deleted while a backup is trapped inside it.
        assert container.rm_calls() == []

    def test_a_failed_drop_site_propagates_and_deletes_nothing(self, container, monkeypatch):
        _patch_sites(monkeypatch, ["a.localhost"])

        def _drop(project, site, **kwargs):
            raise CwcliError(ErrorKind.PRECONDITION, "rm_site.failed", "bench drop-site exited 1")

        monkeypatch.setattr(core_rm_site, "drop_site", _drop)

        with pytest.raises(CwcliError) as exc:
            core_rm_bench.remove_bench("proj", bench="2", consent=True)

        assert exc.value.kind is ErrorKind.PRECONDITION
        assert container.rm_calls() == []

    def test_a_bench_with_no_sites_is_removed_cleanly(self, container, monkeypatch):
        _patch_sites(monkeypatch, [])
        seen = _patch_drops(monkeypatch, {})

        result = core_rm_bench.remove_bench("proj", bench="2", consent=True)

        assert result.status is Status.OK
        assert result.data.ok is True
        assert result.data.dir_removed is True
        assert result.data.sites_dropped == []
        assert seen == []  # nothing to drop
        assert container.rm_calls() == [["rm", "-rf", BENCH]]

    def test_unlistable_sites_fail_closed_before_any_deletion(self, container, monkeypatch):
        _patch_sites(monkeypatch, None)  # could not enumerate
        _patch_drops(monkeypatch, {})

        with pytest.raises(CwcliError) as exc:
            core_rm_bench.remove_bench("proj", bench="2", consent=True)

        assert exc.value.kind is ErrorKind.PRECONDITION
        assert exc.value.code == "bench.sites_unknown"
        assert container.rm_calls() == []

    def test_prune_warning_from_drop_site_is_dropped_since_the_dir_is_deleted(
        self, container, monkeypatch
    ):
        _patch_sites(monkeypatch, ["a.localhost"])
        out = _drop_outcome("a.localhost")
        out.warnings.append(Message("rm_site.archive_not_pruned", "could not prune in-container"))
        out.warnings.append(Message("some.other", "kept"))
        monkeypatch.setattr(core_rm_site, "drop_site", lambda *a, **k: out)

        result = core_rm_bench.remove_bench("proj", bench="2", consent=True)

        codes = {w.code for w in result.warnings}
        assert "rm_site.archive_not_pruned" not in codes  # moot: the dir is deleted
        assert "some.other" in codes


# ---------------------------------------------------------------------- deletion failure


class TestDeletionFailure:
    def test_rm_rf_failure_raises_and_never_reads_as_success(self, container, monkeypatch):
        container.rm_ok = False
        _patch_sites(monkeypatch, ["a.localhost"])
        _patch_drops(monkeypatch, {"a.localhost": _drop_outcome("a.localhost")})

        with pytest.raises(CwcliError) as exc:
            core_rm_bench.remove_bench("proj", bench="2", consent=True)

        assert exc.value.kind is ErrorKind.PRECONDITION
        assert exc.value.code == "bench.dir_remove_failed"


# ------------------------------------------------------------------------- path safety


class TestPathSafety:
    @pytest.mark.parametrize("unsafe", ["/", "/workspace", "workspace", "  "])
    def test_a_non_nested_path_is_refused_before_any_drop(self, monkeypatch, unsafe):
        c = FakeContainer()
        monkeypatch.setattr(core_docker, "get_frappe_container", lambda _p: c)
        monkeypatch.setattr(resolvers, "cached_benches", lambda _p: [{"path": unsafe, "index": 0}])
        monkeypatch.setattr(
            supervision, "discover_stack", lambda *_a, **_k: _StackSnap(supervisor_up=False)
        )
        monkeypatch.setattr(
            supervision, "discover_unsupervised_stack", lambda *_a, **_k: _Unsup(manager_up=False)
        )
        monkeypatch.setattr(bench_sites, "list_sites", lambda *_a, **_k: ["a.localhost"])
        monkeypatch.setattr(
            core_rm_site, "drop_site", lambda *a, **k: pytest.fail("drop_site must not run")
        )

        with pytest.raises(CwcliError) as exc:
            core_rm_bench.remove_bench("proj", bench_path=unsafe, consent=True)

        assert exc.value.kind is ErrorKind.USAGE
        assert exc.value.code == "bench.unsafe_path"
        assert c.rm_calls() == []


# ----------------------------------------------------------------------- resolver forks


class TestResolverForks:
    def test_stopped_container_without_auto_start_is_a_confirm_start_choice(
        self, container, monkeypatch
    ):
        container.status = "exited"
        _patch_sites(monkeypatch, ["a.localhost"])
        _patch_drops(monkeypatch, {})

        result = core_rm_bench.remove_bench("proj", bench="2")

        assert result.status is Status.NEEDS_CHOICE
        assert result.choice.kind == "confirm_start"
        assert container.rm_calls() == []

    def test_multi_bench_without_a_selector_is_a_select_bench_choice(self, monkeypatch):
        c = FakeContainer()
        monkeypatch.setattr(core_docker, "get_frappe_container", lambda _p: c)
        monkeypatch.setattr(
            resolvers,
            "cached_benches",
            lambda _p: [{"path": BENCH, "index": 2}, {"path": "/workspace/other", "index": 3}],
        )
        result = core_rm_bench.remove_bench("proj", consent=True)

        assert result.status is Status.NEEDS_CHOICE
        assert result.choice.kind == "select_bench"
        assert c.rm_calls() == []
