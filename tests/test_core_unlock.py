"""``core.unlock`` - every branch, against faked exec I/O.

`unlock` is `backup`'s near-twin and is built from the SAME core primitives with
none of its own; these tests pin that the shared forks (confirm_start,
select_bench, default-site resolution, the validation/probe helpers) behave
identically here, and that the removal is BUFFERED into a structured `removed`
list rather than streamed.
"""

from __future__ import annotations

import pytest

from caffeinated_whale_cli.core import docker as core_docker
from caffeinated_whale_cli.core import resolvers
from caffeinated_whale_cli.core import unlock as core_unlock
from caffeinated_whale_cli.core.envelope import Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from caffeinated_whale_cli.core.unlock import UnlockOutcome

BENCH = "/workspace/frappe-bench"
SITE = "s.localhost"
LOCKS = f"{BENCH}/sites/{SITE}/locks"

_RM_OUTPUT = (
    f"removed '{LOCKS}/doctype.lock'\n"
    f"removed '{LOCKS}/queue.lock'\n"
    f"removed directory '{LOCKS}'\n"
)


class FakeContainer:
    labels = {"com.docker.compose.service": "frappe"}
    name = "proj-frappe-1"
    id = "cid"

    def __init__(
        self,
        *,
        status="running",
        bench_dir_ok=True,
        site_dir_ok=True,
        locks_exist=True,
        rm_ok=True,
        rm_output=_RM_OUTPUT,
    ):
        self.status = status
        self.bench_dir_ok = bench_dir_ok
        self.site_dir_ok = site_dir_ok
        self.locks_exist = locks_exist
        self.rm_ok = rm_ok
        self.rm_output = rm_output
        self.calls: list[list] = []

    def reload(self):
        pass

    def exec_run(self, cmd, workdir=None):
        self.calls.append(cmd)
        if cmd[0] == "rm":
            return (0 if self.rm_ok else 1, self.rm_output.encode())
        if cmd[:2] == ["test", "-d"]:
            target = cmd[2]
            if target.endswith("/locks"):
                return (0 if self.locks_exist else 1, b"")
            if target.endswith("/sites"):
                return (0 if self.bench_dir_ok else 1, b"")
            return (0 if self.site_dir_ok else 1, b"")
        return (0, b"")


@pytest.fixture
def wire(monkeypatch):
    """Wire a fake container + configurable cache/default-site into the core."""

    def _wire(container, *, benches=None, default_site=None):
        monkeypatch.setattr(core_docker, "get_project_containers", lambda name: [container])
        monkeypatch.setattr(
            resolvers.db_utils,
            "get_cached_project_data",
            lambda name: {"bench_instances": benches} if benches is not None else None,
        )
        monkeypatch.setattr(resolvers.db_utils, "get_default_site", lambda name, path: default_site)

    return _wire


class TestSuccess:
    def test_removes_locks_and_returns_the_removed_paths(self, wire):
        c = FakeContainer()
        wire(c)
        result = core_unlock.unlock("proj", site=SITE)

        assert result.status is Status.OK
        assert result.data == UnlockOutcome(
            site=SITE,
            bench_path=BENCH,
            locks_path=LOCKS,
            removed=[f"{LOCKS}/doctype.lock", f"{LOCKS}/queue.lock", LOCKS],
            already_unlocked=False,
        )

    def test_the_rm_is_one_buffered_call_not_a_stream(self, wire):
        """The removal is a single exec_run; nothing streams chunk by chunk."""
        c = FakeContainer()
        wire(c)
        core_unlock.unlock("proj", site=SITE)
        rm_calls = [cmd for cmd in c.calls if cmd[0] == "rm"]
        assert rm_calls == [["rm", "-rfv", LOCKS]]

    def test_unparseable_rm_output_still_succeeds(self, wire):
        """An rm whose -v output we cannot parse is still a success, not a failure."""
        c = FakeContainer(rm_output="something else entirely\n")
        wire(c)
        result = core_unlock.unlock("proj", site=SITE)
        assert result.status is Status.OK
        assert result.data.removed == []
        assert result.data.already_unlocked is False


