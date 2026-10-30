"""Remote hosts through the REAL bound daemon: the downgrade-only delegation a hub's
bridge sends to a remote daemon, the hub's hosts routes, and the per-host forward
whose answers come back as data only. The transport is faked at the manager's
seam; the real SSH path is the E2E (``tests/e2e/test_remote_hosts_e2e.py``)."""

from __future__ import annotations

import json
import threading
import time
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
    {
        "v": 1,
        "hub": "hub-x",
        "cap": "operator",
        "user": "frappe",
        "address": "10.9.8.7",
        "port": 22,
        "name": "Build box",
        "host_key": HOST_KEY,
    }
)


class _FakeResponse:
    """What the manager hands the handler for a forwarded request."""

    def __init__(self, status=200, body=b'{"data": {"ok": true}}', headers=None, length=True):
        self.status = status
        self.reason = "X"
        self.headers = list(
            headers or [("Content-Type", "application/json"), ("Set-Cookie", "remote=1")]
        )
        self._body = body
        self.content_length = len(body) if length else None
        self.closed = False

    def header(self, name):
        low = name.lower()
        return next((v for k, v in self.headers if k.lower() == low), None)

    def iter_body(self, **kw):
        yield self._body

    def read(self, limit=None):
        return self._body

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
        forwarded.append(
            {
                "host": host_id,
                "method": method,
                "target": target,
                "headers": dict(headers),
                "body": body,
            }
        )
        answer = answers.pop(0) if answers else _FakeResponse()
        if isinstance(answer, Exception):
            raise answer
        return answer

    manager.request = fake_request  # type: ignore[method-assign]
    real_view = manager.link_view
    manager.link_view = lambda host_id: views.get(host_id) or real_view(host_id)  # type: ignore[method-assign]
    httpd = serve_cmd.make_server("127.0.0.1", 0, fleet, hub, TOKEN, hosts=manager)
    threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    ).start()
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
    return {
        "id": "build-box",
        "name": name,
        "state": state,
        "reason": reason,
        "actions_allowed": state == core_hosts.CONNECTED,
        "readable": state in core_hosts.READABLE_STATES,
    }


DELEGATED = {"X-Cwcli-Delegated-User": "alice@hub-x", "X-Cwcli-Delegated-Role": "viewer"}


def _stream(d, headers=None):
    req = urllib.request.Request(
        d.base + "/api/v1/stream", headers={"Authorization": f"Bearer {TOKEN}", **(headers or {})}
    )
    return urllib.request.urlopen(req, timeout=5)  # noqa: S310 - loopback


def _frames(resp, until):
    """``(event, data)`` for each frame up to and including the first ``until`` one."""
    out, event = [], None
    while True:
        line = resp.readline()
        assert line, "the stream ended"
        if line.startswith(b"event: "):
            event = line[7:].strip().decode()
        elif line.startswith(b"data: "):
            out.append((event, json.loads(line[6:])))
            if event == until:
                return out


