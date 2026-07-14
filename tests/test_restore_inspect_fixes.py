"""Regression tests for the six restore + inspect fixes (cwcli-restore-e2e-r6).

Each class pins one of the bugs the captain hit on a live 0.33.0 session:

1. ``--receive`` restore passed the BARE database filename to ``bench restore``
   (rejected as "Invalid path"); it must use the full container path and mirror
   the normal path's arg order.
2. The normal path skipped the interactive MariaDB username prompt and never
   collected the password; both modes (interactive prompt / non-interactive flag)
   must work, and a non-TTY without a password must refuse.
3. ``inspect`` treated ``currentsite.txt`` (a plain file) as a site; sites are
   directories containing ``site_config.json`` only.
4. The default site can live in ``currentsite.txt``, not only
   ``common_site_config.json``'s ``default_site``.
5. The missing-apps warning silently never fired (it read a per-site
   ``apps.json`` Frappe does not write); it must compare the site's real
   installed apps against the bench's available apps.
6. After a successful restore, cwcli runs ``bench migrate`` then restarts.
"""

import shlex
from types import SimpleNamespace

import pytest
import typer

from caffeinated_whale_cli.commands import restore as restore_mod
from caffeinated_whale_cli.utils import bench_sites, db_utils

BENCH_PATH = "/workspace/frappe-bench"
SITE = "development.localhost"


# --------------------------------------------------------------------------- db


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """Point the peewee cache at a throwaway SQLite file for the test's lifetime."""
    orig_path = db_utils.DB_PATH
    dbfile = tmp_path / "cache.db"
    monkeypatch.setattr(db_utils, "DB_PATH", dbfile)
    if not db_utils.db.is_closed():
        db_utils.db.close()
    db_utils.db.init(str(dbfile))
    db_utils.initialize_database()
    yield
    if not db_utils.db.is_closed():
        db_utils.db.close()
    db_utils.db.init(str(orig_path))


class _NullSpinner:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def update(self, *a, **k):
        pass


# ---------------------------------------------------------------- fake container


class RecordingContainer:
    """A frappe container stand-in that records every ``exec_run`` and answers the
    filesystem probes the site/currentsite helpers issue.

    ``sites`` maps a real site name -> its ``bench list-apps`` lines. Stray, non-site
    entries in ``sites/`` (a file like ``currentsite.txt``) are listed by ``ls`` but
    classified NOTASITE by the probe. ``currentsite`` is the currentsite.txt content.
    """

    def __init__(self, sites=None, stray=None, currentsite=None, migrate_exit=0):
        self.sites = dict(sites or {})
        self.stray = list(stray or [])
        self.currentsite = currentsite
        self.migrate_exit = migrate_exit
        self.labels = {"com.docker.compose.service": "frappe"}
        self.status = "running"
        self.exec_calls: list[dict] = []

    def exec_run(self, cmd, workdir=None, environment=None):
        self.exec_calls.append({"cmd": cmd, "workdir": workdir, "environment": environment})
        cmd_str = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)

        # ls the sites dir: real sites + stray non-site entries.
        if cmd_str == f"ls -1 {BENCH_PATH}/sites":
            listing = list(self.sites.keys()) + self.stray
            return (0, "\n".join(listing).encode())
        # Fail-safe site classification probe: the entry dir is the positional arg
        # (the last shlex token), never interpolated into the script.
        if (
            cmd_str.startswith("sh -c '")
            and "echo SITE" in cmd_str
            and "site_config.json" in cmd_str
        ):
            entry = shlex.split(cmd_str)[-1].rsplit("/", 1)[-1]
            return (0, b"SITE\n") if entry in self.sites else (0, b"NOTASITE\n")
        # currentsite.txt read.
        if cmd_str == f"cat {BENCH_PATH}/sites/currentsite.txt":
            if self.currentsite is None:
                return (1, b"")
            return (0, self.currentsite.encode())
        # bench migrate.
        if "migrate" in cmd_str:
            return (self.migrate_exit, b"migrate output")
        return (0, b"")

    def put_archive(self, path, data):
        # The receive flow streams each downloaded backup file into the container.
        return True

    def restore_calls(self):
        return [
            c
            for c in self.exec_calls
            if "restore"
            in (" ".join(c["cmd"]) if isinstance(c["cmd"], (list, tuple)) else str(c["cmd"]))
            and "--force"
            in (" ".join(c["cmd"]) if isinstance(c["cmd"], (list, tuple)) else str(c["cmd"]))
        ]


