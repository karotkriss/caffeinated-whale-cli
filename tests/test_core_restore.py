"""Unit tests for ``core.restore`` - the plan/apply slice (batch 11).

Every branch runs against container/subprocess fakes; no live Docker. Pins the
two choice surfaces (``select_backup``, ``confirm_restore``), the secret riding
``environment=`` never the argv, the exec arg order, the migrate-failure
``WARNING``, the streamed copies, plain-data DTOs, core silence, and the origin
mismatch.
"""

import dataclasses
from datetime import datetime
from types import SimpleNamespace

import pytest

from caffeinated_whale_cli.core import restore as core_restore
from caffeinated_whale_cli.core.envelope import Result, Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind

BENCH_PATH = "/workspace/frappe-bench"
SITE = "development.localhost"
SECRET_PW = "sup3r-s3cr3t-pw"
DB_FILENAME = "20251109_225726-development_localhost-database.sql.gz"
CONFIG_FILENAME = "20251109_225726-development_localhost-site_config_backup.json"
BACKUP_DIR = f"{BENCH_PATH}/sites/{SITE}/private/backups"
DB_FULL_PATH = f"{BACKUP_DIR}/{DB_FILENAME}"


class FakeContainer:
    def __init__(self, *, with_config_backup=False, migrate_exit=0, restore_exit=0, apps="frappe"):
        self.labels = {"com.docker.compose.service": "frappe"}
        self.status = "running"
        self.exec_calls: list[dict] = []
        self.with_config_backup = with_config_backup
        self.migrate_exit = migrate_exit
        self.restore_exit = restore_exit
        self.apps = apps
        self.put_archive_streamed: bool | None = None
        self.put_archive_paths: list[str] = []

    def reload(self):
        return None

    @staticmethod
    def _s(cmd):
        return " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)

    def exec_run(self, cmd, workdir=None, environment=None):
        self.exec_calls.append({"cmd": cmd, "workdir": workdir, "environment": environment})
        s = self._s(cmd)
        if "restore" in s and "--force" in s:
            return (self.restore_exit, b"Restore output")
        if "migrate" in s:
            return (self.migrate_exit, b"migrate output")
        if "find" in s and f"{BENCH_PATH}/sites" in s and "-maxdepth 1" in s and "private/backups" not in s:
            return (0, f"{BENCH_PATH}/sites/{SITE}\n".encode())
        if "find" in s and "private/backups" in s:
            lines = [f"{BACKUP_DIR}/{DB_FILENAME}"]
            if self.with_config_backup:
                lines.append(f"{BACKUP_DIR}/{CONFIG_FILENAME}")
            return (0, ("\n".join(lines) + "\n").encode())
        if "ls -1" in s and f"{BENCH_PATH}/apps" in s:
            return (0, f"{self.apps}\n".encode())
        if "zcat" in s and "installed_apps" in s:
            return (0, b"[\"frappe\"]','installed_apps'")
        if "cat" in s and CONFIG_FILENAME in s:
            return (0, b'{"encryption_key": "KEY-FROM-BACKUP"}')
        if "cat" in s and "site_config.json" in s:
            return (0, b'{"db_name": "x"}')
        return (0, b"")

    def put_archive(self, path, data):
        self.put_archive_streamed = hasattr(data, "read")
        self.put_archive_paths.append(path)
        return True

    def restore_calls(self):
        return [c for c in self.exec_calls if "restore" in self._s(c["cmd"]) and "--force" in self._s(c["cmd"])]


def _patch_common(monkeypatch, container, *, run_state=Status.OK, bench=BENCH_PATH):
    monkeypatch.setattr(core_restore.core_docker, "get_frappe_container", lambda name: container)
    monkeypatch.setattr(
        core_restore.resolvers,
        "resolve_container_state",
        lambda *a, **k: SimpleNamespace(status=run_state, choice=SimpleNamespace(kind="confirm_start")),
    )
    monkeypatch.setattr(
        core_restore.resolvers,
        "resolve_bench",
        lambda *a, **k: Result(status=Status.OK, data=bench, warnings=[]),
    )


