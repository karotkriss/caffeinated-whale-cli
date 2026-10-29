"""Roles and directory sign-in through the REAL bound daemon: the local admin is the
break-glass way in, a directory user gets the role their groups map to, and every
request is held to the least role it needs. The directory itself is stubbed here
(``core.directory`` has its own tests; the real one is the E2E)."""

from __future__ import annotations

import json
import secrets
import threading
import urllib.error
import urllib.request

import pytest

from caffeinated_whale_cli import jobs as core_jobs
from caffeinated_whale_cli.commands import serve as serve_cmd
from caffeinated_whale_cli.core import accounts
from caffeinated_whale_cli.core import directory as core_directory
from caffeinated_whale_cli.core import fleet as core_fleet
from caffeinated_whale_cli.core.envelope import Result, Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind

ADMIN_PW = "break glass pw"
TOKEN = "roles-test-token"
DIRECTORY = {
    "ana": ("ana-pw", "admin"),
    "otto": ("otto-pw", "operator"),
    "vic": ("vic-pw", "viewer"),
}


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("CWCLI_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(core_fleet, "list_instances", lambda: Result(status=Status.OK, data=[]))
    accounts.ensure_default()
    accounts.change_password("admin", ADMIN_PW)

    def _authenticate(username, password):
        pw, role = DIRECTORY.get(username, (None, None))
        if username == "nogroup" and password == "x":
            raise CwcliError(ErrorKind.PRECONDITION, "auth.directory_no_group", "no group")
        if pw is None or pw != password:
            raise CwcliError(ErrorKind.PRECONDITION, "auth.invalid", "no match")
        return core_directory.DirectoryUser(username, f"uid={username}", username.title(), "", role)

    monkeypatch.setattr(serve_cmd.core_directory, "authenticate", _authenticate)
    monkeypatch.setattr(serve_cmd, "_directory_on", lambda: True)
    created: list[str] = []
    monkeypatch.setattr(
        serve_cmd.core_jobs,
        "create_job",
        lambda verb, target, args, **kw: created.append((verb, kw["origin"])) or {"id": "j"},
    )
    fleet = core_fleet.Fleet()
    hub = serve_cmd._Hub(fleet.set_focus)
    fleet.set_publish(hub.publish)
    httpd = serve_cmd.make_server("127.0.0.1", 0, fleet, hub, TOKEN)
    threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    ).start()
    try:
        yield type(
            "D", (), {"base": f"http://127.0.0.1:{httpd.server_address[1]}", "jobs": created}
        )
    finally:
        httpd.shutdown()
        httpd.server_close()


def _req(d, path, payload=None, cookie=None, bearer=False):
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    if bearer:
        headers["Authorization"] = f"Bearer {TOKEN}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(d.base + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310 - loopback
            return r.status, json.loads(r.read() or b"{}"), r.headers
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}"), e.headers


def _sign_in(d, username, password):
    status, body, headers = _req(d, "/api/v1/session", {"username": username, "password": password})
    cookie = (headers.get("Set-Cookie") or "").split(";")[0]
    return status, body, cookie


def _code(body):
    return (body.get("errors") or [{}])[0].get("title")


