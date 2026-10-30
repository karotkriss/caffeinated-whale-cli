"""A wrong MariaDB root password is refused in seconds, before any build step, and a
new bench that fails part-way is removed so a retry with the same name works.

A new bench shares the instance's existing MariaDB, so a password typed into the
New bench dialog used to surface only at ``bench new-site``, minutes into the build
(error 1045), leaving the half-built bench behind to block the retry.
"""

from __future__ import annotations

import pytest

from caffeinated_whale_cli.core import db_access, new_bench, new_site, rm_site
from caffeinated_whale_cli.core.init import InitStepStart
from caffeinated_whale_cli.core.envelope import Result, Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind

DENIED = (1, (None, b"ERROR 1045 (28000): Access denied for user 'root'@'172.21.0.5'"))
REFUSED = (1, (None, b"ERROR 2003 (HY000): Can't connect to MySQL server on 'mariadb'"))
SECRET = "typed-wrong-pw"


class Db:
    """A frappe container whose MariaDB client answers from a queue of replies."""

    def __init__(self, *replies, dirs=()):
        self.replies = list(replies)
        self.calls: list[tuple] = []
        self.dirs = set(dirs)

    def exec_run(self, cmd, workdir=None, environment=None, demux=False, **_kw):
        self.calls.append((cmd, environment))
        script = cmd[2] if len(cmd) == 3 and cmd[:2] in (["sh", "-c"], ["bash", "-lc"]) else ""
        if "SELECT 1" in script:
            return self.replies.pop(0) if self.replies else (0, (b"1\n", None))
        if script.startswith("test -d"):
            return (0 if script.split()[-1].strip("'") in self.dirs else 1), b""
        if cmd[:2] == ["rm", "-rf"]:
            self.dirs.discard(cmd[-1])
        return 0, b""


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    monkeypatch.setattr(db_access.time, "sleep", lambda _s: None)
    monkeypatch.setattr(db_access.db_password, "remembered", lambda project: None)


class TestVerifyRootPassword:
    def test_a_wrong_password_is_refused_without_ever_showing_it(self):
        db = Db(DENIED)
        with pytest.raises(CwcliError) as exc:
            db_access.verify_root_password(db, "acme", SECRET)
        assert exc.value.kind is ErrorKind.PRECONDITION
        assert exc.value.code == "db.root_password_wrong"
        assert "does not set a new one" in exc.value.message
        assert SECRET not in f"{exc.value.message} {exc.value.hint}"
        cmd, env = db.calls[0]
        assert env == {"MYSQL_PWD": SECRET}  # the environment, never the argv
        assert SECRET not in " ".join(cmd)

    def test_the_hint_points_at_the_saved_password_when_cwcli_has_one(self, monkeypatch):
        monkeypatch.setattr(db_access.db_password, "remembered", lambda project: "saved")
        with pytest.raises(CwcliError) as exc:
            db_access.verify_root_password(Db(DENIED), "acme", SECRET)
        assert "Leave the password out" in (exc.value.hint or "")

    def test_a_rejected_saved_password_is_called_out_of_date(self, monkeypatch):
        monkeypatch.setattr(db_access.db_password, "remembered", lambda project: "saved")
        with pytest.raises(CwcliError) as exc:
            db_access.verify_root_password(Db(DENIED), "acme", None)
        hint = exc.value.hint or ""
        assert "out of date" in hint and "--db-root-password" in hint
        assert "Leave the password out" not in hint

    def test_a_right_password_passes_with_one_connection(self):
        db = Db()
        assert db_access.verify_root_password(db, "acme", "right") is None
        assert len(db.calls) == 1

    def test_a_mariadb_still_starting_is_waited_for(self):
        db = Db(REFUSED, REFUSED, (0, (b"1\n", None)))
        assert db_access.verify_root_password(db, "acme", "right") is None
        assert len(db.calls) == 3

    def test_an_unreachable_mariadb_is_a_warning_not_a_refusal(self):
        found = db_access.verify_root_password(Db(*[REFUSED] * 5), "acme", "x", wait_s=0)
        assert found is not None and found.code == "db.root_password_unverified"


def _new_bench(monkeypatch, db, init_bench):
    monkeypatch.setattr(new_bench.core_docker, "get_frappe_container", lambda p: db)
    monkeypatch.setattr(
        new_bench.resolvers, "resolve_container_state", lambda *a, **k: Result(status=Status.OK)
    )
    monkeypatch.setattr(new_bench.core_init, "init_bench", init_bench)
    removed_paths: list[str] = []
    monkeypatch.setattr(new_bench.config_utils, "remove_custom_path", removed_paths.append)
    return lambda: new_bench.new_bench(
        "acme", bench_name="b2", site_name="b2.localhost", admin_password="admin"
    )


