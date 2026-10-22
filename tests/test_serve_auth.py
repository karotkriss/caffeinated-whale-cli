"""``cwcli serve`` bind and authentication, driven against a REAL bound server.

This file is the permanent form of a reproduction. The shipped daemon defaulted
to ``0.0.0.0`` and its only action guard was a same-origin check, so the endpoint
that stops and restarts instances was drivable, with no credentials, from any
host that could route to this machine. That was verified end to end (HTTP 200,
and the core action really ran) before it was fixed, and the three tests in
``TestTheLanActionExposure`` keep the trigger, the masking condition and the
visible consequence separated so a regression in any one of them is named.

No Docker: ``core.list_instances`` and the action's core call are stubbed, but
the socket, the bind address, the handler and the auth guard are the shipped
ones - the parts a mocked handler would never catch.

``TestS1NoCorsAnywhere`` and ``TestS2HostAllowlist`` are the permanent forms of
two later reproductions against a live loopback daemon: every website could read
the fleet through ``Access-Control-Allow-Origin: *``, and a DNS-rebinding request
(``Host`` and ``Origin`` both the attacker's name) reached action dispatch.
"""

from __future__ import annotations

import http.client
import json
import re
import secrets
import socket
import threading
import urllib.error
import urllib.request

import pytest
import typer

from caffeinated_whale_cli import __version__
from caffeinated_whale_cli.commands import serve as serve_cmd
from caffeinated_whale_cli.core import fleet as core_fleet
from caffeinated_whale_cli.core import inspect as core_inspect
from caffeinated_whale_cli.core.envelope import Result, Status
from caffeinated_whale_cli.core.inspect import BenchInfo, InspectReport, SiteInfo
from caffeinated_whale_cli.core.list import InstanceDTO
from caffeinated_whale_cli.core.stop import StopOutcome

_TIMEOUT = 5.0
_TOKEN = "s3cr3t-token-value"
_LOGIN = "one-time-login-code"
_ACTION = json.dumps({"action": "stop_instance", "project": "p"}).encode()


@pytest.fixture
def stopped(monkeypatch):
    """Record every project ``core.stop`` was actually asked to stop.

    The list is the honest measure of the exposure: a 200 that never reached the
    core would be a different (and lesser) bug than one that stopped an instance.
    """
    calls: list[str] = []
    monkeypatch.setattr(
        core_fleet,
        "list_instances",
        lambda: Result(
            status=Status.OK,
            data=[InstanceDTO(project_name="p", status="running", ports=["8000"])],
        ),
    )

    def _stop(project):
        calls.append(project)
        return Result(
            status=Status.OK,
            data=StopOutcome(
                project=project, stopped=1, already_stopped=False, containers=["frappe"]
            ),
        )

    monkeypatch.setattr(serve_cmd.core_stop, "stop", _stop)
    return calls


def _serve_on(host: str, token: str):
    """A live shipped daemon on ``host`` and an ephemeral port. Returns (port, shutdown)."""
    fleet = core_fleet.Fleet()
    hub = serve_cmd._Hub(fleet.set_focus)
    fleet.set_publish(hub.publish)
    fleet.bootstrap()
    httpd = serve_cmd.make_server(host, 0, fleet, hub, token, _LOGIN)
    threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    ).start()

    def _shutdown():
        httpd.shutdown()
        httpd.server_close()

    return httpd.server_address[1], _shutdown


@pytest.fixture
def daemon(stopped):
    """The shipped daemon on its shipped DEFAULT_HOST with a per-launch token, as
    ``serve`` runs it when no ``CWCLI_SERVE_TOKEN`` is set: the token is random and
    unknown to the caller, so the login code is the only way in."""
    port, shutdown = _serve_on(serve_cmd.DEFAULT_HOST, secrets.token_urlsafe(32))
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        shutdown()


@pytest.fixture
def guarded(stopped):
    """The shipped daemon with a token configured, reachable over loopback."""
    port, shutdown = _serve_on("127.0.0.1", _TOKEN)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        shutdown()


def _request(url, *, data=None, headers=None, method=None):
    """Return (status, body-bytes, response-headers), never raising on 4xx/5xx."""
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310 - loopback
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def _post_action(base, headers=None):
    merged = {"Content-Type": "application/json"}
    merged.update(headers or {})
    return _request(base + "/api/action", data=_ACTION, headers=merged, method="POST")


