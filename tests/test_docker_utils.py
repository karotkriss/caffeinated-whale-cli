"""Regression test for the WSL Docker-Desktop-stopped misdiagnosis.

On WSL2, ``/usr/bin/docker`` is a symlink into the Docker Desktop cli-tools
mount (``/mnt/wsl/docker-desktop/...``), which vanishes while Desktop is
stopped. ``shutil.which("docker")`` then returns ``None`` exactly as it would
for a genuinely missing install, so ``handle_docker_errors`` used to report
"Docker is not installed." when the truth is "start Docker Desktop".
"""

import pytest
import typer

from caffeinated_whale_cli.utils import docker_utils


@docker_utils.handle_docker_errors
def _guarded():
    return "ok"


class TestWslDockerDesktopStoppedMisdiagnosis:
    def test_wsl_reports_desktop_stopped_not_uninstalled(self, monkeypatch, capsys):
        monkeypatch.setattr(docker_utils.shutil, "which", lambda _: None)
        # simulate the WSL2 kernel release string
        monkeypatch.setattr(
            docker_utils.platform,
            "uname",
            lambda: type("U", (), {"release": "6.6.114.1-microsoft-standard-WSL2"})(),
        )

        with pytest.raises(typer.Exit) as exc_info:
            _guarded()

        assert exc_info.value.exit_code == 1
        err = capsys.readouterr().err
        assert "docker desktop" in err.lower()
        assert "stopped" in err.lower()
        assert "not installed" not in err.lower()

    def test_non_wsl_reports_not_installed(self, monkeypatch, capsys):
        monkeypatch.setattr(docker_utils.shutil, "which", lambda _: None)
        monkeypatch.setattr(
            docker_utils.platform,
            "uname",
            lambda: type("U", (), {"release": "6.6.0-generic"})(),
        )

        with pytest.raises(typer.Exit) as exc_info:
            _guarded()

        assert exc_info.value.exit_code == 1
        err = capsys.readouterr().err
        assert "not installed" in err.lower()
        assert "docker desktop" not in err.lower()