class TestAlreadyUnlocked:
    def test_absent_locks_dir_is_a_clean_success_not_not_found(self, wire):
        """A site that simply is not locked is exactly what the caller wanted."""
        c = FakeContainer(locks_exist=False)
        wire(c)
        result = core_unlock.unlock("proj", site=SITE)

        assert result.status is Status.OK
        assert result.data.already_unlocked is True
        assert result.data.removed == []

    def test_absent_locks_dir_runs_no_rm_at_all(self, wire):
        c = FakeContainer(locks_exist=False)
        wire(c)
        core_unlock.unlock("proj", site=SITE)
        assert [cmd for cmd in c.calls if cmd[0] == "rm"] == []


class TestChoices:
    def test_stopped_container_is_a_confirm_start_choice(self, wire):
        c = FakeContainer(status="exited")
        wire(c)
        result = core_unlock.unlock("proj", site=SITE)

        assert result.status is Status.NEEDS_CHOICE
        assert result.choice.kind == "confirm_start"
        assert [cmd for cmd in c.calls if cmd[0] == "rm"] == []  # removed nothing

    def test_multi_bench_without_a_selector_is_a_select_bench_choice(self, wire):
        c = FakeContainer()
        wire(c, benches=[{"path": "/a"}, {"path": "/b"}])
        result = core_unlock.unlock("proj", site=SITE)

        assert result.status is Status.NEEDS_CHOICE
        assert result.choice.kind == "select_bench"
        assert [cmd for cmd in c.calls if cmd[0] == "rm"] == []

    def test_bench_selector_picks_the_named_bench(self, wire):
        c = FakeContainer()
        wire(c, benches=[{"path": "/a"}, {"path": "/b", "label": "two"}])
        result = core_unlock.unlock("proj", site=SITE, bench="two")

        assert result.status is Status.OK
        assert result.data.bench_path == "/b"


class TestDefaultSite:
    def test_resolves_the_default_site_when_omitted(self, wire):
        c = FakeContainer()
        wire(c, default_site="default.localhost")
        result = core_unlock.unlock("proj")

        assert result.status is Status.OK
        assert result.data.site == "default.localhost"
        assert any(w.code == "default_site.resolved" for w in result.warnings)

    def test_no_default_site_raises_not_found(self, wire):
        c = FakeContainer()
        wire(c, default_site=None)
        with pytest.raises(CwcliError) as exc:
            core_unlock.unlock("proj")
        assert exc.value.kind is ErrorKind.NOT_FOUND


class TestHardErrors:
    def test_missing_bench_dir_raises_not_found(self, wire):
        c = FakeContainer(bench_dir_ok=False)
        wire(c)
        with pytest.raises(CwcliError) as exc:
            core_unlock.unlock("proj", site=SITE)
        assert exc.value.kind is ErrorKind.NOT_FOUND

    def test_missing_site_raises_not_found(self, wire):
        c = FakeContainer(site_dir_ok=False)
        wire(c)
        with pytest.raises(CwcliError) as exc:
            core_unlock.unlock("proj", site=SITE)
        assert exc.value.kind is ErrorKind.NOT_FOUND

    def test_failed_rm_raises_precondition_carrying_the_output(self, wire):
        c = FakeContainer(rm_ok=False, rm_output="rm: cannot remove: Permission denied")
        wire(c)
        with pytest.raises(CwcliError) as exc:
            core_unlock.unlock("proj", site=SITE)
        assert exc.value.kind is ErrorKind.PRECONDITION
        assert "Permission denied" in exc.value.detail["output"]

    def test_shell_unsafe_site_raises_usage(self, wire):
        c = FakeContainer()
        wire(c)
        with pytest.raises(CwcliError) as exc:
            core_unlock.unlock("proj", site="evil;rm -rf /")
        assert exc.value.kind is ErrorKind.USAGE

    def test_empty_site_raises_usage(self, wire):
        c = FakeContainer()
        wire(c)
        with pytest.raises(CwcliError) as exc:
            core_unlock.unlock("proj", site="   ")
        assert exc.value.kind is ErrorKind.USAGE

    def test_missing_project_raises_not_found(self, monkeypatch):
        monkeypatch.setattr(core_docker, "get_project_containers", lambda name: [])
        with pytest.raises(CwcliError) as exc:
            core_unlock.unlock("proj", site=SITE)
        assert exc.value.kind is ErrorKind.NOT_FOUND