def _stub_server(monkeypatch, resolves_to: str | None = None) -> list:
    """Replace ``make_server`` with one that binds nothing and returns immediately.

    ``serve()`` runs to completion through its banner and teardown, so the parts
    after the bind stay covered; the returned list records the call's argv. Like
    a real socket, it reports the address it was asked to bind, or
    ``resolves_to`` for a hostname bind.
    """
    calls: list = []

    class _Stub:
        def __init__(self, host):
            self.server_address = (resolves_to or host or "0.0.0.0", 8765)  # noqa: S104

        def serve_forever(self):
            raise KeyboardInterrupt

        def shutdown(self):
            pass

        def server_close(self):
            pass

    def _make(*args, **kwargs):
        calls.append(args)
        return _Stub(args[0])

    monkeypatch.setattr(serve_cmd, "make_server", _make)
    return calls


class TestTheLanActionExposure:
    """The reproduction, split into the three things that made it a security bug."""

    def test_trigger_the_default_bind_is_not_reachable_from_off_this_machine(self, daemon):
        """TRIGGER: a request arriving over this host's ROUTABLE address.

        The exposure needed the listener to accept a connection from somewhere
        other than loopback, and the shipped ``0.0.0.0`` default gave it one on
        every interface. This is the closest safe network-boundary check
        available without another machine: the daemon is bound on the real
        ``DEFAULT_HOST`` and dialled on this host's own outbound address, which
        is precisely the address the original probe used from Windows.
        """
        routable = serve_cmd._outbound_address()
        if routable is None:
            pytest.skip("no non-loopback address on this host to dial")
        port = int(daemon.rsplit(":", 1)[1])
        with socket.socket() as s:
            s.settimeout(_TIMEOUT)
            with pytest.raises(OSError):
                s.connect((routable, port))

    def test_masking_condition_the_same_origin_guard_admits_a_headerless_client(self, daemon):
        """MASKING CONDITION: the CSRF guard is silent for non-browser clients.

        ``_action_same_origin`` returns True when a request carries neither
        ``Origin`` nor ``Sec-Fetch-Site``, which is exactly what ``curl`` sends.
        That is CORRECT for a CSRF guard - it defends browsers, and there is no
        browser here - and it is deliberately unchanged. Pinned so nobody
        "fixes" the exposure by breaking the guard's real job instead of adding
        the authentication that was actually missing.
        """
        assert serve_cmd._Handler._action_same_origin(
            type("H", (), {"headers": {}})()
        ), "the CSRF guard must keep admitting headerless clients; auth is what gates them"

    def test_visible_consequence_a_credential_less_action_no_longer_runs(self, guarded, stopped):
        """VISIBLE CONSEQUENCE: HTTP 200 and an instance genuinely stopped.

        The original probe sent no ``Origin``, no ``Sec-Fetch-Site`` and no
        credentials and got ``200 {"ok": true, "action": "stop_instance"}`` with
        ``core.stop`` really invoked. With authentication in force that request
        is refused before dispatch, and the core is never reached.
        """
        status, body, headers = _post_action(guarded)
        assert status == 401
        assert json.loads(body)["error"]["code"] == "auth.required"
        assert headers.get("WWW-Authenticate", "").startswith("Bearer")
        assert stopped == [], "an unauthenticated action must not reach the core"


class TestDefaultBind:
    def test_the_default_host_is_loopback(self):
        assert serve_cmd.is_loopback(serve_cmd.DEFAULT_HOST)

    @pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.53", "::1", "[::1]"])
    def test_loopback_addresses_are_recognised(self, host):
        assert serve_cmd.is_loopback(host)

    @pytest.mark.parametrize(
        "host",
        [
            "0.0.0.0",
            "",
            "::",
            "192.168.1.10",
            "example.internal",
            "localhost",
            "localhost.",
        ],
    )
    def test_everything_else_fails_closed(self, host):
        """A name that is not an IP literal could resolve anywhere, so it needs a token."""
        assert not serve_cmd.is_loopback(host)


