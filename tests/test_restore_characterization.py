"""Characterization net for ``cwcli restore``, pinned green BEFORE the logic-core
migration (batch 11, ``migrate-restore-core``).

These lock the command-level behaviors the two existing restore suites under-cover
so the migration can be proven refactor-under-green:

- the NORMAL-path restore exec arg ORDER and the secret riding ``environment=``
  never the argv (``test_restore_safety.py`` pins only the receive path);
- the encryption-key merge from a backup's ``site_config_backup`` into the live
  ``site_config.json``;
- the post-restore migrate-then-restart order and the migrate-failure exit code.

They are committed against the unmigrated command; the migration re-points the
patch targets to the core (named BY DESIGN in the batch) with assertions intact.
The seams are the container fake's ``exec_run`` (the core execs the same
container), the lazily-imported ``commands.start._start_project`` restart, and
``questionary`` / ``sys.stdin`` (frontend UX that never moves to the core).
"""

from unittest.mock import MagicMock

import pytest
import typer

from caffeinated_whale_cli.commands import restore as restore_mod
from caffeinated_whale_cli.utils import docker_utils as docker_utils_mod

BENCH_PATH = "/workspace/frappe-bench"
SITE = "development.localhost"
SECRET_PW = "sup3r-s3cr3t-pw"
DB_FILENAME = "20251109_225726-development_localhost-database.sql.gz"
CONFIG_FILENAME = "20251109_225726-development_localhost-site_config_backup.json"


def _stub_question(answer):
    q = MagicMock()
    q.ask.return_value = answer
    return q


class _NullSpinner:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def update(self, *args, **kwargs):
        pass