class TestDowngradeOnlyDelegation:
    """The remote daemon's side: a bearer request from a hub's bridge."""

    def test_a_delegated_role_lowers_the_bearers_admin(self, daemon):
        assert _req(daemon, "/api/v1/instances", bearer=True, headers=DELEGATED)[0] == 200
        status, body, _ = _req(
            daemon,
            "/api/v1/jobs",
            {"verb": "start", "target": {"project": "p"}},
            bearer=True,
            headers=DELEGATED,
        )
        assert status == 403 and _code(body) == "auth.role_forbidden"
        status, body, _ = _req(
            daemon,
            "/api/v1/integrations",
            bearer=True,
            headers={**DELEGATED, "X-Cwcli-Delegated-Role": "operator"},
        )
        assert status == 403 and body["errors"][0]["role"] == "operator"
        assert daemon.jobs == []

    def test_an_admin_claim_on_a_bearer_stays_admin_and_the_job_records_the_hub(self, daemon):
        """The hub's name comes from the delegated user's @<hub> suffix, which the bridge
        writes from its own command line; no other header names the hub."""
        headers = {**DELEGATED, "X-Cwcli-Delegated-Role": "admin", "X-Cwcli-Hub": "some-other-hub"}
        status, _b, _ = _req(
            daemon,
            "/api/v1/jobs",
            {"verb": "start", "target": {"project": "p"}},
            bearer=True,
            headers=headers,
        )
        assert status == 202
        ((verb, origin),) = daemon.jobs
        assert verb == "start"
        assert (
            origin["via"] == "hub"
            and origin["account"] == "alice@hub-x"
            and origin["account_source"] == "hub"
            and origin["hub"] == "hub-x"
        )

    def test_a_cookie_session_ignores_the_headers_entirely(self, daemon):
        cookie = _admin_cookie(daemon)
        status, body, _ = _req(daemon, "/api/v1/session", cookie=cookie, headers=DELEGATED)
        assert (
            status == 200
            and body["data"]["role"] == "admin"
            and body["data"]["username"] == "admin"
        )
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
        status, body, _ = _req(
            daemon,
            "/api/v1/jobs",
            {"verb": "start", "target": {"project": "p"}},
            bearer=True,
            headers=headers,
        )
        assert status == 400 and daemon.jobs == []

    def test_the_session_read_names_the_delegated_user(self, daemon):
        status, body, _ = _req(daemon, "/api/v1/session", bearer=True, headers=DELEGATED)
        assert status == 200 and body["data"] == {
            "username": "alice@hub-x",
            "must_change_password": False,
            "account": False,
            "role": "viewer",
            "source": "hub",
            "name": "",
        }

    def test_the_headers_mean_nothing_without_a_credential(self, daemon):
        assert _req(daemon, "/api/v1/instances", headers=DELEGATED)[0] == 401

    def test_a_schedule_saved_through_a_hub_belongs_to_the_hub_user(self, daemon):
        """The remote caps the hub user and records who it is: an Admin-only schedule is
        refused to a delegated Operator, and a delegated Admin's is owned by that hub user
        (source `hub`, which the tick re-checks), never by this machine's own bearer."""
        prune = {
            "op": "save",
            "schedule": {
                "name": "prune",
                "verb": "prune-backups",
                "target": {"project": "p"},
                "cron": "0 2 * * *",
            },
        }
        status, body, _ = _req(
            daemon,
            "/api/v1/schedules",
            prune,
            bearer=True,
            headers={**DELEGATED, "X-Cwcli-Delegated-Role": "operator"},
        )
        assert status == 403 and _code(body) == "auth.role_forbidden"
        status, body, _ = _req(
            daemon,
            "/api/v1/schedules",
            prune,
            bearer=True,
            headers={**DELEGATED, "X-Cwcli-Delegated-Role": "admin"},
        )
        assert status == 200, body
        assert (body["data"]["owner"], body["data"]["owner_source"]) == ("alice@hub-x", "hub")
        status, body, _ = _req(daemon, "/api/v1/schedules", prune, bearer=True)
        assert status == 200 and (body["data"]["owner"], body["data"]["owner_source"]) == (
            "api",
            "api",
        )