class TestRemoteBindRefusesWithoutAuth:
    @pytest.mark.parametrize("host", ["0.0.0.0", "localhost", "localhost."])
    def test_it_refuses_before_anything_is_served(self, monkeypatch, host):
        """The refusal must beat the bind, not tear one down afterwards."""
        monkeypatch.delenv(serve_cmd.TOKEN_ENV, raising=False)
        monkeypatch.setattr(
            serve_cmd,
            "make_server",
            lambda *a, **k: pytest.fail("bound a remote listener without authentication"),
        )
        monkeypatch.setattr(
            core_fleet,
            "list_instances",
            lambda: pytest.fail("probed Docker before refusing the bind"),
        )
        with pytest.raises(typer.Exit) as excinfo:
            serve_cmd.serve(port=0, host=host, interval=2.5)
        assert excinfo.value.exit_code == 2

    def test_a_configured_token_lets_the_remote_bind_through(self, monkeypatch, stopped):
        monkeypatch.setenv(serve_cmd.TOKEN_ENV, _TOKEN)
        bound = _stub_server(monkeypatch)
        serve_cmd.serve(port=0, host="0.0.0.0", interval=2.5)
        assert bound and bound[0][4] == _TOKEN, "an authenticated remote bind must be allowed"

    def test_loopback_needs_no_configured_token_but_still_gets_one(self, monkeypatch, stopped):
        monkeypatch.delenv(serve_cmd.TOKEN_ENV, raising=False)
        bound = _stub_server(monkeypatch)
        serve_cmd.serve(port=0, host=serve_cmd.DEFAULT_HOST, interval=2.5)
        assert bound, "the ordinary loopback workflow must not require CWCLI_SERVE_TOKEN"
        token, login_code = bound[0][4], bound[0][5]
        assert isinstance(token, str) and len(token) >= 32, "auth is never off"
        assert isinstance(login_code, str) and len(login_code) >= 32
        assert token != login_code


class TestAuthIsAlwaysOn:
    """Loopback used to mean no authentication at all; now every read and action needs it."""

    def test_the_loopback_default_refuses_credential_less_reads_and_actions(self, daemon, stopped):
        for path in ("/api/snapshot", "/api/events", "/api/instance/p/detail", "/api/doctor"):
            assert _request(daemon + path)[0] == 401, path
        status, body, _ = _post_action(daemon)
        assert status == 401
        assert json.loads(body)["error"]["code"] == "auth.required"
        assert stopped == [], "an unauthenticated action must not reach the core"


class TestAuthEnabled:
    def test_a_valid_bearer_token_succeeds(self, guarded, stopped):
        status, body, _ = _post_action(guarded, {"Authorization": f"Bearer {_TOKEN}"})
        assert status == 200
        assert json.loads(body)["ok"] is True
        assert stopped == ["p"]

    @pytest.mark.parametrize(
        "header",
        [None, "", "Bearer ", "Bearer wrong-token", f"Basic {_TOKEN}", _TOKEN],
    )
    def test_every_other_credential_shape_is_refused(self, guarded, stopped, header):
        headers = {} if header is None else {"Authorization": header}
        assert _post_action(guarded, headers)[0] == 401
        assert stopped == []

    @pytest.mark.parametrize(
        "path",
        [
            "/api/snapshot",
            "/api/events",
            "/api/instance/p/detail",
            "/api/instance/p/logs",
            "/api/where?q=p",
            "/nope",
        ],
    )
    def test_every_api_read_is_gated(self, guarded, path):
        assert _request(guarded + path)[0] == 401

    def test_the_console_page_itself_stays_open(self, guarded):
        """A gated page would leave a browser with no way to reach the prompt."""
        status, body, _ = _request(guarded + "/")
        assert status == 200
        assert b"<html" in body.lower()
        assert _TOKEN.encode() not in body

    def test_options_is_the_no_data_no_action_auth_exception(self, guarded, stopped):
        status, body, headers = _request(
            guarded + "/api/action",
            headers={
                "Origin": "https://example.invalid",
                "Access-Control-Request-Method": "POST",
            },
            method="OPTIONS",
        )
        assert status == 204
        assert body == b""
        assert headers.get("Access-Control-Allow-Origin") is None
        assert stopped == []


