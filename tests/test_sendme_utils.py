"""Unit coverage for ``utils/sendme_utils.py`` - the sendme binary manager.

sendme is the iroh P2P transport behind ``restore --send``/``--receive`` (cwcli's
most destructive command). This suite exercises the REACHABLE logic - path/target
resolution, install-method detection, archive extraction, PATH setup, the clipboard
helper - mocking ONLY the boundaries a unit test must not cross: the network
(``requests``), the sendme/clipboard subprocesses, and ``platform`` for
cross-platform branches. The extraction logic runs against a REAL tar.gz/zip built
on disk, so the code under test is never mocked - only the download that produces it.

The genuine end-to-end transfer (real bytes over sendme) is proved by the
``e2e_p2p`` send->receive E2E in ``tests/e2e/test_restore_p2p_e2e.py``; nothing here
stands in for that.
"""

from __future__ import annotations

import io
import os
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest
import requests

from caffeinated_whale_cli.utils import sendme_utils


# --------------------------------------------------------------------------- #
# Pure path/target resolution
# --------------------------------------------------------------------------- #
def test_install_dir_is_local_bin(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert sendme_utils.get_sendme_install_dir() == tmp_path / ".local" / "bin"


def test_sendme_path_unix_vs_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(sendme_utils, "get_sendme_install_dir", lambda: tmp_path)
    monkeypatch.setattr(sendme_utils.platform, "system", lambda: "Linux")
    assert sendme_utils.get_sendme_path() == tmp_path / "sendme"
    monkeypatch.setattr(sendme_utils.platform, "system", lambda: "Windows")
    assert sendme_utils.get_sendme_path() == tmp_path / "sendme.exe"


@pytest.mark.parametrize(
    "system,machine,expected",
    [
        ("Windows", "amd64", "windows-x86_64"),
        ("Darwin", "arm64", "darwin-aarch64"),
        ("Darwin", "aarch64", "darwin-aarch64"),
        ("Darwin", "x86_64", "darwin-x86_64"),
        ("Linux", "aarch64", "linux-aarch64"),
        ("Linux", "arm64", "linux-aarch64"),
        ("Linux", "x86_64", "linux-x86_64"),
        ("Plan9", "riscv", "linux-x86_64"),  # unknown OS -> linux-x86_64 default
    ],
)
def test_platform_target(monkeypatch, system, machine, expected):
    monkeypatch.setattr(sendme_utils.platform, "system", lambda: system)
    monkeypatch.setattr(sendme_utils.platform, "machine", lambda: machine)
    assert sendme_utils.get_platform_target() == expected


# --------------------------------------------------------------------------- #
# is_sendme_installed / get_sendme_command (filesystem + PATH boundary)
# --------------------------------------------------------------------------- #
def test_is_installed_prefers_local_binary(monkeypatch, tmp_path):
    binary = tmp_path / "sendme"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setattr(sendme_utils, "get_sendme_path", lambda: binary)
    # Even with nothing on PATH, the executable local binary counts as installed.
    monkeypatch.setattr(sendme_utils.shutil, "which", lambda _: None)
    assert sendme_utils.is_sendme_installed() is True


def test_is_installed_non_executable_falls_back_to_path(monkeypatch, tmp_path):
    binary = tmp_path / "sendme"
    binary.write_text("not executable")
    binary.chmod(0o644)  # present but NOT executable
    monkeypatch.setattr(sendme_utils, "get_sendme_path", lambda: binary)
    monkeypatch.setattr(sendme_utils.shutil, "which", lambda _: "/usr/bin/sendme")
    assert sendme_utils.is_sendme_installed() is True


def test_is_installed_false_when_absent_everywhere(monkeypatch, tmp_path):
    monkeypatch.setattr(sendme_utils, "get_sendme_path", lambda: tmp_path / "nope")
    monkeypatch.setattr(sendme_utils.shutil, "which", lambda _: None)
    assert sendme_utils.is_sendme_installed() is False


def test_command_prefers_local_then_path_then_fallback(monkeypatch, tmp_path):
    binary = tmp_path / "sendme"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setattr(sendme_utils, "get_sendme_path", lambda: binary)
    assert sendme_utils.get_sendme_command() == str(binary)

    # Local absent -> PATH.
    monkeypatch.setattr(sendme_utils, "get_sendme_path", lambda: tmp_path / "gone")
    monkeypatch.setattr(sendme_utils.shutil, "which", lambda _: "/usr/local/bin/sendme")
    assert sendme_utils.get_sendme_command() == "/usr/local/bin/sendme"

    # Neither -> bare-name fallback (will likely fail at exec, but never crashes here).
    monkeypatch.setattr(sendme_utils.shutil, "which", lambda _: None)
    assert sendme_utils.get_sendme_command() == "sendme"


# --------------------------------------------------------------------------- #
# copy_to_clipboard (subprocess boundary only)
# --------------------------------------------------------------------------- #
def test_clipboard_linux_xclip(monkeypatch):
    calls = {}
    monkeypatch.setattr(sendme_utils.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sendme_utils.shutil, "which", lambda tool: tool == "xclip")

    def fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        calls["input"] = kwargs.get("input")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(sendme_utils.subprocess, "run", fake_run)
    assert sendme_utils.copy_to_clipboard("TICKET") is True
    assert calls["cmd"][0] == "xclip"
    assert calls["input"] == "TICKET"


def test_clipboard_linux_xsel_when_no_xclip(monkeypatch):
    calls = {}
    monkeypatch.setattr(sendme_utils.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sendme_utils.shutil, "which", lambda tool: tool == "xsel")
    monkeypatch.setattr(
        sendme_utils.subprocess,
        "run",
        lambda cmd, **kw: calls.setdefault("cmd", cmd) or subprocess.CompletedProcess(cmd, 0),
    )
    assert sendme_utils.copy_to_clipboard("T") is True
    assert calls["cmd"][0] == "xsel"


def test_clipboard_linux_no_tool_returns_false(monkeypatch):
    monkeypatch.setattr(sendme_utils.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sendme_utils.shutil, "which", lambda _: None)
    assert sendme_utils.copy_to_clipboard("T") is False


def test_clipboard_subprocess_failure_returns_false(monkeypatch):
    monkeypatch.setattr(sendme_utils.platform, "system", lambda: "Darwin")

    def boom(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(sendme_utils.subprocess, "run", boom)
    assert sendme_utils.copy_to_clipboard("T") is False


# --------------------------------------------------------------------------- #
# download_file_with_progress (network boundary only)
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, chunks: list[bytes], headers: dict | None = None):
        self._chunks = chunks
        self.headers = headers or {"content-length": str(sum(len(c) for c in chunks))}

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        yield from self._chunks


def test_download_writes_streamed_bytes(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sendme_utils.requests, "get", lambda *a, **k: _FakeResponse([b"abc", b"", b"def"])
    )
    dest = tmp_path / "out.bin"
    assert sendme_utils.download_file_with_progress("http://x", dest) is True
    assert dest.read_bytes() == b"abcdef"  # empty chunk skipped, rest concatenated


def test_download_returns_false_on_error(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise requests.RequestException("network down")

    monkeypatch.setattr(sendme_utils.requests, "get", boom)
    assert sendme_utils.download_file_with_progress("http://x", tmp_path / "o") is False


# --------------------------------------------------------------------------- #
# install_sendme - error paths, and a success path over a REAL archive
# --------------------------------------------------------------------------- #
def _release_json(url: str) -> dict:
    return {"assets": [{"browser_download_url": url}]}


def test_install_no_matching_asset_returns_false(monkeypatch):
    monkeypatch.setattr(sendme_utils, "get_platform_target", lambda: "linux-x86_64")
    monkeypatch.setattr(
        sendme_utils.requests,
        "get",
        lambda *a, **k: _FakeReleaseResponse(
            _release_json("https://x/sendme-darwin-aarch64.tar.gz")
        ),
    )
    assert sendme_utils.install_sendme() is False


def test_install_network_error_returns_false(monkeypatch):
    monkeypatch.setattr(sendme_utils, "get_platform_target", lambda: "linux-x86_64")

    def boom(*a, **k):
        raise requests.RequestException("no route")

    monkeypatch.setattr(sendme_utils.requests, "get", boom)
    assert sendme_utils.install_sendme() is False


class _FakeReleaseResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _make_tar_gz(dest: Path, member_name: str = "sendme") -> None:
    """Write a real .tar.gz holding an executable ``member_name`` script."""
    with tarfile.open(dest, "w:gz") as tar:
        data = b"#!/bin/sh\necho sendme\n"
        info = tarfile.TarInfo(name=member_name)
        info.size = len(data)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(data))


def _make_zip(dest: Path, member_name: str = "sendme.exe") -> None:
    with zipfile.ZipFile(dest, "w") as zf:
        zf.writestr(member_name, b"binary")


def test_install_success_extracts_and_installs_tar(monkeypatch, tmp_path):
    """The tar.gz extract/find/copy/chmod logic runs for real; only the download
    boundary is faked (it writes a genuine archive instead of hitting the network)."""
    install_dir = tmp_path / "bin"
    url = "https://github.com/n0-computer/sendme/releases/download/v1/sendme-linux-x86_64.tar.gz"
    monkeypatch.setattr(sendme_utils, "get_platform_target", lambda: "linux-x86_64")
    monkeypatch.setattr(sendme_utils, "get_sendme_install_dir", lambda: install_dir)
    monkeypatch.setattr(
        sendme_utils.requests, "get", lambda *a, **k: _FakeReleaseResponse(_release_json(url))
    )
    monkeypatch.setattr(
        sendme_utils, "download_file_with_progress", lambda u, d: (_make_tar_gz(d) or True)
    )
    # Ensure setup_path early-returns without touching real shell RC files.
    monkeypatch.setenv("PATH", str(install_dir) + os.pathsep + os.environ.get("PATH", ""))

    assert sendme_utils.install_sendme(verbose=True) is True
    installed = install_dir / "sendme"
    assert installed.exists()
    assert os.access(installed, os.X_OK)


def test_install_success_extracts_zip(monkeypatch, tmp_path):
    install_dir = tmp_path / "bin"
    url = "https://github.com/n0-computer/sendme/releases/download/v1/sendme-windows-x86_64.zip"
    monkeypatch.setattr(sendme_utils, "get_platform_target", lambda: "windows-x86_64")
    monkeypatch.setattr(sendme_utils, "get_sendme_install_dir", lambda: install_dir)
    monkeypatch.setattr(sendme_utils.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        sendme_utils.requests, "get", lambda *a, **k: _FakeReleaseResponse(_release_json(url))
    )
    monkeypatch.setattr(
        sendme_utils, "download_file_with_progress", lambda u, d: (_make_zip(d) or True)
    )
    # On Windows the code updates PATH via PowerShell; stub setup_path so the test
    # stays cross-platform and never shells out.
    monkeypatch.setattr(sendme_utils, "setup_path", lambda *a, **k: None)

    assert sendme_utils.install_sendme() is True
    assert (install_dir / "sendme.exe").exists()


def test_install_download_failure_returns_false(monkeypatch, tmp_path):
    url = "https://x/sendme-linux-x86_64.tar.gz"
    monkeypatch.setattr(sendme_utils, "get_platform_target", lambda: "linux-x86_64")
    monkeypatch.setattr(sendme_utils, "get_sendme_install_dir", lambda: tmp_path / "bin")
    monkeypatch.setattr(
        sendme_utils.requests, "get", lambda *a, **k: _FakeReleaseResponse(_release_json(url))
    )
    monkeypatch.setattr(sendme_utils, "download_file_with_progress", lambda u, d: False)
    assert sendme_utils.install_sendme() is False


# --------------------------------------------------------------------------- #
# setup_path (filesystem boundary; a tmp HOME, never the real one)
# --------------------------------------------------------------------------- #
def test_setup_path_noop_when_already_in_path(monkeypatch, tmp_path):
    install_dir = tmp_path / "bin"
    monkeypatch.setenv("PATH", str(install_dir))
    monkeypatch.setattr(sendme_utils.platform, "system", lambda: "Linux")
    # No RC file should be created/written; Path.home is redirected so a bug is caught.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    sendme_utils.setup_path(install_dir)
    assert not (tmp_path / ".bashrc").exists()


def test_setup_path_appends_export_and_is_idempotent(monkeypatch, tmp_path):
    install_dir = tmp_path / "bin"
    monkeypatch.setenv("PATH", "/usr/bin")  # install_dir NOT in PATH
    monkeypatch.setattr(sendme_utils.platform, "system", lambda: "Linux")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    bashrc = tmp_path / ".bashrc"
    bashrc.write_text("# existing\n")

    sendme_utils.setup_path(install_dir)
    content = bashrc.read_text()
    assert str(install_dir) in content
    assert content.count(str(install_dir)) == 1

    # Second call must not double-append (idempotent on the already-present marker).
    sendme_utils.setup_path(install_dir)
    assert bashrc.read_text().count(str(install_dir)) == 1


def test_setup_path_creates_bashrc_when_no_config(monkeypatch, tmp_path):
    install_dir = tmp_path / "bin"
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setattr(sendme_utils.platform, "system", lambda: "Linux")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    # No RC files exist -> setup_path creates ~/.bashrc and writes the export.
    sendme_utils.setup_path(install_dir)
    bashrc = tmp_path / ".bashrc"
    assert bashrc.exists()
    assert str(install_dir) in bashrc.read_text()


# --------------------------------------------------------------------------- #
# ensure_sendme_installed (composition)
# --------------------------------------------------------------------------- #
def test_ensure_short_circuits_when_installed(monkeypatch):
    monkeypatch.setattr(sendme_utils, "is_sendme_installed", lambda: True)
    # install_sendme must NOT be called when already present.
    monkeypatch.setattr(
        sendme_utils,
        "install_sendme",
        lambda *a, **k: pytest.fail("install_sendme called though already installed"),
    )
    assert sendme_utils.ensure_sendme_installed(verbose=True) is True


def test_ensure_installs_when_absent(monkeypatch):
    monkeypatch.setattr(sendme_utils, "is_sendme_installed", lambda: False)
    monkeypatch.setattr(sendme_utils, "install_sendme", lambda *a, **k: True)
    assert sendme_utils.ensure_sendme_installed() is True


def test_ensure_reports_failure_when_install_fails(monkeypatch):
    monkeypatch.setattr(sendme_utils, "is_sendme_installed", lambda: False)
    monkeypatch.setattr(sendme_utils, "install_sendme", lambda *a, **k: False)
    assert sendme_utils.ensure_sendme_installed() is False
