"""Data-safety regression tests for ``cwcli rm`` (audit findings C1, H5, M11).

Every pre-existing rm test passes ``no_backup=True``, so the backup-before-delete
gate was completely unexercised. These tests drive the backup path with a fake
frappe container and pin the corrected contracts:

- **C1** - a live ``bench backup`` that fails, or whose artifacts do not actually
  land non-empty on the host archive, must NOT delete the named volumes/dir, and
  the command must report failure (never a green "Successfully removed").
- **H5** - a project name that could escape ``PROJECTS_DIR`` (``.``/``..``/
  absolute/separator) is rejected before any ``rmtree`` or volume removal.
- **M11** - volume-removal, directory-removal, backup, and container-removal
  failures are tracked and surfaced as a non-zero exit, not a green check + exit 0.
  A caught container-removal error does not fall through to volume/dir destruction.

The fake-container harness mirrors ``tests/test_inspect_partial_refresh.py``'s
``FakeFrappeContainer`` (records every ``exec_run``, answers the exact probes the
code issues) and ``tests/test_rm_truth.py``'s tmp-filesystem approach.
"""

from unittest.mock import MagicMock

import pytest
import typer

from caffeinated_whale_cli.commands import rm
from caffeinated_whale_cli.utils import docker_utils

BENCH = "/workspace/frappe-bench"


def _db_name(site):
    return f"backup-{site}-database.sql.gz"


def _cfg_name(site):
    return f"backup-{site}-site_config_backup.json"


class FakeFrappeContainer:
    """A running frappe container that answers the exact ``exec_run`` probes
    ``_backup_sites``/``_archive_project_config`` issue, and records every call.

    ``sites`` is the list of site directories. ``backup_ok`` is the ``bench
    backup`` exit result (bool, or per-site dict). ``artifacts`` maps each site
    to ``{filename: bytes}`` - the files ``ls -1t`` reports and ``cat`` returns.
    A value of ``b""`` models a file that copies out empty; a value of ``None``
    models a ``cat`` that fails. ``list_ok=False`` makes the post-backup
    ``ls -1t`` fail.
    """

    def __init__(
        self,
        sites,
        *,
        backup_ok=True,
        artifacts=None,
        list_ok=True,
        bench_path=BENCH,
        name="proj-frappe-1",
    ):
        self.bench_path = bench_path
        self.name = name
        self.status = "running"
        self.labels = {"com.docker.compose.service": "frappe"}
        self.calls: list[str] = []
        self.stopped = False
        self.removed = False
        self.sites = list(sites)
        self._backup_ok = backup_ok
        self._list_ok = list_ok
        if artifacts is None:
            artifacts = {s: {_db_name(s): b"DBDUMPBYTES", _cfg_name(s): b"{}"} for s in self.sites}
        self.artifacts = artifacts

    def _backup_exit(self, site):
        ok = self._backup_ok
        if isinstance(ok, dict):
            ok = ok.get(site, True)
        return 0 if ok else 1

    def exec_run(self, cmd, workdir=None):
        self.calls.append(cmd)
        b = self.bench_path

        if cmd == f"ls -1 {b}/sites":
            listing = ["apps.txt", "common_site_config.json", *self.sites]
            return (0, "\n".join(listing).encode())

        if cmd.startswith("sh -c 'cd ") and "bench --site " in cmd and "backup" in cmd:
            site = cmd.split("bench --site ")[1].split(" ")[0]
            return (self._backup_exit(site), b"backup output")

        for site in self.sites:
            sbdir = f"{b}/sites/{site}/private/backups"
            if cmd == f"ls -1t {sbdir}":
                if not self._list_ok:
                    return (1, b"")
                return (0, "\n".join(self.artifacts.get(site, {}).keys()).encode())
            prefix = f"cat {sbdir}/"
            if cmd.startswith(prefix):
                fname = cmd[len(prefix) :]
                data = self.artifacts.get(site, {}).get(fname)
                if data is None:
                    return (1, b"")
                return (0, data)

        # _archive_project_config probes (compose paths, site_config.json) fail
        # benignly - it archives nothing and still returns True.
        return (1, b"")

    def stop(self):
        self.stopped = True

    def remove(self, v=False, force=False):
        self.removed = True

    def ran_backup(self) -> bool:
        return any("bench --site " in c and "backup" in c for c in self.calls)


def _make_volume(name):
    volume = MagicMock()
    volume.name = name
    return volume