class TestBrowserSession:
    """``EventSource`` cannot send headers, so a browser trades the token for a cookie."""

    def test_the_authentication_dialog_cannot_be_dismissed_with_escape(self):
        page = serve_cmd.CONSOLE_PAGE
        cancel_handler = page.split('modal.addEventListener("cancel"', 1)[1].split(
            "});", 1
        )[0]
        assert "event.preventDefault()" in cancel_handler
        assert "{dismissible: false}" in page.split("function askForToken", 1)[1]

    def test_a_whitespace_token_keeps_the_authentication_dialog_open(self):
        page = serve_cmd.CONSOLE_PAGE
        submit_handler = page.split(
            'el("modal-form").addEventListener("submit"', 1
        )[1].split("});", 1)[0]
        auth_handler = page.split("function askForToken", 1)[1].split(
            "function openTokenSession", 1
        )[0]
        assert "submit(values) === false" in submit_handler
        assert "event.preventDefault()" in submit_handler
        assert "input.setCustomValidity" in auth_handler
        assert "return false" in auth_handler

    def test_the_session_cookie_authenticates_subsequent_requests(self, guarded, stopped):
        status, _, headers = _request(
            guarded + "/api/session",
            data=b"{}",
            headers={"Authorization": f"Bearer {_TOKEN}", "Content-Type": "application/json"},
            method="POST",
        )
        assert status == 204
        cookie = headers["Set-Cookie"]
        assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
        assert _TOKEN not in cookie, "the cookie must carry a session id, never the token"

        jar = cookie.split(";", 1)[0]
        assert _request(guarded + "/api/snapshot", headers={"Cookie": jar})[0] == 200
        assert _post_action(guarded, {"Cookie": jar})[0] == 200
        assert stopped == ["p"]

    def test_an_unauthenticated_caller_cannot_open_a_session(self, guarded):
        status, _, headers = _request(
            guarded + "/api/session",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        assert status == 401
        assert "Set-Cookie" not in headers

    def test_a_forged_session_cookie_is_refused(self, guarded, stopped):
        port = guarded.rsplit(":", 1)[1]
        forged = f"{serve_cmd.SESSION_COOKIE}_{port}=not-the-session-id"
        assert _post_action(guarded, {"Cookie": forged})[0] == 401
        assert stopped == []

    def test_a_malformed_cookie_header_is_refused_not_crashed(self, guarded):
        assert _post_action(guarded, {"Cookie": "=;;;garbage"})[0] == 401


class TestKeepAliveSurvivesEveryPostOutcome:
    """Every POST outcome must consume the request body before it answers.

    HTTP/1.1 keep-alive: a guard that replies without reading the body leaves
    those bytes in the socket, so the NEXT request on that connection is parsed
    starting mid-body and comes back as a bogus ``501 Unsupported method``. Found
    in a real browser on the ``/api/session`` 204, and the pre-existing 403 and
    415 replies had it too, so the drain lives once at the top of ``do_POST``.
    """

    @pytest.mark.parametrize(
        ("headers", "expected"),
        [
            ({}, 401),
            ({"Authorization": f"Bearer {_TOKEN}", "Sec-Fetch-Site": "cross-site"}, 403),
            ({"Authorization": f"Bearer {_TOKEN}", "Content-Type": "text/plain"}, 415),
        ],
    )
    def test_the_next_request_on_the_same_connection_is_answered(self, guarded, headers, expected):
        host, port = guarded.removeprefix("http://").split(":")
        conn = http.client.HTTPConnection(host, int(port), timeout=_TIMEOUT)
        try:
            merged = {"Content-Type": "application/json"}
            merged.update(headers)
            conn.request("POST", "/api/action", body=_ACTION, headers=merged)
            assert conn.getresponse().read() is not None
            assert conn.sock is not None, "the guard closed a keep-alive connection"

            # Connection: close so the server ends the exchange, rather than the
            # test resetting a live keep-alive socket and logging a stock traceback.
            conn.request("GET", "/", headers={"Connection": "close"})
            assert conn.getresponse().status == 200, "body was left unread in the socket"
        finally:
            conn.close()

    def test_the_session_204_leaves_the_connection_usable(self, guarded):
        host, port = guarded.removeprefix("http://").split(":")
        conn = http.client.HTTPConnection(host, int(port), timeout=_TIMEOUT)
        try:
            conn.request(
                "POST",
                "/api/session",
                body=b"{}",
                headers={
                    "Authorization": f"Bearer {_TOKEN}",
                    "Content-Type": "application/json",
                },
            )
            first = conn.getresponse()
            assert first.status == 204
            jar = first.getheader("Set-Cookie").split(";", 1)[0]
            first.read()

            conn.request("GET", "/api/snapshot", headers={"Cookie": jar, "Connection": "close"})
            assert conn.getresponse().status == 200
        finally:
            conn.close()


class TestSameOriginGuardSurvives:
    """Bearer auth is added ALONGSIDE the CSRF guard, never in place of it."""

    def test_a_cross_origin_action_is_refused_even_with_a_valid_token(self, guarded, stopped):
        status, body, _ = _post_action(
            guarded,
            {
                "Authorization": f"Bearer {_TOKEN}",
                "Origin": "http://evil.example",
                "Sec-Fetch-Site": "cross-site",
            },
        )
        assert status == 403
        assert json.loads(body)["error"]["code"] == "action.origin_forbidden"
        assert stopped == []

    def test_the_sensitive_reads_keep_their_guard_too(self, guarded):
        for path in ("/api/instance/p/logs", "/api/where?q=p"):
            status, body, _ = _request(
                guarded + path,
                headers={
                    "Authorization": f"Bearer {_TOKEN}",
                    "Origin": "http://evil.example",
                    "Sec-Fetch-Site": "cross-site",
                },
            )
            assert status == 403, path


class TestTheSecretNeverLeaks:
    def test_no_response_repeats_the_token_or_what_was_presented(self, guarded):
        for headers in ({}, {"Authorization": "Bearer wrong-token"}):
            status, body, response_headers = _post_action(guarded, headers)
            assert status == 401
            blob = body + json.dumps(response_headers).encode()
            assert _TOKEN.encode() not in blob
            assert b"wrong-token" not in blob

    def test_the_token_is_read_from_the_environment_and_has_no_flag(self, monkeypatch):
        """A ``--token`` flag would put the secret in argv, readable by any process."""
        import inspect

        params = inspect.signature(serve_cmd.serve).parameters
        assert "token" not in params
        for param in params.values():
            assert "--token" not in (getattr(param.default, "param_decls", ()) or ())

        monkeypatch.setenv(serve_cmd.TOKEN_ENV, "  spaced-token  ")
        assert serve_cmd.auth_token() == "spaced-token"
        monkeypatch.setenv(serve_cmd.TOKEN_ENV, "   ")
        assert serve_cmd.auth_token() is None
        monkeypatch.delenv(serve_cmd.TOKEN_ENV)
        assert serve_cmd.auth_token() is None

    def test_the_startup_banner_reports_auth_without_printing_it(self, monkeypatch, capsys):
        monkeypatch.setenv(serve_cmd.TOKEN_ENV, _TOKEN)
        monkeypatch.setattr(core_fleet, "list_instances", lambda: Result(status=Status.OK, data=[]))
        _stub_server(monkeypatch)
        serve_cmd.serve(port=0, host="127.0.0.1", interval=2.5)
        printed = capsys.readouterr()
        assert "auth" in printed.out
        assert _TOKEN not in printed.out + printed.err


def _detail_report():
    """A detail read carrying exactly what S1 leaked: a bench path, a site, an app branch."""
    return InspectReport(
        project="p",
        served_from="cache",
        degraded=False,
        benches=[
            BenchInfo(
                index=0,
                path="/workspace/frappe-bench",
                label=None,
                current_site=None,
                default_site=None,
                available_apps=["frappe"],
                sites=[
                    SiteInfo(
                        name="a.localhost",
                        installed_apps=["frappe 16.28.0 fm/in-flight-branch"],
                        installed_apps_verified=False,
                        has_site_config=True,
                    )
                ],
            )
        ],
    )


def _headers_of(base, method, path, headers):
    """Status and headers only, for any method; an SSE body is never read to the end."""
    host, port = base.removeprefix("http://").split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=_TIMEOUT)
    try:
        conn.request(method, path, headers={"Connection": "close", **headers})
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders())
    finally:
        conn.close()