# =========================================================== #1 receive full path


class TestReceiveUsesFullDbPath:
    """#1: receive-mode must pass the FULL container path, not the bare filename,
    and mirror the normal path's arg order."""

    def _run(self, monkeypatch, db_filename, files=None, private=None):
        import subprocess
        from pathlib import Path

        container = RecordingContainer(sites={SITE: ["frappe 15.0.0 version-15"]})

        def fake_sendme_run(cmd, cwd=None, capture_output=True, text=True):
            for name in [db_filename] + (files or []) + (private or []):
                Path(cwd).joinpath(name).write_text("DATA")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_sendme_run)
        monkeypatch.setattr(restore_mod, "ensure_containers_running", lambda *a, **k: True)
        monkeypatch.setattr(restore_mod, "get_project_containers", lambda name: [container])
        monkeypatch.setattr(restore_mod, "get_sendme_command", lambda: "sendme")
        monkeypatch.setattr(restore_mod, "check_missing_apps", lambda *a, **k: [])
        monkeypatch.setattr(restore_mod, "TipSpinner", _NullSpinner)
        monkeypatch.setattr(restore_mod.config_utils, "get_show_tips", lambda: False)
        monkeypatch.setattr(
            restore_mod.questionary,
            "text",
            lambda *a, **k: SimpleNamespace(ask=lambda: "ticket-abc"),
        )
        monkeypatch.setattr(restore_mod.console, "print", lambda *a, **k: None)
        monkeypatch.setattr(restore_mod.stderr_console, "print", lambda *a, **k: None)

        restore_mod.restore_receive_mode(
            project_name="proj",
            site=SITE,
            bench_path=BENCH_PATH,
            mariadb_root_username="root",
            mariadb_root_password="pw",
            admin_password=None,
            no_recache=True,
            verbose=False,
            yes=True,
            no_migrate=True,
        )
        return container

    def test_db_arg_is_full_container_path_not_bare_filename(self, monkeypatch):
        db = "20260704_173426-development_localhost-database.sql.gz"
        container = self._run(monkeypatch, db)
        calls = container.restore_calls()
        assert len(calls) == 1
        cmd = (
            " ".join(calls[0]["cmd"])
            if isinstance(calls[0]["cmd"], (list, tuple))
            else calls[0]["cmd"]
        )
        # The bug: `restore <bare-filename>`. The fix: full path under backups dir.
        full = f"{BENCH_PATH}/sites/{SITE}/private/backups/{db}"
        assert f"restore {full}" in cmd
        assert f"restore {db} " not in cmd  # never the bare filename

    def test_arg_order_matches_normal_path(self, monkeypatch):
        db = "20260704_173426-development_localhost-database.sql.gz"
        files = "20260704_173426-development_localhost-files.tar"
        private = "20260704_173426-development_localhost-private-files.tar"
        container = self._run(monkeypatch, db, files=[files], private=[private])
        cmd = " ".join(container.restore_calls()[0]["cmd"])
        # Order: db path, then credentials + --force, THEN the file archives (the
        # normal path's shape) - not --with-public-files immediately after the db.
        i_db = cmd.index("restore ")
        i_user = cmd.index("--mariadb-root-username")
        i_force = cmd.index("--force")
        i_pub = cmd.index("--with-public-files")
        i_priv = cmd.index("--with-private-files")
        assert i_db < i_user < i_force < i_pub < i_priv


