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
import sys
from io import BytesIO, StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer

from caffeinated_whale_cli.commands import restore as restore_mod

from .test_restore_safety import FakeReceiveContainer, _run_receive

SITE = "development.localhost"
DB_FILENAME = "20251109_225726-development_localhost-database.sql.gz"


@pytest.fixture(autouse=True)
def _no_ambient_tmp_override(monkeypatch):
    """Neutralize any TMPDIR/TEMP/TMP the host happens to have set (macOS always
    sets TMPDIR) so these tests deterministically exercise the cwcli_home()
    default unless a test opts into the override explicitly."""
    for name in restore_mod._TMPDIR_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


class TestDownloadRoot:
    """Fix #1: the download root is managed under ``cwcli_home()``, never
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

    def test_send_uses_a_payload_below_its_managed_working_directory(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("CWCLI_HOME", "home")
        home = tmp_path / "home"
        popen_call = {}

        class FakeProcess:
            stderr = StringIO()

            def __init__(self, *, can_share):
                # Real sendme 0.36.0 refuses when its source is also its cwd:
                # "can not share from the current directory".
                self.stdout = StringIO(
                    "sendme receive ticket-abc\n"
                    if can_share
                    else "can not share from the current directory\n"
                )

            def poll(self):
                return None

            def wait(self):
                return 0

        def fake_popen(cmd, **kwargs):
            popen_call.update(cmd=cmd, **kwargs)
            return FakeProcess(can_share=Path(kwargs["cwd"]) != Path(cmd[-1]))

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
        assert Path(popen_call["cwd"]).is_absolute()
        assert Path(popen_call["cmd"][-1]).parent == Path(popen_call["cwd"])


class TestTmpdirOverride:
    """An explicit TMPDIR/TEMP/TMP is the one legitimate override: a user who
    has already pointed their own temp storage at a disk with more room gets
    to keep using it, but the DEFAULT (nothing set) must never fall back to
    the system temp dir - that silent fallback is exactly what let a small,
    separate filesystem from cwcli_home() go unchecked (field evidence: Docker
    root and cwcli home had plenty of free space; only the system temp dir,
    on its own small volume, was the write path that actually ran out)."""

    def test_default_never_falls_back_to_the_system_temp_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CWCLI_HOME", str(tmp_path / "home"))

        root = restore_mod._sendme_download_root()

        assert root == (tmp_path / "home" / "tmp").resolve()

    def test_tmpdir_override_wins_over_cwcli_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CWCLI_HOME", str(tmp_path / "home"))
        override = tmp_path / "big-disk"
        monkeypatch.setenv("TMPDIR", str(override))

        root = restore_mod._sendme_download_root()

        assert root == (override / "cwcli").resolve()
        assert root.is_dir()

    def test_temp_and_tmp_are_honored_in_priority_order(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CWCLI_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("TMP", str(tmp_path / "tmp-var"))
        monkeypatch.setenv("TEMP", str(tmp_path / "temp-var"))
        # TMPDIR unset: TEMP wins over TMP, the same search order as tempfile.

        root = restore_mod._sendme_download_root()

        assert root == (tmp_path / "temp-var" / "cwcli").resolve()


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

    def test_verbose_receive_preserves_tty_progress_and_captures_stderr(
        self, tmp_path, monkeypatch
    ):
        class TtyStderr:
            def __init__(self):
                self.buffer = BytesIO()

            def isatty(self):
                return True

            def write(self, text):
                return self.buffer.write(text.encode())

            def flush(self):
                pass

        stderr = TtyStderr()
        monkeypatch.setattr(restore_mod.sys, "stderr", stderr)
        command = [
            sys.executable,
            "-c",
            "import os, sys; "
            "sys.stderr.write(f'tty={os.isatty(2)}\\rprogress\\nReceiver closed\\n'); "
            "sys.stderr.flush(); "
            "raise SystemExit(3)",
        ]

        result = restore_mod._run_sendme_receive(command, str(tmp_path), verbose=True)

        assert result.returncode == 3
        assert "tty=True" in result.stderr
        assert "\rprogress" in result.stderr
        assert "Receiver closed" in result.stderr
        assert b"\rprogress" in stderr.buffer.getvalue()

    def test_verbose_receive_retains_only_a_bounded_stderr_tail(self, tmp_path, monkeypatch):
        class Sink:
            def write(self, chunk):
                return len(chunk)

            def flush(self):
                pass

        class NonTtyStderr:
            buffer = Sink()

            def isatty(self):
                return False

        class ChunkStream:
            def __init__(self):
                self.chunks = iter(
                    [
                        b"Receiver clo",
                        b"sed\n" + b"x" * restore_mod._VERBOSE_STDERR_CAPTURE_BYTES,
                        b"x" * restore_mod._VERBOSE_STDERR_CAPTURE_BYTES,
                    ]
                )

            def read1(self, size):
                return next(self.chunks, b"")

            read = read1

        class FakeProcess:
            stderr = ChunkStream()

            def wait(self):
                return 1

            def kill(self):
                pass

        monkeypatch.setattr(restore_mod.sys, "stderr", NonTtyStderr())
        monkeypatch.setattr(restore_mod.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())

        result = restore_mod._run_sendme_receive(["sendme"], str(tmp_path), verbose=True)

        assert result.returncode == 1
        assert len(result.stderr.encode()) <= restore_mod._VERBOSE_STDERR_CAPTURE_BYTES
        assert "Receiver closed" not in result.stderr
        assert result.space_error_detected is True
        assert (
            restore_mod._explain_sendme_failure(
                result.stderr,
                tmp_path,
                space_error_detected=result.space_error_detected,
            )
            is not None
        )

    def test_windows_tty_falls_back_without_openpty(self, tmp_path, monkeypatch):
        class Sink:
            def write(self, chunk):
                return len(chunk)

            def flush(self):
                pass

        class TtyStderr:
            buffer = Sink()

            def isatty(self):
                return True

        class FakeProcess:
            stderr = BytesIO(b"Receiver closed\n")

            def wait(self):
                return 1

            def kill(self):
                pass

        openpty_called = False

        def fail_openpty():
            nonlocal openpty_called
            openpty_called = True
            raise AssertionError("openpty must not be called on Windows")

        monkeypatch.setattr(restore_mod.os, "name", "nt")
        monkeypatch.setattr(restore_mod.os, "openpty", fail_openpty, raising=False)
        monkeypatch.setattr(restore_mod.sys, "stderr", TtyStderr())
        monkeypatch.setattr(restore_mod.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())

        result = restore_mod._run_sendme_receive(["sendme"], str(tmp_path), verbose=True)

        assert openpty_called is False
        assert result.returncode == 1
        assert result.space_error_detected is True

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