class TestSignIn:
    def test_the_local_admin_signs_in_locally_while_the_directory_is_on(self, daemon):
        status, body, _ = _sign_in(daemon, "Admin", ADMIN_PW)
        assert status == 200
        assert body["data"]["source"] == "local" and body["data"]["role"] == "admin"

    def test_a_directory_user_gets_a_session_with_their_role(self, daemon):
        status, body, cookie = _sign_in(daemon, "otto", "otto-pw")
        assert status == 200 and body["data"]["role"] == "operator"
        session = _req(daemon, "/api/v1/session", cookie=cookie)[1]["data"]
        assert session == {
            "username": "otto",
            "must_change_password": False,
            "role": "operator",
            "source": "ldap",
            "name": "Otto",
            "account": True,
        }

    def test_a_refusal_after_the_password_says_why(self, daemon):
        status, body, _ = _sign_in(daemon, "nogroup", "x")
        assert status == 403 and _code(body) == "auth.directory_no_group"

    def test_directory_failures_count_toward_the_lockout(self, daemon):
        for _ in range(accounts.LOCKOUT_AFTER):
            assert _sign_in(daemon, "otto", "wrong")[0] == 401
        assert _sign_in(daemon, "otto", "wrong")[0] == 429

    def test_a_directory_user_cannot_change_a_password_here(self, daemon):
        _s, _b, cookie = _sign_in(daemon, "ana", "ana-pw")
        status, body, _ = _req(daemon, "/api/v1/session/password", {"new": "whatever123"}, cookie)
        assert status == 403 and _code(body) == "auth.directory_password"

    def test_the_page_learns_directory_sign_in_is_on_before_signing_in(self, daemon):
        status, body, _ = _req(daemon, "/api/v1/meta")
        assert status == 200 and body["directory_sign_in"] is True


