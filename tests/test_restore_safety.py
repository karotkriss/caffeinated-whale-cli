"""Regression tests for the ``cwcli restore --receive`` data-safety fixes.

Context (from the gap audit)
----------------------------
``restore --receive`` downloads a peer's backup and, the instant the download
completes, ran ``bench restore --force`` against the live default site with **no**
confirmation and no origin check - the most destructive path in the codebase was
the least guarded (audit finding C2). Two supporting fixes travel with it:

- **M5**: the MariaDB root password used to be appended to the ``bench`` argv
  (``--mariadb-root-password '...'``), so it was visible in the container process
  list (``ps`` / ``docker top`` / exec-inspect). It is now passed via the exec
  environment and referenced as ``"$CWCLI_MARIADB_ROOT_PASSWORD"`` in the command.
- **M4**: the backup tar used to be read whole into host RAM
  (``put_archive(dir, tar_file.read())``); it is now streamed (the file handle is
  passed directly) and the ``put_archive`` result is checked.

These tests pin the corrected receive-mode behavior using a fake frappe container
that records every ``exec_run`` (command, workdir, environment) so the tests can
assert which command ran and how the secret was passed.
"""

import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import typer

from caffeinated_whale_cli.commands import restore as restore_mod
from caffeinated_whale_cli.utils import docker_utils as docker_utils_mod

BENCH_PATH = "/workspace/frappe-bench"
SECRET_PW = "sup3r-s3cr3t-pw"


class FakeReceiveContainer:
    """Stand-in frappe container that records ``exec_run`` calls and answers the
    handful of probes the receive flow issues.

    ``exec_calls`` holds one dict ``{"cmd", "workdir", "environment"}`` per call so
    tests can locate the ``bench ... restore ... --force`` invocation and inspect
    exactly what argv / environment it was handed.
    """

    def __init__(self):
        self.labels = {"com.docker.compose.service": "frappe"}
        self.status = "running"
        self.exec_calls: list[dict] = []
        self.put_archive_paths: list[str] = []
        self.put_archive_streamed: bool | None = None
        self.printed: list[str] = []

    def exec_run(self, cmd, workdir=None, environment=None):
        self.exec_calls.append({"cmd": cmd, "workdir": workdir, "environment": environment})
        # Every probe (``test -d`` backup dir, the restore itself) succeeds.
        return (0, b"")

    def put_archive(self, path, data):
        # M4: the tar must be streamed (a file handle), not slurped into a bytes blob.
        self.put_archive_streamed = hasattr(data, "read")
        self.put_archive_paths.append(path)
        return True

    @staticmethod
    def _cmd_str(cmd) -> str:
        return " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)

    def restore_calls(self) -> list[dict]:
        """The recorded ``bench ... restore ... --force`` invocations (there should
        be exactly one when a restore actually runs, and zero when it is refused)."""
        return [
            c
            for c in self.exec_calls
            if "restore" in self._cmd_str(c["cmd"]) and "--force" in self._cmd_str(c["cmd"])
        ]


def _stub_question(answer):
    """A questionary-prompt stand-in whose ``.ask()`` returns ``answer``."""
    q = MagicMock()
    q.ask.return_value = answer
    return q


class _NullSpinner:
    """No-op replacement for ``TipSpinner`` so tests don't spin up Rich threads."""

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def update(self, *args, **kwargs):
        pass