# ================================================= #2 credential prompting modes


class TestMariadbCredentialModes:
    """#2: username+password prompting works interactively AND non-interactively."""

    def _tty(self, monkeypatch, is_tty):
        monkeypatch.setattr(restore_mod.sys.stdin, "isatty", lambda: is_tty)
        monkeypatch.setattr(restore_mod.console, "print", lambda *a, **k: None)
        monkeypatch.setattr(restore_mod.stderr_console, "print", lambda *a, **k: None)

    def test_interactive_prompts_collect_username_and_password(self, monkeypatch):
        self._tty(monkeypatch, True)
        asked = []

        def fake_text(msg, default=None):
            asked.append(("text", msg))
            return SimpleNamespace(ask=lambda: "root")

        def fake_password(msg):
            asked.append(("password", msg))
            return SimpleNamespace(ask=lambda: "s3cret")

        monkeypatch.setattr(restore_mod.questionary, "text", fake_text)
        monkeypatch.setattr(restore_mod.questionary, "password", fake_password)

        user, pw = restore_mod._prompt_mariadb_credentials(None, None)
        assert (user, pw) == ("root", "s3cret")
        # BOTH the username AND the password prompt actually fired (the bug skipped
        # the username prompt and returned an empty password).
        assert [k for k, _ in asked] == ["text", "password"]

    def test_interactive_blank_username_defaults_to_root(self, monkeypatch):
        self._tty(monkeypatch, True)
        monkeypatch.setattr(
            restore_mod.questionary, "text", lambda *a, **k: SimpleNamespace(ask=lambda: "")
        )
        monkeypatch.setattr(
            restore_mod.questionary, "password", lambda *a, **k: SimpleNamespace(ask=lambda: "pw")
        )
        user, pw = restore_mod._prompt_mariadb_credentials(None, None)
        assert user == "root" and pw == "pw"

    def test_interactive_empty_password_is_error(self, monkeypatch):
        self._tty(monkeypatch, True)
        monkeypatch.setattr(
            restore_mod.questionary, "text", lambda *a, **k: SimpleNamespace(ask=lambda: "root")
        )
        monkeypatch.setattr(
            restore_mod.questionary, "password", lambda *a, **k: SimpleNamespace(ask=lambda: "")
        )
        with pytest.raises(typer.Exit) as e:
            restore_mod._prompt_mariadb_credentials(None, None)
        assert e.value.exit_code != 0

    def test_non_tty_without_password_refuses(self, monkeypatch):
        self._tty(monkeypatch, False)
        # A prompt must NEVER be reached under a non-TTY.
        monkeypatch.setattr(
            restore_mod.questionary,
            "password",
            lambda *a, **k: pytest.fail("must not prompt under a non-TTY"),
        )
        with pytest.raises(typer.Exit) as e:
            restore_mod._prompt_mariadb_credentials(None, None)
        assert e.value.exit_code != 0

    def test_non_tty_with_flags_uses_them_without_prompting(self, monkeypatch):
        self._tty(monkeypatch, False)
        monkeypatch.setattr(
            restore_mod.questionary,
            "password",
            lambda *a, **k: pytest.fail("must not prompt when flags are given"),
        )
        user, pw = restore_mod._prompt_mariadb_credentials("admin", "flagpw")
        assert (user, pw) == ("admin", "flagpw")

    def test_non_tty_password_flag_defaults_username_to_root(self, monkeypatch):
        self._tty(monkeypatch, False)
        user, pw = restore_mod._prompt_mariadb_credentials(None, "flagpw")
        assert user == "root" and pw == "flagpw"

    def test_password_prompt_collects_input_with_username_flag(self, monkeypatch):
        # Root-cause guard for the reintroduced-bug scenario: with
        # --mariadb-root-username supplied by flag the username prompt is skipped,
        # yet the password prompt must still fire and collect the real password.
        # (The stray-Enter-from-the-confirm problem is fixed at its root by
        # auto_enter=False on the restore confirms - see the confirm guard below -
        # so no stdin-flush helper is involved here.)
        self._tty(monkeypatch, True)
        events = []

        def fake_password(msg):
            events.append("password")
            return SimpleNamespace(ask=lambda: "s3cret")

        monkeypatch.setattr(restore_mod.questionary, "password", fake_password)
        user, pw = restore_mod._prompt_mariadb_credentials("root", None)
        assert (user, pw) == ("root", "s3cret")
        assert events == ["password"]

    def test_restore_confirms_disable_auto_enter(self):
        # Root fix: every questionary.confirm in the restore flow must pass
        # auto_enter=False so it consumes its own trailing Enter and cannot leave a
        # stray keystroke for the following password prompt to swallow as empty.
        import inspect

        src = inspect.getsource(restore_mod)
        assert src.count("questionary.confirm(") == 4
        assert src.count("auto_enter=False") == 4