# --------------------------------------------------------------------------- #
# restore_plan
# --------------------------------------------------------------------------- #
class TestRestorePlan:
    def test_latest_resolves_to_a_plan(self, monkeypatch):
        c = FakeContainer()
        _patch_common(monkeypatch, c)
        result = core_restore.restore_plan("proj", site=SITE, latest=True)
        assert result.status is Status.OK
        assert result.data.database_path == DB_FULL_PATH
        assert result.data.backup_filename == DB_FILENAME
        assert result.data.restore_items == ["Database"]

    def test_no_selector_returns_select_backup_choice(self, monkeypatch):
        c = FakeContainer()
        _patch_common(monkeypatch, c)
        result = core_restore.restore_plan("proj", site=SITE)
        assert result.status is Status.NEEDS_CHOICE
        assert result.choice.kind == "select_backup"
        assert result.choice.param == "selected_backup"
        assert any(o["value"] == DB_FULL_PATH for o in result.choice.options)

    def test_selected_backup_matches_and_resolves(self, monkeypatch):
        c = FakeContainer()
        _patch_common(monkeypatch, c)
        result = core_restore.restore_plan("proj", site=SITE, selected_backup=DB_FULL_PATH)
        assert result.status is Status.OK
        assert result.data.database_path == DB_FULL_PATH

    def test_selector_no_match_raises_not_found(self, monkeypatch):
        c = FakeContainer()
        _patch_common(monkeypatch, c)
        with pytest.raises(CwcliError) as e:
            core_restore.restore_plan("proj", site=SITE, backup_file="nope.sql.gz")
        assert e.value.kind is ErrorKind.NOT_FOUND

    def test_stopped_container_returns_confirm_start(self, monkeypatch):
        c = FakeContainer()
        _patch_common(monkeypatch, c, run_state=Status.NEEDS_CHOICE)
        result = core_restore.restore_plan("proj", site=SITE, latest=True)
        assert result.status is Status.NEEDS_CHOICE
        assert result.choice.kind == "confirm_start"

    def test_origin_mismatch_recorded_in_plan(self, monkeypatch):
        c = FakeContainer()
        _patch_common(monkeypatch, c)
        # Restore the same dump into a DIFFERENT site name.
        result = core_restore.restore_plan("proj", site="other.localhost", selected_backup=DB_FULL_PATH)
        assert result.status is Status.OK
        assert result.data.origin_mismatch == "development_localhost"

    def test_missing_apps_recorded(self, monkeypatch):
        c = FakeContainer(apps="frappe")  # backup dump lists only frappe -> none missing
        _patch_common(monkeypatch, c)
        result = core_restore.restore_plan("proj", site=SITE, latest=True)
        assert result.data.missing_apps == []


# --------------------------------------------------------------------------- #
# restore_apply
# --------------------------------------------------------------------------- #
def _plan(**overrides):
    base = dict(
        project_name="proj",
        site=SITE,
        bench_path=BENCH_PATH,
        database_path=DB_FULL_PATH,
        files_path=None,
        private_files_path=None,
        site_config_backup_path=None,
        backup_filename=DB_FILENAME,
        backup_timestamp="2025-11-09 22:57:26",
        restore_items=["Database"],
    )
    base.update(overrides)
    return core_restore.RestorePlan(**base)


def _patch_apply(monkeypatch, container, *, restart_log="/tmp/x.log", restart_raises=False):
    monkeypatch.setattr(core_restore.core_docker, "get_frappe_container", lambda name: container)
    import caffeinated_whale_cli.core.start as core_start

    def fake_start(project_name, *, bench_path=None, restart=False, **k):
        if restart_raises:
            raise CwcliError(ErrorKind.DOCKER, "x", "boom")
        return Result(status=Status.OK, data=SimpleNamespace(log_path=restart_log))

    monkeypatch.setattr(core_start, "start", fake_start)


