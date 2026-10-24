"""``core.drop_site`` - every branch, against faked exec + streaming I/O.

Pins the three things this module owns beyond what ``unlock``/``bench_ops`` already
prove work: the ``consent``-gated destructive confirm (the ``apps.uninstall_apps``
pattern), the backup gate (``core.rm``'s verified backup taken and copied to the
host BEFORE ``bench drop-site --no-backup``, and nothing dropped when it fails),
and the archive-relocation contract (copy the site's archived directory out to
the host, prune it from the container ONLY once that copy and the dump are
verified, and report the honest ``ok`` either way).
"""

from __future__ import annotations

import io
import tarfile
import types
from pathlib import Path

import pytest

from caffeinated_whale_cli.core import docker as core_docker
from caffeinated_whale_cli.core import resolvers
from caffeinated_whale_cli.core import rm_site as core_rm_site
from caffeinated_whale_cli.core.envelope import Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind

BENCH = "/workspace/frappe-bench"
SITE = "task.localhost"
BACKUPS = f"{BENCH}/sites/{SITE}/private/backups"
DUMP = "20261024_120000-task_localhost-database.sql.gz"


def _artifacts():
    """One real ``bench backup --with-files`` run's artifacts (shared timestamp)."""
    return {
        DUMP: b"DBDUMPBYTES",
        "20261024_120000-task_localhost-site_config_backup.json": b"{}",
        "20261024_120000-task_localhost-files.tar": b"TARBYTES",
        "20261024_120000-task_localhost-private-files.tar": b"TARBYTES",
    }


def _tar_bytes(name, data):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=name)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class FakeAPI:
    """Streaming exec surface (client.api), mirroring docker-py's shape."""

    def __init__(self, container):
        self.container = container
        self._pending = None

    def exec_create(self, cid, cmd, workdir=None, tty=False, environment=None):
        self._pending = (cmd, workdir, environment)
        return {"Id": "exec-1"}

    def exec_start(self, exec_id, stream=True, demux=False):
        cmd, _workdir, _env = self._pending
        code, out = self.container._run_shell(cmd)
        self.container._last_code = code
        raw = out.encode() if isinstance(out, str) else out
        yield (raw, None) if demux else raw

    def exec_inspect(self, exec_id):
        return {"ExitCode": self.container._last_code}


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
        drop_site_ok=True,
        get_archive_ok=True,
        prune_ok=True,
        archive_bytes=b"FAKETAR",
        backup_ok=True,
        artifacts=None,
    ):
        self.status = status
        self.bench_dir_ok = bench_dir_ok
        self.site_dir_ok = site_dir_ok
        self.drop_site_ok = drop_site_ok
        self.get_archive_ok = get_archive_ok
        self.prune_ok = prune_ok
        self.archive_bytes = archive_bytes
        self.backup_ok = backup_ok
        self.artifacts = _artifacts() if artifacts is None else artifacts
        self._backed_up = False
        self.calls: list = []
        self.get_archive_path: str | None = None
        self._dropped = False
        self._last_code = 0
        self.client = types.SimpleNamespace(api=FakeAPI(self))

    def reload(self):
        pass

    def _run_shell(self, cmd):
        cmd_str = cmd[-1] if isinstance(cmd, list) else cmd
        self.calls.append(cmd_str)
        if "drop-site" in cmd_str:
            if self.drop_site_ok:
                self._dropped = True
                return 0, "Dropped site\n"
            return 1, "Error dropping site\n"
        return 0, ""

    def exec_run(self, cmd, workdir=None, **kwargs):
        self.calls.append(cmd)
        if isinstance(cmd, str):
            if "bench --site" in cmd and "backup --with-files" in cmd:
                if not self.backup_ok:
                    return (1, b"Backup failed for Site task.localhost. Database corrupted\n")
                self._backed_up = True
                return (0, b"Backup for Site task.localhost has been successfully completed\n")
            if cmd == f"ls -1t {BACKUPS}":
                return (0, "\n".join(self.artifacts if self._backed_up else []).encode())
            return (1, b"")
        if cmd[:2] == ["test", "-d"]:
            target = cmd[2]
            if target == f"{BENCH}/sites":
                return (0 if self.bench_dir_ok else 1, b"")
            if target == f"{BENCH}/archived/sites/{SITE}":
                return (0 if self._dropped else 1, b"")
            return (0 if self.site_dir_ok else 1, b"")
        if cmd[0] == "find":
            return (0, f"{SITE}\n".encode() if self._dropped else b"")
        if cmd[:2] == ["rm", "-rf"]:
            return (0 if self.prune_ok else 1, b"")
        return (0, b"")

    def get_archive(self, path):
        if path.startswith(f"{BACKUPS}/"):
            name = path.rsplit("/", 1)[1]
            data = self.artifacts.get(name)
            if data is None:
                raise FileNotFoundError(path)
            return ([_tar_bytes(name, data)], {"name": name, "size": len(data)})
        self.get_archive_path = path
        if not self.get_archive_ok:
            raise RuntimeError("boom")
        return ([self.archive_bytes], {"size": len(self.archive_bytes)})


