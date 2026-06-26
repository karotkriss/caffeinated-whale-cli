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

from unittest.mock import MagicMock, patch

import pytest

from caffeinated_whale_cli.commands import rm
from caffeinated_whale_cli.utils import docker_utils


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
    monkeypatch.setattr(rm, "PROJECTS_DIR", projects_dir)
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
        with patch.object(rm, "get_project_volumes", return_value=volumes):
            removed = rm._remove_named_volumes("proj")

        assert removed == 2
        for volume in volumes:
            volume.remove.assert_called_once_with(force=True)

    def test_no_volumes_is_safe(self):
        with patch.object(rm, "get_project_volumes", return_value=[]):
            assert rm._remove_named_volumes("proj") == 0


class TestRemoveProjectDirectory:
    """``_remove_project_directory`` deletes the dir and archives a copy first."""

    def test_deletes_directory(self, cwcli_home):
        projects_dir = rm.PROJECTS_DIR
        project_dir = _make_project_dir(projects_dir, "proj")
        assert project_dir.exists()

        assert rm._remove_project_directory("proj") is True
        assert not project_dir.exists()

    def test_archives_before_deleting(self, cwcli_home, tmp_path):
        projects_dir = rm.PROJECTS_DIR
        _make_project_dir(projects_dir, "proj")
        archive_dir = tmp_path / "archive"
        archive_dir.mkdir()

        rm._remove_project_directory("proj", archive_dir=archive_dir)

        archived = archive_dir / "project_files" / "conf" / "docker-compose.yml"
        assert archived.exists()
        assert not (projects_dir / "proj").exists()

    def test_missing_directory_is_noop(self, cwcli_home):
        # No directory created; should report success without raising.
        assert rm._remove_project_directory("ghost") is True


class TestRemoveProjectEndToEnd:
    """Integration: ``_remove_project`` ties volumes + directory together."""

    def _patch_docker(self, monkeypatch):
        """Neutralize the @handle_docker_errors daemon check."""
        monkeypatch.setattr(docker_utils.shutil, "which", lambda _name: "/usr/bin/docker")
        fake_client = MagicMock()
        monkeypatch.setattr(docker_utils.docker, "from_env", lambda: fake_client)

    def test_volumes_and_directory_removed(self, cwcli_home, monkeypatch):
        self._patch_docker(monkeypatch)
        projects_dir = rm.PROJECTS_DIR
        project_dir = _make_project_dir(projects_dir, "proj")

        container = _make_container()
        volumes = [_make_volume("proj_sites"), _make_volume("proj_db-data")]

        monkeypatch.setattr(rm, "get_project_containers", lambda name: [container])
        monkeypatch.setattr(rm, "get_project_volumes", lambda name: list(volumes))
        monkeypatch.setattr(rm.db_utils, "clear_cache_for_project", lambda name: None)

        result = rm._remove_project("proj", remove_volumes=True, no_backup=True)

        assert result == 1
        container.remove.assert_called_once_with(v=True, force=True)
        for volume in volumes:
            volume.remove.assert_called_once_with(force=True)
        assert not project_dir.exists()

    def test_no_volumes_keeps_volumes_but_removes_directory(self, cwcli_home, monkeypatch):
        self._patch_docker(monkeypatch)
        projects_dir = rm.PROJECTS_DIR
        project_dir = _make_project_dir(projects_dir, "proj")

        container = _make_container()
        volumes = [_make_volume("proj_sites")]

        monkeypatch.setattr(rm, "get_project_containers", lambda name: [container])
        get_volumes = MagicMock(return_value=list(volumes))
        monkeypatch.setattr(rm, "get_project_volumes", get_volumes)
        monkeypatch.setattr(rm.db_utils, "clear_cache_for_project", lambda name: None)

        result = rm._remove_project("proj", remove_volumes=False, no_backup=True)

        assert result == 1
        container.remove.assert_called_once_with(v=False, force=True)
        # Named volumes must be left completely untouched.
        get_volumes.assert_not_called()
        for volume in volumes:
            volume.remove.assert_not_called()
        # The project directory is still removed regardless of --no-volumes.
        assert not project_dir.exists()