class TestNewBenchCleanup:
    def test_a_failure_after_the_build_started_removes_the_half_built_bench(self, monkeypatch):
        db = Db()

        def init_bench(project, **kw):
            kw["on_event"](InitStepStart(phase="bench_init", item="b2"))
            db.dirs |= {"/workspace/b2", "/workspace/b2/sites/b2.localhost"}
            raise CwcliError(ErrorKind.DOCKER, "init.exec_failed", "bench new-site failed.")

        run = _new_bench(monkeypatch, db, init_bench)
        with pytest.raises(CwcliError) as exc:
            run()
        assert "/workspace/b2" not in db.dirs
        assert "removed the half-built bench /workspace/b2" in exc.value.message
        assert "retry with the name 'b2' works" in exc.value.message
        drop = [c for c, _env in db.calls if "drop-site" in " ".join(c)]
        assert drop, "the half-made site's database is dropped first"

    def test_an_existing_bench_is_never_removed(self, monkeypatch):
        db = Db(dirs={"/workspace/b2"})

        def init_bench(project, **kw):
            raise CwcliError(ErrorKind.CONFLICT, "bench.exists", "Bench exists.")

        with pytest.raises(CwcliError) as exc:
            _new_bench(monkeypatch, db, init_bench)()
        assert exc.value.code == "bench.exists"
        assert "/workspace/b2" in db.dirs
        assert not [c for c, _env in db.calls if c[:2] == ["rm", "-rf"]]

    def test_a_bench_another_run_created_meanwhile_is_never_removed(self, monkeypatch):
        db = Db()

        def init_bench(project, **kw):
            db.dirs.add("/workspace/b2")  # a concurrent run's bench, not this call's
            raise CwcliError(ErrorKind.CONFLICT, "bench.exists", "Bench exists.")

        with pytest.raises(CwcliError) as exc:
            _new_bench(monkeypatch, db, init_bench)()
        assert exc.value.message == "Bench exists."
        assert "/workspace/b2" in db.dirs
        assert not [c for c, _env in db.calls if c[:2] == ["rm", "-rf"]]

    def test_a_refused_password_built_nothing_and_says_nothing_was_removed(self, monkeypatch):
        def init_bench(project, **kw):
            raise CwcliError(ErrorKind.PRECONDITION, "db.root_password_wrong", "Wrong.")

        with pytest.raises(CwcliError) as exc:
            _new_bench(monkeypatch, Db(), init_bench)()
        assert exc.value.message == "Wrong."

    def test_a_cleanup_that_fails_never_hides_the_build_failure(self, monkeypatch):
        db = Db()

        def init_bench(project, **kw):
            kw["on_event"](InitStepStart(phase="bench_init", item="b2"))
            db.dirs.add("/workspace/b2")
            raise CwcliError(ErrorKind.DOCKER, "init.exec_failed", "bench init failed.")

        def broken(*a, **k):
            raise ConnectionError("Docker went away")

        run = _new_bench(monkeypatch, db, init_bench)
        monkeypatch.setattr(new_bench, "_remove_half_built", broken)
        with pytest.raises(CwcliError) as exc:
            run()
        assert exc.value.message == "bench init failed."


class TestOtherFlowsCheckFirst:
    def test_new_site_refuses_before_bench_new_site(self, monkeypatch):
        db = Db(DENIED, dirs={"/workspace/frappe-bench"})
        monkeypatch.setattr(new_site.core_docker, "get_frappe_container", lambda p: db)
        monkeypatch.setattr(
            new_site.resolvers, "resolve_container_state", lambda *a, **k: Result(status=Status.OK)
        )
        monkeypatch.setattr(
            new_site.resolvers,
            "resolve_bench",
            lambda *a: Result(status=Status.OK, data="/workspace/frappe-bench"),
        )
        monkeypatch.setattr(new_site.resolvers, "require_bench_dir", lambda *a: None)
        monkeypatch.setattr(db_access.supervision, "common_site_config", lambda c, b: {})
        with pytest.raises(CwcliError) as exc:
            new_site.new_site("acme", site="shop.localhost", admin_password="a")
        assert exc.value.code == "db.root_password_wrong"
        assert not [c for c, _env in db.calls if "new-site" in " ".join(c)]

    def test_drop_site_refuses_before_the_backup(self, monkeypatch):
        db = Db(DENIED)
        monkeypatch.setattr(
            rm_site.resolvers,
            "resolve_container_and_bench",
            lambda *a, **k: (db, "/workspace/frappe-bench", []),
        )
        for name in ("require_bench_dir", "require_site_dir"):
            monkeypatch.setattr(rm_site.resolvers, name, lambda *a: None)
        monkeypatch.setattr(db_access.supervision, "common_site_config", lambda c, b: {})
        backups: list[str] = []
        monkeypatch.setattr(rm_site, "backup_before_drop", lambda *a, **k: backups.append(1))
        with pytest.raises(CwcliError) as exc:
            rm_site.drop_site("acme", "shop.localhost", consent=True)
        assert exc.value.code == "db.root_password_wrong"
        assert backups == []