@pytest.fixture()
def container(monkeypatch):
    c = FakeContainer()
    monkeypatch.setattr(core_docker, "get_frappe_container", lambda _p: c)
    monkeypatch.setattr(resolvers, "cached_benches", lambda _p: [{"path": BENCH}])
    return c


@pytest.fixture(autouse=True)
def isolated_archive_dir(tmp_path, monkeypatch):
    """Never let a unit test touch the real ``~/.cwcli/archive``."""
    monkeypatch.setattr(core_rm_site, "cwcli_home", lambda: tmp_path)
    return tmp_path


# --------------------------------------------------------------------------- consent


class TestConsent:
    def test_no_consent_is_a_confirm_drop_site_choice_and_nothing_runs(self, container):
        result = core_rm_site.drop_site("proj", SITE)

        assert result.status is Status.NEEDS_CHOICE
        assert result.choice.kind == "confirm_drop_site"
        assert result.choice.param == "consent"
        assert SITE in result.choice.prompt
        assert not any("drop-site" in str(c) for c in container.calls)

    def test_missing_site_refuses_before_any_consent_prompt(self, container):
        container.site_dir_ok = False
        with pytest.raises(CwcliError) as exc:
            core_rm_site.drop_site("proj", SITE)
        assert exc.value.kind is ErrorKind.NOT_FOUND

    def test_consent_true_proceeds(self, container):
        result = core_rm_site.drop_site("proj", SITE, consent=True)
        assert result.status is not Status.NEEDS_CHOICE
        assert any("drop-site" in str(c) for c in container.calls)


# --------------------------------------------------------------------------- success


class TestSuccess:
    def test_full_success_copies_out_and_prunes_the_archive(self, container, tmp_path):
        result = core_rm_site.drop_site("proj", SITE, consent=True)

        assert result.status is Status.OK
        outcome = result.data
        assert outcome.project == "proj"
        assert outcome.site == SITE
        assert outcome.bench_path == BENCH
        assert outcome.ok is True
        assert outcome.archive_pruned_in_container is True
        assert outcome.archived_host_path is not None
        drop_dir = Path(outcome.archived_host_path)
        assert drop_dir.parent == tmp_path / "archive" / "proj_dropped_sites"
        assert drop_dir.name.startswith(f"{SITE}_")
        site_tar = drop_dir / f"{SITE}.tar"
        assert site_tar.read_bytes() == container.archive_bytes
        assert site_tar.stat().st_mode & 0o777 == 0o600
        assert drop_dir.stat().st_mode & 0o777 == 0o700
        assert drop_dir.parent.stat().st_mode & 0o777 == 0o700
        # The verified backup is in the same host archive, byte for byte.
        backups = drop_dir / "backups"
        assert {f.name: f.read_bytes() for f in backups.iterdir()} == container.artifacts

        prune_calls = [c for c in container.calls if isinstance(c, list) and c[:2] == ["rm", "-rf"]]
        assert prune_calls == [["rm", "-rf", f"{BENCH}/archived/sites/{SITE}"]]

    def test_the_drop_site_command_never_carries_the_password(self, container):
        core_rm_site.drop_site("proj", SITE, consent=True, db_root_password="s3cr3t")

        commands = [c for c in container.calls if isinstance(c, str)]
        assert not any("s3cr3t" in c for c in commands)
        assert any("$CWCLI_DB_ROOT_PASSWORD" in c for c in commands)

    def test_drops_with_no_backup_and_never_force(self, container):
        # bench's own drop-time backup is the one --force silently skips on
        # failure, so cwcli takes its own and never passes --force.
        core_rm_site.drop_site("proj", SITE, consent=True)
        commands = [c for c in container.calls if isinstance(c, str) and "drop-site" in c]
        assert commands and all("--no-backup" in c for c in commands)
        assert not any("--force" in c for c in commands)

    def test_the_backup_runs_before_the_drop(self, container):
        core_rm_site.drop_site("proj", SITE, consent=True)
        strings = [c for c in container.calls if isinstance(c, str)]
        backup = next(i for i, c in enumerate(strings) if "backup --with-files" in c)
        drop = next(i for i, c in enumerate(strings) if "drop-site" in c)
        assert backup < drop


