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
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import typer

from caffeinated_whale_cli.commands import restore as restore_mod

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
):
    """Drive ``restore_receive_mode`` end-to-end against ``container``.

    Everything external is stubbed: ``sendme`` is faked to "download" a single
    database backup named ``db_filename`` into its working directory, container
    discovery returns ``container``, the missing-apps check returns none, and the
    TTY / confirmation answers are controlled by ``isatty`` / ``confirm_answer``.
    Credentials are supplied directly so no interactive credential prompt fires.
    """

    def fake_sendme_run(cmd, cwd=None, capture_output=True, text=True):
        Path(cwd).joinpath(db_filename).write_text("SQL DUMP DATA")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_sendme_run)
    monkeypatch.setattr(restore_mod, "ensure_containers_running", lambda *a, **k: True)
    monkeypatch.setattr(restore_mod, "get_project_containers", lambda name: [container])
    monkeypatch.setattr(restore_mod, "get_sendme_command", lambda: "sendme")
    monkeypatch.setattr(restore_mod, "check_missing_apps", lambda *a, **k: [])
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