class TestRestoreApply:
    def test_no_consent_returns_confirm_restore_and_does_not_exec(self, monkeypatch):
        c = FakeContainer()
        _patch_apply(monkeypatch, c)
        result = core_restore.restore_apply(
            _plan(), mariadb_root_username="root", mariadb_root_password=SECRET_PW, consent=False
        )
        assert result.status is Status.NEEDS_CHOICE
        assert result.choice.kind == "confirm_restore"
        assert c.restore_calls() == []

    def test_secret_rides_environment_not_argv_with_arg_order(self, monkeypatch):
        c = FakeContainer()
        _patch_apply(monkeypatch, c)
        result = core_restore.restore_apply(
            _plan(files_path=f"{BACKUP_DIR}/f-files.tar", private_files_path=f"{BACKUP_DIR}/f-private-files.tar"),
            mariadb_root_username="root",
            mariadb_root_password=SECRET_PW,
            consent=True,
        )
        assert result.status is Status.OK
        calls = c.restore_calls()
        assert len(calls) == 1
        cmd = FakeContainer._s(calls[0]["cmd"])
        assert SECRET_PW not in cmd
        assert calls[0]["environment"]["CWCLI_MARIADB_ROOT_PASSWORD"] == SECRET_PW
        # Arg order: db path -> credentials -> --force -> file archives.
        assert cmd.index(DB_FILENAME) < cmd.index("--mariadb-root-username")
        assert cmd.index("--mariadb-root-username") < cmd.index("--force")
        assert cmd.index("--force") < cmd.index("--with-public-files")

    def test_admin_password_rides_environment(self, monkeypatch):
        c = FakeContainer()
        _patch_apply(monkeypatch, c)
        core_restore.restore_apply(
            _plan(), mariadb_root_username="root", mariadb_root_password=SECRET_PW,
            admin_password="adm1n", consent=True,
        )
        call = c.restore_calls()[0]
        assert "adm1n" not in FakeContainer._s(call["cmd"])
        assert call["environment"]["CWCLI_ADMIN_PASSWORD"] == "adm1n"

    def test_hard_restore_failure_raises_precondition(self, monkeypatch):
        c = FakeContainer(restore_exit=1)
        _patch_apply(monkeypatch, c)
        with pytest.raises(CwcliError) as e:
            core_restore.restore_apply(
                _plan(), mariadb_root_username="root", mariadb_root_password=SECRET_PW, consent=True
            )
        assert e.value.kind is ErrorKind.PRECONDITION

    def test_migrate_failure_is_warning_and_still_restarts(self, monkeypatch):
        c = FakeContainer(migrate_exit=1)
        restarts = []
        monkeypatch.setattr(core_restore.core_docker, "get_frappe_container", lambda name: c)
        import caffeinated_whale_cli.core.start as core_start

        def fake_start(project_name, *, bench_path=None, restart=False, **k):
            restarts.append((bench_path, restart))
            return Result(status=Status.OK, data=SimpleNamespace(log_path="/tmp/x.log"))

        monkeypatch.setattr(core_start, "start", fake_start)

        result = core_restore.restore_apply(
            _plan(), mariadb_root_username="root", mariadb_root_password=SECRET_PW, consent=True
        )
        assert result.status is Status.WARNING
        assert result.data.migrate_ran is True
        assert result.data.migrate_ok is False
        assert len(restarts) == 1  # restart still happened
        assert restarts[0] == (BENCH_PATH, True)

    def test_web_not_ready_warning_reaches_result(self, monkeypatch):
        # core.start's web-readiness timeout must not be silently dropped by the
        # post-restore restart - the codebase's most safety-critical path.
        c = FakeContainer()
        monkeypatch.setattr(core_restore.core_docker, "get_frappe_container", lambda name: c)
        import caffeinated_whale_cli.core.start as core_start
        from caffeinated_whale_cli.core.envelope import Message

        def fake_start(project_name, *, bench_path=None, restart=False, **k):
            return Result(
                status=Status.OK,
                data=SimpleNamespace(log_path="/tmp/x.log"),
                warnings=[Message("start.web_not_ready", "web did not begin serving on :8000")],
            )

        monkeypatch.setattr(core_start, "start", fake_start)

        result = core_restore.restore_apply(
            _plan(), mariadb_root_username="root", mariadb_root_password=SECRET_PW, consent=True
        )
        assert result.status is Status.OK
        assert any(w.code == "start.web_not_ready" for w in result.warnings)

    def test_no_migrate_skips_migrate_and_restart(self, monkeypatch):
        c = FakeContainer()
        _patch_apply(monkeypatch, c)
        result = core_restore.restore_apply(
            _plan(), mariadb_root_username="root", mariadb_root_password=SECRET_PW,
            consent=True, no_migrate=True,
        )
        assert result.status is Status.OK
        assert result.data.migrate_ran is False
        assert not any("migrate" in FakeContainer._s(x["cmd"]) for x in c.exec_calls)

    def test_encryption_key_merged_from_backup(self, monkeypatch):
        c = FakeContainer(with_config_backup=True)
        _patch_apply(monkeypatch, c)
        result = core_restore.restore_apply(
            _plan(site_config_backup_path=f"{BACKUP_DIR}/{CONFIG_FILENAME}"),
            mariadb_root_username="root", mariadb_root_password=SECRET_PW, consent=True,
        )
        assert result.data.encryption_key_updated is True
        writes = [
            x for x in c.exec_calls
            if "site_config.json" in FakeContainer._s(x["cmd"]) and "EOF" in FakeContainer._s(x["cmd"])
        ]
        assert writes and "KEY-FROM-BACKUP" in FakeContainer._s(writes[0]["cmd"])

    def test_invalid_username_raises_usage(self, monkeypatch):
        c = FakeContainer()
        _patch_apply(monkeypatch, c)
        with pytest.raises(CwcliError) as e:
            core_restore.restore_apply(
                _plan(), mariadb_root_username="root;rm -rf", mariadb_root_password=SECRET_PW, consent=True
            )
        assert e.value.kind is ErrorKind.USAGE
        assert c.restore_calls() == []

    def test_missing_dump_raises_not_found(self, monkeypatch):
        class NoDump(FakeContainer):
            def exec_run(self, cmd, workdir=None, environment=None):
                s = self._s(cmd)
                if "test" in s and "-f" in s:
                    return (1, b"")
                return super().exec_run(cmd, workdir, environment)

        c = NoDump()
        _patch_apply(monkeypatch, c)
        with pytest.raises(CwcliError) as e:
            core_restore.restore_apply(
                _plan(), mariadb_root_username="root", mariadb_root_password=SECRET_PW, consent=True
            )
        assert e.value.kind is ErrorKind.NOT_FOUND


