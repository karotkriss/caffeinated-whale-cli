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
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import urllib.error
import urllib.request

import pytest
import typer

from caffeinated_whale_cli.commands import serve as serve_cmd
from caffeinated_whale_cli.core import fleet as core_fleet
from caffeinated_whale_cli.core.envelope import Result, Status
from caffeinated_whale_cli.core.list import InstanceDTO
from caffeinated_whale_cli.core.stop import StopOutcome

_TIMEOUT = 5.0
_TOKEN = "s3cr3t-token-value"
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


def _serve_on(host: str, token: str | None):
    """A live shipped daemon on ``host`` and an ephemeral port. Returns (port, shutdown)."""
    fleet = core_fleet.Fleet()
    hub = serve_cmd._Hub(fleet.set_focus)
    fleet.set_publish(hub.publish)
    fleet.bootstrap()
    httpd = serve_cmd.make_server(host, 0, fleet, hub, token)
    threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    ).start()

    def _shutdown():
        httpd.shutdown()
        httpd.server_close()

    return httpd.server_address[1], _shutdown


@pytest.fixture
def daemon(stopped):
    """The shipped daemon on its shipped DEFAULT_HOST, authentication OFF."""
    port, shutdown = _serve_on(serve_cmd.DEFAULT_HOST, None)
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


def _stub_server(monkeypatch) -> list:
    """Replace ``make_server`` with one that binds nothing and returns immediately.

    ``serve()`` runs to completion through its banner and teardown, so the parts
    after the bind stay covered; the returned list records the call's argv.
    """
    calls: list = []

    class _Stub:
        def serve_forever(self):
            raise KeyboardInterrupt

        def shutdown(self):
            pass

        def server_close(self):
            pass

    def _make(*args, **kwargs):
        calls.append(args)
        return _Stub()

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

    def test_loopback_needs_no_token(self, monkeypatch, stopped):
        monkeypatch.delenv(serve_cmd.TOKEN_ENV, raising=False)
        bound = _stub_server(monkeypatch)
        serve_cmd.serve(port=0, host=serve_cmd.DEFAULT_HOST, interval=2.5)
        assert bound, "the ordinary loopback workflow must not require a token"
        assert bound[0][4] is None


class TestAuthDisabledByDefault:
    """The ordinary loopback browser workflow, unchanged."""

    def test_reads_and_actions_work_with_no_credentials(self, daemon, stopped):
        assert _request(daemon + "/api/snapshot")[0] == 200
        status, body, _ = _post_action(daemon)
        assert status == 200
        assert json.loads(body)["ok"] is True
        assert stopped == ["p"]


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

    def test_the_bearer_comparison_is_timing_safe(self):
        """Pinned at the comparison itself: ``==`` here leaks the token bytewise."""
        import inspect

        assert "hmac.compare_digest" in inspect.getsource(serve_cmd._secret_matches)
        # Both credential forms route through the timing-safe helper; the only
        # plain comparison left is the ``Bearer`` scheme name, which is public.
        checked = inspect.getsource(serve_cmd._Handler._authenticated)
        assert checked.count("_secret_matches") == 2


class TestBrowserSession:
    """``EventSource`` cannot send headers, so a browser trades the token for a cookie."""

    def test_the_authentication_dialog_cannot_be_dismissed_with_escape(self):
        page = serve_cmd.CONSOLE_PAGE
        cancel_handler = page.split('modal.addEventListener("cancel"', 1)[1].split(
            "});", 1
        )[0]
        assert "event.preventDefault()" in cancel_handler
        assert "{dismissible: false}" in page.split("function askForToken", 1)[1]

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
        forged = f"{serve_cmd.SESSION_COOKIE}=not-the-session-id"
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