def _run_receive(
    monkeypatch,
    container,
    *,
    site,
    db_filename,
    yes,
    isatty,
    confirm_answer=True,
    admin_password=None,
    missing_apps=None,
):
    """Drive ``restore_receive_mode`` end-to-end against ``container``.

    Everything external is stubbed: ``sendme`` is faked to "download" a single
    database backup named ``db_filename`` into its working directory, container
    discovery returns ``container``, the missing-apps check returns
    ``missing_apps`` (none by default), and the TTY / confirmation answers are
    controlled by ``isatty`` / ``confirm_answer``. Credentials are supplied
    directly so no interactive credential prompt fires.
    """

    def fake_sendme_run(cmd, cwd=None, capture_output=True, text=True):
        Path(cwd).joinpath(db_filename).write_text("SQL DUMP DATA")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_sendme_run)
    monkeypatch.setattr(restore_mod, "ensure_containers_running", lambda *a, **k: True)
    monkeypatch.setattr(restore_mod, "get_project_containers", lambda name: [container])
    monkeypatch.setattr(restore_mod, "get_sendme_command", lambda: "sendme")
    monkeypatch.setattr(restore_mod, "check_missing_apps", lambda *a, **k: missing_apps or [])
    monkeypatch.setattr(restore_mod, "TipSpinner", _NullSpinner)
    monkeypatch.setattr(restore_mod.config_utils, "get_show_tips", lambda: False)
    monkeypatch.setattr(
        restore_mod.questionary, "text", lambda *a, **k: _stub_question("ticket-abc")
    )
    monkeypatch.setattr(
        restore_mod.questionary, "confirm", lambda *a, **k: _stub_question(confirm_answer)
    )
    monkeypatch.setattr(restore_mod.sys.stdin, "isatty", lambda: isatty)

    # Capture printed output (both consoles) so tests can assert on warnings.
    def record(*args, **kwargs):
        container.printed.append(" ".join(str(a) for a in args))

    monkeypatch.setattr(restore_mod.console, "print", record)
    monkeypatch.setattr(restore_mod.stderr_console, "print", record)

    restore_mod.restore_receive_mode(
        project_name="proj",
        site=site,
        bench_path=BENCH_PATH,
        mariadb_root_username="root",
        mariadb_root_password=SECRET_PW,
        admin_password=admin_password,
        no_recache=True,
        verbose=False,
        yes=yes,
        # These tests pin the restore command/confirm behavior only; skip the
        # post-restore migrate + instance restart (covered by its own tests).
        no_migrate=True,
    )


class TestReceiveConfirmation:
    """C2: receive mode must warn + confirm before the destructive restore."""

    def test_declined_confirm_does_not_restore_and_exits_nonzero(self, monkeypatch):
        container = FakeReceiveContainer()
        with pytest.raises(typer.Exit) as excinfo:
            _run_receive(
                monkeypatch,
                container,
                site="development.localhost",
                db_filename="20251109_225726-development_localhost-database.sql.gz",
                yes=False,
                isatty=True,
                confirm_answer=False,
            )

        assert excinfo.value.exit_code != 0
        assert container.restore_calls() == []

    def test_non_tty_without_yes_refuses_and_exits_nonzero(self, monkeypatch):
        container = FakeReceiveContainer()
        # A non-TTY with no --yes must refuse rather than silently proceeding or
        # exiting 0 (which would look like a successful restore).
        monkeypatch.setattr(
            restore_mod.questionary,
            "confirm",
            lambda *a, **k: pytest.fail("confirm must not be reached under a non-TTY"),
        )
        with pytest.raises(typer.Exit) as excinfo:
            _run_receive(
                monkeypatch,
                container,
                site="development.localhost",
                db_filename="20251109_225726-development_localhost-database.sql.gz",
                yes=False,
                isatty=False,
            )

        assert excinfo.value.exit_code != 0
        assert container.restore_calls() == []

    def test_yes_proceeds_with_restore(self, monkeypatch):
        container = FakeReceiveContainer()
        # --yes proceeds even under a non-TTY.
        _run_receive(
            monkeypatch,
            container,
            site="development.localhost",
            db_filename="20251109_225726-development_localhost-database.sql.gz",
            yes=True,
            isatty=False,
        )

        assert len(container.restore_calls()) == 1
        # M4: the tar was streamed, not read whole into RAM.
        assert container.put_archive_streamed is True


class TestReceiveOriginCheck:
    """C2: an origin-site mismatch between the backup and the target is surfaced."""

    def test_origin_mismatch_is_surfaced(self, monkeypatch):
        container = FakeReceiveContainer()
        _run_receive(
            monkeypatch,
            container,
            site="development.localhost",
            # Backup came from a *different* site than the restore target.
            db_filename="20251109_225726-othersite_localhost-database.sql.gz",
            yes=True,
            isatty=False,
        )

        assert any("Origin mismatch" in line for line in container.printed)

    def test_matching_origin_does_not_warn(self, monkeypatch):
        container = FakeReceiveContainer()
        _run_receive(
            monkeypatch,
            container,
            site="development.localhost",
            db_filename="20251109_225726-development_localhost-database.sql.gz",
            yes=True,
            isatty=False,
        )

        assert not any("Origin mismatch" in line for line in container.printed)


