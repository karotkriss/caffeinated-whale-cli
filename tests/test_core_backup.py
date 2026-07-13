"""``core.backup`` branch coverage against faked I/O (task 3.4).

Every fork is exercised: the success outcome, the multi-bench and stopped-container
NEEDS_CHOICE forks, missing-site NOT_FOUND, failed-backup PRECONDITION, and the
shell-unsafe site/path USAGE errors - plus default-site resolution. The core
prints/prompts/exits nothing, so these are plain function calls.
"""

import dataclasses

import pytest

from caffeinated_whale_cli.core import backup as core_backup
from caffeinated_whale_cli.core import docker as core_docker
from caffeinated_whale_cli.core import resolvers
from caffeinated_whale_cli.core.backup import BackupOutcome
from caffeinated_whale_cli.core.envelope import Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind

_DUMP = (
    "/workspace/frappe-bench/sites/s.localhost/private/backups/20260712_000000-s-database.sql.gz"
)


class FakeContainer:
    """Programmable frappe container: routes exec_run by command shape."""

    def __init__(
        self,
        *,
        status="running",
        bench_dir_ok=True,
        site_dir_ok=True,
        backup_dir_exists=True,
        mkdir_ok=True,
        backup_ok=True,
        dump_path=_DUMP,
    ):
        self.status = status
        self.labels = {"com.docker.compose.service": "frappe"}
        self.calls = []
        self.bench_dir_ok = bench_dir_ok
        self.site_dir_ok = site_dir_ok
        self.backup_dir_exists = backup_dir_exists
        self.mkdir_ok = mkdir_ok
        self.backup_ok = backup_ok
        self.dump_path = dump_path

    def reload(self):
        pass

    def exec_run(self, cmd, workdir=None, environment=None):
        self.calls.append((cmd, workdir))
        if cmd.startswith("bench --site"):
            return (0 if self.backup_ok else 1, b"bench backup chatter")
        if "ls -1t" in cmd:
            return (0, (self.dump_path or "").encode())
        if "mkdir -p" in cmd:
            return (0 if self.mkdir_ok else 1, b"mkdir failed")
        if "test -d" in cmd:
            if "/private/backups" in cmd:
                return (0 if self.backup_dir_exists else 1, b"")
            if "/sites/" in cmd:  # {bench}/sites/{site}
                return (0 if self.site_dir_ok else 1, b"")
            return (0 if self.bench_dir_ok else 1, b"")  # {bench}/sites
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
        monkeypatch.setattr(
            core_backup.db_utils, "get_default_site", lambda name, path: default_site
        )

    return _wire


class TestSuccess:
    def test_success_returns_outcome(self, wire):
        c = FakeContainer()
        wire(c)
        result = core_backup.backup("proj", site="s.localhost")
        assert result.status is Status.OK
        assert result.data == BackupOutcome(
            site="s.localhost",
            bench_path="/workspace/frappe-bench",
            artifact_path=_DUMP,
            included_files=False,
        )
        # No cache -> default bench path, surfaced as a warning (not an error).
        assert any(w.code == "bench.default_used" for w in result.warnings)

    def test_with_files_flag_flows_through(self, wire):
        c = FakeContainer()
        wire(c)
        result = core_backup.backup("proj", site="s.localhost", with_files=True)
        assert result.data.included_files is True
        assert any("--with-files" in cmd for cmd, _ in c.calls)

    def test_outcome_dto_is_json_safe(self, wire):
        wire(FakeContainer())
        result = core_backup.backup("proj", site="s.localhost")
        blob = dataclasses.asdict(result.data)
        assert set(blob) == {"site", "bench_path", "artifact_path", "included_files"}

    def test_created_backup_dir_warns(self, wire):
        wire(FakeContainer(backup_dir_exists=False, mkdir_ok=True))
        result = core_backup.backup("proj", site="s.localhost")
        assert result.status is Status.OK
        assert any(w.code == "backup_dir.created" for w in result.warnings)

    def test_existing_backup_dir_does_not_warn(self, wire):
        wire(FakeContainer(backup_dir_exists=True))
        result = core_backup.backup("proj", site="s.localhost")
        assert not any(w.code == "backup_dir.created" for w in result.warnings)