class TestRoles:
    def _as(self, daemon, who):
        if who == "admin":
            return _sign_in(daemon, "admin", ADMIN_PW)[2]
        return _sign_in(daemon, who, DIRECTORY[who][0])[2]

    @pytest.mark.parametrize("who", ["vic", "otto", "ana"])
    def test_every_role_reads(self, daemon, who):
        cookie = self._as(daemon, who)
        assert _req(daemon, "/api/v1/instances", cookie=cookie)[0] == 200
        assert _req(daemon, "/api/v1/jobs", cookie=cookie)[0] == 200

    def test_a_viewer_changes_nothing(self, daemon):
        cookie = self._as(daemon, "vic")
        for path, payload in (
            ("/api/v1/jobs", {"verb": "start", "target": {"project": "p"}}),
            ("/api/v1/actions", {"action": "stop_instance", "project": "p"}),
            ("/api/v1/schedules", {"op": "delete", "id": "x"}),
            ("/api/v1/settings", {"setting": "tips.enable"}),
        ):
            status, body, _ = _req(daemon, path, payload, cookie)
            assert status == 403 and _code(body) == "auth.role_forbidden", path
        assert daemon.jobs == []
        assert _req(daemon, "/api/v1/backups/download?key=a/b/c", cookie=cookie)[0] == 403

    def test_an_operator_runs_the_fleet_but_not_settings_or_removals(self, daemon):
        cookie = self._as(daemon, "otto")
        assert (
            _req(daemon, "/api/v1/jobs", {"verb": "start", "target": {"project": "p"}}, cookie)[0]
            == 202
        )
        for verb in sorted(accounts.ADMIN_VERBS):
            status, body, _ = _req(
                daemon, "/api/v1/jobs", {"verb": verb, "target": {"project": "p"}}, cookie
            )
            assert status == 403 and _code(body) == "auth.role_forbidden", verb
        for verb in ("restore-set", "prune-backups", "s3-prune", "move-sites"):
            assert verb in accounts.ADMIN_VERBS
        checkout = {"verb": "apps-checkout", "target": {"project": "p"}, "args": {"ref": "x"}}
        assert _req(daemon, "/api/v1/jobs", checkout, cookie)[0] == 202
        checkout["args"]["reset"] = True
        status, body, _ = _req(daemon, "/api/v1/jobs", checkout, cookie)
        assert status == 403 and _code(body) == "auth.role_forbidden"
        update = {"verb": "apps-update", "target": {"project": "p"}, "args": {"apps": ["x"]}}
        assert _req(daemon, "/api/v1/jobs", update, cookie)[0] == 202
        update["args"]["force"] = True
        status, body, _ = _req(daemon, "/api/v1/jobs", update, cookie)
        assert status == 403 and _code(body) == "auth.role_forbidden"
        assert _req(daemon, "/api/v1/settings", {"setting": "tips.enable"}, cookie)[0] == 403
        assert _req(daemon, "/api/v1/integrations", cookie=cookie)[0] == 403
        assert (
            _req(daemon, "/api/v1/integrations", {"op": "disable", "id": "ldap"}, cookie)[0] == 403
        )

    def test_only_the_value_the_role_check_gates_forces_an_update(self, daemon, monkeypatch):
        """A truthy non-boolean ``force`` passes the Operator check, so it must not force."""
        seen = []
        monkeypatch.setattr(
            core_jobs.core_update, "update", lambda *a, **kw: seen.append(kw["force"])
        )
        cookie = self._as(daemon, "otto")
        for force in (1, "yes"):
            args = {"apps": ["x"], "force": force}
            payload = {"verb": "apps-update", "target": {"project": "p"}, "args": args}
            assert _req(daemon, "/api/v1/jobs", payload, cookie)[0] == 202
            core_jobs._dispatch("apps-update", {"project": "p"}, args, False, {}, None)
        core_jobs._dispatch("apps-update", {"project": "p"}, {"force": True}, False, {}, None)
        assert seen == [False, False, True]

    def test_an_operator_cannot_retry_an_admin_only_job(self, daemon, monkeypatch):
        cookie = self._as(daemon, "otto")
        monkeypatch.setattr(core_jobs, "read_job", lambda job_id, **kw: {"verb": "rm"})
        status, body, _ = _req(daemon, "/api/v1/jobs/abcdefgh1/retry", {}, cookie)
        assert status == 403 and _code(body) == "auth.role_forbidden"

    def test_an_operator_cannot_retry_a_reset_checkout(self, daemon, monkeypatch):
        cookie = self._as(daemon, "otto")
        job = {"verb": "apps-checkout", "args": {"reset": True}}
        monkeypatch.setattr(core_jobs, "read_job", lambda job_id, **kw: job)
        status, body, _ = _req(daemon, "/api/v1/jobs/abcdefgh1/retry", {}, cookie)
        assert status == 403 and _code(body) == "auth.role_forbidden"

    def test_an_operator_cannot_retry_a_publish_to_another_address(self, daemon, monkeypatch):
        cookie = self._as(daemon, "otto")
        job = {"verb": "publish-site", "args": {"address": "203.0.113.5"}}
        monkeypatch.setattr(core_jobs, "read_job", lambda job_id, **kw: job)
        status, body, _ = _req(daemon, "/api/v1/jobs/abcdefgh1/retry", {}, cookie)
        assert status == 403 and _code(body) == "auth.role_forbidden"

    def test_an_operator_cannot_continue_a_job_into_a_forced_update(self, daemon, monkeypatch):
        cookie = self._as(daemon, "otto")
        job = {"verb": "apps-update", "args": {"apps": ["x"]}, "state": "needs_choice"}
        monkeypatch.setattr(core_jobs, "read_job", lambda job_id, **kw: job)
        answer = {"target": {"project": "p", "bench": "1"}, "args": {"apps": ["x"], "force": True}}
        status, body, _ = _req(daemon, "/api/v1/jobs/abcdefgh1/continue", answer, cookie)
        assert status == 403 and _code(body) == "auth.role_forbidden"

    def test_only_an_admin_changes_retention(self, daemon, monkeypatch):
        saved = []
        monkeypatch.setattr(
            serve_cmd.core_backup_store, "save_settings", lambda p, v: saved.append(p) or v
        )
        payload = {"project": "p", "values": {"local": {"daily": 1}}}
        for who in ("otto", "vic"):
            status, body, _ = _req(
                daemon, "/api/v1/backups/settings", payload, self._as(daemon, who)
            )
            assert status == 403 and _code(body) == "auth.role_forbidden", who
        assert saved == []
        assert _req(daemon, "/api/v1/backups/settings", payload, self._as(daemon, "ana"))[0] == 200
        assert saved == ["p"]

    def test_only_an_admin_schedules_a_prune(self, daemon):
        otto, ana = self._as(daemon, "otto"), self._as(daemon, "ana")
        prune = {
            "name": "p",
            "verb": "prune-backups",
            "target": {"project": "p"},
            "cron": "0 2 * * *",
        }
        status, body, _ = _req(daemon, "/api/v1/schedules", {"op": "save", "schedule": prune}, otto)
        assert status == 403 and _code(body) == "auth.role_forbidden"
        status, body, _ = _req(daemon, "/api/v1/schedules", {"op": "save", "schedule": prune}, ana)
        assert status == 200
        row = body["data"]
        assert (row["owner"], row["owner_source"]) == ("ana", "ldap")
        for payload in (
            {"op": "save", "schedule": {**prune, "id": row["id"], "verb": "backup-sites"}},
            {"op": "enable", "id": row["id"], "enabled": True},
            {"op": "run", "id": row["id"]},
        ):
            status, body, _ = _req(daemon, "/api/v1/schedules", payload, otto)
            assert status == 403 and _code(body) == "auth.role_forbidden", payload["op"]
        backup = {**prune, "verb": "backup-sites"}
        status, _b, _ = _req(daemon, "/api/v1/schedules", {"op": "save", "schedule": backup}, otto)
        assert status == 200

    def test_looking_up_a_directory_user_is_admin_only(self, daemon):
        payload = {"op": "action", "id": "ldap", "action": "lookup_user", "args": {"username": "x"}}
        for who in ("otto", "vic"):
            status, body, _ = _req(daemon, "/api/v1/integrations", payload, self._as(daemon, who))
            assert status == 403 and _code(body) == "auth.role_forbidden", who

    def test_only_an_admin_adds_updates_or_removes_community_integrations(
        self, daemon, monkeypatch
    ):
        added = []
        monkeypatch.setattr(
            serve_cmd.core_community,
            "install",
            lambda url, ref, *, commit, by: added.append((url, commit, by)) or {"id": "hello"},
        )
        monkeypatch.setattr(serve_cmd.core_integrations, "view", lambda i=None: [{"id": i}])
        add = {"op": "community_add", "url": "https://github.com/o/r", "commit": "c0ffee"}
        for payload in (
            {"op": "community_preview", "url": "https://github.com/o/r"},
            add,
            {"op": "community_check", "id": "hello"},
            {"op": "community_update", "id": "hello", "commit": "c0ffee"},
            {"op": "community_remove", "id": "hello"},
        ):
            for who in ("otto", "vic"):
                status, body, _ = _req(
                    daemon, "/api/v1/integrations", payload, self._as(daemon, who)
                )
                assert status == 403 and _code(body) == "auth.role_forbidden", (who, payload)
        assert added == []
        status, body, _ = _req(daemon, "/api/v1/integrations", add, self._as(daemon, "ana"))
        assert status == 200 and body["data"]["integration"] == {"id": "hello"}
        assert added == [("https://github.com/o/r", "c0ffee", "ana")]

    def test_an_admin_from_the_directory_may_remove(self, daemon):
        cookie = self._as(daemon, "ana")
        assert (
            _req(daemon, "/api/v1/jobs", {"verb": "rm", "target": {"project": "p"}}, cookie)[0]
            == 202
        )
        assert _req(daemon, "/api/v1/integrations", cookie=cookie)[0] == 200

    def test_the_bearer_token_is_an_admin(self, daemon):
        assert _req(daemon, "/api/v1/integrations", bearer=True)[0] == 200

    def test_a_job_records_the_directory_username(self, daemon):
        cookie = self._as(daemon, "otto")
        _req(daemon, "/api/v1/jobs", {"verb": "start", "target": {"project": "p"}}, cookie)
        verb, origin = daemon.jobs[-1]
        assert verb == "start" and origin["account"] == "otto"
        assert origin["account_source"] == "ldap"