class TestReceivePasswordNotOnArgv:
    """M5: the DB root password is passed via the exec environment, not the argv."""

    def test_password_passed_via_environment_not_command(self, monkeypatch):
        container = FakeReceiveContainer()
        _run_receive(
            monkeypatch,
            container,
            site="development.localhost",
            db_filename="20251109_225726-development_localhost-database.sql.gz",
            yes=True,
            isatty=False,
        )

        calls = container.restore_calls()
        assert len(calls) == 1
        call = calls[0]
        cmd_str = FakeReceiveContainer._cmd_str(call["cmd"])

        # The secret must NOT appear in the command the container process list shows.
        assert SECRET_PW not in cmd_str
        # It must be referenced as an env var and supplied through ``environment=``.
        assert "CWCLI_MARIADB_ROOT_PASSWORD" in cmd_str
        assert call["environment"] is not None
        assert call["environment"].get("CWCLI_MARIADB_ROOT_PASSWORD") == SECRET_PW


class TestReceiveMissingAppsGate:
    """C2: the missing-apps 'continue anyway?' gate must honor --yes / non-TTY too,
    so ``restore --receive --yes`` is genuinely non-interactive and a non-TTY
    without --yes refuses (non-zero) instead of silently exiting 0."""

    def test_yes_proceeds_despite_missing_apps(self, monkeypatch):
        container = FakeReceiveContainer()
        # --yes must not stall on the missing-apps prompt even under a non-TTY.
        monkeypatch.setattr(
            restore_mod.questionary,
            "confirm",
            lambda *a, **k: pytest.fail("confirm must not be reached with --yes"),
        )
        _run_receive(
            monkeypatch,
            container,
            site="development.localhost",
            db_filename="20251109_225726-development_localhost-database.sql.gz",
            yes=True,
            isatty=False,
            missing_apps=["erpnext"],
        )

        assert len(container.restore_calls()) == 1
        assert any("not available on this bench" in line for line in container.printed)

    def test_non_tty_without_yes_refuses_and_exits_nonzero(self, monkeypatch):
        container = FakeReceiveContainer()
        # A non-TTY with missing apps and no --yes must refuse, not silently exit 0.
        monkeypatch.setattr(
            restore_mod.questionary,
            "confirm",
            lambda *a, **k: pytest.fail("confirm must not be reached under a non-TTY"),
        )
        with pytest.raises(typer.Exit) as excinfo:
            _run_receive(
                monkeypatch,
                container,
                site="development.localhost",
                db_filename="20251109_225726-development_localhost-database.sql.gz",
                yes=False,
                isatty=False,
                missing_apps=["erpnext"],
            )

        assert excinfo.value.exit_code != 0
        assert container.restore_calls() == []


# ---------------------------------------------------------------------------
# Issue #40: non-interactive selectors and honest exit codes for the NORMAL
# (non --send/--receive) restore path.
#
# The receive path's tests above drove ``restore_receive_mode`` directly (a plain
# function). The normal path lives inside the ``restore`` Typer command, so these
# tests call ``restore_mod.restore(...)`` with EVERY parameter explicit - omitting
# any Typer ``Option`` leaves its default truthy object (CLAUDE.md / AGENTS.md flag
# this trap for ``inspect``; it applies identically here). The ``@handle_docker_errors``
# decorator wrapping ``restore`` is bypassed by patching the docker client at the
# module's imported names so the decorator's ``shutil.which`` / ``docker.from_env``
# checks succeed.
# ---------------------------------------------------------------------------

DB_FILENAME = "20251109_225726-development_localhost-database.sql.gz"
DB_FULL_PATH = f"{BENCH_PATH}/sites/development.localhost/private/backups/{DB_FILENAME}"


def _backup_set(site_dir: str = "development.localhost", filename: str = DB_FILENAME) -> dict:
    """Build a single backup-set dict shaped like ``group_and_sort_backups`` output."""
    return {
        "timestamp": datetime(2025, 11, 9, 22, 57, 26),
        "timestamp_str": "20251109_225726",
        "site_name": "development_localhost",
        "site_dir": site_dir,
        "database": {
            "filename": filename,
            "full_path": f"{BENCH_PATH}/sites/{site_dir}/private/backups/{filename}",
        },
        "files": None,
        "private_files": None,
        "site_config_backup": None,
    }


