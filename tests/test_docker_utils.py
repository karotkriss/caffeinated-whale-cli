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


class TestExecIntoContainerPlatformHandover:
    """The Docker interactive-shell handover must split on ``os.name``.

    On POSIX ``os.execvp`` replaces the process (correct: one console owner,
    signal/process-tree preserved). On Windows ``os.exec*`` does NOT replace -
    it spawns a child and kills Python without the launching shell waiting, so
    PowerShell and the docker-exec bash both read the same console and keystrokes
    interleave. The Windows path must instead run docker exec as a WAITED child
    that inherits the console and propagate its exit code.
    """

    def _cmd_recorders(self, monkeypatch):
        execvp_calls = []
        run_calls = []
        monkeypatch.setattr(
            docker_utils.os, "execvp", lambda file, args: execvp_calls.append((file, args))
        )
        monkeypatch.setattr(
            docker_utils.subprocess,
            "run",
            lambda cmd, *a, **k: (run_calls.append(cmd), type("P", (), {"returncode": 0})())[1],
        )
        return execvp_calls, run_calls

    def test_posix_uses_execvp_not_subprocess(self, monkeypatch):
        monkeypatch.setattr(docker_utils.os, "name", "posix")
        execvp_calls, run_calls = self._cmd_recorders(monkeypatch)

        docker_utils.exec_into_container("frappe15-frappe-1", working_dir="/workspace")

        assert run_calls == []
        assert len(execvp_calls) == 1
        file, args = execvp_calls[0]
        assert file == "docker"
        assert args == ["docker", "exec", "-it", "-w", "/workspace", "frappe15-frappe-1", "bash"]

    def test_windows_uses_waited_subprocess_not_execvp(self, monkeypatch):
        monkeypatch.setattr(docker_utils.os, "name", "nt")
        execvp_calls, run_calls = self._cmd_recorders(monkeypatch)

        with pytest.raises(SystemExit) as exc_info:
            docker_utils.exec_into_container("frappe15-frappe-1", working_dir="/workspace")

        # No process replacement on Windows.
        assert execvp_calls == []
        # Docker exec ran as a single waited child inheriting the console (no
        # stdin/stdout/stderr redirection args).
        assert run_calls == [
            ["docker", "exec", "-it", "-w", "/workspace", "frappe15-frappe-1", "bash"]
        ]
        # Exit code is propagated from the child.
        assert exc_info.value.code == 0

    def test_windows_propagates_nonzero_exit_code(self, monkeypatch):
        monkeypatch.setattr(docker_utils.os, "name", "nt")
        monkeypatch.setattr(docker_utils.os, "execvp", lambda *a: pytest.fail("execvp on nt"))
        monkeypatch.setattr(
            docker_utils.subprocess,
            "run",
            lambda cmd, *a, **k: type("P", (), {"returncode": 3})(),
        )

        with pytest.raises(SystemExit) as exc_info:
            docker_utils.exec_into_container("c", working_dir=None)

        assert exc_info.value.code == 3
