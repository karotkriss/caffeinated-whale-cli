"""Regression tests for ``cwcli rm`` on a STOPPED project (deadlock fix).

When a project's containers are not running, the old ``rm`` flow ran its
pre-removal recache inside a Rich ``console.status`` spinner. The recache calls
``inspect`` -> ``ensure_containers_running(require_running=True)``, which issued
an interactive ``questionary.confirm("start the containers?")`` prompt. Because
the spinner owns the terminal, that prompt was painted over and could never
receive input, so ``cwcli rm <stopped-project>`` hung forever.

These tests pin the corrected, non-interactive contract:
- ``ensure_containers_running(prompt=False)`` never prompts; it returns False when
  the containers are not running,
- the recache path (``recache_project`` -> ``inspect``) is wired non-interactively,
  so no ``questionary.confirm`` is reachable from inside the recache spinner,
- ``rm`` skips the recache entirely for a stopped project (a live backup needs a
  running project), rather than auto-starting containers it is about to delete.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from caffeinated_whale_cli.commands import inspect as inspect_mod
from caffeinated_whale_cli.commands import rm
from caffeinated_whale_cli.commands import utils as cmd_utils
from caffeinated_whale_cli.core import rm as core_rm
from caffeinated_whale_cli.core.envelope import Result, Status
from caffeinated_whale_cli.utils import cache, db_utils, docker_utils

# The destructive logic moved to ``core.rm`` (batch 12); the transient-start /
# run-state reads stay on the frontend. ``_patch_attr`` patches every rm module
# that defines the attribute; ``_remove_project`` adapts the core's typed Result
# back to the old dict for the moved fail-closed tests.
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


def _stopped_frappe_container():
    container = MagicMock()
    container.status = "exited"
    container.labels = {"com.docker.compose.service": "frappe"}
    return container


class TestEnsureContainersRunningNonInteractive:
    """``ensure_containers_running(prompt=False)`` must never ask a question."""

    def test_stopped_container_returns_false_without_prompting(self):
        container = _stopped_frappe_container()
        with (
            patch.object(cmd_utils, "get_frappe_container", return_value=container),
            patch.object(cmd_utils, "questionary") as fake_questionary,
        ):
            result = cmd_utils.ensure_containers_running("proj", require_running=True, prompt=False)

        assert result is False
        # The whole point: no interactive prompt is issued in non-interactive mode.
        fake_questionary.confirm.assert_not_called()

    def test_running_container_returns_true_without_prompting(self):
        container = _stopped_frappe_container()
        container.status = "running"
        with (
            patch.object(cmd_utils, "get_frappe_container", return_value=container),
            patch.object(cmd_utils, "questionary") as fake_questionary,
        ):
            result = cmd_utils.ensure_containers_running("proj", require_running=True, prompt=False)

        assert result is True
        fake_questionary.confirm.assert_not_called()


class TestRecacheIsNonInteractive:
    """The recache path must not be able to prompt under the spinner."""

    def test_recache_of_stopped_project_never_prompts(self, monkeypatch):
        # Make inspect believe there is no cached data so it proceeds to the
        # ensure_containers_running gate, and the frappe container is stopped.
        container = _stopped_frappe_container()
        monkeypatch.setattr(db_utils, "get_cached_project_data", lambda name: None)
        monkeypatch.setattr(cache.db_utils, "clear_cache_for_project", lambda name: None)
        monkeypatch.setattr(cmd_utils, "get_frappe_container", lambda name: container)

        with patch.object(cmd_utils, "questionary") as fake_questionary:
            ok = cache.recache_project("proj")

        # A stopped project cannot be inspected, so recache degrades to False -
        # but it must do so WITHOUT ever reaching an interactive prompt.
        assert ok is False
        fake_questionary.confirm.assert_not_called()


class TestFrappeContainerRunning:
    """``_frappe_container_running`` drives whether rm even attempts a recache."""

    def test_running(self):
        """`_frappe_container_running` reports True for a running frappe container."""
        c = _stopped_frappe_container()
        c.status = "running"
        with patch.object(rm, "get_project_containers", return_value=[c]):
            assert rm._frappe_container_running("proj") is True

    def test_stopped(self):
        """`_frappe_container_running` reports False when the frappe container is stopped."""
        with patch.object(rm, "get_project_containers", return_value=[_stopped_frappe_container()]):
            assert rm._frappe_container_running("proj") is False

    def test_no_containers(self):
        """`_frappe_container_running` reports False when the project has no containers."""
        with patch.object(rm, "get_project_containers", return_value=[]):
            assert rm._frappe_container_running("proj") is False

    def test_docker_error(self):
        """`_frappe_container_running` reports False when the Docker lookup fails."""
        with patch.object(rm, "get_project_containers", return_value=None):
            assert rm._frappe_container_running("proj") is False

    def test_no_frappe_service(self):
        """`_frappe_container_running` reports False when no container is the frappe service."""
        other = MagicMock()
        other.labels = {"com.docker.compose.service": "db"}
        with patch.object(rm, "get_project_containers", return_value=[other]):
            assert rm._frappe_container_running("proj") is False


class TestRmStoppedProjectSkipsRecache:
    """End-to-end: rm of a stopped project skips recache and never prompts there."""

    def test_stopped_project_skips_recache_and_does_not_prompt(self, monkeypatch):
        # Neutralize the @handle_docker_errors daemon check on rm.rm() so this
        # test stays independent of whether Docker is actually installed/running.
        monkeypatch.setattr(docker_utils.shutil, "which", lambda _name: "/usr/bin/docker")
        monkeypatch.setattr(docker_utils.docker, "from_env", lambda: MagicMock())

        # Frappe container exists but is stopped.
        monkeypatch.setattr(rm, "_frappe_container_running", lambda name: False)

        recache_called = MagicMock()
        monkeypatch.setattr(rm.cache, "recache_project", recache_called)

        # Stub out the actual teardown: this test only pins the recache-skip
        # contract, which happens before removal. Stubbing keeps it independent of
        # whether Docker is installed (``core.remove`` is Docker-gated, so on a
        # host without Docker it would raise ``CwcliError`` and crash the test).
        monkeypatch.setattr(
            rm.core_rm,
            "remove",
            MagicMock(
                return_value=Result(
                    status=Status.OK,
                    data=core_rm.RemovalOutcome(
                        project="proj",
                        found=True,
                        orphan=False,
                        containers_removed=0,
                        volumes_removed=0,
                        dir_removed=False,
                        backup_ok=True,
                        failures=[],
                    ),
                )
            ),
        )

        # questionary.confirm must NOT be reached from inside a recache spinner.
        fake_confirm = MagicMock()
        fake_confirm.ask.return_value = True
        monkeypatch.setattr(rm.questionary, "confirm", MagicMock(return_value=fake_confirm))

        # stdin is a tty so no piped names are read; pass the name as an argument.
        monkeypatch.setattr(rm.sys.stdin, "isatty", lambda: True)

        try:
            rm.rm(ctx=MagicMock(), project_name=["proj"])
        except SystemExit:
            pass  # typer.Exit is possible on some flow exits

        # Recache must have been skipped entirely for the stopped project.
        recache_called.assert_not_called()


class TestStoppedRemoveProjectFailsClosed:
    """A present-but-stopped frappe container on the --volumes path must FAIL CLOSED.

    A live ``bench backup`` needs a running frappe + mariadb, so ``_remove_project``
    cannot back up a stopped project itself. Rather than delete the databases with
    no backup (the old, accepted-tradeoff bug), the core now refuses: it marks the
    backup not-OK so the EARLY gate aborts before any container is removed, keeping
    every byte intact and reporting a failure. The transient start-for-backup is
    orchestrated by the CLI frontend (``rm()``) BEFORE ``_remove_project`` runs;
    reaching the stopped branch here means the start never happened, so fail closed.
    ``--no-backup`` (delete without a backup) and ``--no-volumes`` (destroy no data)
    still proceed.
    """

    def _patch_docker(self, monkeypatch):
        monkeypatch.setattr(docker_utils.shutil, "which", lambda _name: "/usr/bin/docker")
        monkeypatch.setattr(docker_utils.docker, "from_env", lambda: MagicMock())

    def _wire(self, monkeypatch, cwcli_home, container, volumes):
        project_dir = cwcli_home / "projects" / "proj"
        (project_dir / "conf").mkdir(parents=True)
        (project_dir / "conf" / "docker-compose.yml").write_text("# fake\n")
        _patch_attr(monkeypatch, "PROJECTS_DIR", cwcli_home / "projects")
        _patch_attr(monkeypatch, "get_project_containers", lambda name: [container])
        _patch_attr(monkeypatch, "get_project_volumes", lambda name: list(volumes))
        monkeypatch.setattr(db_utils, "clear_cache_for_project", lambda name: None)
        return project_dir

    def test_stopped_volumes_backup_is_refused_not_deleted(self, cwcli_home, monkeypatch, capsys):
        self._patch_docker(monkeypatch)
        container = _stopped_frappe_container()
        container.name = "proj-frappe-1"
        container.remove = MagicMock()
        container.stop = MagicMock()
        volumes = [MagicMock(name="vol")]
        volumes[0].name = "proj_db-data"
        project_dir = self._wire(monkeypatch, cwcli_home, container, volumes)

        backup = MagicMock()
        archive_cfg = MagicMock()
        monkeypatch.setattr(core_rm, "_backup_sites", backup)
        monkeypatch.setattr(core_rm, "_archive_project_config", archive_cfg)

        result = _remove_project("proj", remove_volumes=True, no_backup=False)

        # No exec backup attempted (cannot exec into a stopped container)...
        backup.assert_not_called()
        archive_cfg.assert_not_called()
        # ...and CRUCIALLY nothing is destroyed: the early gate aborts.
        assert result["backup_ok"] is False
        assert result["failures"]  # non-empty -> non-zero exit
        assert result["containers"] == 0
        assert result["volumes"] == 0
        assert result["dir_removed"] is False
        container.remove.assert_not_called()
        container.stop.assert_not_called()
        assert not volumes[0].remove.called
        assert project_dir.exists()  # bench tree preserved
        err = capsys.readouterr().err.lower()
        assert "not running" in err

    def test_stopped_no_backup_still_deletes(self, cwcli_home, monkeypatch):
        """``--no-backup`` is the escape hatch: a stopped project is deleted."""
        self._patch_docker(monkeypatch)
        container = _stopped_frappe_container()
        container.name = "proj-frappe-1"
        volumes = [MagicMock(name="vol")]
        volumes[0].name = "proj_db-data"
        project_dir = self._wire(monkeypatch, cwcli_home, container, volumes)

        result = _remove_project("proj", remove_volumes=True, no_backup=True)

        assert result["backup_ok"] is True
        assert not result["failures"]
        assert result["containers"] == 1
        assert result["volumes"] == 1
        assert result["dir_removed"] is True
        assert not project_dir.exists()

    def test_stopped_no_volumes_still_removes_without_backup(self, cwcli_home, monkeypatch):
        """``--no-volumes`` destroys no data, so a stopped project still cleans up
        its containers + directory and keeps the volumes (no failure)."""
        self._patch_docker(monkeypatch)
        container = _stopped_frappe_container()
        container.name = "proj-frappe-1"
        volumes = [MagicMock(name="vol")]
        volumes[0].name = "proj_db-data"
        project_dir = self._wire(monkeypatch, cwcli_home, container, volumes)

        result = _remove_project("proj", remove_volumes=False, no_backup=False)

        assert result["backup_ok"] is True
        assert not result["failures"]
        assert result["containers"] == 1
        assert result["volumes"] == 0  # volumes preserved
        assert not volumes[0].remove.called
        assert result["dir_removed"] is True
        assert not project_dir.exists()

    def test_orphan_volumes_backup_is_refused(self, cwcli_home, monkeypatch, capsys):
        """An orphan (no containers) can never be started, so on the --volumes
        backup path it is refused too (route through --no-backup to clean up)."""
        self._patch_docker(monkeypatch)
        volumes = [MagicMock(name="vol")]
        volumes[0].name = "proj_db-data"
        project_dir = cwcli_home / "projects" / "proj"
        (project_dir / "conf").mkdir(parents=True)
        _patch_attr(monkeypatch, "PROJECTS_DIR", cwcli_home / "projects")
        _patch_attr(monkeypatch, "get_project_containers", lambda name: [])
        _patch_attr(monkeypatch, "get_project_volumes", lambda name: list(volumes))
        monkeypatch.setattr(db_utils, "clear_cache_for_project", lambda name: None)

        result = _remove_project("proj", remove_volumes=True, no_backup=False)

        assert result["orphan"] is True
        assert result["backup_ok"] is False
        assert result["failures"]
        assert result["volumes"] == 0
        assert not volumes[0].remove.called
        assert project_dir.exists()
        assert "no containers to start" in capsys.readouterr().err.lower()

    def test_orphan_no_backup_cleans_up(self, cwcli_home, monkeypatch):
        """An orphan WITH --no-backup still cleans up its leftover volume + dir."""
        self._patch_docker(monkeypatch)
        volumes = [MagicMock(name="vol")]
        volumes[0].name = "proj_db-data"
        project_dir = cwcli_home / "projects" / "proj"
        (project_dir / "conf").mkdir(parents=True)
        _patch_attr(monkeypatch, "PROJECTS_DIR", cwcli_home / "projects")
        _patch_attr(monkeypatch, "get_project_containers", lambda name: [])
        _patch_attr(monkeypatch, "get_project_volumes", lambda name: list(volumes))
        monkeypatch.setattr(db_utils, "clear_cache_for_project", lambda name: None)

        result = _remove_project("proj", remove_volumes=True, no_backup=True)

        assert result["orphan"] is True
        assert not result["failures"]
        assert result["volumes"] == 1
        assert result["dir_removed"] is True
        assert not project_dir.exists()


@pytest.fixture()
def cwcli_home(tmp_path, monkeypatch):
    """Redirect the archive root (derived from Path.home()) into tmp."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # Some modules cache Path.home(); make sure the archive lands in tmp.
    assert Path.home() == tmp_path
    return tmp_path
