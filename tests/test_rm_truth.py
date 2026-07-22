"""Regression tests for ``cwcli rm`` actually deleting what it claims (issue #19).

Before this fix ``rm`` only ran ``Container.remove(v=True)``, which removes a
container's *anonymous* volumes but leaves the named compose volumes (``sites``,
``db-data``) - so the databases and sites survived despite the
"ALL DATA WILL BE PERMANENTLY LOST" banner. It also never deleted the local
project directory at ``~/.cwcli/projects/{name}/``, so removed projects lingered
on disk.

These tests pin the corrected behavior:
- named volumes are removed when the user opts into volume deletion,
- named volumes are preserved with ``--no-volumes``,
- the local project directory is always deleted (it is config, not data),
- a copy of the project directory is archived before deletion.
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

from caffeinated_whale_cli.commands import rm
from caffeinated_whale_cli.core import rm as core_rm
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from caffeinated_whale_cli.utils import db_utils, docker_utils

# The destructive logic moved to ``core.rm`` (batch 12). ``_patch_attr`` patches
# every rm module that defines the attribute (core, and the frontend for its
# own run-state reads); the adapters keep the moved helpers' old test surface.
_RM_MODULES = (core_rm, rm)


def _patch_attr(monkeypatch, attr, value):
    for mod in _RM_MODULES:
        if hasattr(mod, attr):
            monkeypatch.setattr(mod, attr, value)


def _render_event(event):
    if isinstance(event, core_rm.RmNotice):
        print(f"  {event.text}")
    elif isinstance(event, core_rm.RmWarning):
        print(f"Warning: {event.text}", file=sys.stderr)
        if event.hint:
            print(event.hint, file=sys.stderr)
    elif isinstance(event, core_rm.RmError):
        print(f"Error: {event.text}", file=sys.stderr)


def _remove_project(name, *, remove_volumes, no_backup):
    result = core_rm.remove(
        name, remove_volumes=remove_volumes, no_backup=no_backup, on_event=_render_event
    )
    d = result.data
    return {
        "found": d.found,
        "orphan": d.orphan,
        "containers": d.containers_removed,
        "volumes": d.volumes_removed,
        "dir_removed": d.dir_removed,
        "backup_ok": d.backup_ok,
        "failures": list(d.failures),
    }


def _delete_project_directory(project, verbose=False, failures=None):
    return core_rm._delete_project_directory(project, _render_event, failures=failures)


def _archive_project_directory(project, archive_dir=None, verbose=False):
    return core_rm._archive_project_directory(project, _render_event, archive_dir=archive_dir)


def _remove_named_volumes(project, verbose=False, status=None, failures=None):
    return core_rm._remove_named_volumes(project, _render_event, failures=failures)


def _make_container(service="frappe", name="proj-frappe-1"):
    """Build a fake compose container that reports as already stopped."""
    container = MagicMock()
    container.name = name
    container.status = "exited"
    container.labels = {"com.docker.compose.service": service}
    return container


def _make_volume(name):
    volume = MagicMock()
    volume.name = name
    return volume


@pytest.fixture()
def cwcli_home(tmp_path, monkeypatch):
    """Redirect both the project directory and the archive root into tmp."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    _patch_attr(monkeypatch, "PROJECTS_DIR", projects_dir)
    # Archive root is derived from Path.home(); steer it at tmp too.
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _make_project_dir(projects_dir, name):
    project_dir = projects_dir / name
    (project_dir / "conf").mkdir(parents=True)
    (project_dir / "conf" / "docker-compose.yml").write_text("# fake compose\n")
    return project_dir


class TestRemoveNamedVolumes:
    """``_remove_named_volumes`` must remove every labeled compose volume."""

    def test_removes_all_named_volumes(self):
        volumes = [_make_volume("proj_sites"), _make_volume("proj_db-data")]
        with patch.object(core_rm, "get_project_volumes", return_value=volumes):
            removed = _remove_named_volumes("proj")

        assert removed == 2
        for volume in volumes:
            volume.remove.assert_called_once_with(force=True)

    def test_no_volumes_is_safe(self):
        with patch.object(core_rm, "get_project_volumes", return_value=[]):
            assert _remove_named_volumes("proj") == 0


