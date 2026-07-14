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

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from caffeinated_whale_cli.commands import inspect as inspect_mod
from caffeinated_whale_cli.commands import rm
from caffeinated_whale_cli.commands import utils as cmd_utils
from caffeinated_whale_cli.utils import cache, docker_utils


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
        monkeypatch.setattr(inspect_mod.db_utils, "get_cached_project_data", lambda name: None)
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
        # Frappe container exists but is stopped.
        monkeypatch.setattr(rm, "_frappe_container_running", lambda name: False)

        recache_called = MagicMock()
        monkeypatch.setattr(rm.cache, "recache_project", recache_called)

        # Stub out the actual teardown: this test only pins the recache-skip
        # contract, which happens before removal. Stubbing keeps it independent of
        # whether Docker is installed (``_remove_project`` is Docker-gated, so on a
        # host without Docker it would raise ``typer.Exit`` - which is NOT a
        # ``SystemExit`` subclass - and crash the test in CI).
        monkeypatch.setattr(
            rm,
            "_remove_project",
            MagicMock(
                return_value={
                    "found": True,
                    "containers": 0,
                    "volumes": 0,
                    "dir_removed": False,
                    "orphan": False,
                }
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


class TestStoppedContainerSkipsExecBackup:
    """A present-but-stopped frappe container must not be shelled into for backup.

    ``_backup_sites`` and ``_archive_project_config`` both ``exec_run`` inside the
    frappe container, which only works while it is running. For a stopped project
    they would just emit confusing "could not backup/archive" warnings, so
    ``_remove_project`` skips them and emits the same clean "no container was
    running" warning as the orphan path - while still tearing everything down via
    the host-side conf/ archive.
    """

    def _patch_docker(self, monkeypatch):
        monkeypatch.setattr(docker_utils.shutil, "which", lambda _name: "/usr/bin/docker")
        monkeypatch.setattr(docker_utils.docker, "from_env", lambda: MagicMock())

    def test_stopped_container_is_not_exec_backed_up(self, cwcli_home, monkeypatch, capsys):
        self._patch_docker(monkeypatch)
        project_dir = cwcli_home / "projects" / "proj"
        (project_dir / "conf").mkdir(parents=True)
        (project_dir / "conf" / "docker-compose.yml").write_text("# fake\n")
        monkeypatch.setattr(rm, "PROJECTS_DIR", cwcli_home / "projects")

        container = _stopped_frappe_container()
        container.name = "proj-frappe-1"
        volumes = [MagicMock(name="vol")]
        volumes[0].name = "proj_db-data"

        monkeypatch.setattr(rm, "get_project_containers", lambda name: [container])
        monkeypatch.setattr(rm, "get_project_volumes", lambda name: list(volumes))
        monkeypatch.setattr(rm.db_utils, "clear_cache_for_project", lambda name: None)

        backup = MagicMock()
        archive_cfg = MagicMock()
        monkeypatch.setattr(rm, "_backup_sites", backup)
        monkeypatch.setattr(rm, "_archive_project_config", archive_cfg)

        # no_backup=False: a running container WOULD be backed up; a stopped one
        # must NOT be (it cannot be exec'd into).
        result = rm._remove_project("proj", remove_volumes=True, no_backup=False)

        backup.assert_not_called()
        archive_cfg.assert_not_called()
        # Teardown still happens via the host-side conf/ archive.
        assert result["containers"] == 1
        assert result["volumes"] == 1
        assert result["dir_removed"] is True
        assert not project_dir.exists()
        # And the user is told a live backup was impossible.
        assert "no container was running" in capsys.readouterr().err.lower()


@pytest.fixture()
def cwcli_home(tmp_path, monkeypatch):
    """Redirect the archive root (derived from Path.home()) into tmp."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # Some modules cache Path.home(); make sure the archive lands in tmp.
    assert Path.home() == tmp_path
    return tmp_path