class FakeNormalContainer:
    """Stand-in frappe container for the normal restore path.

    Answers the bench/site ``test -d`` probes, the backup-scan ``find``, the
    missing-apps ``ls apps``/``zcat`` probes, and the restore ``exec_run``. Every
    call is recorded for assertions.
    """

    def __init__(self):
        self.labels = {"com.docker.compose.service": "frappe"}
        self.status = "running"
        self.exec_calls: list[dict] = []
        self.scan_backups: str = DB_FILENAME  # one backup file line
        self.printed: list[str] = []

    def exec_run(self, cmd, workdir=None, environment=None):
        self.exec_calls.append({"cmd": cmd, "workdir": workdir, "environment": environment})
        cmd_str = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)

        # Bench sites dir exists.
        if (
            "test -d" in cmd_str
            and f"{BENCH_PATH}/sites" in cmd_str
            and "development.localhost" not in cmd_str
        ):
            return (0, b"")
        # Site dir exists.
        if "test -d" in cmd_str and f"{BENCH_PATH}/sites/development.localhost" in cmd_str:
            return (0, b"")
        # Backup-dir existence probe (test -d <backup_dir>).
        if "test -d" in cmd_str and "private/backups" in cmd_str:
            return (0, b"")
        # Backup file existence probe (test -f).
        if "test -f" in cmd_str and DB_FILENAME in cmd_str:
            return (0, b"")
        # List sites in scan_backups_for_all_sites.
        if "find" in cmd_str and f"{BENCH_PATH}/sites" in cmd_str and "-maxdepth 1" in cmd_str:
            return (0, f"{BENCH_PATH}/sites/development.localhost\n".encode())
        # List backup files.
        if "find" in cmd_str and "private/backups" in cmd_str:
            return (0, f"{self.scan_backups}\n".encode()) if self.scan_backups else (0, b"")
        # missing-apps: ls apps dir.
        if "ls -1" in cmd_str and f"{BENCH_PATH}/apps" in cmd_str:
            return (0, b"frappe\n")
        # missing-apps: zcat the dump for installed_apps.
        if "zcat" in cmd_str and "installed_apps" in cmd_str:
            return (0, b"[\"frappe\"]','installed_apps'")
        # The restore itself (and any migrate) - succeed.
        return (0, b"")

    @staticmethod
    def _cmd_str(cmd) -> str:
        return " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)

    def restore_calls(self) -> list[dict]:
        return [
            c
            for c in self.exec_calls
            if "restore" in self._cmd_str(c["cmd"]) and "--force" in self._cmd_str(c["cmd"])
        ]