# ===================================================== #3 site detection (inspect)


class TestSiteDetectionExcludesStrayFiles:
    """#3: currentsite.txt (and other stray entries) are never sites."""

    def test_currentsite_txt_is_not_a_site(self):
        c = RecordingContainer(
            sites={SITE: ["frappe"]},
            stray=["apps.txt", "apps.json", "assets", "common_site_config.json", "currentsite.txt"],
        )
        assert bench_sites.list_sites(c, BENCH_PATH) == [SITE]

    def test_ls_failure_returns_none(self):
        class Boom:
            def exec_run(self, cmd, workdir=None, environment=None):
                return (1, b"")

        assert bench_sites.list_sites(Boom(), BENCH_PATH) is None

    def test_ambiguous_entry_is_failsafe_included(self):
        class Ambiguous:
            def exec_run(self, cmd, workdir=None, environment=None):
                s = " ".join(cmd) if isinstance(cmd, (list, tuple)) else cmd
                if s == f"ls -1 {BENCH_PATH}/sites":
                    return (0, b"weird\n")
                if "echo SITE" in s:
                    return (0, b"AMBIGUOUS\n")
                return (0, b"")

        # AMBIGUOUS (unreadable/erroring) fails closed -> treated as a real site.
        assert bench_sites.list_sites(Ambiguous(), BENCH_PATH) == ["weird"]


# =============================================== #4 default site from currentsite


class TestDefaultSiteFromCurrentSite:
    """#4: default resolves from currentsite.txt when common config lacks it."""

    def test_read_current_site(self):
        """`read_current_site` reads and strips the bench's current site."""
        c = RecordingContainer(currentsite="development.localhost\n")
        assert bench_sites.read_current_site(c, BENCH_PATH) == "development.localhost"

    def test_read_current_site_missing_is_none(self):
        c = RecordingContainer(currentsite=None)
        assert bench_sites.read_current_site(c, BENCH_PATH) is None

    def test_cache_roundtrips_current_site(self, temp_db):
        db_utils.cache_project_data(
            "proj",
            [{"path": BENCH_PATH, "sites": [], "available_apps": [], "current_site": SITE}],
        )
        data = db_utils.get_cached_project_data("proj")
        assert data["bench_instances"][0]["current_site"] == SITE

    def test_get_default_site_falls_back_to_current_site(self, temp_db):
        # common_site_config has NO default_site, but currentsite.txt named the site.
        db_utils.cache_project_data(
            "proj",
            [
                {
                    "path": BENCH_PATH,
                    "sites": [],
                    "available_apps": [],
                    "current_site": SITE,
                    "common_site_config": {"db_host": "mariadb"},  # no default_site
                }
            ],
        )
        assert db_utils.get_default_site("proj", BENCH_PATH) == SITE

    def test_get_default_site_prefers_explicit_default_site(self, temp_db):
        db_utils.cache_project_data(
            "proj",
            [
                {
                    "path": BENCH_PATH,
                    "sites": [],
                    "available_apps": [],
                    "current_site": "pointer.localhost",
                    "common_site_config": {"default_site": "explicit.localhost"},
                }
            ],
        )
        assert db_utils.get_default_site("proj", BENCH_PATH) == "explicit.localhost"

    def test_migration_adds_current_site_column(self, temp_db):
        db_utils.db.execute_sql("DROP TABLE bench")
        db_utils.db.execute_sql(
            "CREATE TABLE bench (id INTEGER PRIMARY KEY, project_id INTEGER, path VARCHAR)"
        )
        cols = {r[1] for r in db_utils.db.execute_sql("PRAGMA table_info(bench)").fetchall()}
        assert "current_site" not in cols
        db_utils._migrate_bench_current_site_column()
        cols = {r[1] for r in db_utils.db.execute_sql("PRAGMA table_info(bench)").fetchall()}
        assert "current_site" in cols
        db_utils._migrate_bench_current_site_column()  # idempotent

    def test_resolve_default_site_live_fallback(self, temp_db, monkeypatch):
        # No cache entry -> get_default_site returns None -> live currentsite read.
        container = RecordingContainer(currentsite="live.localhost")
        site = restore_mod._resolve_default_site("proj", BENCH_PATH, container)
        assert site == "live.localhost"