def _make_project_dir(projects_dir, name):
    project_dir = projects_dir / name
    (project_dir / "conf").mkdir(parents=True)
    (project_dir / "conf" / "docker-compose.yml").write_text("# fake compose\n")
    return project_dir


@pytest.fixture()
def cwcli_home(tmp_path, monkeypatch):
    """Redirect PROJECTS_DIR and the archive root (from Path.home()) into tmp."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    monkeypatch.setattr(rm, "PROJECTS_DIR", projects_dir)
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _patch_docker(monkeypatch):
    """Neutralize the @handle_docker_errors daemon check."""
    monkeypatch.setattr(docker_utils.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(docker_utils.docker, "from_env", lambda: MagicMock())


def _wire(monkeypatch, container, volumes):
    """Point _remove_project's collaborators at the fakes."""
    monkeypatch.setattr(rm, "get_project_containers", lambda name: [container])
    monkeypatch.setattr(rm, "get_project_volumes", lambda name: list(volumes))
    monkeypatch.setattr(rm.db_utils, "clear_cache_for_project", lambda name: None)
    monkeypatch.setattr(rm.db_utils, "get_cached_project_data", lambda name: None)


# --------------------------------------------------------------------------- #
# C1 - a failed/unverified backup must not delete data
# --------------------------------------------------------------------------- #


class TestBackupGate:
    def test_failed_bench_backup_blocks_volume_deletion(self, cwcli_home, monkeypatch):
        _patch_docker(monkeypatch)
        project_dir = _make_project_dir(rm.PROJECTS_DIR, "proj")
        container = FakeFrappeContainer(["site1.localhost"], backup_ok=False)
        volumes = [_make_volume("proj_sites"), _make_volume("proj_db-data")]
        _wire(monkeypatch, container, volumes)

        result = rm._remove_project("proj", remove_volumes=True, no_backup=False)

        # The backup was attempted and failed, so NOTHING destructive may run.
        assert container.ran_backup() is True
        assert result["backup_ok"] is False
        assert result["volumes"] == 0
        assert result["dir_removed"] is False
        assert result["failures"], "a failed backup must be recorded as a failure"
        for volume in volumes:
            volume.remove.assert_not_called()
        assert project_dir.exists()

    def test_empty_host_artifact_blocks_volume_deletion(self, cwcli_home, monkeypatch):
        # bench backup exits 0, but the database dump copies out EMPTY (0 bytes),
        # i.e. the "backup" exists only inside the volume we are about to delete.
        _patch_docker(monkeypatch)
        project_dir = _make_project_dir(rm.PROJECTS_DIR, "proj")
        site = "site1.localhost"
        artifacts = {site: {_db_name(site): b"", _cfg_name(site): b"{}"}}
        container = FakeFrappeContainer([site], artifacts=artifacts)
        volumes = [_make_volume("proj_sites")]
        _wire(monkeypatch, container, volumes)

        result = rm._remove_project("proj", remove_volumes=True, no_backup=False)

        assert result["backup_ok"] is False
        assert result["volumes"] == 0
        assert result["dir_removed"] is False
        volumes[0].remove.assert_not_called()
        assert project_dir.exists()

    def test_uncopyable_host_artifact_blocks_volume_deletion(self, cwcli_home, monkeypatch):
        # bench backup exits 0 and the dump is listed, but the copy-out `cat`
        # fails - so nothing lands on the host and deletion must be refused.
        _patch_docker(monkeypatch)
        project_dir = _make_project_dir(rm.PROJECTS_DIR, "proj")
        site = "site1.localhost"
        artifacts = {site: {_db_name(site): None, _cfg_name(site): b"{}"}}
        container = FakeFrappeContainer([site], artifacts=artifacts)
        volumes = [_make_volume("proj_sites")]
        _wire(monkeypatch, container, volumes)

        result = rm._remove_project("proj", remove_volumes=True, no_backup=False)

        assert result["backup_ok"] is False
        assert result["volumes"] == 0
        volumes[0].remove.assert_not_called()
        assert project_dir.exists()

    def test_verified_backup_allows_volume_deletion(self, cwcli_home, monkeypatch):
        # Positive control: a fully verified backup (non-empty db dump on host)
        # lets removal proceed exactly as before.
        _patch_docker(monkeypatch)
        project_dir = _make_project_dir(rm.PROJECTS_DIR, "proj")
        site = "site1.localhost"
        container = FakeFrappeContainer([site])
        volumes = [_make_volume("proj_sites"), _make_volume("proj_db-data")]
        _wire(monkeypatch, container, volumes)

        result = rm._remove_project("proj", remove_volumes=True, no_backup=False)

        assert result["backup_ok"] is True
        assert not result["failures"]
        assert result["volumes"] == 2
        assert result["dir_removed"] is True
        for volume in volumes:
            volume.remove.assert_called_once_with(force=True)
        assert not project_dir.exists()

        # The db dump actually landed non-empty in the archive.
        archives = list(
            (cwcli_home / ".cwcli" / "archive").glob(f"proj_*/backups/{site}/*database*")
        )
        assert archives and archives[0].stat().st_size > 0

    def test_no_backup_flag_bypasses_gate(self, cwcli_home, monkeypatch):
        # --no-backup opts out of the backup net entirely: no backup is attempted
        # and removal proceeds (the user explicitly accepted no fresh backup).
        _patch_docker(monkeypatch)
        _make_project_dir(rm.PROJECTS_DIR, "proj")
        container = FakeFrappeContainer(["site1.localhost"], backup_ok=False)
        volumes = [_make_volume("proj_sites")]
        _wire(monkeypatch, container, volumes)

        result = rm._remove_project("proj", remove_volumes=True, no_backup=True)

        assert container.ran_backup() is False
        assert result["backup_ok"] is True
        assert result["volumes"] == 1
        volumes[0].remove.assert_called_once_with(force=True)

    def test_no_volumes_failed_backup_still_removes_directory(self, cwcli_home, monkeypatch):
        # Under --no-volumes the databases (named volumes) are kept, so no data is
        # destroyed. A failed live backup must therefore NOT block removal of the
        # recreatable project directory nor be recorded as a failure - the backup
        # gate applies only when the volumes will actually be deleted.
        _patch_docker(monkeypatch)
        project_dir = _make_project_dir(rm.PROJECTS_DIR, "proj")
        container = FakeFrappeContainer(["site1.localhost"], backup_ok=False)
        volumes = [_make_volume("proj_sites"), _make_volume("proj_db-data")]
        _wire(monkeypatch, container, volumes)

        result = rm._remove_project("proj", remove_volumes=False, no_backup=False)

        # The backup was attempted and failed, but no volume data is being destroyed.
        assert container.ran_backup() is True
        assert result["backup_ok"] is False
        # The volumes are left untouched (the user chose --no-volumes).
        assert result["volumes"] == 0
        for volume in volumes:
            volume.remove.assert_not_called()
        # The recreatable directory is still removed, and no backup-gate failure
        # is recorded, so the command exits cleanly.
        assert result["dir_removed"] is True
        assert not result["failures"]
        assert not project_dir.exists()