class TestS1NoCorsAnywhere:
    """S1: snapshot, events and detail answered EVERY origin with ACAO ``*``.

    So any website open in the browser while the daemon ran could read every
    bench path, site name and app branch. A browser only hands a cross-origin
    page the response when an ``Access-Control-Allow-Origin`` header permits it,
    so the fix is that no response carries any ``Access-Control-*`` header.
    """

    _FOREIGN = {"Origin": "https://evil.example"}

    def test_the_three_leaking_reads_no_longer_grant_a_foreign_origin(self, guarded, monkeypatch):
        monkeypatch.setattr(
            core_inspect, "inspect", lambda p, **kw: Result(status=Status.OK, data=_detail_report())
        )
        auth = {"Authorization": f"Bearer {_TOKEN}", **self._FOREIGN}
        for path in ("/api/snapshot", "/api/events", "/api/instance/p/detail"):
            status, headers = _headers_of(guarded, "GET", path, auth)
            assert status == 200, path
            leaked = [h for h in headers if h.lower().startswith("access-control-")]
            assert leaked == [], f"{path} still grants cross-origin reads: {leaked}"

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/"),
            ("GET", "/api/v1/meta"),
            ("GET", "/api/snapshot"),
            ("GET", "/api/instance/p/detail"),
            ("GET", "/nope"),
            ("OPTIONS", "/api/snapshot"),
            ("OPTIONS", "/api/action"),
            ("POST", "/api/session"),
            ("POST", "/api/action"),
        ],
    )
    def test_no_response_of_any_kind_carries_a_cors_header(self, daemon, method, path):
        status, headers = _headers_of(
            daemon, method, path, {**self._FOREIGN, "Access-Control-Request-Method": "GET"}
        )
        assert status < 500
        assert not [h for h in headers if h.lower().startswith("access-control-")]