# --------------------------------------------------------------------------- #
# receive_plan
# --------------------------------------------------------------------------- #
class TestReceivePlan:
    def test_copies_in_streamed_and_builds_plan(self, monkeypatch, tmp_path):
        c = FakeContainer()
        _patch_common(monkeypatch, c)
        f = tmp_path / DB_FILENAME
        f.write_text("SQL")
        result = core_restore.receive_plan("proj", site=SITE, bench_path=BENCH_PATH, downloaded_files=[f])
        assert result.status is Status.OK
        assert result.data.is_receive is True
        assert result.data.database_path == f"{BACKUP_DIR}/{DB_FILENAME}"
        assert c.put_archive_streamed is True  # M4: streamed handle

    def test_no_database_raises_precondition(self, monkeypatch, tmp_path):
        c = FakeContainer()
        _patch_common(monkeypatch, c)
        junk = tmp_path / "notabackup.txt"
        junk.write_text("x")
        with pytest.raises(CwcliError) as e:
            core_restore.receive_plan("proj", site=SITE, bench_path=BENCH_PATH, downloaded_files=[junk])
        assert e.value.kind is ErrorKind.PRECONDITION


# --------------------------------------------------------------------------- #
# copy_backup_files_out (send)
# --------------------------------------------------------------------------- #
class TestCopyOut:
    def test_streams_via_get_archive(self, tmp_path):
        import io
        import tarfile

        # Build a tar stream the fake get_archive yields.
        def make_tar(name, body):
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as tar:
                info = tarfile.TarInfo(name=name)
                data = body.encode()
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            return buf.getvalue()

        class GetArchiveContainer:
            def get_archive(self, path):
                name = path.split("/")[-1]
                return [make_tar(name, "DUMP")], {"name": name}

        core_restore.copy_backup_files_out(
            GetArchiveContainer(), [f"{BACKUP_DIR}/{DB_FILENAME}"], tmp_path
        )
        assert (tmp_path / DB_FILENAME).read_text() == "DUMP"