class TestNoCodeChangeFromAHub:
    """The remote daemon's own guard behind the bridge's: a delegated request never
    installs or updates third-party code here or replaces cwcli, whatever its role,
    while this machine's own Admin still does both."""

    ADMIN = {**DELEGATED, "X-Cwcli-Delegated-Role": "admin"}

    @pytest.mark.parametrize("op", ["community_add", "community_update"])
    def test_a_delegated_community_install_or_update_is_refused(self, daemon, monkeypatch, op):
        reached = []
        monkeypatch.setattr(serve_cmd.core_community, "install", lambda *a, **k: reached.append(a))
        monkeypatch.setattr(serve_cmd.core_community, "update", lambda *a, **k: reached.append(a))
        payload = {"op": op, "url": "https://example.com/x.git", "id": "x", "commit": "abc"}
        status, body, _ = _req(
            daemon, "/api/v1/integrations", payload, bearer=True, headers=self.ADMIN
        )
        assert status == 403 and _code(body) == "auth.code_change_refused" and reached == []

    def test_a_delegated_self_update_or_its_retry_is_refused_and_the_machines_own_admin_still_runs_both(
        self, daemon, monkeypatch
    ):
        monkeypatch.setattr(
            serve_cmd.core_jobs,
            "read_job",
            lambda job_id, *a, **k: {"id": job_id, "verb": "self-update", "args": {}},
        )
        retried = []
        monkeypatch.setattr(
            serve_cmd.core_jobs,
            "retry_job",
            lambda job_id, **k: retried.append(job_id) or {"id": job_id},
        )
        for path, payload in (
            ("/api/v1/jobs", {"verb": "self-update"}),
            ("/api/v1/jobs/j1/retry", {}),
        ):
            status, body, _ = _req(daemon, path, payload, bearer=True, headers=self.ADMIN)
            assert status == 403 and _code(body) == "auth.code_change_refused"
        assert daemon.jobs == [] and retried == []
        assert _req(daemon, "/api/v1/jobs", {"verb": "self-update"}, bearer=True)[0] == 202
        assert _req(daemon, "/api/v1/jobs/j1/retry", {}, bearer=True)[0] == 202
        assert [verb for verb, _origin in daemon.jobs] == ["self-update"] and retried == ["j1"]

    def test_the_machines_own_admin_still_installs_and_an_ordinary_delegated_admin_op_passes(
        self, daemon, monkeypatch
    ):
        def install(*a, **k):
            raise serve_cmd.CwcliError(
                serve_cmd.ErrorKind.NOT_FOUND, "community.reached", "reached the install"
            )

        monkeypatch.setattr(serve_cmd.core_community, "install", install)
        payload = {"op": "community_add", "url": "https://example.com/x.git", "commit": "abc"}
        assert (
            _code(_req(daemon, "/api/v1/integrations", payload, bearer=True)[1])
            == "community.reached"
        )
        assert (
            _req(
                daemon,
                "/api/v1/settings",
                {"setting": "cache.enable"},
                bearer=True,
                headers=self.ADMIN,
            )[0]
            == 200
        )


class TestExactVerbsAndOps:
    """The verb a role or code-change check reads is the verb that runs: a padded job verb
    or integration op is a usage error before any of those checks, on the hub and here."""

    PADDED = [" self-update", "self-update\n", "rm ", " rm"]

    @pytest.mark.parametrize("verb", PADDED)
    def test_a_padded_job_verb_is_refused_before_the_role_and_code_change_checks(
        self, daemon, verb
    ):
        payload = {
            "verb": verb,
            "target": {"project": "x"},
            "args": {"no_backup": True},
            "consent": True,
        }
        for role in ("viewer", "operator", "admin", None):
            headers = {**DELEGATED, "X-Cwcli-Delegated-Role": role} if role else {}
            status, body, _ = _req(daemon, "/api/v1/jobs", payload, bearer=True, headers=headers)
            assert (status, _code(body)) == (400, "job.verb_unsupported"), role
        assert daemon.jobs == []
        operator = {**DELEGATED, "X-Cwcli-Delegated-Role": "operator"}
        assert (
            _req(
                daemon,
                "/api/v1/jobs",
                {"verb": "start", "target": {"project": "x"}},
                bearer=True,
                headers=operator,
            )[0]
            == 202
        )
        assert [verb for verb, _origin in daemon.jobs] == ["start"]

    @pytest.mark.parametrize("verb", PADDED)
    def test_the_hub_refuses_a_padded_verb_before_forwarding(self, daemon, verb):
        daemon.views["build-box"] = _connected()
        payload = {
            "verb": verb,
            "target": {"project": "x"},
            "args": {"no_backup": True},
            "consent": True,
        }
        status, body, _ = _req(daemon, "/api/v1/hosts/build-box/jobs", payload, bearer=True)
        assert (status, _code(body)) == (400, "job.verb_unsupported") and daemon.forwarded == []
        assert (
            _req(
                daemon,
                "/api/v1/hosts/build-box/jobs",
                {"verb": "start", "target": {"project": "x"}},
                bearer=True,
            )[0]
            == 200
        )
        assert [f["target"] for f in daemon.forwarded] == ["/api/v1/jobs"]

    @pytest.mark.parametrize("op", [" community_add", "community_update\n"])
    def test_a_padded_integration_op_is_refused_before_the_role_and_code_change_checks(
        self, daemon, monkeypatch, op
    ):
        reached = []
        monkeypatch.setattr(serve_cmd.core_community, "install", lambda *a, **k: reached.append(a))
        payload = {"op": op, "url": "https://example.com/x.git", "id": "x", "commit": "abc"}
        for headers in (
            {**DELEGATED, "X-Cwcli-Delegated-Role": "viewer"},
            {**DELEGATED, "X-Cwcli-Delegated-Role": "admin"},
            {},
        ):
            status, body, _ = _req(
                daemon, "/api/v1/integrations", payload, bearer=True, headers=headers
            )
            assert (status, _code(body)) == (400, "integration.op_invalid")
        assert reached == []