class TestChoices:
    def test_stopped_container_returns_confirm_start(self, wire):
        wire(FakeContainer(status="exited"))
        result = core_backup.backup("proj", site="s.localhost")
        assert result.status is Status.NEEDS_CHOICE
        assert result.choice.kind == "confirm_start"

    def test_multi_bench_returns_select_bench(self, wire):
        c = FakeContainer()
        wire(c, benches=[{"path": "/w/b0"}, {"path": "/w/b1"}])
        result = core_backup.backup("proj", site="s.localhost")
        assert result.status is Status.NEEDS_CHOICE
        assert result.choice.kind == "select_bench"
        assert [o["value"] for o in result.choice.options] == ["0", "1"]
        # No backup ran.
        assert not any(cmd.startswith("bench --site") for cmd, _ in c.calls)


class TestHardErrors:
    def test_missing_site_raises_not_found(self, wire):
        wire(FakeContainer(site_dir_ok=False))
        with pytest.raises(CwcliError) as exc:
            core_backup.backup("proj", site="s.localhost")
        assert exc.value.kind is ErrorKind.NOT_FOUND
        assert exc.value.code == "site.not_found"

    def test_missing_bench_dir_raises_not_found(self, wire):
        wire(FakeContainer(bench_dir_ok=False))
        with pytest.raises(CwcliError) as exc:
            core_backup.backup("proj", site="s.localhost")
        assert exc.value.code == "bench.dir_missing"

    def test_failed_backup_raises_precondition(self, wire):
        wire(FakeContainer(backup_ok=False))
        with pytest.raises(CwcliError) as exc:
            core_backup.backup("proj", site="s.localhost")
        assert exc.value.kind is ErrorKind.PRECONDITION
        assert exc.value.code == "backup.failed"
        assert "bench backup chatter" in exc.value.detail["output"]

    def test_failed_mkdir_raises_precondition(self, wire):
        wire(FakeContainer(backup_dir_exists=False, mkdir_ok=False))
        with pytest.raises(CwcliError) as exc:
            core_backup.backup("proj", site="s.localhost")
        assert exc.value.code == "backup_dir.failed"

    def test_shell_unsafe_site_raises_usage(self, wire):
        wire(FakeContainer())
        with pytest.raises(CwcliError) as exc:
            core_backup.backup("proj", site="s;rm -rf /")
        assert exc.value.kind is ErrorKind.USAGE
        assert exc.value.code == "site.invalid_chars"

    def test_empty_site_raises_usage(self, wire):
        wire(FakeContainer())
        with pytest.raises(CwcliError) as exc:
            core_backup.backup("proj", site="   ")
        assert exc.value.code == "site.empty"

    def test_shell_unsafe_path_raises_usage(self, wire):
        wire(FakeContainer())
        with pytest.raises(CwcliError) as exc:
            core_backup.backup("proj", site="s.localhost", bench_path="/w;/x")
        assert exc.value.kind is ErrorKind.USAGE
        assert exc.value.code == "bench_path.invalid_chars"

    def test_project_not_found_raises_not_found(self, monkeypatch):
        monkeypatch.setattr(core_docker, "get_project_containers", lambda name: [])
        with pytest.raises(CwcliError) as exc:
            core_backup.backup("proj", site="s.localhost")
        assert exc.value.kind is ErrorKind.NOT_FOUND


class TestDefaultSite:
    def test_resolves_default_site_when_omitted(self, wire):
        wire(FakeContainer(), default_site="def.localhost")
        result = core_backup.backup("proj")
        assert result.status is Status.OK
        assert result.data.site == "def.localhost"
        assert any(w.code == "default_site.resolved" for w in result.warnings)

    def test_no_default_site_raises_not_found(self, wire):
        wire(FakeContainer(), default_site=None)
        with pytest.raises(CwcliError) as exc:
            core_backup.backup("proj")
        assert exc.value.code == "site.no_default"