def _run_normal(
    monkeypatch,
    container,
    *,
    site="development.localhost",
    latest=False,
    backup_file=None,
    yes=False,
    isatty=True,
    confirm_answer=True,
    mariadb_root_password=SECRET_PW,
    missing_apps=None,
):
    """Drive the ``restore`` Typer command's normal path against ``container``.

    All external deps are stubbed: the docker client (the decorator + container
    discovery + bench resolver), the bench-path cache, site/backup probes, the
    missing-apps check, TipSpinner, questionary, and the post-restore restart
    (skipped via ``no_migrate=True``). Every Typer parameter is passed explicitly
    so no ``Option`` default object leaks through. The ``@handle_docker_errors``
    decorator is bypassed by patching the docker module names it imports.
    """
    mono = MagicMock()
    mono.isatty.return_value = isatty
    monkeypatch.setattr(restore_mod.sys, "stdin", mono)

    fake_docker = MagicMock()
    fake_docker.ping.return_value = None
    # @handle_docker_errors reads shutil + docker from docker_utils' own namespace.
    monkeypatch.setattr(docker_utils_mod, "shutil", MagicMock())
    monkeypatch.setattr(docker_utils_mod, "docker", MagicMock(from_env=lambda: fake_docker))

    monkeypatch.setattr(restore_mod, "ensure_containers_running", lambda *a, **k: True)
    monkeypatch.setattr(restore_mod, "get_project_containers", lambda name: [container])
    monkeypatch.setattr(
        restore_mod,
        "resolve_bench_path",
        lambda project, bench, path, *, on_ambiguous="error", verbose=False: BENCH_PATH,
    )
    monkeypatch.setattr(restore_mod.db_utils, "get_cached_project_data", lambda name: None)
    monkeypatch.setattr(restore_mod, "scan_backups_for_all_sites", lambda *a, **k: [])
    monkeypatch.setattr(
        restore_mod,
        "group_and_sort_backups",
        lambda backups, target: (
            [_backup_set()] if (latest or backup_file is not None) else [],
            [],
        ),
    )
    monkeypatch.setattr(restore_mod, "check_missing_apps", lambda *a, **k: missing_apps or [])
    monkeypatch.setattr(restore_mod, "TipSpinner", _NullSpinner)
    monkeypatch.setattr(restore_mod.config_utils, "get_show_tips", lambda: False)
    monkeypatch.setattr(restore_mod, "ensure_sendme_installed", lambda *a, **k: True)
    monkeypatch.setattr(restore_mod.questionary, "select", lambda *a, **k: _stub_question(None))
    monkeypatch.setattr(
        restore_mod.questionary, "confirm", lambda *a, **k: _stub_question(confirm_answer)
    )

    # Capture printed output so tests can assert on refusal messages.
    def record(*args, **kwargs):
        container.printed.append(" ".join(str(a) for a in args))

    monkeypatch.setattr(restore_mod.console, "print", record)
    monkeypatch.setattr(restore_mod.stderr_console, "print", record)

    restore_mod.restore(
        project_name="proj",
        site=site,
        latest=latest,
        backup_file=backup_file,
        bench=None,
        bench_path=BENCH_PATH,
        mariadb_root_username="root",
        mariadb_root_password=mariadb_root_password,
        admin_password=None,
        send=False,
        receive=False,
        no_recache=True,
        yes=yes,
        no_migrate=True,
        verbose=False,
    )


class TestSelectBackupSet:
    """Issue #40 Step 6.1: pure-function unit tests for the non-interactive selector."""

    def test_latest_picks_newest_target_backup(self):
        newest = _backup_set(
            filename="20251110_000000-development_localhost-database.sql.gz",
        )
        older = _backup_set(
            filename="20251109_000000-development_localhost-database.sql.gz",
        )
        # group_and_sort_backups returns newest-first, so newest is index 0.
        assert (
            restore_mod.select_backup_set([newest, older], [], latest=True, backup_file=None)
            is newest
        )

    def test_latest_with_empty_target_returns_none_not_other(self):
        other = _backup_set(
            site_dir="othersite",
            filename="20251109_225726-othersite-database.sql.gz",
        )
        # --latest must NOT fall through to other_backups (that would restore a
        # different site's data over the target site).
        assert restore_mod.select_backup_set([], [other], latest=True, backup_file=None) is None

    def test_backup_file_matches_by_filename(self):
        bset = _backup_set()
        assert (
            restore_mod.select_backup_set([bset], [], latest=False, backup_file=DB_FILENAME) is bset
        )

    def test_backup_file_matches_by_full_path(self):
        bset = _backup_set()
        assert (
            restore_mod.select_backup_set([bset], [], latest=False, backup_file=DB_FULL_PATH)
            is bset
        )

    def test_backup_file_no_match_returns_none(self):
        bset = _backup_set()
        assert (
            restore_mod.select_backup_set([bset], [], latest=False, backup_file="nope.sql.gz")
            is None
        )

    def test_backup_file_searches_other_sites_too(self):
        other = _backup_set(
            site_dir="othersite",
            filename="20251109_225726-othersite-database.sql.gz",
        )
        assert (
            restore_mod.select_backup_set(
                [],
                [other],
                latest=False,
                backup_file="20251109_225726-othersite-database.sql.gz",
            )
            is other
        )