# ===================================================== #5 missing-apps warning


DB_PATH = f"{BENCH_PATH}/sites/{SITE}/private/backups/20260705_000000-development_localhost-database.sql.gz"


class _AppsContainer:
    """Serves `ls apps` and the dump's installed_apps grep for check_missing_apps.

    ``available`` are the apps physically in apps/. ``dump_apps`` is what the backup
    dump records under the installed_apps global; None means the grep finds nothing
    (unreadable/absent marker) -> the check must fail safe.
    """

    def __init__(self, available, dump_apps):
        self.available = list(available)
        self.dump_apps = dump_apps

    def exec_run(self, cmd, workdir=None, environment=None):
        s = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
        if f"ls -1 {BENCH_PATH}/apps" in s:
            return (0, "\n".join(self.available).encode())
        if "installed_apps" in s and "zcat" in s:
            if self.dump_apps is None:
                return (0, b"")  # marker not found -> empty output
            # Mimic mysqldump's escaped-quote array before ,'installed_apps'.
            arr = ", ".join(f'\\"{a}\\"' for a in self.dump_apps)
            return (0, f"[{arr}]','installed_apps'".encode())
        return (0, b"")


class TestMissingAppsCheck:
    """#5: compare the apps the BACKUP needs (from its dump) against the bench's
    available apps - the captain's "the backup uses apps the bench does NOT have"."""

    def test_warns_when_backup_app_absent_from_bench(self):
        # Backup was taken on a site with [frappe, widgets]; bench only has frappe.
        c = _AppsContainer(available=["frappe"], dump_apps=["frappe", "widgets"])
        missing = restore_mod.check_missing_apps(c, "proj", BENCH_PATH, DB_PATH, no_recache=True)
        assert missing == ["widgets"]

    def test_no_warning_when_all_present(self):
        c = _AppsContainer(available=["frappe", "widgets"], dump_apps=["frappe", "widgets"])
        assert restore_mod.check_missing_apps(c, "proj", BENCH_PATH, DB_PATH, no_recache=True) == []

    def test_fail_safe_when_backup_apps_unreadable(self):
        # Dump marker not found -> cannot tell -> no false warning.
        c = _AppsContainer(available=["frappe"], dump_apps=None)
        assert restore_mod.check_missing_apps(c, "proj", BENCH_PATH, DB_PATH, no_recache=True) == []

    def test_reads_backup_apps_from_dump_helper(self):
        c = _AppsContainer(available=[], dump_apps=["frappe", "widgets"])
        assert restore_mod._read_backup_installed_apps(c, DB_PATH) == {"frappe", "widgets"}