class FakeRestoreContainer:
    """Frappe container fake for the normal restore path.

    Answers the bench/site ``test -d`` probes, the backup scan, the missing-apps
    ``ls``/``zcat`` probes, the ``test -f`` dump check, the encryption-key
    ``cat``/heredoc, and the restore/migrate execs. Records every call so tests
    can inspect the argv and environment. ``migrate_exit`` controls the migrate
    exec's exit code so the migrate-failure path is drivable.
    """

    def __init__(self, *, with_config_backup=False, migrate_exit=0):
        self.labels = {"com.docker.compose.service": "frappe"}
        self.status = "running"
        self.exec_calls: list[dict] = []
        self.with_config_backup = with_config_backup
        self.migrate_exit = migrate_exit
        self.printed: list[str] = []

    def reload(self):
        return None

    @staticmethod
    def _cmd_str(cmd) -> str:
        return " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)

    def exec_run(self, cmd, workdir=None, environment=None):
        self.exec_calls.append({"cmd": cmd, "workdir": workdir, "environment": environment})
        s = self._cmd_str(cmd)

        # The restore itself: honor the real exit path (success).
        if "restore" in s and "--force" in s:
            return (0, b"Restore successful")
        # bench migrate: controllable exit code.
        if "migrate" in s:
            return (self.migrate_exit, b"migrate output")
        # Backup scan: list one site then its backup files. The site-listing find
        # and the backup-file find both name ``/sites`` and ``-maxdepth 1``; the
        # backup-file one is distinguished by ``private/backups``.
        if (
            "find" in s
            and f"{BENCH_PATH}/sites" in s
            and "-maxdepth 1" in s
            and "private/backups" not in s
        ):
            return (0, f"{BENCH_PATH}/sites/{SITE}\n".encode())
        if "find" in s and "private/backups" in s:
            backup_dir = f"{BENCH_PATH}/sites/{SITE}/private/backups"
            lines = [f"{backup_dir}/{DB_FILENAME}"]
            if self.with_config_backup:
                lines.append(f"{backup_dir}/{CONFIG_FILENAME}")
            return (0, ("\n".join(lines) + "\n").encode())
        # missing-apps: ls apps + zcat installed_apps (no missing apps here).
        if "ls -1" in s and f"{BENCH_PATH}/apps" in s:
            return (0, b"frappe\n")
        if "zcat" in s and "installed_apps" in s:
            return (0, b"[\"frappe\"]','installed_apps'")
        # Encryption-key merge: read either config; return a JSON blob.
        if "cat" in s and CONFIG_FILENAME in s:
            return (0, b'{"encryption_key": "KEY-FROM-BACKUP"}')
        if "cat" in s and "site_config.json" in s:
            return (0, b'{"db_name": "x"}')
        # Any test -d / test -f / heredoc write: succeed.
        return (0, b"")

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
    latest=True,
    yes=True,
    no_migrate=True,
    start_stub=None,
):
    """Drive the real ``restore`` command's normal path (selector-driven, --yes).

    Re-pointed BY DESIGN at the migrated seams: container discovery, run-state, and
    bench resolution moved into ``core.restore`` (so they are patched there), the
    frontend keeps only the ``ensure_containers_running`` / ``resolve_bench_path``
    prologue, and the post-restore restart is ``core.start.start``.
    """
    from types import SimpleNamespace

    from caffeinated_whale_cli.core import restore as core_restore
    from caffeinated_whale_cli.core.envelope import Result, Status

    mono = MagicMock()
    mono.isatty.return_value = False
    monkeypatch.setattr(restore_mod.sys, "stdin", mono)

    fake_docker = MagicMock()
    fake_docker.ping.return_value = None
    monkeypatch.setattr(docker_utils_mod, "shutil", MagicMock())
    monkeypatch.setattr(docker_utils_mod, "docker", MagicMock(from_env=lambda: fake_docker))

    # Frontend prologue seams.
    monkeypatch.setattr(restore_mod, "ensure_containers_running", lambda *a, **k: True)
    monkeypatch.setattr(
        restore_mod,
        "resolve_bench_path",
        lambda project, bench, path, *, on_ambiguous="error", verbose=False: BENCH_PATH,
    )
    monkeypatch.setattr(restore_mod, "TipSpinner", _NullSpinner)
    monkeypatch.setattr(restore_mod.config_utils, "get_show_tips", lambda: False)
    monkeypatch.setattr(restore_mod.questionary, "select", lambda *a, **k: _stub_question(None))
    monkeypatch.setattr(restore_mod.questionary, "confirm", lambda *a, **k: _stub_question(True))

    # Core seams (container discovery + run-state + bench moved here).
    monkeypatch.setattr(core_restore.core_docker, "get_frappe_container", lambda name: container)
    monkeypatch.setattr(
        core_restore.resolvers,
        "resolve_container_state",
        lambda *a, **k: SimpleNamespace(status=Status.OK, choice=None),
    )
    monkeypatch.setattr(
        core_restore.resolvers,
        "resolve_bench",
        lambda *a, **k: Result(status=Status.OK, data=BENCH_PATH, warnings=[]),
    )

    import caffeinated_whale_cli.core.start as core_start

    if start_stub is not None:
        # Adapt the old _start_project(**kwargs)->log_path stub to core.start.start.
        def core_start_stub(project_name, *, bench_path=None, restart=False, **k):
            log = start_stub(project_name, bench_path_override=bench_path, restart=restart)
            return Result(status=Status.OK, data=SimpleNamespace(log_path=log))

        monkeypatch.setattr(core_start, "start", core_start_stub)

    def record(*args, **kwargs):
        container.printed.append(" ".join(str(a) for a in args))

    monkeypatch.setattr(restore_mod.console, "print", record)
    monkeypatch.setattr(restore_mod.stderr_console, "print", record)

    restore_mod.restore(
        project_name="proj",
        site=SITE,
        latest=latest,
        backup_file=None,
        bench=None,
        bench_path=BENCH_PATH,
        mariadb_root_username="root",
        mariadb_root_password=SECRET_PW,
        admin_password=None,
        send=False,
        receive=False,
        no_recache=True,
        yes=yes,
        no_migrate=no_migrate,
        verbose=False,
    )


class TestNormalPathSecretOffArgv:
    """The normal restore exec builds the command with the arg order and the
    secret in ``environment=``, never on the argv (mirrors receive's M5)."""

    def test_restore_command_arg_order_and_secret_in_environment(self, monkeypatch):
        container = FakeRestoreContainer()
        _run_normal(monkeypatch, container)

        calls = container.restore_calls()
        assert len(calls) == 1
        call = calls[0]
        cmd_str = FakeRestoreContainer._cmd_str(call["cmd"])

        # The dump full path, credentials, and --force in order.
        assert f"{BENCH_PATH}/sites/{SITE}/private/backups/{DB_FILENAME}" in cmd_str
        assert "--mariadb-root-username root" in cmd_str
        assert "--mariadb-root-password \"$CWCLI_MARIADB_ROOT_PASSWORD\"" in cmd_str
        assert "--force" in cmd_str
        # The order: db path precedes credentials precede --force.
        assert cmd_str.index(DB_FILENAME) < cmd_str.index("--mariadb-root-username")
        assert cmd_str.index("--mariadb-root-username") < cmd_str.index("--force")

        # The secret is NOT on the argv; it rides the environment.
        assert SECRET_PW not in cmd_str
        assert call["environment"] is not None
        assert call["environment"].get("CWCLI_MARIADB_ROOT_PASSWORD") == SECRET_PW