# --------------------------------------------------------------------------- #
# DTO / purity contracts
# --------------------------------------------------------------------------- #
class TestContracts:
    def test_dtos_are_plain_data_with_no_password(self, monkeypatch):
        c = FakeContainer()
        _patch_common(monkeypatch, c)
        plan = core_restore.restore_plan("proj", site=SITE, latest=True).data
        d = dataclasses.asdict(plan)
        assert isinstance(d, dict)
        flat = repr(d).lower()
        assert "password" not in flat and "container" not in flat

        _patch_apply(monkeypatch, c)
        report = core_restore.restore_apply(
            _plan(), mariadb_root_username="root", mariadb_root_password=SECRET_PW,
            consent=True, no_migrate=True,
        ).data
        rd = dataclasses.asdict(report)
        assert "password" not in repr(rd).lower()

    def test_core_prints_nothing(self, monkeypatch, capsys):
        c = FakeContainer()
        _patch_common(monkeypatch, c)
        _patch_apply(monkeypatch, c)
        plan = core_restore.restore_plan("proj", site=SITE, latest=True).data
        core_restore.restore_apply(
            plan, mariadb_root_username="root", mariadb_root_password=SECRET_PW,
            consent=True, no_migrate=True,
        )
        out, err = capsys.readouterr()
        assert out == "" and err == ""


# --------------------------------------------------------------------------- #
# Pure selector (moved from test_restore_safety.py::TestSelectBackupSet)
# --------------------------------------------------------------------------- #
def _bset(site_dir=SITE, filename=DB_FILENAME):
    return {
        "timestamp": datetime(2025, 11, 9, 22, 57, 26),
        "timestamp_str": "20251109_225726",
        "site_name": "development_localhost",
        "site_dir": site_dir,
        "database": {"filename": filename, "full_path": f"{BENCH_PATH}/sites/{site_dir}/private/backups/{filename}"},
        "files": None,
        "private_files": None,
        "site_config_backup": None,
    }


class TestNoAxiRestoreVerb:
    """There is deliberately NO ``axi restore`` verb in this batch, and it is
    DEFERRED, not refused (design Decision 9 of ``migrate-restore-core``): the
    plan/apply core shape serializes cleanly and would make an ``axi restore``
    thin, but a ``bench restore --force`` that drops and recreates a live site's
    database is the single most destructive operation cwcli performs - a product
    decision the captain owns on its own evidence (the ``axi apps install``/
    ``uninstall`` captain-lock and the ``axi init`` deferral class). This test
    keeps the deferral legible so it can never read as "forgotten"."""

    def test_axi_registry_has_no_restore_command(self):
        from caffeinated_whale_cli.commands import axi as axi_mod

        registered = {c.name for c in axi_mod.app.registered_commands}
        assert "restore" not in registered
        for group in axi_mod.app.registered_groups:
            sub = {c.name for c in group.typer_instance.registered_commands}
            assert "restore" not in sub


class TestSelectBackupSet:
    def test_latest_picks_newest_target(self):
        newest = _bset(filename="20251110_000000-development_localhost-database.sql.gz")
        older = _bset(filename="20251109_000000-development_localhost-database.sql.gz")
        assert core_restore.select_backup_set([newest, older], [], latest=True, backup_file=None) is newest

    def test_latest_empty_target_returns_none(self):
        other = _bset(site_dir="other", filename="20251109_225726-other-database.sql.gz")
        assert core_restore.select_backup_set([], [other], latest=True, backup_file=None) is None

    def test_backup_file_matches_across_both_lists(self):
        other = _bset(site_dir="other", filename="20251109_225726-other-database.sql.gz")
        assert core_restore.select_backup_set([], [other], latest=False, backup_file="20251109_225726-other-database.sql.gz") is other

    def test_multi_match_records_a_warning(self):
        from caffeinated_whale_cli.core.envelope import Message

        a = _bset()
        b = _bset()
        warnings: list[Message] = []
        core_restore.select_backup_set([a, b], [], latest=False, backup_file=DB_FILENAME, warnings=warnings)
        assert any(w.code == "backup.multi_match" for w in warnings)