class TestRemoveProjectDirectory:
    """Archive-then-delete: the dir is copied to the archive before deletion."""

    def test_deletes_directory(self, cwcli_home):
        """`_delete_project_directory` removes the project directory."""
        projects_dir = core_rm.PROJECTS_DIR
        project_dir = _make_project_dir(projects_dir, "proj")
        assert project_dir.exists()

        assert _delete_project_directory("proj") is True
        assert not project_dir.exists()

    def test_archives_before_deleting(self, cwcli_home, tmp_path):
        """`_archive_project_directory` copies conf out before deletion."""
        projects_dir = core_rm.PROJECTS_DIR
        _make_project_dir(projects_dir, "proj")
        archive_dir = tmp_path / "archive"
        archive_dir.mkdir()

        assert _archive_project_directory("proj", archive_dir=archive_dir) is True
        assert _delete_project_directory("proj") is True

        archived = archive_dir / "project_files" / "conf" / "docker-compose.yml"
        assert archived.exists()
        assert not (projects_dir / "proj").exists()

    def test_missing_directory_is_noop(self, cwcli_home):
        # No directory created; archiving has nothing to do but reports success,
        # and deletion reports that nothing was removed.
        assert _archive_project_directory("ghost") is True
        assert _delete_project_directory("ghost") is False

    def test_archives_only_conf_not_whole_bench(self, cwcli_home, tmp_path):
        # The real project dir is the frappe-docker devcontainer bind mount: a
        # large frappe-bench whose virtualenv/node_modules contain dangling
        # symlinks that do not resolve on the host. The previous implementation
        # copytree'd the whole tree and raised on those dangling symlinks, which
        # aborted removal and left the volume + dir behind (issue #19 again).
        # Archiving only conf/ must succeed regardless, and must not drag the
        # heavy bench into the archive.
        projects_dir = core_rm.PROJECTS_DIR
        project_dir = _make_project_dir(projects_dir, "proj")
        bench = project_dir / "frappe-bench" / "env" / "bin"
        bench.mkdir(parents=True)
        # A dangling symlink that resolves to nothing on the host - exactly what
        # broke copytree(symlinks=False) in the live E2E.
        (bench / "python").symlink_to("/nonexistent/container/python")

        archive_dir = tmp_path / "archive"
        archive_dir.mkdir()

        assert _archive_project_directory("proj", archive_dir=archive_dir) is True
        # Only the cwcli config landed in the archive...
        assert (archive_dir / "project_files" / "conf" / "docker-compose.yml").exists()
        # ...and the heavy bench (with its dangling symlink) was NOT archived.
        assert not (archive_dir / "project_files" / "frappe-bench").exists()

        # And the whole directory, dangling symlink included, deletes cleanly
        # (rmtree does not follow symlinks).
        assert _delete_project_directory("proj") is True
        assert not project_dir.exists()

    def test_archive_succeeds_when_no_conf_dir(self, cwcli_home, tmp_path):
        # An older/partial layout with no conf/ has no cwcli config to preserve;
        # archiving reports success so the caller may still delete the directory.
        project_dir = core_rm.PROJECTS_DIR / "proj"
        project_dir.mkdir(parents=True)
        archive_dir = tmp_path / "archive"
        archive_dir.mkdir()

        assert _archive_project_directory("proj", archive_dir=archive_dir) is True
        assert not (archive_dir / "project_files").exists()

    def test_failed_archive_reports_false(self, cwcli_home, tmp_path, monkeypatch):
        # If the copy raises, the archive step must report failure so the caller
        # refuses to delete anything that was not safely archived.
        _make_project_dir(core_rm.PROJECTS_DIR, "proj")
        archive_dir = tmp_path / "archive"
        archive_dir.mkdir()

        def boom(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(core_rm.shutil, "copytree", boom)

        assert _archive_project_directory("proj", archive_dir=archive_dir) is False
        # The directory is left intact for the caller to preserve.
        assert (core_rm.PROJECTS_DIR / "proj").exists()


class TestRemoveProjectEndToEnd:
    """Integration: ``_remove_project`` ties volumes + directory together."""

    def _patch_docker(self, monkeypatch):
        """Neutralize the @handle_docker_errors daemon check."""
        monkeypatch.setattr(docker_utils.shutil, "which", lambda _name: "/usr/bin/docker")
        fake_client = MagicMock()
        monkeypatch.setattr(docker_utils.docker, "from_env", lambda: fake_client)

    def test_volumes_and_directory_removed(self, cwcli_home, monkeypatch):
        self._patch_docker(monkeypatch)
        projects_dir = core_rm.PROJECTS_DIR
        project_dir = _make_project_dir(projects_dir, "proj")

        container = _make_container()
        volumes = [_make_volume("proj_sites"), _make_volume("proj_db-data")]

        _patch_attr(monkeypatch, "get_project_containers", lambda name: [container])
        _patch_attr(monkeypatch, "get_project_volumes", lambda name: list(volumes))
        _patch_attr(monkeypatch, "get_project_networks", lambda name: [])
        monkeypatch.setattr(db_utils, "clear_cache_for_project", lambda name: None)

        result = _remove_project("proj", remove_volumes=True, no_backup=True)

        assert result["found"] is True
        assert result["orphan"] is False
        assert result["containers"] == 1
        assert result["volumes"] == 2
        assert result["dir_removed"] is True
        container.remove.assert_called_once_with(v=True, force=True)
        for volume in volumes:
            volume.remove.assert_called_once_with(force=True)
        assert not project_dir.exists()

    def test_no_volumes_keeps_volumes_but_removes_directory(self, cwcli_home, monkeypatch):
        self._patch_docker(monkeypatch)
        projects_dir = core_rm.PROJECTS_DIR
        project_dir = _make_project_dir(projects_dir, "proj")

        container = _make_container()
        volumes = [_make_volume("proj_sites")]

        _patch_attr(monkeypatch, "get_project_containers", lambda name: [container])
        get_volumes = MagicMock(return_value=list(volumes))
        _patch_attr(monkeypatch, "get_project_volumes", get_volumes)
        _patch_attr(monkeypatch, "get_project_networks", lambda name: [])
        monkeypatch.setattr(db_utils, "clear_cache_for_project", lambda name: None)

        result = _remove_project("proj", remove_volumes=False, no_backup=True)

        assert result["found"] is True
        assert result["containers"] == 1
        assert result["dir_removed"] is True
        container.remove.assert_called_once_with(v=False, force=True)
        # Named volumes must be left completely untouched.
        get_volumes.assert_not_called()
        for volume in volumes:
            volume.remove.assert_not_called()
        # The project directory is still removed regardless of --no-volumes.
        assert not project_dir.exists()

    def test_missing_project_is_not_found(self, cwcli_home, monkeypatch):
        self._patch_docker(monkeypatch)
        # No containers, no volumes, no project directory -> genuine "not found".
        _patch_attr(monkeypatch, "get_project_containers", lambda name: [])
        _patch_attr(monkeypatch, "get_project_volumes", lambda name: [])
        _patch_attr(monkeypatch, "get_project_networks", lambda name: [])
        monkeypatch.setattr(db_utils, "clear_cache_for_project", lambda name: None)

        result = _remove_project("ghost", remove_volumes=True, no_backup=True)

        assert result["found"] is False
        assert result["orphan"] is False

    def test_docker_error_does_not_clean_blindly(self, cwcli_home, monkeypatch):
        self._patch_docker(monkeypatch)
        project_dir = _make_project_dir(core_rm.PROJECTS_DIR, "proj")
        # A Docker connection error surfaces as None from get_project_containers.
        _patch_attr(monkeypatch, "get_project_containers", lambda name: None)
        cleared = MagicMock()
        monkeypatch.setattr(db_utils, "clear_cache_for_project", cleared)

        # Changed BY DESIGN in batch 12: the core RAISES CwcliError(DOCKER) on a
        # Docker connection error (the core.stop precedent), where the old
        # _remove_project returned a found=False dict. Either way, nothing
        # destructive runs.
        with pytest.raises(CwcliError) as exc:
            _remove_project("proj", remove_volumes=True, no_backup=True)

        assert exc.value.kind is ErrorKind.DOCKER
        # Nothing destructive must happen on a blind Docker error.
        assert project_dir.exists()
        cleared.assert_not_called()

    def test_orphan_cleans_volumes_and_directory(self, cwcli_home, monkeypatch, capsys):
        self._patch_docker(monkeypatch)
        project_dir = _make_project_dir(core_rm.PROJECTS_DIR, "proj")
        volumes = [_make_volume("proj_sites"), _make_volume("proj_db-data")]

        # Containers already gone (empty list, NOT a Docker error), volumes and
        # the project directory linger - the orphaned-project scenario.
        _patch_attr(monkeypatch, "get_project_containers", lambda name: [])
        _patch_attr(monkeypatch, "get_project_volumes", lambda name: list(volumes))
        _patch_attr(monkeypatch, "get_project_networks", lambda name: [])
        monkeypatch.setattr(db_utils, "clear_cache_for_project", lambda name: None)

        result = _remove_project("proj", remove_volumes=True, no_backup=True)

        assert result["found"] is True
        assert result["orphan"] is True
        assert result["containers"] == 0
        assert result["volumes"] == 2
        assert result["dir_removed"] is True

        # (a) the config dir is archived under a timestamped archive directory.
        archive_root = cwcli_home / ".cwcli" / "archive"
        archived = list(archive_root.glob("proj_*/project_files/conf/docker-compose.yml"))
        assert archived, "expected the project config to be archived before deletion"
        # (b) the named volumes were removed, and (c) the project dir is gone.
        for volume in volumes:
            volume.remove.assert_called_once_with(force=True)
        assert not project_dir.exists()
        # (d) the user is warned that a live DB backup could not be taken.
        err = capsys.readouterr().err
        assert "no container was running" in err.lower()

    def test_failed_archive_preserves_volumes_and_directory(self, cwcli_home, monkeypatch):
        self._patch_docker(monkeypatch)
        project_dir = _make_project_dir(core_rm.PROJECTS_DIR, "proj")
        container = _make_container()
        volumes = [_make_volume("proj_sites")]

        _patch_attr(monkeypatch, "get_project_containers", lambda name: [container])
        _patch_attr(monkeypatch, "get_project_volumes", lambda name: list(volumes))
        _patch_attr(monkeypatch, "get_project_networks", lambda name: [])
        monkeypatch.setattr(db_utils, "clear_cache_for_project", lambda name: None)

        def boom(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(core_rm.shutil, "copytree", boom)

        result = _remove_project("proj", remove_volumes=True, no_backup=True)

        # Containers are still removed, but nothing that was not archived is.
        assert result["containers"] == 1
        assert result["volumes"] == 0
        assert result["dir_removed"] is False
        for volume in volumes:
            volume.remove.assert_not_called()
        assert project_dir.exists()


class TestArgOrderForgiveness:
    """Flags that trail the project name must still be honored (papercut)."""

    def test_yes_after_project_name_is_recovered(self):
        # `cwcli rm proj --yes` previously parsed `--yes` as a second project
        # name (a variadic argument greedily eats trailing options), so it still
        # prompted and tried to remove a project literally named "--yes".
        names, verbose, yes, no_backup, volumes = rm._recover_trailing_flags(
            ["proj", "--yes"], False, False, False, True
        )
        assert names == ["proj"]
        assert yes is True
        # Untouched flags keep their incoming values.
        assert verbose is False
        assert no_backup is False
        assert volumes is True

    def test_recovers_every_trailing_flag(self):
        names, verbose, yes, no_backup, volumes = rm._recover_trailing_flags(
            ["proj", "-y", "-v", "--no-backup", "--no-volumes"], False, False, False, True
        )
        assert names == ["proj"]
        assert verbose is True
        assert yes is True
        assert no_backup is True
        assert volumes is False

    def test_volumes_flag_after_name_re_enables(self):
        # Defaults flip the right way: --volumes after the name re-enables.
        _, _, _, _, volumes = rm._recover_trailing_flags(
            ["proj", "--volumes"], False, False, False, False
        )
        assert volumes is True

    def test_plain_names_pass_through(self):
        names, *_ = rm._recover_trailing_flags(["a", "b"], False, False, False, True)
        assert names == ["a", "b"]
