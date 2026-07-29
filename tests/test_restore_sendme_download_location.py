"""Regression tests for the ``restore --receive`` "Receiver closed" fix.

Root cause (see the ``sendme-receiver-closed`` incident report): ``sendme
receive`` writes its on-disk store into whatever directory it is run from.
``restore --receive`` used to run it from the system temp dir
(``tempfile.TemporaryDirectory()`` with no ``dir=``), which is tmpfs
(RAM-backed) or otherwise small on some hosts; a multi-GiB transfer then hit
ENOSPC mid-download and sendme surfaced the cryptic internal error
``error sending over irpc: Receiver closed`` instead of the real cause.

These tests pin the four parts of the fix: the download root moves under
``cwcli_home()``, a free-space preflight refuses early on a too-small
filesystem, a known sendme failure signature gets an actionable explanation,
and a failed download leaves no partial store behind.
"""

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer

from caffeinated_whale_cli.commands import restore as restore_mod

from .test_restore_safety import FakeReceiveContainer, _run_receive

SITE = "development.localhost"
DB_FILENAME = "20251109_225726-development_localhost-database.sql.gz"


class TestDownloadRoot:
    """Fix #1: the download root is disk-backed, under ``cwcli_home()``, never
    the system temp dir."""

    def test_download_root_is_under_cwcli_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CWCLI_HOME", str(tmp_path))

        root = restore_mod._sendme_download_root()

        assert root == tmp_path / "tmp"
        assert root.is_dir()

    def test_receive_runs_sendme_under_the_cwcli_home_root(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("CWCLI_HOME", str(home))
        container = FakeReceiveContainer()

        _run_receive(
            monkeypatch,
            container,
            site=SITE,
            db_filename=DB_FILENAME,
            yes=True,
            isatty=False,
        )

        assert len(container.restore_calls()) == 1
        assert container.sendme_cwd is not None
        # Never the system temp dir - always under the cwcli-home root.
        assert str((home / "tmp").resolve()) in str(container.sendme_cwd)


class TestPreflightFreeSpace:
    """Fix #2: refuse early, before starting the download, when the receive
    location doesn't have a sane amount of free space."""

    def test_refuses_when_free_space_is_below_the_floor(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("CWCLI_HOME", str(home))
        monkeypatch.setattr(
            shutil, "disk_usage", lambda path: SimpleNamespace(total=10_000_000_000, free=1_000_000)
        )
        container = FakeReceiveContainer()

        with pytest.raises(typer.Exit) as excinfo:
            _run_receive(
                monkeypatch,
                container,
                site=SITE,
                db_filename=DB_FILENAME,
                yes=True,
                isatty=False,
            )

        assert excinfo.value.exit_code != 0
        # Refused before any download attempt or destructive restore.
        assert container.sendme_cwd is None
        assert container.restore_calls() == []
        assert any("Not enough free space" in line for line in container.printed)
        assert any(str(home / "tmp") in line for line in container.printed)

    def test_proceeds_when_free_space_is_above_the_floor(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("CWCLI_HOME", str(home))
        monkeypatch.setattr(
            shutil,
            "disk_usage",
            lambda path: SimpleNamespace(total=100_000_000_000, free=50_000_000_000),
        )
        container = FakeReceiveContainer()

        _run_receive(
            monkeypatch,
            container,
            site=SITE,
            db_filename=DB_FILENAME,
            yes=True,
            isatty=False,
        )

        assert len(container.restore_calls()) == 1


class TestErrorTranslation:
    """Fix #3: a known sendme out-of-space failure signature gets an
    actionable explanation naming the download location, instead of the bare
    generic wrapper line; an unrelated sendme failure keeps today's generic
    message + raw stderr passthrough."""

    def test_receiver_closed_signature_gets_an_actionable_explanation(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("CWCLI_HOME", str(home))
        container = FakeReceiveContainer()

        with pytest.raises(typer.Exit) as excinfo:
            _run_receive(
                monkeypatch,
                container,
                site=SITE,
                db_filename=DB_FILENAME,
                yes=True,
                isatty=False,
                sendme_returncode=1,
                sendme_stderr="error sending over irpc: Receiver closed\nerror: Receiver closed\n",
            )

        assert excinfo.value.exit_code != 0
        assert container.restore_calls() == []
        assert any("may be out of space" in line for line in container.printed)
        assert any(str(home / "tmp") in line for line in container.printed)
        # The generic wrapper line is replaced, not just supplemented.
        assert not any("Failed to download files via sendme" in line for line in container.printed)
        # Raw sendme stderr is still surfaced.
        assert any("Receiver closed" in line for line in container.printed)

    def test_unrelated_failure_keeps_the_generic_message_and_raw_stderr(
        self, tmp_path, monkeypatch
    ):
        home = tmp_path / "home"
        monkeypatch.setenv("CWCLI_HOME", str(home))
        container = FakeReceiveContainer()

        with pytest.raises(typer.Exit) as excinfo:
            _run_receive(
                monkeypatch,
                container,
                site=SITE,
                db_filename=DB_FILENAME,
                yes=True,
                isatty=False,
                sendme_returncode=1,
                sendme_stderr="Hit the end of buffer, expected more data\n",
            )

        assert excinfo.value.exit_code != 0
        assert container.restore_calls() == []
        assert any("Failed to download files via sendme" in line for line in container.printed)
        assert any("Hit the end of buffer" in line for line in container.printed)
        assert not any("may be out of space" in line for line in container.printed)


class TestPartialDownloadCleanup:
    """Fix #4: a failed download leaves no partial store behind, so retries
    don't silently accumulate gigabytes."""

    def test_failed_download_dir_does_not_survive(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("CWCLI_HOME", str(home))
        container = FakeReceiveContainer()

        with pytest.raises(typer.Exit):
            _run_receive(
                monkeypatch,
                container,
                site=SITE,
                db_filename=DB_FILENAME,
                yes=True,
                isatty=False,
                sendme_returncode=1,
                sendme_stderr="error sending over irpc: Receiver closed\n",
            )

        assert container.sendme_cwd is not None
        # The per-attempt TemporaryDirectory is cleaned up on unwind; nothing
        # is left behind under the cwcli-home download root.
        assert not Path(container.sendme_cwd).exists()
        download_root = home / "tmp"
        assert list(download_root.iterdir()) == []