class TestBackupSitesReturn:
    """``_backup_sites`` must report success only when every site is captured."""

    def _archive(self, tmp_path):
        d = tmp_path / "archive"
        d.mkdir()
        return d

    def test_all_sites_backed_up_returns_true(self, tmp_path):
        container = FakeFrappeContainer(["a.localhost", "b.localhost"])
        assert rm._backup_sites("proj", container, BENCH, self._archive(tmp_path)) is True

    def test_partial_backup_returns_false(self, tmp_path):
        # One site succeeds, the other's `bench backup` fails -> overall False.
        container = FakeFrappeContainer(
            ["a.localhost", "b.localhost"],
            backup_ok={"a.localhost": True, "b.localhost": False},
        )
        assert rm._backup_sites("proj", container, BENCH, self._archive(tmp_path)) is False

    def test_no_sites_returns_true(self, tmp_path):
        container = FakeFrappeContainer([])
        assert rm._backup_sites("proj", container, BENCH, self._archive(tmp_path)) is True

    def test_empty_dump_returns_false(self, tmp_path):
        site = "a.localhost"
        container = FakeFrappeContainer([site], artifacts={site: {_db_name(site): b""}})
        assert rm._backup_sites("proj", container, BENCH, self._archive(tmp_path)) is False

    def test_no_dump_among_files_returns_false(self, tmp_path):
        # bench backup exits 0 but only a config file exists - no database dump.
        site = "a.localhost"
        container = FakeFrappeContainer([site], artifacts={site: {_cfg_name(site): b"{}"}})
        assert rm._backup_sites("proj", container, BENCH, self._archive(tmp_path)) is False

    def test_secondary_artifact_copy_failure_returns_false(self, tmp_path):
        # The db dump copies out fine, but a companion --with-files artifact fails
        # to copy. Fail closed: a backup missing any part is not trustworthy.
        site = "a.localhost"
        artifacts = {
            site: {
                _db_name(site): b"DBDUMPBYTES",
                f"backup-{site}-files.tar": None,  # cat fails for this one
            }
        }
        container = FakeFrappeContainer([site], artifacts=artifacts)
        assert rm._backup_sites("proj", container, BENCH, self._archive(tmp_path)) is False