class TestRemoteFocus:
    def test_a_tabs_remote_focus_goes_to_that_host_the_latest_tab_winning(self):
        local, remote = [], []
        hub = serve_cmd._Hub(local.append)
        hub.on_host_focus = remote.append
        first, _ = hub.subscribe("p", "build-box")
        hub.subscribe("q")
        latest, _ = hub.subscribe("r", "build-box")
        assert (local[-1], remote[-1]) == ({"q"}, {"build-box": "r"})
        hub.unsubscribe(latest)
        assert remote[-1] == {"build-box": "p"}
        hub.unsubscribe(first)
        assert (local[-1], remote[-1]) == ({"q"}, {})

    def test_the_stream_query_carries_the_host_but_a_hubs_own_stream_never_steers_the_links(
        self, daemon
    ):
        seen = []
        daemon.hub.on_host_focus = seen.append

        def open_stream(headers):
            req = urllib.request.Request(
                daemon.base + "/api/v1/stream?focus=p&host=build-box",
                headers={"Authorization": f"Bearer {TOKEN}", **headers},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
                line = b""
                while not line.startswith(b"data: "):
                    line = resp.readline()

        open_stream(DELEGATED)
        assert seen and all(focus == {} for focus in seen)
        open_stream({})
        assert {"build-box": "p"} in seen


class TestHostsRoutes:
    def test_the_hub_view_names_the_hub_and_carries_no_private_key(self, daemon, tmp_path):
        core_hosts.keys_dir().mkdir(parents=True)
        core_hosts.hub_key_path().write_text(
            "-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\n-----END OPENSSH PRIVATE KEY-----\n"
        )
        core_hosts.hub_key_path().with_suffix(".pub").write_text(
            "ssh-ed25519 AAAA cwcli-hub:hub-t\n"
        )
        status, body, _ = _req(daemon, "/api/v1/hosts", bearer=True)
        assert status == 200
        assert (
            body["data"]["hub"]["public_key"] == "ssh-ed25519 AAAA cwcli-hub:hub-t"
            and body["data"]["hosts"] == []
        )
        assert "secret" not in json.dumps(body) and "PRIVATE" not in json.dumps(body)

    def test_the_protocol_comes_from_core_and_the_page_meta_says_so(self, daemon):
        assert serve_cmd.PROTOCOL_VERSION == PROTOCOL_VERSION
        assert _req(daemon, "/api/v1/meta")[1]["protocol"] == PROTOCOL_VERSION

    def test_every_role_reads_the_hosts_but_only_an_admin_changes_them(self, daemon):
        status, body, _ = _req(
            daemon,
            "/api/v1/hosts",
            {"op": "remove", "id": "x"},
            bearer=True,
            headers={**DELEGATED, "X-Cwcli-Delegated-Role": "operator"},
        )
        assert status == 403 and _code(body) == "auth.role_forbidden"
        assert _req(daemon, "/api/v1/hosts", bearer=True, headers=DELEGATED)[0] == 200

    def test_add_refuses_without_a_host_key_and_pins_with_one(self, daemon):
        bare = core_hosts.make_pairing(
            {
                "v": 1,
                "hub": "hub-x",
                "cap": "operator",
                "user": "frappe",
                "address": "10.9.8.7",
                "port": 22,
                "name": "Build box",
                "host_key": None,
            }
        )
        status, body, _ = _req(daemon, "/api/v1/hosts", {"op": "add", "pairing": bare}, bearer=True)
        assert status == 412 and _code(body) == "host.key_required"
        status, body, _ = _req(
            daemon,
            "/api/v1/hosts",
            {"op": "add", "pairing": PAIRING, "address": "build.lan"},
            bearer=True,
        )
        assert status == 200, body
        host = body["data"]["host"]
        assert (
            host["id"] == "build-box"
            and host["address"] == "build.lan"
            and host["state"] == core_hosts.TRANSPORT_OFF
        )
        assert core_hosts.known_hosts_path().read_text() == f"cwcli-host-build-box {HOST_KEY}\n"
        status, body, _ = _req(
            daemon, "/api/v1/hosts", {"op": "remove", "id": "build-box"}, bearer=True
        )
        assert (
            status == 200
            and body["data"]["removed"] == "build-box"
            and core_hosts.list_hosts() == []
        )

    def test_a_host_check_waiting_on_a_slow_transport_never_holds_up_a_settings_write(self, daemon):
        """Check now on a host that just went dark waits on the network for a probe's
        length; a settings or integration write must not queue behind it."""
        entered, release = threading.Event(), threading.Event()

        class Slow:
            id = "ssh"

            def open(self, host):
                entered.set()
                release.wait(10)
                raise core_hosts.LinkError(core_hosts.UNREACHABLE, "timed out")

            def close(self, host):
                pass

        rec = core_hosts.add_host(PAIRING)
        link = core_hosts.HostLink(
            rec, Slow(), on_state=daemon.manager._state_changed, on_frame=daemon.manager._frame
        )
        link.status.state = core_hosts.CONNECTED
        daemon.manager._links[rec.id] = link
        checking = threading.Thread(
            target=_req,
            args=(daemon, "/api/v1/hosts", {"op": "check", "id": rec.id}),
            kwargs={"bearer": True},
        )
        checking.start()
        try:
            assert entered.wait(5)
            started = time.monotonic()
            status, body, _ = _req(
                daemon, "/api/v1/settings", {"setting": "tips.disable"}, bearer=True
            )
            assert status == 200, body
            assert time.monotonic() - started < 3
        finally:
            release.set()
            checking.join(10)

    def test_an_unknown_op_is_a_usage_error(self, daemon):
        status, body, _ = _req(daemon, "/api/v1/hosts", {"op": "explode", "id": "x"}, bearer=True)
        assert status == 400 and _code(body) == "host.op_invalid"

    def test_a_hubs_stream_never_learns_this_machines_own_hosts(self, daemon):
        """A remote that is itself a hub keeps its own hosts to itself: the stream a hub holds
        through the bridge gets this machine's instance and job frames, but no `hosts` in its
        snapshot and no host frame, while a stream of this machine's own still gets both."""
        assert (
            _req(daemon, "/api/v1/hosts", {"op": "add", "pairing": PAIRING}, bearer=True)[0] == 200
        )
        via_hub, own = _stream(daemon, DELEGATED), _stream(daemon)
        try:
            ((_, hub_snapshot),) = _frames(via_hub, "snapshot")
            ((_, own_snapshot),) = _frames(own, "snapshot")
            daemon.manager.resync()  # the host's link state and last-known instances
            daemon.hub.publish("fast", "acme", None, None)
            daemon.hub.publish_job({"id": "j1", "state": "running"})
            hub_seen = [event for event, _data in _frames(via_hub, "job")]
            own_seen = [event for event, _data in _frames(own, "job")]
        finally:
            via_hub.close()
            own.close()
        assert "hosts" not in hub_snapshot and {"instances", "jobs"} <= set(hub_snapshot)
        assert [h["id"] for h in own_snapshot["hosts"]] == ["build-box"]
        assert hub_seen == ["delta", "job"]
        assert own_seen == ["host_link", "host_snapshot", "delta", "job"]

    def test_the_stream_snapshot_carries_the_hosts(self, daemon):
        req = urllib.request.Request(
            daemon.base + "/api/v1/stream", headers={"Authorization": f"Bearer {TOKEN}"}
        )
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
        status, body, headers = _req(
            daemon,
            "/api/v1/hosts/build-box/instances?x=1",
            cookie=cookie,
            headers={"X-Cwcli-Delegated-Role": "viewer"},
        )
        assert status == 200 and body == {"data": {"ok": True}}
        (sent,) = daemon.forwarded
        assert (
            sent["method"] == "GET"
            and sent["target"] == "/api/v1/instances?x=1"
            and sent["host"] == "build-box"
        )
        # The hub builds the headers itself: the signed-in user and the SESSION's role, not
        # what the browser claimed, and nothing else the browser sent.
        assert (
            sent["headers"]["X-Cwcli-Delegated-User"] == "admin"
            and sent["headers"]["X-Cwcli-Delegated-Role"] == "admin"
        )
        assert "Cookie" not in sent["headers"] and "Authorization" not in sent["headers"]
        assert (
            headers["X-Content-Type-Options"] == "nosniff"
            and headers["Content-Security-Policy"] == "default-src 'none'; sandbox"
        )
        assert headers.get("Set-Cookie") is None and headers["Cache-Control"] == "no-store"

    def test_a_post_is_forwarded_whole_and_the_hub_applies_its_own_least_role_first(self, daemon):
        daemon.views["build-box"] = _connected()
        payload = {"verb": "rm", "target": {"project": "p"}, "consent": True}
        status, body, _ = _req(
            daemon,
            "/api/v1/hosts/build-box/jobs",
            payload,
            bearer=True,
            headers={**DELEGATED, "X-Cwcli-Delegated-Role": "operator"},
        )
        assert status == 403 and _code(body) == "auth.role_forbidden" and daemon.forwarded == []
        status, body, _ = _req(daemon, "/api/v1/hosts/build-box/jobs", payload, bearer=True)
        assert status == 200
        (sent,) = daemon.forwarded
        assert sent["method"] == "POST" and json.loads(sent["body"]) == payload
        assert (
            sent["headers"]["Content-Type"] == "application/json"
            and sent["headers"]["X-Cwcli-Delegated-Role"] == "admin"
        )
        assert sent["headers"]["X-Cwcli-Delegated-User"] == "api"

    def test_a_user_whose_name_is_not_ascii_still_reaches_the_remote(self, daemon, monkeypatch):
        """The bridge refuses any byte outside printable ASCII, so the hub sends the name
        escaped; the bridge then files it as ``hub@<hub>``, as before."""
        daemon.views["build-box"] = _connected()
        monkeypatch.setattr(serve_cmd._Handler, "_account_name", lambda self: "jos\u00e9")
        status, _b, _ = _req(daemon, "/api/v1/hosts/build-box/instances", bearer=True)
        assert status == 200
        (sent,) = daemon.forwarded
        assert sent["headers"]["X-Cwcli-Delegated-User"] == "jos\\xe9"

    def test_a_folded_content_type_from_the_browser_never_reaches_the_remote(self, daemon):
        """The browser's Content-Type is parsed with its folded continuation kept inside
        the value; forwarded as is, the bridge would read that continuation as a
        delegation header of its own. The hub sends the one type it admits instead."""
        daemon.views["build-box"] = _connected()
        folded = {
            "Content-Type": "application/json;\r\n X-Cwcli-Delegated-Role: admin",
            **DELEGATED,
            "X-Cwcli-Delegated-Role": "operator",
        }
        status, _b, _ = _req(
            daemon,
            "/api/v1/hosts/build-box/jobs",
            {"verb": "start", "target": {"project": "p"}},
            bearer=True,
            headers=folded,
        )
        assert status == 200
        (sent,) = daemon.forwarded
        assert sent["headers"]["Content-Type"] == "application/json"
        assert sent["headers"]["X-Cwcli-Delegated-Role"] == "operator"
        assert "admin" not in json.dumps(sent["headers"])

    @pytest.mark.parametrize(
        "state",
        [
            core_hosts.UNREACHABLE,
            core_hosts.AUTH_REFUSED,
            core_hosts.HOST_KEY_CHANGED,
            core_hosts.DAEMON_DOWN,
            core_hosts.CONNECTING,
            core_hosts.TRANSPORT_OFF,
        ],
    )
    def test_a_read_of_a_host_that_is_not_readable_never_dials(self, daemon, state):
        daemon.views["build-box"] = _connected(state=state, reason="because")
        status, body, _ = _req(daemon, "/api/v1/hosts/build-box/instances", bearer=True)
        assert status == 409 and _code(body) == "host.not_connected" and daemon.forwarded == []

    @pytest.mark.parametrize(
        "status, title",
        [(401, "auth.required"), (403, "auth.password_change_required")],
    )
    def test_a_remote_cannot_end_the_hubs_own_session(self, daemon, status, title):
        """The page reads a 401, or a 403 asking for a password change, as the end of ITS
        session with this hub; a remote answering either becomes a 502 naming the host."""
        daemon.views["build-box"] = _connected()
        body = json.dumps({"errors": [{"title": title, "message": "x"}]}).encode()
        daemon.answers.append(_FakeResponse(status=status, body=body))
        got, answer, _ = _req(daemon, "/api/v1/hosts/build-box/instances", bearer=True)
        assert (
            got == 502
            and _code(answer) == "host.refused"
            and answer["errors"][0]["host"] == "build-box"
        )

    def test_a_remote_role_refusal_is_relayed_as_its_own_answer(self, daemon):
        daemon.views["build-box"] = _connected()
        body = json.dumps(
            {"errors": [{"title": "auth.role_forbidden", "role": "operator"}]}
        ).encode()
        daemon.answers.append(_FakeResponse(status=403, body=body))
        got, answer, headers = _req(
            daemon, "/api/v1/hosts/build-box/settings", {"setting": "tips.enable"}, bearer=True
        )
        assert got == 403 and answer["errors"][0]["role"] == "operator"
        assert headers["Content-Security-Policy"] == "default-src 'none'; sandbox"

    @pytest.mark.parametrize(
        "state",
        [
            core_hosts.VERSION_MISMATCH,
            core_hosts.UNREACHABLE,
            core_hosts.CONNECTING,
            core_hosts.DAEMON_DOWN,
        ],
    )
    def test_an_action_needs_the_host_connected_on_an_equal_protocol(self, daemon, state):
        daemon.views["build-box"] = _connected(state=state, reason="because")
        status, body, _ = _req(
            daemon,
            "/api/v1/hosts/build-box/jobs",
            {"verb": "start", "target": {"project": "p"}},
            bearer=True,
        )
        assert status == 409
        assert _code(body) == (
            "host.version_mismatch"
            if state == core_hosts.VERSION_MISMATCH
            else "host.not_connected"
        )
        assert daemon.forwarded == []

    def test_a_read_is_still_attempted_on_a_version_mismatch(self, daemon):
        daemon.views["build-box"] = _connected(state=core_hosts.VERSION_MISMATCH)
        assert _req(daemon, "/api/v1/hosts/build-box/instances", bearer=True)[0] == 200

    @pytest.mark.parametrize(
        "inner",
        ["session", "session/password", "stream", "hosts", "hosts/x/instances", "backups/upload"],
    )
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
        assert (
            status == 502
            and _code(body) == "host.auth_refused"
            and body["errors"][0]["state"] == "auth_refused"
        )

    def test_a_remote_answering_a_page_is_relayed_as_an_attachment_never_rendered(self, daemon):
        daemon.views["build-box"] = _connected()
        daemon.answers.append(
            _FakeResponse(
                body=b"<script>alert(1)</script>",
                headers=[
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Content-Disposition", 'inline; filename="../x.html"'),
                ],
            )
        )
        status, body, headers = _req(daemon, "/api/v1/hosts/build-box/instances", bearer=True)
        assert status == 200 and body == b"<script>alert(1)</script>"
        assert headers["Content-Type"] == "application/octet-stream"
        assert headers["Content-Disposition"] == 'attachment; filename="download"'
        assert (
            headers["Content-Security-Policy"] == "default-src 'none'; sandbox"
            and headers["X-Content-Type-Options"] == "nosniff"
        )

    def test_a_download_keeps_a_safe_filename_and_an_image_gets_the_logo_policy(self, daemon):
        daemon.views["build-box"] = _connected()
        daemon.answers.append(
            _FakeResponse(
                body=b"tar",
                headers=[
                    ("Content-Type", "application/x-tar"),
                    ("Content-Disposition", 'attachment; filename="site_2026.tar"'),
                ],
            )
        )
        _s, _b, headers = _req(
            daemon, "/api/v1/hosts/build-box/backups/download?key=k", bearer=True
        )
        assert (
            headers["Content-Disposition"] == 'attachment; filename="site_2026.tar"'
            and headers["Content-Type"] == "application/x-tar"
        )
        daemon.answers.append(
            _FakeResponse(body=b"<svg/>", headers=[("Content-Type", "image/svg+xml")])
        )
        _s, _b, headers = _req(daemon, "/api/v1/hosts/build-box/apps/logo/x", bearer=True)
        assert (
            headers["Content-Type"] == "image/svg+xml"
            and headers["Content-Security-Policy"]
            == "default-src 'none'; style-src 'unsafe-inline'; sandbox"
        )

    def test_the_forward_is_same_origin_only_and_a_viewer_may_read_but_not_act(self, daemon):
        daemon.views["build-box"] = _connected()
        status, body, _ = _req(
            daemon,
            "/api/v1/hosts/build-box/instances",
            bearer=True,
            headers={"Origin": "http://evil.example"},
        )
        assert status == 403 and _code(body) == "action.origin_forbidden"
        assert (
            _req(
                daemon,
                "/api/v1/hosts/build-box/instances",
                bearer=True,
                headers=self._as_viewer(daemon),
            )[0]
            == 200
        )
        status, body, _ = _req(
            daemon,
            "/api/v1/hosts/build-box/actions",
            {"action": "stop_instance", "project": "p"},
            bearer=True,
            headers=self._as_viewer(daemon),
        )
        assert status == 403 and _code(body) == "auth.role_forbidden"
        status, body, _ = _req(
            daemon,
            "/api/v1/hosts/build-box/integrations",
            bearer=True,
            headers={**DELEGATED, "X-Cwcli-Delegated-Role": "operator"},
        )
        assert status == 403

    def test_options_knows_the_forwarded_post_paths(self, daemon):
        assert serve_cmd._is_post_path("/api/v1/hosts/build-box/jobs")
        assert serve_cmd._is_post_path("/api/v1/hosts/build-box/jobs/abc/interrupt")
        assert not serve_cmd._is_post_path("/api/v1/hosts/build-box/instances")
        assert serve_cmd._host_route("/api/v1/hosts/build-box") is None
        assert serve_cmd._host_route("/api/v1/hosts/b%20x/instances/p") == ("b x", "instances/p")