class TestS2HostAllowlist:
    """S2: the action guard compared ``Origin`` with ``Host``, which a rebinding page passes.

    The page lives at ``rebind.evil.example``, whose DNS later answers
    127.0.0.1, so both headers carry the attacker's name and the browser even
    labels the request ``same-origin``. Only ``Host`` still tells them apart.
    """

    _REBIND = {
        "Host": "rebind.evil.example:18765",
        "Origin": "http://rebind.evil.example:18765",
        "Sec-Fetch-Site": "same-origin",
    }

    def test_the_rebinding_shaped_action_is_refused_before_dispatch(self, guarded, stopped):
        """Even carrying a VALID token: the Host guard runs before every other one."""
        status, body, _ = _post_action(
            guarded, {**self._REBIND, "Authorization": f"Bearer {_TOKEN}"}
        )
        assert status == 403
        assert json.loads(body)["error"]["code"] == "host.forbidden"
        assert stopped == [], "a rebinding request must not reach the core"

    @pytest.mark.parametrize("path", ["/", "/api/v1/meta", "/api/snapshot", "/api/events"])
    def test_every_read_including_the_static_page_is_refused_too(self, guarded, path):
        status, _ = _headers_of(
            guarded, "GET", path, {**self._REBIND, "Authorization": f"Bearer {_TOKEN}"}
        )
        assert status == 403, path

    def test_the_session_exchange_is_refused_and_the_code_survives(self, daemon):
        status, body, headers = _request(
            daemon + "/api/session",
            data=json.dumps({"code": _LOGIN}).encode(),
            headers={"Content-Type": "application/json", **self._REBIND},
            method="POST",
        )
        assert status == 403 and "Set-Cookie" not in headers
        assert _redeem(daemon)[0] == 204, "a refused request must not burn the login code"

    @pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
    def test_the_loopback_names_are_admitted(self, guarded, stopped, host):
        port = guarded.rsplit(":", 1)[1]
        status, _, _ = _post_action(
            guarded, {"Host": f"{host}:{port}", "Authorization": f"Bearer {_TOKEN}"}
        )
        assert status == 200
        assert stopped == ["p"]

    @pytest.mark.parametrize(
        "host",
        [
            "localhost",
            "localhost:8765",
            "LOCALHOST:8765",
            "127.0.0.1",
            "127.0.0.1:8765",
            "127.1.2.3:8765",
            "[::1]",
            "[::1]:8765",
        ],
    )
    def test_host_allowed_admits_loopback(self, host):
        assert serve_cmd.host_allowed(host, remote_bind=False)
        assert serve_cmd.host_allowed(host, remote_bind=True)

    @pytest.mark.parametrize(
        "host",
        [
            None,
            "",
            "rebind.evil.example:8765",
            "localhost.evil.example",
            "127.0.0.1.evil.example",
            "evil.localhost",
            "localhost.",
            "127.0.0.1@evil.example",
            "user@127.0.0.1",
            "[::1",
            "[::1]evil",
            "::1",
        ],
    )
    def test_host_allowed_refuses_every_name_and_malformed_header(self, host):
        assert not serve_cmd.host_allowed(host, remote_bind=False)
        assert not serve_cmd.host_allowed(host, remote_bind=True)

    @pytest.mark.parametrize("host", ["192.168.1.10:8765", "172.24.87.141", "[fe80::1]:8765"])
    def test_an_ip_literal_is_admitted_only_on_a_remote_bind(self, host):
        """How a browser on another machine addresses a token-gated ``0.0.0.0`` bind;
        an IP literal cannot be rebound, so it carries no rebinding risk."""
        assert not serve_cmd.host_allowed(host, remote_bind=False)
        assert serve_cmd.host_allowed(host, remote_bind=True)

    def test_a_loopback_bind_refusal_points_at_loopback(self, guarded):
        status, body, _ = _request(guarded + "/api/v1/meta", headers=self._REBIND)
        assert status == 403
        assert "http://127.0.0.1:<port>/" in json.loads(body)["error"]["hint"]

    def test_a_remote_bind_refusal_points_at_the_machine_ip_not_loopback(self, stopped):
        port, shutdown = _serve_on("0.0.0.0", _TOKEN)  # noqa: S104 - the remote-bind shape
        try:
            status, body, _ = _request(f"http://127.0.0.1:{port}/api/v1/meta", headers=self._REBIND)
        finally:
            shutdown()
        assert status == 403
        hint = json.loads(body)["error"]["hint"]
        assert "IP address" in hint
        assert "127.0.0.1" not in hint and "localhost" not in hint

    def test_the_bind_address_decides_remote_bind(self, stopped):
        fleet = core_fleet.Fleet()
        httpd = serve_cmd.make_server(
            "127.0.0.1", 0, fleet, serve_cmd._Hub(fleet.set_focus), _TOKEN, _LOGIN
        )
        try:
            assert httpd.RequestHandlerClass.remote_bind is False
        finally:
            httpd.server_close()


