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
        assert _req(daemon, "/api/v1/settings", {"setting": "tips.enable"}, cookie)[0] == 403
        assert _req(daemon, "/api/v1/integrations", cookie=cookie)[0] == 403
        assert (
            _req(daemon, "/api/v1/integrations", {"op": "disable", "id": "ldap"}, cookie)[0] == 403
        )

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