# --------------------------------------------------------------------- archive honesty


class TestArchiveHonesty:
    def test_copy_failure_leaves_the_in_container_archive_and_reports_not_ok(self, container):
        container.get_archive_ok = False

        result = core_rm_site.drop_site("proj", SITE, consent=True)

        assert result.status is Status.WARNING
        outcome = result.data
        assert outcome.ok is False
        assert outcome.archived_host_path is None
        assert outcome.archive_pruned_in_container is False
        # Fail closed: never prune what was never verified copied out.
        assert not [c for c in container.calls if isinstance(c, list) and c[:2] == ["rm", "-rf"]]
        assert any(w.code == "rm_site.archive_not_copied" for w in result.warnings)

    def test_prune_failure_reports_not_ok_but_keeps_the_verified_host_copy(self, container):
        container.prune_ok = False

        result = core_rm_site.drop_site("proj", SITE, consent=True)

        assert result.status is Status.WARNING
        outcome = result.data
        assert outcome.ok is False
        assert outcome.archived_host_path is not None
        assert outcome.archive_pruned_in_container is False
        assert any(w.code == "rm_site.archive_not_pruned" for w in result.warnings)

    def test_no_new_archive_entry_is_reported_not_silently_ignored(self, monkeypatch, container):
        # bench dropped the site but the archive path cwcli expects is absent
        # (e.g. --no-backup was somehow effective, or a bench version differs).
        original = container.exec_run

        def _exec_run(cmd, workdir=None, **kwargs):
            if isinstance(cmd, str):
                return original(cmd, workdir=workdir, **kwargs)
            container.calls.append(cmd)
            if cmd[:2] == ["test", "-d"] and cmd[2] == f"{BENCH}/archived/sites/{SITE}":
                return (1, b"")  # never archived, before or after
            if cmd[:2] == ["test", "-d"]:
                return (0, b"")
            return (0, b"")

        monkeypatch.setattr(container, "exec_run", _exec_run)

        result = core_rm_site.drop_site("proj", SITE, consent=True)

        assert result.status is Status.WARNING
        assert result.data.ok is False
        assert result.data.archived_host_path is None
        assert any(w.code == "rm_site.archive_not_found" for w in result.warnings)

    def test_new_suffixed_archive_is_selected_instead_of_stale_exact_path(
        self, monkeypatch, container
    ):
        listings = iter([(0, f"{SITE}\n".encode()), (0, f"{SITE}\n{SITE}1\n".encode())])
        original = container.exec_run

        def _exec_run(cmd, workdir=None, **kwargs):
            if cmd[0] == "find":
                return next(listings)
            return original(cmd, workdir=workdir, **kwargs)

        monkeypatch.setattr(container, "exec_run", _exec_run)

        result = core_rm_site.drop_site("proj", SITE, consent=True)

        assert result.status is Status.OK
        assert container.get_archive_path == f"{BENCH}/archived/sites/{SITE}1"
        assert [c for c in container.calls if isinstance(c, list) and c[:2] == ["rm", "-rf"]] == [
            ["rm", "-rf", f"{BENCH}/archived/sites/{SITE}1"]
        ]

    def test_archive_directory_creation_failure_refuses_before_the_drop(
        self, monkeypatch, container
    ):
        def _mkdir(*args, **kwargs):
            raise PermissionError("read only")

        monkeypatch.setattr(Path, "mkdir", _mkdir)

        with pytest.raises(CwcliError) as exc:
            core_rm_site.drop_site("proj", SITE, consent=True)

        assert exc.value.code == "rm_site.backup_failed"
        assert not any("drop-site" in str(c) for c in container.calls)
        assert not [c for c in container.calls if isinstance(c, list) and c[:2] == ["rm", "-rf"]]

    def test_post_drop_archive_listing_exception_returns_an_honest_outcome(
        self, monkeypatch, container
    ):
        original = container.exec_run
        finds = 0

        def _exec_run(cmd, workdir=None, **kwargs):
            nonlocal finds
            if cmd[0] == "find":
                finds += 1
                if finds == 2:
                    raise RuntimeError("Docker connection lost")
            return original(cmd, workdir=workdir, **kwargs)

        monkeypatch.setattr(container, "exec_run", _exec_run)

        result = core_rm_site.drop_site("proj", SITE, consent=True)

        assert result.status is Status.WARNING
        assert result.data.ok is False
        assert result.data.archived_host_path is None
        assert any(w.code == "rm_site.archive_not_found" for w in result.warnings)

    def test_post_drop_archive_probe_exception_returns_an_honest_outcome(
        self, monkeypatch, container
    ):
        original = container.exec_run

        def _exec_run(cmd, workdir=None, **kwargs):
            if (
                container._dropped
                and cmd[:2] == ["test", "-d"]
                and cmd[2] == f"{BENCH}/archived/sites/{SITE}"
            ):
                raise RuntimeError("Docker connection lost")
            return original(cmd, workdir=workdir, **kwargs)

        monkeypatch.setattr(container, "exec_run", _exec_run)

        result = core_rm_site.drop_site("proj", SITE, consent=True)

        assert result.status is Status.WARNING
        assert result.data.ok is False
        assert result.data.archived_host_path is None
        assert any(w.code == "rm_site.archive_probe_failed" for w in result.warnings)

    def test_post_drop_prune_exception_keeps_the_verified_copy_and_reports_not_ok(
        self, monkeypatch, container
    ):
        original = container.exec_run

        def _exec_run(cmd, workdir=None, **kwargs):
            if cmd[:2] == ["rm", "-rf"]:
                raise RuntimeError("Docker connection lost")
            return original(cmd, workdir=workdir, **kwargs)

        monkeypatch.setattr(container, "exec_run", _exec_run)

        result = core_rm_site.drop_site("proj", SITE, consent=True)

        assert result.status is Status.WARNING
        assert result.data.ok is False
        assert result.data.archived_host_path is not None
        assert result.data.archive_pruned_in_container is False
        assert any(w.code == "rm_site.archive_not_pruned" for w in result.warnings)


