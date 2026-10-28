"""Steering 041: site database users host-scoped to a container IP that changed. The
check reads mysql.user and how MariaDB sees the container; the repair renames to '%';
a new site is widened right after creation. The SQL client is faked."""

from __future__ import annotations

import json

import pytest

from caffeinated_whale_cli.core import db_access
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind


class FakeDb:
    def __init__(self, users, client="172.21.0.5"):
        self.users = users  # user -> hosts
        self.client = client
        self.sql = []
        self.site_users = {"shop.localhost": "_a1", "crm.localhost": "_b2"}

    def exec_run(self, cmd, workdir=None, environment=None, **_kw):
        script = cmd[2]
        if "glob.glob" in script:
            return 0, json.dumps(self.site_users).encode()
        assert environment and environment.get(
            "MYSQL_PWD"
        ), "root password must ride the environment"
        import shlex

        sql = shlex.split(script.split("; ", 1)[1])[-1]
        self.sql.append(sql)
        if "USER()" in sql:
            return 0, (self.client + "\n").encode()
        if "FROM mysql.user" in sql:
            rows = [f"{u}\t{h}" for u, hs in self.users.items() for h in hs]
            return 0, ("\n".join(rows) + "\n").encode()
        for stmt in sql.split("; "):
            parts = stmt.split("'")
            if stmt.startswith("RENAME USER"):
                u, src, dst = parts[1], parts[3], parts[7]
                if dst in self.users[u]:  # MariaDB refuses an existing account as the target
                    return 1, b"ERROR 1396 (HY000): Operation RENAME USER failed"
                self.users[u] = [dst if h == src else h for h in self.users[u]]
            elif stmt.startswith("DROP USER"):
                self.users[parts[1]].remove(parts[3])
        return 0, b""


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    monkeypatch.setattr(
        db_access.supervision, "common_site_config", lambda c, b: {"db_host": "mariadb"}
    )
    monkeypatch.setattr(db_access.db_password, "resolve", lambda project, given: given or "root")


def test_check_flags_a_user_scoped_to_an_address_the_container_no_longer_has():
    db = FakeDb({"_a1": ["172.25.0.5"], "_b2": ["%"]})
    found = {a.site: a for a in db_access.check(db, "/b", project="p")}
    assert found["shop.localhost"].ok is False and found["shop.localhost"].hosts == ["172.25.0.5"]
    assert found["shop.localhost"].client_host == "172.21.0.5"
    assert found["crm.localhost"].ok is True
    warning = db_access.postflight(db, "/b", project="p")
    assert warning.code == "db.login_refused"
    assert "database login refused for site shop.localhost" in warning.text
    assert "cwcli db-access repair p --yes" in warning.text


def test_a_user_scoped_to_the_current_address_is_fine():
    db = FakeDb({"_a1": ["172.21.0.5"], "_b2": ["%"]})
    assert all(a.ok for a in db_access.check(db, "/b", project="p"))
    assert db_access.postflight(db, "/b", project="p") is None


def test_repair_renames_the_first_host_to_wildcard_drops_the_rest_and_flushes():
    db = FakeDb({"_a1": ["172.25.0.5", "172.25.0.9"], "_b2": ["%"]})
    events = []
    result = db_access.repair(db, "/b", project="p", on_event=events.append)
    assert (
        "RENAME USER '_a1'@'172.25.0.5' TO '_a1'@'%'; DROP USER '_a1'@'172.25.0.9'; FLUSH PRIVILEGES"
        in db.sql
    )
    assert not any("_b2" in s and "RENAME" in s for s in db.sql)  # the ok site is untouched
    repaired = {a.site: a for a in result.data}
    assert repaired["shop.localhost"].repaired and repaired["shop.localhost"].hosts == ["%"]
    assert any(e.phase == "db_access" and e.status == "ok" for e in events)


def test_ensure_wildcard_after_creation_and_an_unreachable_root_is_a_warning_not_a_crash():
    db = FakeDb({"_a1": ["172.21.0.5"]})
    got = db_access.ensure_wildcard(db, "/b", project="p", site="shop.localhost")
    assert got.ok and got.hosts == ["%"] and got.repaired

    class Down(FakeDb):
        def exec_run(self, cmd, workdir=None, environment=None, **_kw):
            if "glob.glob" in cmd[2]:
                return super().exec_run(cmd, workdir, environment)
            return 1, b"ERROR 2002 (HY000): Can't connect"

    warning = db_access.postflight(Down({"_a1": ["%"]}), "/b", project="p")
    assert warning.code == "db.access_unverified"
    with pytest.raises(CwcliError):
        db_access.root_sql(Down({}), "/b", "SELECT 1", "root")


class TestWidenAfterCreate:
    """The one post-creation hook init, the UI new-site and restore all call."""

    def test_a_restore_that_adds_a_scoped_row_beside_wildcard_drops_it(self):
        # ``bench restore --force`` drops and re-creates the user only on the container's
        # address, so the '%' row cwcli made at creation survives beside the new one.
        db = FakeDb({"_a1": ["%", "172.21.0.5"]})
        widened, warning = db_access.widen_after_create(
            db, "/b", project="p", site="shop.localhost", root_password="x"
        )
        assert warning is None and widened is True
        assert db.users["_a1"] == ["%"]

    def test_a_freshly_scoped_user_is_widened_and_reported(self):
        db = FakeDb({"_a1": ["172.21.0.5"], "_b2": ["%"]})
        widened, warning = db_access.widen_after_create(
            db, "/b", project="p", site="shop.localhost", root_password="x"
        )
        assert widened is True and warning is None
        assert db.users["_a1"] == ["%"]

    def test_a_failed_widen_is_a_warning_not_a_failed_creation(self, monkeypatch):
        def boom(*a, **k):
            raise CwcliError(ErrorKind.DOCKER, "db.root_sql_failed", "mariadb down")

        monkeypatch.setattr(db_access, "ensure_wildcard", boom)
        widened, warning = db_access.widen_after_create(
            object(), "/b", project="p", site="shop.localhost"
        )
        assert widened is False
        assert warning is not None and warning.code == "db.widen_failed"
        assert "shop.localhost" in warning.text and "mariadb down" in warning.text


def test_a_rename_that_fails_after_creation_is_reported_not_silent():
    class RenameFails(FakeDb):
        def exec_run(self, cmd, workdir=None, environment=None, **_kw):
            if "RENAME USER" in cmd[2]:
                return 1, b"ERROR 1396 (HY000): Operation RENAME USER failed"
            return super().exec_run(cmd, workdir, environment)

    widened, warning = db_access.widen_after_create(
        RenameFails({"_a1": ["172.21.0.5"]}), "/b", project="p", site="shop.localhost"
    )
    assert widened is False
    assert warning is not None and warning.code == "db.widen_failed"
    assert "RENAME USER failed" in warning.text


def test_a_named_site_with_no_database_user_is_not_found_rather_than_ok():
    db = FakeDb({"_a1": ["172.25.0.5"]})
    with pytest.raises(CwcliError) as exc:
        db_access.repair(db, "/b", project="p", sites=["shop.localhst"])
    assert exc.value.kind is ErrorKind.NOT_FOUND and "shop.localhst" in exc.value.message
    assert not any("RENAME" in s for s in db.sql)