class TestEncryptionKeyMerge:
    """A backup carrying a site_config_backup has its encryption_key merged into
    the live site_config.json after the restore."""

    def test_encryption_key_written_from_backup(self, monkeypatch):
        container = FakeRestoreContainer(with_config_backup=True)
        _run_normal(monkeypatch, container)

        # The heredoc write of site_config.json must carry the backup's key.
        writes = [
            c
            for c in container.exec_calls
            if "site_config.json" in FakeRestoreContainer._cmd_str(c["cmd"])
            and "EOF" in FakeRestoreContainer._cmd_str(c["cmd"])
        ]
        assert writes, "expected a heredoc write of site_config.json"
        assert any("KEY-FROM-BACKUP" in FakeRestoreContainer._cmd_str(w["cmd"]) for w in writes)


class TestMigrateAndRestart:
    """Post-restore: migrate then restart; a migrate failure still restarts but
    exits non-zero."""

    def test_migrate_then_restart_in_order(self, monkeypatch):
        container = FakeRestoreContainer(migrate_exit=0)
        restart_calls = []

        def start_stub(project_name, **kwargs):
            restart_calls.append((project_name, kwargs))
            return "/tmp/bench-start.log"

        _run_normal(monkeypatch, container, no_migrate=False, start_stub=start_stub)

        # The migrate exec ran, and the restart was requested for the same bench.
        migrate_idx = next(
            i
            for i, c in enumerate(container.exec_calls)
            if "migrate" in FakeRestoreContainer._cmd_str(c["cmd"])
        )
        restore_idx = next(
            i
            for i, c in enumerate(container.exec_calls)
            if "--force" in FakeRestoreContainer._cmd_str(c["cmd"])
        )
        assert restore_idx < migrate_idx  # restore precedes migrate
        assert len(restart_calls) == 1
        assert restart_calls[0][1].get("bench_path_override") == BENCH_PATH
        assert restart_calls[0][1].get("restart") is True

    def test_migrate_failure_still_restarts_and_exits_nonzero(self, monkeypatch):
        container = FakeRestoreContainer(migrate_exit=1)
        restart_calls = []

        def start_stub(project_name, **kwargs):
            restart_calls.append(project_name)
            return "/tmp/bench-start.log"

        with pytest.raises(typer.Exit) as excinfo:
            _run_normal(monkeypatch, container, no_migrate=False, start_stub=start_stub)

        assert excinfo.value.exit_code != 0
        # A failed migrate does NOT skip the restart.
        assert len(restart_calls) == 1


class TestMutualExclusions:
    """The flag guards that stay frontend UX after the migration."""

    def _bare_call(self, monkeypatch, **overrides):
        mono = MagicMock()
        mono.isatty.return_value = False
        monkeypatch.setattr(restore_mod.sys, "stdin", mono)
        monkeypatch.setattr(docker_utils_mod, "shutil", MagicMock())
        monkeypatch.setattr(
            docker_utils_mod, "docker", MagicMock(from_env=lambda: MagicMock())
        )
        monkeypatch.setattr(
            restore_mod,
            "resolve_bench_path",
            lambda project, bench, path, *, on_ambiguous="error", verbose=False: BENCH_PATH,
        )
        monkeypatch.setattr(restore_mod.console, "print", lambda *a, **k: None)
        monkeypatch.setattr(restore_mod.stderr_console, "print", lambda *a, **k: None)
        params = dict(
            project_name="proj",
            site=SITE,
            latest=False,
            backup_file=None,
            bench=None,
            bench_path=BENCH_PATH,
            mariadb_root_username="root",
            mariadb_root_password=SECRET_PW,
            admin_password=None,
            send=False,
            receive=False,
            no_recache=True,
            yes=True,
            no_migrate=True,
            verbose=False,
        )
        params.update(overrides)
        restore_mod.restore(**params)

    def test_send_and_receive_together_exits_nonzero(self, monkeypatch):
        with pytest.raises(typer.Exit) as excinfo:
            self._bare_call(monkeypatch, send=True, receive=True)
        assert excinfo.value.exit_code != 0

    def test_latest_and_backup_file_together_exits_nonzero(self, monkeypatch):
        with pytest.raises(typer.Exit) as excinfo:
            self._bare_call(monkeypatch, latest=True, backup_file=DB_FILENAME)
        assert excinfo.value.exit_code != 0

    def test_selector_with_send_exits_nonzero(self, monkeypatch):
        with pytest.raises(typer.Exit) as excinfo:
            self._bare_call(monkeypatch, latest=True, send=True)
        assert excinfo.value.exit_code != 0
