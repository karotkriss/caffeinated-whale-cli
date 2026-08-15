"""``open_in_vscode``'s editor-extension checks (``utils/vscode_utils.py``).

Cursor aliases the Microsoft Dev Containers extension id
(``ms-vscode-remote.remote-containers``) to its own publisher id
(``anysphere.remote-containers``), so ``--list-extensions`` on Cursor never
reports the id cwcli used to look for. That made cwcli believe the extension
was always missing on Cursor, so it ran a needless install on every open -
and when Cursor's marketplace returned a transient 5xx for that install, cwcli
aborted the open even though the extension was already installed locally.

No real ``cursor``/``code`` binary is invoked; every ``subprocess.run`` call
is faked.
"""

import subprocess

import pytest
import typer

from caffeinated_whale_cli.utils import vscode_utils

DOCKER_EXT_LISTED = "ms-azuretools.vscode-docker\n"


@pytest.fixture(autouse=True)
def _disable_tips(monkeypatch):
    # TipSpinner just needs a bool; avoid touching real user config.
    monkeypatch.setattr(vscode_utils.config_utils, "get_show_tips", lambda: False)


def _docker_inspect_ok(cmd):
    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def _folder_uri_ok(cmd):
    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


class TestIsDevContainersInstalled:
    def test_recognizes_the_microsoft_id(self, monkeypatch):
        monkeypatch.setattr(
            vscode_utils.subprocess,
            "run",
            lambda cmd, **kw: subprocess.CompletedProcess(
                cmd, 0, stdout="ms-vscode-remote.remote-containers\n", stderr=""
            ),
        )
        assert vscode_utils.is_dev_containers_installed("code") is True

    def test_recognizes_the_cursor_aliased_id(self, monkeypatch):
        monkeypatch.setattr(
            vscode_utils.subprocess,
            "run",
            lambda cmd, **kw: subprocess.CompletedProcess(
                cmd, 0, stdout="anysphere.remote-containers\n", stderr=""
            ),
        )
        assert vscode_utils.is_dev_containers_installed("cursor") is True

    def test_absent_when_neither_id_is_listed(self, monkeypatch):
        monkeypatch.setattr(
            vscode_utils.subprocess,
            "run",
            lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=DOCKER_EXT_LISTED, stderr=""),
        )
        assert vscode_utils.is_dev_containers_installed("cursor") is False


class TestOpenInVscodeCursorDevContainersResilience:
    """The reproduced bug: extension already installed + install call fails 5xx."""

    def test_already_installed_locally_skips_install_entirely_and_opens(self, monkeypatch):
        """No marketplace call is made when the extension is already present."""
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if "--list-extensions" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=DOCKER_EXT_LISTED + "anysphere.remote-containers\n", stderr=""
                )
            if "docker" in cmd and "inspect" in cmd:
                return _docker_inspect_ok(cmd)
            if "--folder-uri" in cmd:
                return _folder_uri_ok(cmd)
            raise AssertionError(f"unexpected command: {cmd}")

        monkeypatch.setattr(vscode_utils.subprocess, "run", fake_run)

        vscode_utils.open_in_vscode("cursor", "proj-frappe-1", "/workspace/dev/frappe-bench")

        install_calls = [c for c in calls if "--install-extension" in c]
        assert install_calls == []

    def test_install_5xx_but_extension_already_present_still_opens(self, monkeypatch):
        """The regression scenario: the first dev-containers lookup misses the extension
        (e.g. an id our known set doesn't yet cover), the install attempt hits a
        marketplace 5xx, but a post-failure recheck finds the extension present
        locally - so the open proceeds instead of aborting."""
        list_calls = {"n": 0}

        def fake_run(cmd, **kw):
            if "--list-extensions" in cmd:
                list_calls["n"] += 1
                if list_calls["n"] < 3:
                    # Docker-extension check (call 1), then the first dev-containers
                    # check (call 2): neither has seen the dev containers id yet.
                    return subprocess.CompletedProcess(cmd, 0, stdout=DOCKER_EXT_LISTED, stderr="")
                # Recheck after the failed install: it really is there.
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=DOCKER_EXT_LISTED + "anysphere.remote-containers\n", stderr=""
                )
            if "--install-extension" in cmd:
                return subprocess.CompletedProcess(
                    cmd,
                    1,
                    stdout="",
                    stderr="Error while installing extensions: Server returned 503",
                )
            if "docker" in cmd and "inspect" in cmd:
                return _docker_inspect_ok(cmd)
            if "--folder-uri" in cmd:
                return _folder_uri_ok(cmd)
            raise AssertionError(f"unexpected command: {cmd}")

        monkeypatch.setattr(vscode_utils.subprocess, "run", fake_run)

        # Must not raise/abort.
        vscode_utils.open_in_vscode("cursor", "proj-frappe-1", "/workspace/dev/frappe-bench")

    def test_install_reports_already_installed_is_treated_as_success(self, monkeypatch):
        def fake_run(cmd, **kw):
            if "--list-extensions" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=DOCKER_EXT_LISTED, stderr="")
            if "--install-extension" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 1, stdout="Extension is already installed.", stderr=""
                )
            if "docker" in cmd and "inspect" in cmd:
                return _docker_inspect_ok(cmd)
            if "--folder-uri" in cmd:
                return _folder_uri_ok(cmd)
            raise AssertionError(f"unexpected command: {cmd}")

        monkeypatch.setattr(vscode_utils.subprocess, "run", fake_run)

        vscode_utils.open_in_vscode("cursor", "proj-frappe-1", "/workspace/dev/frappe-bench")

    def test_genuinely_missing_extension_still_aborts_with_real_error(self, monkeypatch, capsys):
        """The safety property must not weaken: absent + install genuinely fails -> refuse."""

        def fake_run(cmd, **kw):
            if "--list-extensions" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=DOCKER_EXT_LISTED, stderr="")
            if "--install-extension" in cmd:
                return subprocess.CompletedProcess(
                    cmd,
                    1,
                    stdout="",
                    stderr="Error while installing extensions: Server returned 503",
                )
            if "docker" in cmd and "inspect" in cmd:
                return _docker_inspect_ok(cmd)
            raise AssertionError(f"unexpected command: {cmd}")

        monkeypatch.setattr(vscode_utils.subprocess, "run", fake_run)

        with pytest.raises(typer.Exit) as exc_info:
            vscode_utils.open_in_vscode("cursor", "proj-frappe-1", "/workspace/dev/frappe-bench")

        assert exc_info.value.exit_code == 1
        assert "Cannot open without Dev Containers extension" in capsys.readouterr().err

    def test_vscode_path_unchanged(self, monkeypatch):
        """VS Code (non-Cursor) still works via the Microsoft id, untouched."""
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if "--list-extensions" in cmd:
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout=DOCKER_EXT_LISTED + "ms-vscode-remote.remote-containers\n",
                    stderr="",
                )
            if "docker" in cmd and "inspect" in cmd:
                return _docker_inspect_ok(cmd)
            if "--folder-uri" in cmd:
                return _folder_uri_ok(cmd)
            raise AssertionError(f"unexpected command: {cmd}")

        monkeypatch.setattr(vscode_utils.subprocess, "run", fake_run)

        vscode_utils.open_in_vscode("code", "proj-frappe-1", "/workspace/dev/frappe-bench")

        install_calls = [c for c in calls if "--install-extension" in c]
        assert install_calls == []