class TestNormalPathSelectorsAndExitCodes:
    """Issue #40 Steps 6.2-6.7: the normal restore path is non-interactive-drivable
    and declines/refusals exit non-zero."""

    def test_non_tty_no_selector_exits_nonzero_and_does_not_restore(self, monkeypatch):
        container = FakeNormalContainer()
        with pytest.raises(typer.Exit) as excinfo:
            _run_normal(monkeypatch, container, isatty=False, latest=False, backup_file=None)
        assert excinfo.value.exit_code != 0
        assert container.restore_calls() == []
        assert any("--latest" in line or "--backup-file" in line for line in container.printed)

    def test_non_tty_latest_yes_password_runs_restore(self, monkeypatch):
        container = FakeNormalContainer()
        # The full non-interactive happy path must reach the restore command.
        _run_normal(monkeypatch, container, isatty=False, latest=True, yes=True)
        calls = container.restore_calls()
        assert len(calls) == 1
        # The destructive + missing-apps prompts must NOT have fired under --yes
        # (confirm is stubbed below to fail in the dedicated --yes test).

    def test_non_tty_latest_no_yes_refuses_at_destructive_confirm(self, monkeypatch):
        container = FakeNormalContainer()
        with pytest.raises(typer.Exit) as excinfo:
            _run_normal(monkeypatch, container, isatty=False, latest=True, yes=False)
        assert excinfo.value.exit_code != 0
        assert container.restore_calls() == []

    def test_interactive_destructive_decline_exits_nonzero(self, monkeypatch):
        container = FakeNormalContainer()
        with pytest.raises(typer.Exit) as excinfo:
            _run_normal(
                monkeypatch,
                container,
                isatty=True,
                latest=True,
                yes=False,
                confirm_answer=False,
            )
        assert excinfo.value.exit_code != 0
        assert container.restore_calls() == []

    def test_interactive_missing_apps_decline_exits_nonzero(self, monkeypatch):
        container = FakeNormalContainer()
        with pytest.raises(typer.Exit) as excinfo:
            _run_normal(
                monkeypatch,
                container,
                isatty=True,
                latest=True,
                yes=False,
                confirm_answer=False,
                missing_apps=["erpnext"],
            )
        assert excinfo.value.exit_code != 0
        assert container.restore_calls() == []

    def test_latest_and_backup_file_together_exits_nonzero_before_restore(self, monkeypatch):
        container = FakeNormalContainer()
        with pytest.raises(typer.Exit) as excinfo:
            _run_normal(
                monkeypatch,
                container,
                isatty=False,
                latest=True,
                backup_file=DB_FILENAME,
                yes=True,
            )
        assert excinfo.value.exit_code != 0
        assert container.restore_calls() == []

    def test_yes_skips_both_confirms_no_prompt_fires(self, monkeypatch):
        container = FakeNormalContainer()

        # If questionary.confirm were reached it would RAISE, proving --yes bypassed it.
        def fail_confirm(*a, **k):
            raise AssertionError("confirm prompt must not fire under --yes")

        monkeypatch.setattr(restore_mod.questionary, "confirm", fail_confirm)
        _run_normal(
            monkeypatch,
            container,
            isatty=True,  # TTY so the non-TTY refusal branch is NOT why no prompt fires
            latest=True,
            yes=True,
            missing_apps=["erpnext"],  # forces the missing-apps gate to be reached
        )
        assert len(container.restore_calls()) == 1

    def test_non_tty_no_yes_with_missing_apps_refuses_nonzero(self, monkeypatch):
        container = FakeNormalContainer()
        with pytest.raises(typer.Exit) as excinfo:
            _run_normal(
                monkeypatch,
                container,
                isatty=False,
                latest=True,
                yes=False,
                missing_apps=["erpnext"],
            )
        assert excinfo.value.exit_code != 0
        assert container.restore_calls() == []

    def test_backup_file_no_match_exits_nonzero(self, monkeypatch):
        container = FakeNormalContainer()
        # Stub group_and_sort_backups to return a set whose filename does NOT match.
        nope = _backup_set(
            filename="20251109_000000-development_localhost-database.sql.gz",
        )
        monkeypatch.setattr(
            restore_mod,
            "group_and_sort_backups",
            lambda backups, target: ([nope], []),
        )
        with pytest.raises(typer.Exit) as excinfo:
            _run_normal(
                monkeypatch,
                container,
                isatty=False,
                backup_file="does-not-exist.sql.gz",
                yes=True,
            )
        assert excinfo.value.exit_code != 0
        assert container.restore_calls() == []