# --------------------------------------------------------------------------- #
# H5 - project-name validation before any rmtree / volume op
# --------------------------------------------------------------------------- #


class TestProjectNameValidation:
    @pytest.mark.parametrize("name", ["proj", "my-project", "a_b.localhost"])
    def test_valid_names_accepted(self, cwcli_home, name):
        assert rm._is_valid_project_name(name) is True

    @pytest.mark.parametrize(
        "name",
        ["", ".", "..", "/", "/etc", "a/b", "..\\x", "sub/../../etc", "\0evil"],
    )
    def test_escaping_names_rejected(self, cwcli_home, name):
        assert rm._is_valid_project_name(name) is False

    def test_delete_directory_refuses_escaping_path(self, cwcli_home):
        # PROJECTS_DIR/.. resolves to the tmp root; a sentinel there must survive.
        sentinel = cwcli_home / "SENTINEL"
        sentinel.write_text("keep me")

        assert rm._delete_project_directory("..") is False
        assert sentinel.exists(), "rmtree escaped PROJECTS_DIR and deleted the parent"
        assert rm.PROJECTS_DIR.exists()

    def test_archive_directory_refuses_escaping_path(self, cwcli_home, tmp_path):
        archive_dir = cwcli_home / "archive"
        archive_dir.mkdir()
        # An escaping name must not be archived (returning False keeps the caller
        # from then proceeding to delete).
        assert rm._archive_project_directory("..", archive_dir=archive_dir) is False

    def test_remove_project_rejects_invalid_name(self, cwcli_home, monkeypatch):
        # Defense-in-depth: even a direct call must refuse before touching Docker.
        _patch_docker(monkeypatch)
        called = MagicMock()
        monkeypatch.setattr(rm, "get_project_containers", called)

        result = rm._remove_project("..", remove_volumes=True, no_backup=True)

        assert result["found"] is False
        assert result["failures"]
        called.assert_not_called()

    @pytest.mark.parametrize("name", ["..", ".", "/etc", "a/b"])
    def test_cli_rejects_escaping_name_before_removal(self, cwcli_home, monkeypatch, name):
        _patch_docker(monkeypatch)
        removed = MagicMock()
        monkeypatch.setattr(rm, "_remove_project", removed)
        monkeypatch.setattr(rm.sys.stdin, "isatty", lambda: True)

        with pytest.raises(typer.Exit) as exc:
            rm.rm(
                ctx=MagicMock(),
                verbose=False,
                volumes=True,
                no_backup=True,
                yes=True,
                project_name=[name],
            )

        assert exc.value.exit_code == 1
        removed.assert_not_called()


# --------------------------------------------------------------------------- #
# M11 - honest exit codes / step accounting
# --------------------------------------------------------------------------- #


