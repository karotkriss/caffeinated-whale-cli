"""Remote hosts through the REAL bound daemon: the downgrade-only delegation a hub's
bridge sends to a remote daemon, the hub's hosts routes, and the per-host forward
whose answers come back as data only. The transport is faked at the manager's
seam; the real SSH path is the E2E (``tests/e2e/test_remote_hosts_e2e.py``)."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from caffeinated_whale_cli.commands import serve as serve_cmd
from caffeinated_whale_cli.core import accounts
from caffeinated_whale_cli.core import fleet as core_fleet
from caffeinated_whale_cli.core import hosts as core_hosts
from caffeinated_whale_cli.core.envelope import Result, Status
from caffeinated_whale_cli.core.protocol import PROTOCOL_VERSION

ADMIN_PW = "break glass pw"
TOKEN = "hosts-test-token"
HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFmBvLshzLAzHjvkIyGPGlSs9uXUe8kK/zwyIT14B3QM"
PAIRING = core_hosts.make_pairing(
    {"v": 1, "hub": "hub-x", "cap": "operator", "user": "frappe", "address": "10.9.8.7", "port": 22, "name": "Build box", "host_key": HOST_KEY}
)


class _FakeResponse:
    """What the manager hands the handler for a forwarded request."""

    def __init__(self, status=200, body=b'{"data": {"ok": true}}', headers=None, length=True):
        self.status = status
        self.reason = "X"
        self.headers = list(headers or [("Content-Type", "application/json"), ("Set-Cookie", "remote=1")])
        self._body = body
        self.content_length = len(body) if length else None
        self.closed = False

    def header(self, name):
        low = name.lower()
        return next((v for k, v in self.headers if k.lower() == low), None)

    def iter_body(self, **kw):
        yield self._body

    def close(self):
        self.closed = True


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("CWCLI_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(core_fleet, "list_instances", lambda: Result(status=Status.OK, data=[]))
    accounts.ensure_default()
    accounts.change_password("admin", ADMIN_PW)
    created: list[tuple] = []
    monkeypatch.setattr(
        serve_cmd.core_jobs,
        "create_job",
        lambda verb, target, args, **kw: created.append((verb, kw["origin"])) or {"id": "j"},
    )
    fleet = core_fleet.Fleet()
    hub = serve_cmd._Hub(fleet.set_focus)
    fleet.set_publish(hub.publish)
    manager = core_hosts.HostManager(publish=hub.publish_event, transports_fn=lambda: {})
    forwarded: list[dict] = []
    answers: list = []
    views: dict = {}

    def fake_request(host_id, method, target, headers, body=b""):
        forwarded.append({"host": host_id, "method": method, "target": target, "headers": dict(headers), "body": body})
        answer = answers.pop(0) if answers else _FakeResponse()
        if isinstance(answer, Exception):
            raise answer
        return answer

    manager.request = fake_request  # type: ignore[method-assign]
    real_view = manager.link_view
    manager.link_view = lambda host_id: views.get(host_id) or real_view(host_id)  # type: ignore[method-assign]
    httpd = serve_cmd.make_server("127.0.0.1", 0, fleet, hub, TOKEN, hosts=manager)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    try:
        yield type(
            "D",
            (),
            {
                "base": f"http://127.0.0.1:{httpd.server_address[1]}",
                "jobs": created,
                "forwarded": forwarded,
                "answers": answers,
                "views": views,
                "manager": manager,
                "hub": hub,
            },
        )
    finally:
        httpd.shutdown()
        httpd.server_close()


def _req(d, path, payload=None, *, cookie=None, bearer=False, headers=None, method=None):
    h = {"Content-Type": "application/json"}
    if cookie:
        h["Cookie"] = cookie
    if bearer:
        h["Authorization"] = f"Bearer {TOKEN}"
    h.update(headers or {})
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(d.base + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310 - loopback
            raw = r.read()
            return r.status, (json.loads(raw) if raw[:1] in (b"{", b"[") else raw), r.headers
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw[:1] in (b"{", b"[") else raw), e.headers


def _code(body):
    return (body.get("errors") or [{}])[0].get("title")


def _admin_cookie(d):
    _s, _b, headers = _req(d, "/api/v1/session", {"username": "admin", "password": ADMIN_PW})
    return (headers.get("Set-Cookie") or "").split(";")[0]


def _connected(name="Build box", state=core_hosts.CONNECTED, reason=None):
    return {"id": "build-box", "name": name, "state": state, "reason": reason, "actions_allowed": state == core_hosts.CONNECTED, "readable": state in core_hosts.READABLE_STATES}


DELEGATED = {"X-Cwcli-Delegated-User": "alice@hub-x", "X-Cwcli-Delegated-Role": "viewer", "X-Cwcli-Hub": "hub-x"}


class TestDowngradeOnlyDelegation:
    """The remote daemon's side: a bearer request from a hub's bridge."""

    def test_a_delegated_role_lowers_the_bearers_admin(self, daemon):
        assert _req(daemon, "/api/v1/instances", bearer=True, headers=DELEGATED)[0] == 200
        status, body, _ = _req(daemon, "/api/v1/jobs", {"verb": "start", "target": {"project": "p"}}, bearer=True, headers=DELEGATED)
        assert status == 403 and _code(body) == "auth.role_forbidden"
        status, body, _ = _req(daemon, "/api/v1/integrations", bearer=True, headers={**DELEGATED, "X-Cwcli-Delegated-Role": "operator"})
        assert status == 403 and body["errors"][0]["role"] == "operator"
        assert daemon.jobs == []

    def test_an_admin_claim_on_a_bearer_stays_admin_and_the_job_records_the_hub(self, daemon):
        headers = {**DELEGATED, "X-Cwcli-Delegated-Role": "admin"}
        status, _b, _ = _req(daemon, "/api/v1/jobs", {"verb": "start", "target": {"project": "p"}}, bearer=True, headers=headers)
        assert status == 202
        (verb, origin), = daemon.jobs
        assert verb == "start"
        assert origin["via"] == "hub" and origin["account"] == "alice@hub-x" and origin["account_source"] == "hub" and origin["hub"] == "hub-x"

    def test_a_cookie_session_ignores_the_headers_entirely(self, daemon):
        cookie = _admin_cookie(daemon)
        status, body, _ = _req(daemon, "/api/v1/session", cookie=cookie, headers=DELEGATED)
        assert status == 200 and body["data"]["role"] == "admin" and body["data"]["username"] == "admin"
        assert _req(daemon, "/api/v1/integrations", cookie=cookie, headers=DELEGATED)[0] == 200

    @pytest.mark.parametrize(
        "headers",
        [
            {"X-Cwcli-Delegated-User": "alice@hub-x", "X-Cwcli-Delegated-Role": "root"},
            {"X-Cwcli-Delegated-User": "alice", "X-Cwcli-Delegated-Role": "viewer"},
            {"X-Cwcli-Delegated-Role": "viewer"},
            {"X-Cwcli-Delegated-User": "alice@hub-x"},
            {"X-Cwcli-Delegated-User": "alice@hub-x\x01", "X-Cwcli-Delegated-Role": "viewer"},
        ],
    )
    def test_a_malformed_pair_is_refused_never_the_tokens_admin(self, daemon, headers):
        status, body, _ = _req(daemon, "/api/v1/instances", bearer=True, headers=headers)
        assert status == 400 and _code(body) == "auth.delegation_invalid"
        status, body, _ = _req(daemon, "/api/v1/jobs", {"verb": "start", "target": {"project": "p"}}, bearer=True, headers=headers)
        assert status == 400 and daemon.jobs == []

    def test_the_session_read_names_the_delegated_user(self, daemon):
        status, body, _ = _req(daemon, "/api/v1/session", bearer=True, headers=DELEGATED)
        assert status == 200 and body["data"] == {"username": "alice@hub-x", "must_change_password": False, "account": False, "role": "viewer", "source": "hub", "name": ""}

    def test_the_headers_mean_nothing_without_a_credential(self, daemon):
        assert _req(daemon, "/api/v1/instances", headers=DELEGATED)[0] == 401


class TestHostsRoutes:
    def test_the_hub_view_names_the_hub_and_carries_no_private_key(self, daemon, tmp_path):
        core_hosts.keys_dir().mkdir(parents=True)
        core_hosts.hub_key_path().write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\n-----END OPENSSH PRIVATE KEY-----\n")
        core_hosts.hub_key_path().with_suffix(".pub").write_text("ssh-ed25519 AAAA cwcli-hub:hub-t\n")
        status, body, _ = _req(daemon, "/api/v1/hosts", bearer=True)
        assert status == 200
        assert body["data"]["hub"]["public_key"] == "ssh-ed25519 AAAA cwcli-hub:hub-t" and body["data"]["hosts"] == []
        assert "secret" not in json.dumps(body) and "PRIVATE" not in json.dumps(body)

    def test_the_protocol_comes_from_core_and_the_page_meta_says_so(self, daemon):
        assert serve_cmd.PROTOCOL_VERSION == PROTOCOL_VERSION
        assert _req(daemon, "/api/v1/meta")[1]["protocol"] == PROTOCOL_VERSION

    def test_every_role_reads_the_hosts_but_only_an_admin_changes_them(self, daemon):
        status, body, _ = _req(daemon, "/api/v1/hosts", {"op": "remove", "id": "x"}, bearer=True, headers={**DELEGATED, "X-Cwcli-Delegated-Role": "operator"})
        assert status == 403 and _code(body) == "auth.role_forbidden"
        assert _req(daemon, "/api/v1/hosts", bearer=True, headers=DELEGATED)[0] == 200

    def test_add_refuses_without_a_host_key_and_pins_with_one(self, daemon):
        bare = core_hosts.make_pairing({"v": 1, "hub": "hub-x", "cap": "operator", "user": "frappe", "address": "10.9.8.7", "port": 22, "name": "Build box", "host_key": None})
        status, body, _ = _req(daemon, "/api/v1/hosts", {"op": "add", "pairing": bare}, bearer=True)
        assert status == 412 and _code(body) == "host.key_required"
        status, body, _ = _req(daemon, "/api/v1/hosts", {"op": "add", "pairing": PAIRING, "address": "build.lan"}, bearer=True)
        assert status == 200, body
        host = body["data"]["host"]
        assert host["id"] == "build-box" and host["address"] == "build.lan" and host["state"] == core_hosts.TRANSPORT_OFF
        assert core_hosts.known_hosts_path().read_text() == f"cwcli-host-build-box {HOST_KEY}\n"
        status, body, _ = _req(daemon, "/api/v1/hosts", {"op": "remove", "id": "build-box"}, bearer=True)
        assert status == 200 and body["data"]["removed"] == "build-box" and core_hosts.list_hosts() == []

    def test_an_unknown_op_is_a_usage_error(self, daemon):
        status, body, _ = _req(daemon, "/api/v1/hosts", {"op": "explode", "id": "x"}, bearer=True)
        assert status == 400 and _code(body) == "host.op_invalid"

    def test_the_stream_snapshot_carries_the_hosts(self, daemon):
        req = urllib.request.Request(daemon.base + "/api/v1/stream", headers={"Authorization": f"Bearer {TOKEN}"})
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            line = b""
            while not line.startswith(b"data: "):
                line = resp.readline()
        assert json.loads(line[6:])["hosts"] == []


class TestForward:
    def _as_viewer(self, daemon):
        return {**DELEGATED, "X-Cwcli-Delegated-Role": "viewer"}

    def test_a_read_is_forwarded_as_the_caller_with_their_role_and_comes_back_as_data(self, daemon):
        daemon.views["build-box"] = _connected()
        cookie = _admin_cookie(daemon)
        status, body, headers = _req(daemon, "/api/v1/hosts/build-box/instances?x=1", cookie=cookie, headers={"X-Cwcli-Delegated-Role": "viewer"})
        assert status == 200 and body == {"data": {"ok": True}}
        (sent,) = daemon.forwarded
        assert sent["method"] == "GET" and sent["target"] == "/api/v1/instances?x=1" and sent["host"] == "build-box"
        # The hub builds the headers itself: the signed-in user and the SESSION's role, not
        # what the browser claimed, and nothing else the browser sent.
        assert sent["headers"]["X-Cwcli-Delegated-User"] == "admin" and sent["headers"]["X-Cwcli-Delegated-Role"] == "admin"
        assert "Cookie" not in sent["headers"] and "Authorization" not in sent["headers"]
        assert headers["X-Content-Type-Options"] == "nosniff" and headers["Content-Security-Policy"] == "default-src 'none'; sandbox"
        assert headers["X-Cwcli-Host"] == "build-box" and headers.get("Set-Cookie") is None
        assert headers["Cache-Control"] == "no-store"

    def test_a_post_is_forwarded_whole_and_the_hub_applies_its_own_least_role_first(self, daemon):
        daemon.views["build-box"] = _connected()
        payload = {"verb": "rm", "target": {"project": "p"}, "consent": True}
        status, body, _ = _req(daemon, "/api/v1/hosts/build-box/jobs", payload, bearer=True, headers={**DELEGATED, "X-Cwcli-Delegated-Role": "operator"})
        assert status == 403 and _code(body) == "auth.role_forbidden" and daemon.forwarded == []
        status, body, _ = _req(daemon, "/api/v1/hosts/build-box/jobs", payload, bearer=True)
        assert status == 200
        (sent,) = daemon.forwarded
        assert sent["method"] == "POST" and json.loads(sent["body"]) == payload
        assert sent["headers"]["Content-Type"] == "application/json" and sent["headers"]["X-Cwcli-Delegated-Role"] == "admin"
        assert sent["headers"]["X-Cwcli-Delegated-User"] == "api"

    @pytest.mark.parametrize("state", [core_hosts.VERSION_MISMATCH, core_hosts.UNREACHABLE, core_hosts.CONNECTING, core_hosts.DAEMON_DOWN])
    def test_an_action_needs_the_host_connected_on_an_equal_protocol(self, daemon, state):
        daemon.views["build-box"] = _connected(state=state, reason="because")
        status, body, _ = _req(daemon, "/api/v1/hosts/build-box/jobs", {"verb": "start", "target": {"project": "p"}}, bearer=True)
        assert status == 409
        assert _code(body) == ("host.version_mismatch" if state == core_hosts.VERSION_MISMATCH else "host.not_connected")
        assert daemon.forwarded == []

    def test_a_read_is_still_attempted_on_a_version_mismatch(self, daemon):
        daemon.views["build-box"] = _connected(state=core_hosts.VERSION_MISMATCH)
        assert _req(daemon, "/api/v1/hosts/build-box/instances", bearer=True)[0] == 200

    @pytest.mark.parametrize("inner", ["session", "session/password", "stream", "hosts", "hosts/x/instances", "backups/upload"])
    def test_the_remotes_session_stream_hosts_and_upload_are_never_forwarded(self, daemon, inner):
        daemon.views["build-box"] = _connected()
        status, body, _ = _req(daemon, f"/api/v1/hosts/build-box/{inner}", bearer=True)
        assert status == 404 and daemon.forwarded == []

    def test_an_unknown_host_is_not_found(self, daemon):
        status, body, _ = _req(daemon, "/api/v1/hosts/nope/instances", bearer=True)
        assert status == 404 and _code(body) == "host.unknown"

    def test_a_link_failure_is_a_502_naming_the_state(self, daemon):
        daemon.views["build-box"] = _connected()
        daemon.answers.append(core_hosts.LinkError(core_hosts.AUTH_REFUSED, "denied"))
        status, body, _ = _req(daemon, "/api/v1/hosts/build-box/instances", bearer=True)
        assert status == 502 and _code(body) == "host.auth_refused" and body["errors"][0]["state"] == "auth_refused"

    def test_a_remote_answering_a_page_is_relayed_as_an_attachment_never_rendered(self, daemon):
        daemon.views["build-box"] = _connected()
        daemon.answers.append(_FakeResponse(body=b"<script>alert(1)</script>", headers=[("Content-Type", "text/html; charset=utf-8"), ("Content-Disposition", 'inline; filename="../x.html"')]))
        status, body, headers = _req(daemon, "/api/v1/hosts/build-box/instances", bearer=True)
        assert status == 200 and body == b"<script>alert(1)</script>"
        assert headers["Content-Type"] == "application/octet-stream"
        assert headers["Content-Disposition"] == 'attachment; filename="download"'
        assert headers["Content-Security-Policy"] == "default-src 'none'; sandbox" and headers["X-Content-Type-Options"] == "nosniff"

    def test_a_download_keeps_a_safe_filename_and_an_image_gets_the_logo_policy(self, daemon):
        daemon.views["build-box"] = _connected()
        daemon.answers.append(_FakeResponse(body=b"tar", headers=[("Content-Type", "application/x-tar"), ("Content-Disposition", 'attachment; filename="site_2026.tar"')]))
        _s, _b, headers = _req(daemon, "/api/v1/hosts/build-box/backups/download?key=k", bearer=True)
        assert headers["Content-Disposition"] == 'attachment; filename="site_2026.tar"' and headers["Content-Type"] == "application/x-tar"
        daemon.answers.append(_FakeResponse(body=b"<svg/>", headers=[("Content-Type", "image/svg+xml")]))
        _s, _b, headers = _req(daemon, "/api/v1/hosts/build-box/apps/logo/x", bearer=True)
        assert headers["Content-Type"] == "image/svg+xml" and headers["Content-Security-Policy"] == "default-src 'none'; style-src 'unsafe-inline'; sandbox"

    def test_the_forward_is_same_origin_only_and_a_viewer_may_read_but_not_act(self, daemon):
        daemon.views["build-box"] = _connected()
        status, body, _ = _req(daemon, "/api/v1/hosts/build-box/instances", bearer=True, headers={"Origin": "http://evil.example"})
        assert status == 403 and _code(body) == "action.origin_forbidden"
        assert _req(daemon, "/api/v1/hosts/build-box/instances", bearer=True, headers=self._as_viewer(daemon))[0] == 200
        status, body, _ = _req(daemon, "/api/v1/hosts/build-box/actions", {"action": "stop_instance", "project": "p"}, bearer=True, headers=self._as_viewer(daemon))
        assert status == 403 and _code(body) == "auth.role_forbidden"
        status, body, _ = _req(daemon, "/api/v1/hosts/build-box/integrations", bearer=True, headers={**DELEGATED, "X-Cwcli-Delegated-Role": "operator"})
        assert status == 403

    def test_options_knows_the_forwarded_post_paths(self, daemon):
        assert serve_cmd._is_post_path("/api/v1/hosts/build-box/jobs")
        assert serve_cmd._is_post_path("/api/v1/hosts/build-box/jobs/abc/interrupt")
        assert not serve_cmd._is_post_path("/api/v1/hosts/build-box/instances")
        assert serve_cmd._host_route("/api/v1/hosts/build-box") is None
        assert serve_cmd._host_route("/api/v1/hosts/b%20x/instances/p") == ("b x", "instances/p")