# ==================================================== #6 post-restore migrate/restart


class TestPostRestoreMigrateAndRestart:
    """#6: a successful restore is followed by bench migrate then an instance restart."""

    def _patch(self, monkeypatch):
        calls = {"start": [], "start_kwargs": []}
        monkeypatch.setattr(restore_mod, "TipSpinner", _NullSpinner)
        monkeypatch.setattr(restore_mod.config_utils, "get_show_tips", lambda: False)
        monkeypatch.setattr(restore_mod.console, "print", lambda *a, **k: None)
        monkeypatch.setattr(restore_mod.stderr_console, "print", lambda *a, **k: None)
        import caffeinated_whale_cli.commands.start as start_mod

        def fake_start(project_name, verbose=False, **k):
            calls["start"].append(project_name)
            calls["start_kwargs"].append(k)
            return "/tmp/bench-proj.log"

        monkeypatch.setattr(start_mod, "_start_project", fake_start)
        return calls

    def test_runs_migrate_then_restart_on_success(self, monkeypatch):
        calls = self._patch(monkeypatch)
        container = RecordingContainer(sites={SITE: ["frappe"]}, migrate_exit=0)
        ok = restore_mod._post_restore_migrate_and_restart(
            container, "proj", BENCH_PATH, SITE, verbose=False
        )
        assert ok is True
        # bench migrate ran at the bench path...
        migrates = [
            c
            for c in container.exec_calls
            if "migrate"
            in (" ".join(c["cmd"]) if isinstance(c["cmd"], (list, tuple)) else c["cmd"])
        ]
        assert len(migrates) == 1
        assert migrates[0]["workdir"] == BENCH_PATH
        # ...then the instance was restarted.
        assert calls["start"] == ["proj"]

    def test_restart_targets_the_restored_bench_not_the_first(self, monkeypatch):
        # A multi-bench restore into a NON-first bench must restart THAT bench: the
        # restored bench_path is threaded through as an explicit override so
        # _start_project never falls back to resolve_bench_path's first-bench guess.
        calls = self._patch(monkeypatch)
        other_bench = "/workspace/second-bench"
        container = RecordingContainer(sites={SITE: ["frappe"]}, migrate_exit=0)
        restore_mod._post_restore_migrate_and_restart(
            container, "proj", other_bench, SITE, verbose=False
        )
        assert calls["start"] == ["proj"]
        assert calls["start_kwargs"][0].get("bench_path_override") == other_bench

    def test_migrate_failure_still_restarts_and_returns_false(self, monkeypatch):
        calls = self._patch(monkeypatch)
        container = RecordingContainer(sites={SITE: ["frappe"]}, migrate_exit=1)
        ok = restore_mod._post_restore_migrate_and_restart(
            container, "proj", BENCH_PATH, SITE, verbose=False
        )
        # A failed migrate does not abort the restart, but is reported as False so
        # the caller can exit non-zero.
        assert ok is False
        assert calls["start"] == ["proj"]

    def test_migrate_exec_exception_still_restarts_and_returns_false(self, monkeypatch):
        # A Docker/API EXCEPTION from exec_run (not just a non-zero exit) must be
        # treated as a failed-but-reported migrate and MUST still restart.
        calls = self._patch(monkeypatch)

        class RaisingMigrate(RecordingContainer):
            def exec_run(self, cmd, workdir=None, environment=None):
                s = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
                if "migrate" in s:
                    raise RuntimeError("docker exec boom")
                return super().exec_run(cmd, workdir=workdir, environment=environment)

        container = RaisingMigrate(sites={SITE: ["frappe"]})
        ok = restore_mod._post_restore_migrate_and_restart(
            container, "proj", BENCH_PATH, SITE, verbose=False
        )
        assert ok is False
        assert calls["start"] == ["proj"]