# ----------------------------------------------------------------------- hard failures


class TestHardFailures:
    def test_stopped_container_is_a_confirm_start_choice(self, container):
        container.status = "exited"
        result = core_rm_site.drop_site("proj", SITE)

        assert result.status is Status.NEEDS_CHOICE
        assert result.choice.kind == "confirm_start"
        assert not any("drop-site" in str(c) for c in container.calls)

    def test_failed_drop_site_raises_precondition(self, container):
        container.drop_site_ok = False
        with pytest.raises(CwcliError) as exc:
            core_rm_site.drop_site("proj", SITE, consent=True)
        assert exc.value.kind is ErrorKind.PRECONDITION

    def test_shell_unsafe_site_raises_usage(self, container):
        with pytest.raises(CwcliError) as exc:
            core_rm_site.drop_site("proj", "evil;rm -rf /", consent=True)
        assert exc.value.kind is ErrorKind.USAGE

    def test_empty_site_raises_usage(self, container):
        with pytest.raises(CwcliError) as exc:
            core_rm_site.drop_site("proj", "   ", consent=True)
        assert exc.value.kind is ErrorKind.USAGE

    def test_project_name_cannot_escape_the_managed_archive_directory(self, container):
        with pytest.raises(CwcliError) as exc:
            core_rm_site.drop_site("../proj", SITE, consent=True)
        assert exc.value.kind is ErrorKind.USAGE

    @pytest.mark.parametrize("site", ["../other.localhost", "nested/site.localhost", ".", ".."])
    def test_site_must_be_one_plain_directory_name(self, container, site):
        with pytest.raises(CwcliError) as exc:
            core_rm_site.drop_site("proj", site, consent=True)
        assert exc.value.kind is ErrorKind.USAGE

    def test_missing_bench_dir_raises_not_found(self, container):
        container.bench_dir_ok = False
        with pytest.raises(CwcliError) as exc:
            core_rm_site.drop_site("proj", SITE, consent=True)
        assert exc.value.kind is ErrorKind.NOT_FOUND