class TestHonestOutcomes:
    def test_volume_removal_failure_is_recorded(self, cwcli_home, monkeypatch):
        _patch_docker(monkeypatch)
        _make_project_dir(rm.PROJECTS_DIR, "proj")
        container = FakeFrappeContainer(["s.localhost"], name="proj-frappe-1")
        bad_volume = _make_volume("proj_db-data")
        bad_volume.remove.side_effect = RuntimeError("volume in use")
        _wire(monkeypatch, container, [bad_volume])

        result = rm._remove_project("proj", remove_volumes=True, no_backup=True)

        assert result["volumes"] == 0
        assert any("volume" in f for f in result["failures"])

    def test_volume_enumeration_failure_is_recorded(self, cwcli_home, monkeypatch):
        _patch_docker(monkeypatch)
        _make_project_dir(rm.PROJECTS_DIR, "proj")
        container = FakeFrappeContainer(["s.localhost"])
        monkeypatch.setattr(rm, "get_project_containers", lambda name: [container])
        # None = a Docker error enumerating volumes (distinct from "no volumes").
        monkeypatch.setattr(rm, "get_project_volumes", lambda name: None)
        monkeypatch.setattr(rm.db_utils, "clear_cache_for_project", lambda name: None)
        monkeypatch.setattr(rm.db_utils, "get_cached_project_data", lambda name: None)

        result = rm._remove_project("proj", remove_volumes=True, no_backup=True)

        assert any("enumerate" in f for f in result["failures"])

    def test_directory_removal_failure_is_recorded(self, cwcli_home, monkeypatch):
        _patch_docker(monkeypatch)
        _make_project_dir(rm.PROJECTS_DIR, "proj")
        container = FakeFrappeContainer(["s.localhost"])
        _wire(monkeypatch, container, [_make_volume("proj_sites")])

        def boom(_path):
            raise OSError("permission denied")

        monkeypatch.setattr(rm.shutil, "rmtree", boom)

        result = rm._remove_project("proj", remove_volumes=True, no_backup=True)

        assert result["dir_removed"] is False
        assert any("project directory" in f for f in result["failures"])

    def test_container_removal_failure_blocks_destruction(self, cwcli_home, monkeypatch):
        # A caught container-removal error must NOT fall through to destroy the
        # volumes/dir, and it must be recorded as a failure.
        _patch_docker(monkeypatch)
        project_dir = _make_project_dir(rm.PROJECTS_DIR, "proj")
        container = FakeFrappeContainer(["s.localhost"])
        container.remove = MagicMock(side_effect=RuntimeError("daemon error"))
        volumes = [_make_volume("proj_sites")]
        _wire(monkeypatch, container, volumes)

        result = rm._remove_project("proj", remove_volumes=True, no_backup=True)

        assert result["containers"] == 0
        assert result["volumes"] == 0
        assert result["dir_removed"] is False
        assert any("container" in f for f in result["failures"])
        volumes[0].remove.assert_not_called()
        assert project_dir.exists()

    def test_container_name_property_raising_does_not_nameerror(self, cwcli_home, monkeypatch):
        # If ``container.name`` itself raises, the except handler must still run
        # cleanly (it previously hit an unbound ``container_name`` NameError).
        _patch_docker(monkeypatch)
        _make_project_dir(rm.PROJECTS_DIR, "proj")

        class BadNameContainer:
            status = "running"
            labels = {"com.docker.compose.service": "frappe"}

            @property
            def name(self):
                raise KeyError("Name")

            def exec_run(self, cmd, workdir=None):
                return (1, b"")  # archive-config probes fail benignly

            def stop(self):
                pass

            def remove(self, v=False, force=False):
                pass

        container = BadNameContainer()
        _wire(monkeypatch, container, [_make_volume("proj_sites")])

        # Must not raise NameError; the failure is recorded instead.
        result = rm._remove_project("proj", remove_volumes=True, no_backup=True)

        assert result["containers"] == 0
        assert any("container" in f for f in result["failures"])

    def test_cli_exits_nonzero_when_a_step_fails(self, cwcli_home, monkeypatch, capsys):
        _patch_docker(monkeypatch)
        monkeypatch.setattr(rm.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(
            rm,
            "_remove_project",
            lambda *a, **k: {
                "found": True,
                "orphan": False,
                "containers": 1,
                "volumes": 0,
                "dir_removed": False,
                "backup_ok": True,
                "failures": ["could not remove volume 'proj_db-data'"],
            },
        )

        with pytest.raises(typer.Exit) as exc:
            rm.rm(
                ctx=MagicMock(),
                verbose=False,
                volumes=True,
                no_backup=True,
                yes=True,
                project_name=["proj"],
            )

        assert exc.value.exit_code == 1
        out = capsys.readouterr()
        # No misleading green "Successfully removed" on a partial failure.
        assert "Successfully removed" not in out.out

    def test_cli_exits_zero_on_full_success(self, cwcli_home, monkeypatch, capsys):
        _patch_docker(monkeypatch)
        monkeypatch.setattr(rm.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(
            rm,
            "_remove_project",
            lambda *a, **k: {
                "found": True,
                "orphan": False,
                "containers": 1,
                "volumes": 2,
                "dir_removed": True,
                "backup_ok": True,
                "failures": [],
            },
        )

        # A clean run must NOT raise typer.Exit and must print the success line.
        rm.rm(
            ctx=MagicMock(),
            verbose=False,
            volumes=True,
            no_backup=True,
            yes=True,
            project_name=["proj"],
        )
        assert "Successfully removed" in capsys.readouterr().out
