"""Regression tests for the ``restore --receive`` "Receiver closed" fix.

``sendme receive`` writes its on-disk store into its working directory.
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
import subprocess
from io import StringIO
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

    def test_send_runs_sendme_under_the_cwcli_home_root(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("CWCLI_HOME", str(home))
        popen_call = {}

        class FakeProcess:
            stdout = StringIO("sendme receive ticket-abc\n")
            stderr = StringIO()

            def poll(self):
                return None

            def wait(self):
                return 0

        def fake_popen(cmd, **kwargs):
            popen_call.update(cmd=cmd, **kwargs)
            return FakeProcess()

        monkeypatch.setattr(restore_mod, "_get_frappe_container", lambda project: object())
        monkeypatch.setattr(
            restore_mod.core_restore,
            "scan_backups_for_all_sites",
            lambda container, bench_path: [object()],
        )
        monkeypatch.setattr(
            restore_mod.core_restore,
            "group_and_sort_backups",
            lambda backups, site: ([], []),
        )
        monkeypatch.setattr(restore_mod.core_restore, "_menu_options", lambda *args: ["backup"])
        monkeypatch.setattr(
            restore_mod, "_render_backup_menu", lambda options, site, offer_remote: "backup"
        )
        monkeypatch.setattr(
            restore_mod.core_restore,
            "_match_selected",
            lambda *args: {"database": {"full_path": "/backups/database.sql.gz"}},
        )
        monkeypatch.setattr(
            restore_mod.core_restore,
            "copy_backup_files_out",
            lambda container, files, destination: None,
        )
        monkeypatch.setattr(restore_mod, "get_sendme_command", lambda: "sendme")
        monkeypatch.setattr(restore_mod, "copy_to_clipboard", lambda ticket: True)
        monkeypatch.setattr(subprocess, "Popen", fake_popen)

        restore_mod._run_send("project", site=SITE, bench_path="/bench", verbose=False)

        assert popen_call["cwd"].startswith(str(home / "tmp"))
        assert popen_call["cwd"] == popen_call["cmd"][-1]


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

    @pytest.mark.parametrize(
        ("failure_point", "expected_action"),
        [
            ("mkdir", "create the transfer directory"),
            ("disk_usage", "check available disk space"),
            ("temporary_directory", "create a temporary transfer directory"),
        ],
    )
    def test_filesystem_failures_exit_cleanly(
        self, tmp_path, monkeypatch, failure_point, expected_action
    ):
        home = tmp_path / "home"
        monkeypatch.setenv("CWCLI_HOME", str(home))
        container = FakeReceiveContainer()

        if failure_point == "mkdir":
            def fail_mkdir(self, **kwargs):
                raise OSError("read-only filesystem")

            monkeypatch.setattr(Path, "mkdir", fail_mkdir)
        elif failure_point == "disk_usage":
            def fail_disk_usage(path):
                raise OSError("filesystem unavailable")

            monkeypatch.setattr(shutil, "disk_usage", fail_disk_usage)
        else:
            def fail_temp_dir(**kwargs):
                raise OSError("no space left")

            monkeypatch.setattr(restore_mod.tempfile, "TemporaryDirectory", fail_temp_dir)

        with pytest.raises(typer.Exit) as excinfo:
            _run_receive(
                monkeypatch,
                container,
                site=SITE,
                db_filename=DB_FILENAME,
                yes=True,
                isatty=False,
            )

        assert excinfo.value.exit_code == 1
        assert container.restore_calls() == []
        assert any(expected_action in line for line in container.printed)
        assert any(str(home / "tmp") in line for line in container.printed)


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

    def test_verbose_receiver_closed_is_still_classified(self, tmp_path, monkeypatch):
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
                verbose=True,
                sendme_returncode=1,
                sendme_stderr="error sending over irpc: Receiver closed\n",
            )

        assert excinfo.value.exit_code == 1
        assert any("may be out of space" in line for line in container.printed)

    def test_error_translation_survives_disk_usage_failure(self, tmp_path, monkeypatch):
        download_dir = tmp_path / "download"

        def fail_disk_usage(path):
            raise OSError("filesystem unavailable")

        monkeypatch.setattr(shutil, "disk_usage", fail_disk_usage)

        explanation = restore_mod._explain_sendme_failure(
            "error sending over irpc: Receiver closed", download_dir
        )

        assert explanation is not None
        assert str(download_dir) in explanation
        assert "available space could not be checked" in explanation


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