# -------------------------------------------------------------------- the backup gate


def _dropped_site_dirs(tmp_path):
    root = tmp_path / "archive" / "proj_dropped_sites"
    return sorted(root.iterdir()) if root.exists() else []


class TestBackupGate:
    """Nothing is dropped unless cwcli's own verified backup landed on the host first.

    The defect these pin: ``bench drop-site --force`` ignores a failed drop-time
    backup and drops the database anyway, and cwcli reported ``ok: true`` from a
    non-empty archive with no dump in it.
    """

    def _assert_nothing_dropped(self, container, tmp_path):
        assert not any("drop-site" in str(c) for c in container.calls)
        assert not [c for c in container.calls if isinstance(c, list) and c[:2] == ["rm", "-rf"]]
        # The half-written host archive dir of the failed attempt is not left behind.
        assert _dropped_site_dirs(tmp_path) == []

    def test_a_failed_bench_backup_drops_nothing_and_carries_bench_s_error(
        self, container, tmp_path
    ):
        container.backup_ok = False

        with pytest.raises(CwcliError) as exc:
            core_rm_site.drop_site("proj", SITE, consent=True)

        assert exc.value.kind is ErrorKind.PRECONDITION
        assert exc.value.code == "rm_site.backup_failed"
        assert "nothing was dropped" in exc.value.message
        assert "Database corrupted" in exc.value.detail["output"]
        assert f"cwcli run proj --path {BENCH} -i drop-site {SITE} --no-backup" in exc.value.hint
        self._assert_nothing_dropped(container, tmp_path)

    @pytest.mark.parametrize(
        "artifacts",
        [
            pytest.param({k: v for k, v in _artifacts().items() if k != DUMP}, id="no-dump"),
            pytest.param({**_artifacts(), DUMP: b""}, id="empty-dump"),
            pytest.param({**_artifacts(), DUMP: None}, id="dump-not-copyable"),
        ],
    )
    def test_an_unverified_backup_drops_nothing(self, container, tmp_path, artifacts):
        # bench backup exits 0, but this run's artifacts do not verify on the host.
        container.artifacts = artifacts

        with pytest.raises(CwcliError) as exc:
            core_rm_site.drop_site("proj", SITE, consent=True)

        assert exc.value.code == "rm_site.backup_failed"
        self._assert_nothing_dropped(container, tmp_path)

    def test_a_docker_error_mid_backup_drops_nothing(self, monkeypatch, container, tmp_path):
        original = container.exec_run

        def _exec_run(cmd, workdir=None, **kwargs):
            if isinstance(cmd, str) and "backup --with-files" in cmd:
                raise RuntimeError("Docker connection lost")
            return original(cmd, workdir=workdir, **kwargs)

        monkeypatch.setattr(container, "exec_run", _exec_run)

        with pytest.raises(CwcliError) as exc:
            core_rm_site.drop_site("proj", SITE, consent=True)

        assert exc.value.code == "rm_site.backup_failed"
        assert "Docker connection lost" in exc.value.detail["output"]
        self._assert_nothing_dropped(container, tmp_path)

    def test_drop_backed_up_site_refuses_without_a_verified_dump(self, container, tmp_path):
        # The drop half is never a drop-without-a-backup, whoever calls it.
        empty = tmp_path / "not-a-backup"
        (empty / "backups").mkdir(parents=True)

        with pytest.raises(CwcliError) as exc:
            core_rm_site.drop_backed_up_site(container, "proj", BENCH, SITE, empty)

        assert exc.value.code == "rm_site.backup_missing"
        assert not any("drop-site" in str(c) for c in container.calls)