def test_the_integrations_api_never_returns_a_secret(daemon):
    secret = "s3-secret-" + secrets.token_hex(4)
    status, body, _ = _req(
        daemon,
        "/api/v1/integrations",
        {
            "op": "configure",
            "id": "s3",
            "settings": {"endpoint": "http://127.0.0.1:1", "bucket": "b", "access_key": "ak"},
            "secrets": {"secret_key": secret},
        },
        bearer=True,
    )
    assert status == 200 and secret not in json.dumps(body)
    assert body["data"]["integration"]["secrets_set"] == {"secret_key": True}
    listing = _req(daemon, "/api/v1/integrations", bearer=True)[1]
    assert secret not in json.dumps(listing)


def test_publication_reads_checks_and_jobs_by_role(daemon, monkeypatch):
    publish = serve_cmd.core_publish
    monkeypatch.setattr(publish, "view", lambda: {"sites": {}, "dns": None, "proxy": None})
    monkeypatch.setattr(publish, "options", lambda: {"dns": None, "proxy": None})
    monkeypatch.setattr(
        publish,
        "preflight",
        lambda p, b, s, address=None, proxied=None: [
            {"part": "dns", "state": "ok", "text": f"{p} {b} {s} {address} {proxied}"}
        ],
    )
    monkeypatch.setattr(publish, "check_site", lambda s: {"domain": s, "ok": True, "parts": {}})
    viewer = _sign_in(daemon, "vic", "vic-pw")[2]
    operator = _sign_in(daemon, "otto", "otto-pw")[2]
    admin = _sign_in(daemon, "admin", ADMIN_PW)[2]
    assert _req(daemon, "/api/v1/publications", cookie=viewer)[0] == 200
    assert _req(daemon, "/api/v1/publish/options", cookie=viewer)[0] == 403
    assert _req(daemon, "/api/v1/publish/options", cookie=operator)[0] == 200
    site = {
        "project": "p",
        "bench": 0,
        "site": "erp.example.com",
        "address": "203.0.113.5",
        "proxied": True,
    }
    for path in ("/api/v1/publish/preflight", "/api/v1/publish/check"):
        assert _req(daemon, path, site, viewer)[0] == 403
    status, body, _ = _req(daemon, "/api/v1/publish/preflight", site, operator)
    assert status == 403 and _code(body) == "auth.role_forbidden"
    status, body, _ = _req(daemon, "/api/v1/publish/preflight", site, admin)
    assert status == 200
    assert body["data"]["rows"][0]["text"] == "p 0 erp.example.com 203.0.113.5 True"
    status, body, _ = _req(daemon, "/api/v1/publish/preflight", {**site, "address": ""}, operator)
    assert status == 200 and body["data"]["rows"][0]["text"] == "p 0 erp.example.com  True"
    status, body, _ = _req(daemon, "/api/v1/publish/check", site, operator)
    assert status == 200 and body["data"]["domain"] == "erp.example.com"
    job = {"verb": "publish-site", "target": {"project": "p", "site": "erp.example.com"}}
    assert _req(daemon, "/api/v1/jobs", job, operator)[0] == 202
    for args in ({"address": "203.0.113.5"}, {"move": True}):
        status, body, _ = _req(daemon, "/api/v1/jobs", {**job, "args": args}, operator)
        assert status == 403 and _code(body) == "auth.role_forbidden", args
        assert _req(daemon, "/api/v1/jobs", {**job, "args": args}, admin)[0] == 202
    assert _req(daemon, "/api/v1/jobs", {**job, "args": {"address": " "}}, operator)[0] == 202
    new_site = {
        "verb": "new-site",
        "target": {"project": "p", "site": "erp.example.com"},
        "args": {"publish": {"address": "", "proxied": False}},
    }
    assert _req(daemon, "/api/v1/jobs", new_site, operator)[0] == 202
    new_site["args"]["publish"]["address"] = "203.0.113.5"
    status, body, _ = _req(daemon, "/api/v1/jobs", new_site, operator)
    assert status == 403 and _code(body) == "auth.role_forbidden"
    assert _req(daemon, "/api/v1/jobs", new_site, admin)[0] == 202
    job["verb"] = "unpublish-site"
    assert _req(daemon, "/api/v1/jobs", job, operator)[0] == 403