def _redeem(base, code=_LOGIN):
    return _request(
        base + "/api/session",
        data=json.dumps({"code": code}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )


class TestOneTimeLoginCode:
    """A browser signs in by redeeming the code the banner printed, exactly once."""

    def test_the_code_buys_a_session_cookie_that_authenticates(self, daemon, stopped):
        status, _, headers = _redeem(daemon)
        assert status == 204
        cookie = headers["Set-Cookie"]
        assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
        assert _LOGIN not in cookie, "the cookie carries a session id, never the code"
        jar = cookie.split(";", 1)[0]
        assert _request(daemon + "/api/snapshot", headers={"Cookie": jar})[0] == 200
        assert _post_action(daemon, {"Cookie": jar})[0] == 200
        assert stopped == ["p"]

    def test_a_second_daemon_on_this_machine_does_not_sign_the_first_out(self, daemon, stopped):
        """Browsers do not scope cookies by port, so one jar holds both daemons'
        cookies for 127.0.0.1 and sends all of them to each."""
        port, shutdown = _serve_on("127.0.0.1", secrets.token_urlsafe(32))
        try:
            jar: dict[str, str] = {}
            for base in (daemon, f"http://127.0.0.1:{port}"):
                name, _, value = _redeem(base)[2]["Set-Cookie"].split(";", 1)[0].partition("=")
                jar[name] = value
            sent = "; ".join(f"{k}={v}" for k, v in jar.items())
            assert _request(daemon + "/api/snapshot", headers={"Cookie": sent})[0] == 200
            assert (
                _request(f"http://127.0.0.1:{port}/api/snapshot", headers={"Cookie": sent})[0]
                == 200
            )
        finally:
            shutdown()

    def test_the_code_works_once(self, daemon):
        assert _redeem(daemon)[0] == 204
        status, body, headers = _redeem(daemon)
        assert status == 401
        assert "Set-Cookie" not in headers
        assert json.loads(body)["error"]["code"] == "auth.required"

    @pytest.mark.parametrize(
        "body",
        [b"{}", b'{"code": "wrong"}', b'{"code": 1}', b'{"code": null}', b"not json", b"[]"],
    )
    def test_anything_but_the_code_is_refused_and_does_not_burn_it(self, daemon, body):
        status, _, headers = _request(
            daemon + "/api/session",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        assert status == 401 and "Set-Cookie" not in headers
        assert _redeem(daemon)[0] == 204

    def test_a_cross_site_redemption_is_refused_and_does_not_burn_it(self, daemon):
        status, _, headers = _request(
            daemon + "/api/session",
            data=json.dumps({"code": _LOGIN}).encode(),
            headers={"Content-Type": "application/json", "Sec-Fetch-Site": "cross-site"},
            method="POST",
        )
        assert status == 403 and "Set-Cookie" not in headers
        assert _redeem(daemon)[0] == 204

    def test_the_banner_prints_the_login_link_and_never_a_token(self, monkeypatch, capsys):
        monkeypatch.setenv(serve_cmd.TOKEN_ENV, _TOKEN)
        monkeypatch.setattr(core_fleet, "list_instances", lambda: Result(status=Status.OK, data=[]))
        bound = _stub_server(monkeypatch)
        serve_cmd.serve(port=0, host="127.0.0.1", interval=2.5)
        out = capsys.readouterr().out
        assert f"http://127.0.0.1:8765/#login={bound[0][5]}" in out
        assert _TOKEN not in out

    def test_an_ipv6_loopback_banner_brackets_the_host(self, monkeypatch, capsys):
        monkeypatch.setenv(serve_cmd.TOKEN_ENV, _TOKEN)
        monkeypatch.setattr(core_fleet, "list_instances", lambda: Result(status=Status.OK, data=[]))
        bound = _stub_server(monkeypatch)
        serve_cmd.serve(port=0, host="::1", interval=2.5)
        out = capsys.readouterr().out
        assert "http://[::1]:8765" in out
        assert f"http://[::1]:8765/#login={bound[0][5]}" in out
        assert "http://::1:" not in out

    @pytest.mark.parametrize(
        ("host", "resolves_to", "link_host"),
        [
            ("0.0.0.0", None, "10.9.9.9"),
            ("", None, "10.9.9.9"),
            ("192.168.1.10", None, "192.168.1.10"),
            ("localhost", "127.0.0.1", "127.0.0.1"),
            ("mybox.local", "192.168.1.20", "192.168.1.20"),
        ],
    )
    def test_the_login_link_names_the_bound_address(
        self, monkeypatch, capsys, host, resolves_to, link_host
    ):
        """Only a wildcard bind is reached via the outbound address; any other bind
        is reached at the address the socket listens on, which the Host allowlist
        admits even when the bind was given as a hostname."""
        monkeypatch.setenv(serve_cmd.TOKEN_ENV, _TOKEN)
        monkeypatch.setattr(core_fleet, "list_instances", lambda: Result(status=Status.OK, data=[]))
        monkeypatch.setattr(serve_cmd, "_outbound_address", lambda: "10.9.9.9")
        bound = _stub_server(monkeypatch, resolves_to)
        serve_cmd.serve(port=0, host=host, interval=2.5)
        out = capsys.readouterr().out
        assert f"http://{link_host}:8765/#login={bound[0][5]}" in out
        remote_bind = not serve_cmd.is_loopback(host)
        assert serve_cmd.host_allowed(f"{link_host}:8765", remote_bind=remote_bind)

    def test_without_a_configured_token_the_random_one_is_not_printed(self, monkeypatch, capsys):
        monkeypatch.delenv(serve_cmd.TOKEN_ENV, raising=False)
        monkeypatch.setattr(core_fleet, "list_instances", lambda: Result(status=Status.OK, data=[]))
        bound = _stub_server(monkeypatch)
        serve_cmd.serve(port=0, host="127.0.0.1", interval=2.5)
        out = capsys.readouterr().out
        assert "per-launch token" in out
        assert re.search(r"#login=" + re.escape(bound[0][5]), out)
        assert bound[0][4] not in out


class TestMeta:
    def test_meta_answers_unauthenticated_with_an_integer_protocol(self, daemon):
        """Open like the static page: it carries no fleet data, and a page checks it
        before it has signed in."""
        status, body, _ = _request(daemon + "/api/v1/meta")
        assert status == 200
        meta = json.loads(body)
        assert meta == {"version": __version__, "protocol": serve_cmd.PROTOCOL_VERSION}
        assert type(meta["protocol"]) is int
