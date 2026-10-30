"""``cwcli serve`` - the HTTP/SSE frontend, driven against a REAL bound server.

No Docker: ``core.list_instances`` and ``core.inspect`` are stubbed, but the
socket, the handler, the SSE framing, the fan-out and the disconnect cleanup are
the shipped ones - the parts a mocked handler would never catch.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from caffeinated_whale_cli.commands import serve as serve_cmd
from caffeinated_whale_cli.core import doctor as core_doctor
from caffeinated_whale_cli.core import fleet as core_fleet
from caffeinated_whale_cli.core import inspect as core_inspect
from caffeinated_whale_cli.core.apps import AppResult, AppsOutput, AppsReport
from caffeinated_whale_cli.core.doctor import CheckResult, CheckStatus, DoctorReport
from caffeinated_whale_cli.core.envelope import Choice, Message, Result, Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from caffeinated_whale_cli.core.inspect import BenchInfo, InspectReport, SiteInfo
from caffeinated_whale_cli.core.label import LabelOutcome
from caffeinated_whale_cli.core.list import InstanceDTO
from caffeinated_whale_cli.core.logs import LogsTail, ProcessTail
from caffeinated_whale_cli.core.restart import ProcessRestartOutcome
from caffeinated_whale_cli.core.start import ProcessLaunch, StartOutcome
from caffeinated_whale_cli.core.stop import BenchStopOutcome, StopOutcome
from caffeinated_whale_cli.core.unlock import UnlockOutcome
from caffeinated_whale_cli.core.url import UrlProbe
from caffeinated_whale_cli.core.where import WhereMatch, WhereResult
from caffeinated_whale_cli.utils import agent_hooks

_TIMEOUT = 5.0
_TOKEN = "frontend-test-token"


@pytest.fixture
def daemon(monkeypatch):
    """A live server on an ephemeral port, plus the fleet behind it."""
    rows = [InstanceDTO(project_name="p", status="running", ports=["8000"])]
    monkeypatch.setattr(
        core_fleet, "list_instances", lambda: Result(status=Status.OK, data=list(rows))
    )
    # A short keepalive keeps the idle transport path reachable inside a test.
    monkeypatch.setattr(serve_cmd, "KEEPALIVE_S", 0.05)

    fleet = core_fleet.Fleet()
    hub = serve_cmd._Hub(fleet.set_focus)
    fleet.set_publish(hub.publish)
    fleet.bootstrap()

    httpd = serve_cmd.make_server("127.0.0.1", 0, fleet, hub, _TOKEN)
    # Authentication is always on, so every urllib request in this file carries
    # the bearer token. The gate itself is exercised in test_serve_auth.py.
    opener = urllib.request.build_opener()
    opener.addheaders = [("Authorization", f"Bearer {_TOKEN}")]
    urllib.request.install_opener(opener)
    # A tight poll interval only so each test's shutdown is prompt (the default
    # 0.5s is the shipped one and costs a full second per test here).
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield type(
            "Daemon",
            (),
            {"base": base, "fleet": fleet, "hub": hub, "rows": rows, "httpd": httpd},
        )
    finally:
        urllib.request.install_opener(None)
        httpd.shutdown()
        httpd.server_close()


def _get(url):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {_TOKEN}"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310 - fixed localhost
        return resp.status, json.loads(resp.read())


def _post(url, payload, headers=None):
    # The bearer rides on every request explicitly: the installed global opener also
    # carries it, but urllib's opener is process-wide state and a request issued while
    # it is reset (another fixture, a background thread) came back 401 once in the tier.
    request_headers = {"Content-Type": "application/json", "Authorization": f"Bearer {_TOKEN}"}
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers=request_headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310 - fixed localhost
        return resp.status, json.loads(resp.read())


class _Stream:
    """Reads SSE frames off a live connection; each frame is ``(event, data)``."""

    def __init__(self, url):
        self.resp = urllib.request.urlopen(url, timeout=_TIMEOUT)  # noqa: S310
        self.keepalives = 0

    def next_frame(self):
        event = None
        while True:
            line = self.resp.readline().decode().rstrip("\n")
            if line.startswith(": "):
                self.keepalives += 1
            elif line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                return event, json.loads(line[len("data: ") :])

    def close(self):
        self.resp.close()


class _RawSseSocket:
    """A browser-style SSE socket that closes request writes but keeps reading."""

    def __init__(self, base, path):
        host, port_s = base.removeprefix("http://").split(":", 1)
        self.sock = socket.create_connection((host, int(port_s)), timeout=_TIMEOUT)
        self.file = self.sock.makefile("rb")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port_s}\r\n"
            f"Authorization: Bearer {_TOKEN}\r\n"
            "Accept: text/event-stream\r\n"
            "Connection: close\r\n"
            "\r\n"
        )
        self.sock.sendall(request.encode())
        self.sock.shutdown(socket.SHUT_WR)
        status = self.file.readline().decode()
        assert " 200 " in status
        while self.file.readline() not in {b"\r\n", b""}:
            pass

    def next_frame(self):
        event = None
        while True:
            line = self.file.readline().decode().rstrip("\n")
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                return event, json.loads(line[len("data: ") :])

    def close(self):
        self.file.close()
        self.sock.close()


def _wait_for(predicate, message):
    deadline = time.monotonic() + _TIMEOUT
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(message)


class TestSnapshot:
    def test_the_whole_model_is_one_json_read(self, daemon):
        status, body = _get(daemon.base + "/api/v1/instances")
        assert status == 200
        assert [i["project"] for i in body["data"]] == ["p"]
        # Nothing has probed yet, and the snapshot says so rather than guessing.
        assert body["data"][0]["overall"] == core_fleet.UNKNOWN
        assert body["data"][0]["probe_ms"] is None
        assert body["data"][0]["probe_failed_at"] is None

    def test_an_unknown_path_is_404(self, daemon):
        with pytest.raises(urllib.error.HTTPError) as e:
            _get(daemon.base + "/api/nope")
        assert e.value.code == 404


class TestWebUi:
    """``/`` and every non-API path serve the built single-page app (package data)."""

    @pytest.fixture
    def built(self, monkeypatch, tmp_path):
        dist = tmp_path / "dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text("<!doctype html><div id=app></div>")
        (dist / "assets" / "app-abc123.js").write_text("console.log(1)")
        (dist / "csp.json").write_text(json.dumps({"style-src": ["'sha256-reka'"]}))
        (tmp_path / "secret.txt").write_text("outside dist")
        monkeypatch.setattr(serve_cmd, "WEBUI_DIST", dist)
        return dist

    def test_an_unbuilt_install_says_so_at_the_root_only(self, daemon, monkeypatch, tmp_path):
        monkeypatch.setattr(serve_cmd, "WEBUI_DIST", tmp_path / "missing")
        with urllib.request.urlopen(daemon.base + "/", timeout=_TIMEOUT) as resp:  # noqa: S310
            body = resp.read().decode()
        assert "not built" in body and "npm run build" in body
        assert "<script" not in body
        assert resp.headers["Content-Security-Policy"].startswith("default-src 'none'")
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(daemon.base + "/instances", timeout=_TIMEOUT)  # noqa: S310
        assert e.value.code == 404

    def test_the_index_carries_the_strict_csp_with_the_builds_hashes(self, daemon, built):
        with urllib.request.urlopen(daemon.base + "/", timeout=_TIMEOUT) as resp:  # noqa: S310
            body = resp.read().decode()
        assert "id=app" in body
        csp = resp.headers["Content-Security-Policy"]
        assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
        directives = dict(d.strip().split(" ", 1) for d in csp.split(";"))
        assert directives["default-src"] == "'none'"
        assert directives["script-src"] == "'self'"
        assert directives["style-src"] == "'self' 'sha256-reka'"  # merged from csp.json
        assert directives["connect-src"] == "'self'"
        assert directives["frame-ancestors"] == "'none'"
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert resp.headers["Cache-Control"] == "no-cache"

    def test_deep_links_fall_back_to_the_index_so_the_router_owns_them(self, daemon, built):
        for path in ("/instances", "/instances/p/benches/0", "/settings"):
            with urllib.request.urlopen(daemon.base + path, timeout=_TIMEOUT) as resp:  # noqa: S310
                assert "id=app" in resp.read().decode(), path
                assert "Content-Security-Policy" in resp.headers

    def test_hashed_assets_are_immutable_and_typed(self, daemon, built):
        url = daemon.base + "/assets/app-abc123.js"
        with urllib.request.urlopen(url, timeout=_TIMEOUT) as resp:  # noqa: S310
            assert resp.read() == b"console.log(1)"
        assert resp.headers["Content-Type"] == "text/javascript"
        assert "immutable" in resp.headers["Cache-Control"]
        assert "Content-Security-Policy" not in resp.headers

    @pytest.mark.parametrize(
        "path", ["/../secret.txt", "/assets/../../secret.txt", "/%2e%2e/secret.txt"]
    )
    def test_a_traversal_never_leaves_dist(self, daemon, built, path):
        # A refused segment degrades to the index fallback, never to the file.
        with urllib.request.urlopen(daemon.base + path, timeout=_TIMEOUT) as resp:  # noqa: S310
            body = resp.read().decode()
        assert "outside dist" not in body

    def test_spa_file_refuses_every_escape_shape(self, built):
        assert serve_cmd._spa_file("/../secret.txt") is None
        assert serve_cmd._spa_file("/assets/..\\..\\secret.txt") is None
        assert serve_cmd._spa_file("/nope.js") is None
        assert serve_cmd._spa_file("/assets/app-abc123.js") is not None

    def test_the_page_and_every_api_route_are_distinct(self, daemon, built):
        """``/api/*`` is never the page: an unknown API path is a 404, not index.html."""
        with pytest.raises(urllib.error.HTTPError) as e:
            _get(daemon.base + "/api/v1/nope")
        assert e.value.code == 404
        assert json.loads(e.value.read())["errors"][0]["title"] == "route.not_found"


class TestEventStream:
    def test_the_first_frame_is_a_full_snapshot(self, daemon):
        stream = _Stream(daemon.base + "/api/v1/stream")
        try:
            event, data = stream.next_frame()
            assert event == "snapshot"
            assert [i["project"] for i in data["instances"]] == ["p"]
        finally:
            stream.close()

    def test_a_fast_delta_reaches_the_client_tagged_with_its_tier(self, daemon):
        stream = _Stream(daemon.base + "/api/v1/stream")
        try:
            stream.next_frame()  # the snapshot
            _wait_for(lambda: daemon.hub.client_count() == 1, "client never registered")

            daemon.hub.publish("fast", "p", daemon.fleet.get("p"), None)

            event, data = stream.next_frame()
            assert event == "delta"
            assert data["tier"] == "fast"
            assert data["project"] == "p"
            assert data["removed"] is False
        finally:
            stream.close()

    def test_an_instant_delta_carries_the_docker_action_that_caused_it(self, daemon):
        stream = _Stream(daemon.base + "/api/v1/stream")
        try:
            stream.next_frame()
            _wait_for(lambda: daemon.hub.client_count() == 1, "client never registered")

            daemon.rows[:] = [InstanceDTO(project_name="p", status="exited", ports=[])]
            daemon.fleet.apply_event(
                {
                    "Action": "die",
                    "Actor": {
                        "Attributes": {
                            "com.docker.compose.project": "p",
                            "com.docker.compose.service": "frappe",
                        }
                    },
                }
            )

            _event, data = stream.next_frame()
            assert data["tier"] == "instant"
            assert data["cause"]["action"] == "die"
            assert data["instance"]["overall"] == "offline"
        finally:
            stream.close()

    def test_a_removal_is_published_as_a_null_instance(self, daemon):
        stream = _Stream(daemon.base + "/api/v1/stream")
        try:
            stream.next_frame()
            _wait_for(lambda: daemon.hub.client_count() == 1, "client never registered")

            daemon.rows[:] = []
            daemon.fleet.bootstrap()

            _event, data = stream.next_frame()
            assert data["removed"] is True and data["instance"] is None
        finally:
            stream.close()

    def test_every_client_gets_the_same_delta(self, daemon):
        streams = [_Stream(daemon.base + "/api/v1/stream") for _ in range(3)]
        try:
            for s in streams:
                s.next_frame()
            _wait_for(lambda: daemon.hub.client_count() == 3, "clients never registered")

            daemon.hub.publish("fast", "p", daemon.fleet.get("p"), None)

            seen = [s.next_frame()[1] for s in streams]
            assert all(d == seen[0] for d in seen)
        finally:
            for s in streams:
                s.close()

    def test_an_idle_connection_gets_keepalive_comments(self, daemon):
        stream = _Stream(daemon.base + "/api/v1/stream")
        try:
            stream.next_frame()
            _wait_for(lambda: daemon.hub.client_count() == 1, "client never registered")

            # Nothing changes, so the only thing holding the connection open is the
            # keepalive comment. Publish once so next_frame() returns after them.
            time.sleep(0.3)
            daemon.hub.publish("fast", "p", daemon.fleet.get("p"), None)
            stream.next_frame()

            assert stream.keepalives > 0
        finally:
            stream.close()

    def test_a_browser_half_closed_request_still_receives_deltas(self, daemon):
        stream = _RawSseSocket(daemon.base, "/api/v1/stream?focus=p")
        try:
            stream.next_frame()
            _wait_for(lambda: daemon.hub.client_count() == 1, "client never registered")

            daemon.hub.publish("fast", "p", daemon.fleet.get("p"), None)

            event, data = stream.next_frame()
            assert event == "delta"
            assert data["tier"] == "fast"
            assert data["project"] == "p"
            assert daemon.fleet.should_probe_web("p") is True
        finally:
            stream.close()


class TestFocus:
    def test_concurrent_client_changes_deliver_the_latest_focus_snapshot(self):
        delivered = []
        empty_started = threading.Event()
        release_empty = threading.Event()
        block_empty = False

        def _on_focus(focused):
            if block_empty and not focused:
                empty_started.set()
                assert release_empty.wait(2)
            delivered.append(focused)

        hub = serve_cmd._Hub(_on_focus)
        client_id, _q = hub.subscribe("p")
        block_empty = True
        disconnect = threading.Thread(target=hub.unsubscribe, args=(client_id,))
        disconnect.start()
        assert empty_started.wait(2)
        connect = threading.Thread(target=hub.subscribe, args=("q",))
        connect.start()
        release_empty.set()
        disconnect.join(2)
        connect.join(2)

        assert not disconnect.is_alive()
        assert not connect.is_alive()
        assert delivered[-1] == {"q"}

    def test_the_connection_itself_carries_which_instance_is_open(self, daemon):
        assert daemon.fleet.should_probe_web("p") is False

        stream = _Stream(daemon.base + "/api/v1/stream?focus=p")
        try:
            stream.next_frame()
            _wait_for(
                lambda: daemon.fleet.should_probe_web("p"),
                "focus never reached the fleet",
            )
        finally:
            stream.close()

    def test_a_disconnect_retracts_focus_when_a_write_observes_the_closed_stream(
        self, daemon, monkeypatch
    ):
        monkeypatch.setattr(serve_cmd, "KEEPALIVE_S", 0.05)
        stream = _Stream(daemon.base + "/api/v1/stream?focus=p")
        stream.next_frame()
        _wait_for(lambda: daemon.fleet.should_probe_web("p"), "focus never reached the fleet")

        stream.close()

        _wait_for(
            lambda: not daemon.fleet.should_probe_web("p") and daemon.hub.client_count() == 0,
            "a closed client left its focus and its queue behind after the stream wrote",
        )


class TestDetail:
    """The LAZY tier: whatever ``core.inspect`` says, including its freshness labels."""

    def _report(self, monkeypatch, report):
        monkeypatch.setattr(
            core_inspect, "inspect", lambda p, **kw: Result(status=Status.OK, data=report)
        )

    def test_it_carries_served_from_and_apps_verified_through_unchanged(self, daemon, monkeypatch):
        self._report(
            monkeypatch,
            InspectReport(
                project="p",
                served_from="partial",
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
                                installed_apps=["frappe 16.28.0 version-16"],
                                installed_apps_verified=False,
                                has_site_config=True,
                            )
                        ],
                    )
                ],
            ),
        )

        status, body = _get(daemon.base + "/api/v1/instances/p")

        assert status == 200
        assert body["data"]["served_from"] == "partial"
        site = body["data"]["benches"][0]["sites"][0]
        assert site["installed_apps_verified"] is False, "a remembered ref must not read as fresh"

    @pytest.mark.parametrize("saved", ["s3cret-root", None])
    def test_it_says_whether_the_root_password_is_saved_never_what_it_is(
        self, daemon, monkeypatch, saved
    ):
        from caffeinated_whale_cli.core import db_password

        monkeypatch.setattr(db_password, "remembered", lambda project: saved)
        monkeypatch.setattr(
            core_inspect,
            "inspect",
            lambda project, **kw: Result(
                status=Status.OK,
                data=InspectReport(project=project, served_from="cache", degraded=False, benches=[]),
            ),
        )
        status, body = _get(daemon.base + "/api/v1/instances/p")

        assert status == 200
        assert body["data"]["db_root_password_saved"] is (saved is not None)
        assert "s3cret-root" not in json.dumps(body)

    def test_it_never_auto_starts_a_stopped_instance(self, daemon, monkeypatch):
        seen = {}

        def _inspect(project, **kwargs):
            seen.update(kwargs)
            return Result(
                status=Status.OK,
                data=InspectReport(
                    project=project, served_from="cache", degraded=False, benches=[]
                ),
            )

        monkeypatch.setattr(core_inspect, "inspect", _inspect)
        _get(daemon.base + "/api/v1/instances/p")

        assert seen["refresh"] == "auto"
        assert seen["offer_choice"] is False
        assert "auto_start" not in seen or seen["auto_start"] is False

    @pytest.mark.parametrize(
        "kind,expected",
        [
            (ErrorKind.NOT_FOUND, 404),
            (ErrorKind.NOT_RUNNING, 409),
            (ErrorKind.DOCKER, 503),
            (ErrorKind.INTERNAL, 500),
        ],
    )
    def test_a_core_error_becomes_the_http_status_that_means_the_same(
        self, daemon, monkeypatch, kind, expected
    ):
        def _raise(project, **kwargs):
            raise CwcliError(kind, "some.code", "nope")

        monkeypatch.setattr(core_inspect, "inspect", _raise)

        with pytest.raises(urllib.error.HTTPError) as e:
            _get(daemon.base + "/api/v1/instances/p")
        assert e.value.code == expected
        assert json.loads(e.value.read())["errors"][0]["title"] == "some.code"


class TestActions:
    """The Console rail exposes only the approved narrow action set."""

    def test_action_preflight_does_not_grant_cross_origin_access(self, daemon):
        req = urllib.request.Request(
            daemon.base + "/api/v1/actions",
            method="OPTIONS",
            headers={
                "Origin": "https://example.invalid",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )

        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310
            assert resp.status == 204
            assert resp.headers.get("Access-Control-Allow-Origin") is None

    def test_cross_origin_simple_post_is_rejected_before_dispatch(self, daemon, monkeypatch):
        monkeypatch.setattr(
            serve_cmd.core_stop,
            "stop",
            lambda project: pytest.fail("cross-origin action must not dispatch"),
        )
        req = urllib.request.Request(
            daemon.base + "/api/v1/actions",
            data=b'{"action": "stop_instance", "project": "p"}',
            method="POST",
            headers={
                "Origin": "https://example.invalid",
                "Sec-Fetch-Site": "cross-site",
                "Content-Type": "text/plain",
            },
        )

        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=_TIMEOUT)  # noqa: S310 - fixed localhost

        assert e.value.code == 403
        body = json.loads(e.value.read())
        assert body["errors"][0]["title"] == "action.origin_forbidden"

    def test_action_body_must_be_json_before_dispatch(self, daemon, monkeypatch):
        monkeypatch.setattr(
            serve_cmd.core_stop,
            "stop",
            lambda project: pytest.fail("non-JSON action must not dispatch"),
        )
        req = urllib.request.Request(
            daemon.base + "/api/v1/actions",
            data=b'{"action": "stop_instance", "project": "p"}',
            method="POST",
            headers={"Content-Type": "text/plain"},
        )

        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=_TIMEOUT)  # noqa: S310 - fixed localhost

        assert e.value.code == 415
        body = json.loads(e.value.read())
        assert body["errors"][0]["title"] == "request.content_type_invalid"

    def test_negative_content_length_is_rejected_before_reading(self, daemon):
        host, port = daemon.base.removeprefix("http://").split(":", 1)
        connection = http.client.HTTPConnection(host, int(port), timeout=_TIMEOUT)
        connection.putrequest("POST", "/api/v1/actions")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", "-1")
        connection.endheaders()

        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()

        assert response.status == 400
        assert body["errors"][0]["title"] == "request.length_invalid"

    def test_start_instance_dispatches_to_the_core(self, daemon, monkeypatch):
        called = {}
        monkeypatch.setattr(serve_cmd.core_jobs.start_cmd, "_frappe_running", lambda project: True)

        def _start(project, **kwargs):
            called.update({"project": project, **kwargs})
            return Result(
                status=Status.OK,
                data=StartOutcome(
                    project=project,
                    container="p-frappe-1",
                    bench_path="/workspace/frappe-bench",
                    supervisor="supervisord",
                    log_path="/workspace/frappe-bench/logs",
                    already_running=False,
                    processes=[ProcessLaunch(label="web", pid=11)],
                    web_ready=True,
                ),
            )

        monkeypatch.setattr(serve_cmd.core_start, "start", _start)

        status, body = _post(
            daemon.base + "/api/v1/actions",
            {"action": "start_instance", "project": "p", "bench": "0"},
        )

        assert status == 200
        assert "data" in body
        assert body["data"]["outcome"]["bench_path"] == "/workspace/frappe-bench"
        assert called["project"] == "p"
        assert called["bench"] == "0"

    def test_stop_instance_dispatches_to_the_core(self, daemon, monkeypatch):
        called = {}

        def _stop(project):
            called["project"] = project
            return Result(
                status=Status.OK,
                data=StopOutcome(
                    project=project,
                    stopped=3,
                    already_stopped=False,
                    containers=["p-frappe-1", "p-db-1", "p-redis-1"],
                ),
            )

        monkeypatch.setattr(serve_cmd.core_stop, "stop", _stop)

        status, body = _post(
            daemon.base + "/api/v1/actions",
            {"action": "stop_instance", "project": "p"},
            headers={
                "Origin": daemon.base,
                "Sec-Fetch-Site": "same-origin",
            },
        )

        assert status == 200
        assert "data" in body
        assert body["data"]["outcome"]["stopped"] == 3
        assert called["project"] == "p"

    def test_stop_instance_reports_an_unclean_database_as_failure(self, daemon, monkeypatch):
        warning = Message(
            "stop.db_unclean",
            "The database exited with code 137. MariaDB will run crash recovery.",
        )
        monkeypatch.setattr(
            serve_cmd.core_stop,
            "stop",
            lambda project: Result(
                status=Status.WARNING,
                data=StopOutcome(
                    project=project,
                    stopped=3,
                    already_stopped=False,
                    containers=["p-frappe-1", "p-redis-1", "p-mariadb-1"],
                    db_clean_shutdown=False,
                ),
                warnings=[warning],
            ),
        )

        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(daemon.base + "/api/v1/actions", {"action": "stop_instance", "project": "p"})

        assert exc.value.code == 500
        body = json.loads(exc.value.read())
        assert "errors" in body
        assert body["errors"][0]["title"] == "stop.db_unclean"
        assert body["errors"][0]["warnings"][0]["code"] == "stop.db_unclean"
        assert body["errors"][0]["warnings"][0]["text"] == warning.text

    def test_restart_instance_completes_with_the_stop_warning(self, daemon, monkeypatch):
        warning = Message("stop.db_unclean", "The database exited with code 137.")
        monkeypatch.setattr(serve_cmd.core_jobs.start_cmd, "_frappe_running", lambda project: True)
        monkeypatch.setattr(
            serve_cmd.core_resolvers,
            "resolve_bench",
            lambda project, bench, path: Result(status=Status.OK, data="/workspace/frappe-bench"),
        )
        monkeypatch.setattr(
            serve_cmd.core_stop,
            "stop",
            lambda project: Result(
                status=Status.WARNING,
                data=StopOutcome(
                    project=project,
                    stopped=3,
                    already_stopped=False,
                    containers=["p-frappe-1", "p-redis-1", "p-mariadb-1"],
                    db_clean_shutdown=False,
                ),
                warnings=[warning],
            ),
        )
        monkeypatch.setattr(
            serve_cmd.core_start,
            "start",
            lambda project, **kwargs: Result(
                status=Status.OK,
                data=StartOutcome(
                    project=project,
                    container="p-frappe-1",
                    bench_path=kwargs["bench_path"],
                    supervisor="supervisord",
                    log_path="/workspace/frappe-bench/logs",
                    already_running=False,
                    processes=[ProcessLaunch(label="web", pid=11)],
                    web_ready=True,
                ),
            ),
        )

        status, body = _post(
            daemon.base + "/api/v1/actions", {"action": "restart_instance", "project": "p"}
        )

        assert status == 200
        assert "data" in body
        assert body["data"]["warnings"][0]["code"] == "stop.db_unclean"
        assert body["data"]["warnings"][0]["text"] == warning.text

    def test_restart_process_dispatches_to_the_core_and_refreshes_fast_tier(
        self, daemon, monkeypatch
    ):
        called = {}

        def _restart_process(project, process, **kwargs):
            called.update({"project": project, "process": process, **kwargs})
            return Result(
                status=Status.OK,
                data=ProcessRestartOutcome(
                    project=project,
                    bench_path="/workspace/frappe-bench",
                    label=process,
                    old_pid=11,
                    new_pid=12,
                    supervisor_state="RUNNING",
                ),
            )

        monkeypatch.setattr(serve_cmd.core_restart, "restart_process", _restart_process)
        monkeypatch.setattr(
            daemon.fleet, "probe", lambda project: called.update({"probe": project})
        )

        status, body = _post(
            daemon.base + "/api/v1/actions",
            {"action": "restart_process", "project": "p", "bench": "0", "process": "web"},
        )

        assert status == 200
        assert "data" in body
        assert body["data"]["outcome"]["old_pid"] == 11
        assert body["data"]["outcome"]["new_pid"] == 12
        assert called == {"project": "p", "process": "web", "bench": "0", "probe": "p"}

    def test_start_refuses_port_conflicts_without_stopping_other_instances(
        self, daemon, monkeypatch
    ):
        monkeypatch.setattr(serve_cmd.core_jobs.start_cmd, "_frappe_running", lambda project: False)
        monkeypatch.setattr(
            serve_cmd.core_jobs.start_cmd,
            "detect_port_conflicts",
            lambda project: (["other"], [8100]),
        )
        monkeypatch.setattr(
            serve_cmd.core_start,
            "start",
            lambda *a, **kw: pytest.fail("start must not run when ports conflict"),
        )

        with pytest.raises(urllib.error.HTTPError) as e:
            _post(daemon.base + "/api/v1/actions", {"action": "start_instance", "project": "p"})

        assert e.value.code == 409
        body = json.loads(e.value.read())
        assert body["errors"][0]["title"] == "start.port_conflict"
        assert "other" in body["errors"][0]["message"]
        assert "8100" in body["errors"][0]["message"]

    def test_restart_instance_resolves_bench_before_stopping(self, daemon, monkeypatch):
        choice = Choice(
            kind="select_bench",
            param="bench",
            prompt="Project 'p' has multiple benches; select one.",
            options=[{"value": "0", "label": "/workspace/frappe-bench"}],
        )
        monkeypatch.setattr(serve_cmd.core_jobs.start_cmd, "_frappe_running", lambda project: True)
        monkeypatch.setattr(
            serve_cmd.core_resolvers,
            "resolve_bench",
            lambda project, bench, path: Result(status=Status.NEEDS_CHOICE, choice=choice),
        )
        monkeypatch.setattr(
            serve_cmd.core_stop,
            "stop",
            lambda project: pytest.fail("stop must not run before a bench choice is resolved"),
        )

        with pytest.raises(urllib.error.HTTPError) as e:
            _post(daemon.base + "/api/v1/actions", {"action": "restart_instance", "project": "p"})

        assert e.value.code == 409
        body = json.loads(e.value.read())
        assert body["errors"][0]["title"] == "select_bench"
        assert body["errors"][0]["choice"]["options"][0]["value"] == "0"

    def test_same_project_actions_are_serialized(self, daemon, monkeypatch):
        entered = threading.Event()
        release = threading.Event()
        overlap = threading.Event()
        state_lock = threading.Lock()
        active = 0

        def _stop(project):
            nonlocal active
            with state_lock:
                active += 1
                if active > 1:
                    overlap.set()
            entered.set()
            assert release.wait(_TIMEOUT)
            with state_lock:
                active -= 1
            return Result(
                status=Status.OK,
                data=StopOutcome(
                    project=project,
                    stopped=1,
                    already_stopped=False,
                    containers=["p-frappe-1"],
                ),
            )

        monkeypatch.setattr(serve_cmd.core_stop, "stop", _stop)
        errors = []

        def _request():
            try:
                _post(
                    daemon.base + "/api/v1/actions",
                    {"action": "stop_instance", "project": "p"},
                )
            except Exception as error:
                errors.append(error)

        first = threading.Thread(target=_request)
        second = threading.Thread(target=_request)
        first.start()
        assert entered.wait(_TIMEOUT)
        second.start()
        try:
            assert not overlap.wait(0.2)
        finally:
            release.set()
            first.join(_TIMEOUT)
            second.join(_TIMEOUT)

        assert not first.is_alive()
        assert not second.is_alive()
        assert not errors

    def test_action_lock_storage_is_bounded(self, daemon):
        handler = daemon.httpd.RequestHandlerClass
        request = object.__new__(handler)
        locks = handler.action_locks

        for index in range(10_000):
            request._action_lock(f"unknown-{index}")

        assert len(locks) == serve_cmd._ACTION_LOCK_STRIPES
        assert handler.action_locks is locks
        assert request._action_lock("p") is request._action_lock("p")

    def test_unsupported_actions_are_rejected(self, daemon):
        with pytest.raises(urllib.error.HTTPError) as e:
            _post(daemon.base + "/api/v1/actions", {"action": "rm_instance", "project": "p"})

        assert e.value.code == 400
        body = json.loads(e.value.read())
        assert body["errors"][0]["title"] == "action.unsupported"

    def test_the_action_set_is_exactly_tier_a(self, daemon):
        """The allowed set is the captain's Tier A ruling and nothing more: no
        long-running Tier B verb (init/backup/install/update/migrate/run-tests/
        build/rm/rm-site) and no deferred Tier C verb (restore/uninstall/run/
        config) may appear here without its own decision. ``open_editor`` is the
        one Tier C addition with its decision: the captain asked for Open in
        VS Code / Cursor on 2026-10-27 (steering 013); it launches the editor's
        Dev Containers attach on the host and never opens a shell."""
        with pytest.raises(urllib.error.HTTPError) as e:
            _post(daemon.base + "/api/v1/actions", {"action": "nope", "project": "p"})

        hint = json.loads(e.value.read())["errors"][0]["hint"]
        named = set(hint.removeprefix("Allowed actions are ").removesuffix(".").split(", "))
        assert named == {
            "start_instance",
            "stop_instance",
            "restart_instance",
            "stop_bench",
            "restart_process",
            "refresh_status",
            "set_label",
            "unlock_site",
            "clear_maintenance",
            "scale_instance",
            "checkout_app",
            "open_editor",
        }
        assert named == set(serve_cmd.ACTIONS)


class TestTierAActions:
    """The Tier A expansion: safe synchronous verbs, consent enforced in core."""

    def test_set_label_dispatches_to_the_core_and_reprobes(self, daemon, monkeypatch):
        called = {}

        def _set_label(project, *, bench, label):
            called.update({"project": project, "bench": bench, "label": label})
            return Result(
                status=Status.OK,
                data=LabelOutcome(
                    project=project,
                    bench_path="/workspace/frappe-bench",
                    label=label,
                    previous_label=None,
                    marker_path="/workspace/frappe-bench/.cwcli-bench",
                    cleared=False,
                ),
            )

        monkeypatch.setattr(serve_cmd.core_label, "set_label", _set_label)
        monkeypatch.setattr(
            daemon.fleet, "probe", lambda project: called.update({"probe": project})
        )

        status, body = _post(
            daemon.base + "/api/v1/actions",
            {"action": "set_label", "project": "p", "bench": "0", "label": "dev"},
        )

        assert status == 200
        assert "data" in body
        assert body["data"]["outcome"]["label"] == "dev"
        assert called == {"project": "p", "bench": "0", "label": "dev", "probe": "p"}

    def test_set_label_requires_a_label(self, daemon):
        with pytest.raises(urllib.error.HTTPError) as e:
            _post(daemon.base + "/api/v1/actions", {"action": "set_label", "project": "p"})

        assert e.value.code == 400
        assert json.loads(e.value.read())["errors"][0]["title"] == "action.label_required"

    def test_unlock_site_dispatches_to_the_core(self, daemon, monkeypatch):
        called = {}

        def _unlock(project, *, site, bench):
            called.update({"project": project, "site": site, "bench": bench})
            return Result(
                status=Status.OK,
                data=UnlockOutcome(
                    site="a.localhost",
                    bench_path="/workspace/frappe-bench",
                    locks_path="/workspace/frappe-bench/sites/a.localhost/locks",
                    removed=["a.lock"],
                    already_unlocked=False,
                ),
            )

        monkeypatch.setattr(serve_cmd.core_unlock, "unlock", _unlock)

        status, body = _post(
            daemon.base + "/api/v1/actions",
            {"action": "unlock_site", "project": "p", "site": "a.localhost"},
        )

        assert status == 200
        assert "data" in body
        assert body["data"]["outcome"]["removed"] == ["a.lock"]
        # site omitted or blank means "the bench's default site", resolved by core.
        assert called == {"project": "p", "site": "a.localhost", "bench": None}

    def test_unlock_needs_choice_surfaces_as_409_not_an_auto_start(self, daemon, monkeypatch):
        """A stopped instance comes back as core's confirm_start choice; the
        daemon relays it and never starts anything on unlock's behalf."""
        choice = Choice(
            kind="confirm_start",
            param="yes",
            prompt="Container for 'p' is not running. Start it?",
        )
        monkeypatch.setattr(
            serve_cmd.core_unlock,
            "unlock",
            lambda project, **kw: Result(status=Status.NEEDS_CHOICE, choice=choice),
        )

        with pytest.raises(urllib.error.HTTPError) as e:
            _post(daemon.base + "/api/v1/actions", {"action": "unlock_site", "project": "p"})

        assert e.value.code == 409
        body = json.loads(e.value.read())
        assert body["errors"][0]["type"] == "needs_choice"
        assert body["errors"][0]["title"] == "confirm_start"
        assert body["errors"][0]["hint"] == "Start the instance first (cwcli start p), then retry."

    def test_stop_bench_dispatches_to_the_core_and_reprobes(self, daemon, monkeypatch):
        called = {}

        def _stop_bench(project, *, bench, bench_path=None):
            called.update({"project": project, "bench": bench})
            return Result(
                status=Status.OK,
                data=BenchStopOutcome(
                    project=project,
                    bench_path="/workspace/frappe-bench",
                    stopped_processes=["web", "worker"],
                    already_stopped=False,
                ),
            )

        monkeypatch.setattr(serve_cmd.core_stop, "stop_bench", _stop_bench)
        monkeypatch.setattr(serve_cmd.core_stop, "stop", lambda p: pytest.fail("not the instance"))
        monkeypatch.setattr(
            core_fleet.Fleet, "probe", lambda self, project: called.update({"probe": project})
        )

        status, body = _post(
            daemon.base + "/api/v1/actions",
            {"action": "stop_bench", "project": "p", "bench": "1"},
        )

        assert status == 200
        assert body["data"]["outcome"]["stopped_processes"] == ["web", "worker"]
        assert called == {"project": "p", "bench": "1", "probe": "p"}

    def test_clear_maintenance_turns_maintenance_off_for_one_named_site(self, daemon, monkeypatch):
        called = {}
        container = object()
        monkeypatch.setattr(
            serve_cmd.core_resolvers,
            "resolve_container_and_bench",
            lambda project, bench, path: (container, "/workspace/frappe-bench", []),
        )

        def _clear(c, bench_path, site, *, recheck):
            called.update(
                {"container": c, "bench_path": bench_path, "site": site, "recheck": recheck}
            )
            return True

        monkeypatch.setattr(serve_cmd.core_bench_ops, "clear_maintenance", _clear)
        monkeypatch.setattr(core_fleet.Fleet, "probe", lambda self, project: None)

        status, body = _post(
            daemon.base + "/api/v1/actions",
            {"action": "clear_maintenance", "project": "p", "site": "a.localhost"},
        )

        assert status == 200
        assert body["data"]["outcome"] == {
            "project": "p",
            "bench_path": "/workspace/frappe-bench",
            "site": "a.localhost",
            "maintenance_off": True,
        }
        # Only ever OFF (recheck is the interrupt path's settle; not needed here).
        assert called == {
            "container": container,
            "bench_path": "/workspace/frappe-bench",
            "site": "a.localhost",
            "recheck": False,
        }

    def test_clear_maintenance_requires_a_site_and_validates_it(self, daemon, monkeypatch):
        monkeypatch.setattr(
            serve_cmd.core_resolvers,
            "resolve_container_and_bench",
            lambda *a: pytest.fail("must not resolve before the site is validated"),
        )
        with pytest.raises(urllib.error.HTTPError) as e:
            _post(daemon.base + "/api/v1/actions", {"action": "clear_maintenance", "project": "p"})
        assert e.value.code == 400
        assert json.loads(e.value.read())["errors"][0]["title"] == "action.site_required"

        with pytest.raises(urllib.error.HTTPError) as e:
            _post(
                daemon.base + "/api/v1/actions",
                {"action": "clear_maintenance", "project": "p", "site": "a;rm -rf /"},
            )
        assert e.value.code == 400

    def test_clear_maintenance_that_leaves_the_site_on_is_a_failure(self, daemon, monkeypatch):
        monkeypatch.setattr(
            serve_cmd.core_resolvers,
            "resolve_container_and_bench",
            lambda project, bench, path: (object(), "/workspace/frappe-bench", []),
        )
        monkeypatch.setattr(serve_cmd.core_bench_ops, "clear_maintenance", lambda *a, **kw: False)
        monkeypatch.setattr(core_fleet.Fleet, "probe", lambda self, project: None)

        with pytest.raises(urllib.error.HTTPError) as e:
            _post(
                daemon.base + "/api/v1/actions",
                {"action": "clear_maintenance", "project": "p", "site": "a.localhost"},
            )
        assert e.value.code == 500
        body = json.loads(e.value.read())
        assert body["errors"][0]["title"] == "maintenance.still_on"
        assert "set-maintenance-mode off" in body["errors"][0]["hint"]
        assert body["errors"][0]["outcome"]["maintenance_off"] is False

    def test_clear_maintenance_on_a_stopped_instance_is_a_409_not_an_auto_start(
        self, daemon, monkeypatch
    ):
        choice = Choice(
            kind="confirm_start",
            param="auto_start",
            prompt="Project 'p' is not running.",
            options=[],
        )
        monkeypatch.setattr(
            serve_cmd.core_resolvers,
            "resolve_container_and_bench",
            lambda project, bench, path: Result(status=Status.NEEDS_CHOICE, choice=choice),
        )
        monkeypatch.setattr(
            serve_cmd.core_bench_ops,
            "clear_maintenance",
            lambda *a, **kw: pytest.fail("must not run on a stopped instance"),
        )
        with pytest.raises(urllib.error.HTTPError) as e:
            _post(
                daemon.base + "/api/v1/actions",
                {"action": "clear_maintenance", "project": "p", "site": "a.localhost"},
            )
        assert e.value.code == 409
        assert json.loads(e.value.read())["errors"][0]["title"] == "confirm_start"

    def test_scale_without_consent_relays_the_core_confirm(self, daemon, monkeypatch):
        """The scale confirm is CORE-driven: core.scale answers NEEDS_CHOICE/
        confirm_scale and the daemon renders it as 409 needs_choice, carrying
        core's own warning text for the page's typed-name modal. No page-local
        confirm could bypass this because the gate is in cwcli, not the page."""
        called = {}

        def _scale(project, **kwargs):
            called.update({"project": project, **kwargs})
            return Result(
                status=Status.NEEDS_CHOICE,
                choice=Choice(
                    kind="confirm_scale",
                    param="consent",
                    prompt="Expanding project 'p' RESTARTS every serving bench. Continue?",
                    default="false",
                ),
            )

        monkeypatch.setattr(serve_cmd.core_scale, "scale", _scale)

        with pytest.raises(urllib.error.HTTPError) as e:
            _post(daemon.base + "/api/v1/actions", {"action": "scale_instance", "project": "p"})

        assert e.value.code == 409
        body = json.loads(e.value.read())
        assert body["errors"][0]["type"] == "needs_choice"
        assert body["errors"][0]["title"] == "confirm_scale"
        assert "RESTARTS" in body["errors"][0]["message"]
        assert called == {"project": "p", "to": None, "consent": False}

    def test_scale_consent_must_be_the_json_boolean_true(self, daemon, monkeypatch):
        """Consent is strictly `true`; a truthy string does not count, and the
        dispatch passes consent ONLY - nothing that could start a stopped
        instance rides along with it (consent never fuses with auto-start)."""
        calls = []

        def _scale(project, **kwargs):
            calls.append(kwargs)
            return Result(
                status=Status.NEEDS_CHOICE,
                choice=Choice(kind="confirm_scale", param="consent", prompt="Continue?"),
            )

        monkeypatch.setattr(serve_cmd.core_scale, "scale", _scale)

        with pytest.raises(urllib.error.HTTPError):
            _post(
                daemon.base + "/api/v1/actions",
                {"action": "scale_instance", "project": "p", "consent": "yes"},
            )
        with pytest.raises(urllib.error.HTTPError):
            _post(
                daemon.base + "/api/v1/actions",
                {"action": "scale_instance", "project": "p", "consent": 1},
            )

        assert calls == [{"to": None, "consent": False}, {"to": None, "consent": False}]

    def test_scale_forwards_a_valid_to_and_refuses_a_non_integer_one(self, daemon, monkeypatch):
        calls = []

        def _scale(project, **kwargs):
            calls.append(kwargs)
            return Result(
                status=Status.NEEDS_CHOICE,
                choice=Choice(kind="confirm_scale", param="consent", prompt="Continue?"),
            )

        monkeypatch.setattr(serve_cmd.core_scale, "scale", _scale)

        with pytest.raises(urllib.error.HTTPError):
            _post(
                daemon.base + "/api/v1/actions",
                {"action": "scale_instance", "project": "p", "to": 8},
            )
        assert calls == [{"to": 8, "consent": False}]

        # JSON true is an int subclass in Python and must not read as "1 bench";
        # a string is not a count either. Both are refused before dispatch.
        for bad in (True, "8"):
            with pytest.raises(urllib.error.HTTPError) as e:
                _post(
                    daemon.base + "/api/v1/actions",
                    {"action": "scale_instance", "project": "p", "to": bad},
                )
            assert e.value.code == 400
            assert json.loads(e.value.read())["errors"][0]["title"] == "action.to_invalid"
        assert len(calls) == 1

    def test_checkout_app_never_forwards_reset_or_auto_start(self, daemon, monkeypatch):
        """Tier A ships checkout's no-reset form ONLY: a request smuggling
        reset:true is dispatched without it (core's default False), and
        auto_start is never passed. The CLI verbs are the way through a dirty
        tree - the Console deliberately has no destructive opt-in to offer."""
        called = {}

        def _checkout(project, app, ref, **kwargs):
            called.update({"project": project, "app": app, "ref": ref, **kwargs})
            report = AppsReport(
                project=project,
                bench_path="/workspace/frappe-bench",
                results=[
                    AppResult(app=app, site=None, action="fetch", ok=True),
                    AppResult(app=app, site=None, action="checkout", ok=True),
                ],
                ok=True,
            )
            return Result(status=Status.OK, data=report)

        monkeypatch.setattr(serve_cmd.core_apps, "checkout_app", _checkout)
        monkeypatch.setattr(serve_cmd.cache, "recache_project", lambda project, **_k: True)

        status, body = _post(
            daemon.base + "/api/v1/actions",
            {
                "action": "checkout_app",
                "project": "p",
                "app": "erpnext",
                "ref": "feature-x",
                "reset": True,
                "auto_start": True,
            },
        )

        assert status == 200
        assert "data" in body
        assert called["project"] == "p"
        assert called["app"] == "erpnext"
        assert called["ref"] == "feature-x"
        assert "reset" not in called
        assert "auto_start" not in called

    def test_checkout_app_rejects_path_traversal_before_dispatch(self, daemon, monkeypatch):
        dispatched = []
        monkeypatch.setattr(
            serve_cmd.core_apps,
            "checkout_app",
            lambda *args, **kwargs: dispatched.append((args, kwargs)),
        )

        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(
                daemon.base + "/api/v1/actions",
                {
                    "action": "checkout_app",
                    "project": "p",
                    "app": "../other",
                    "ref": "main",
                },
            )

        assert exc.value.code == 400
        assert json.loads(exc.value.read())["errors"][0]["title"] == "app.invalid_component"
        assert dispatched == []

    def test_a_failed_checkout_reports_failure_with_gits_own_words(self, daemon, monkeypatch):
        """ok reads report.ok, not the envelope status (the exit-code precedent
        in HTTP clothes), and the failure message carries a bounded tail of
        git's bytes - the only place an unknown-ref or auth failure names
        itself. Nothing succeeded, so no recache runs either."""
        recached = []

        def _checkout(project, app, ref, *, on_event=None, **kwargs):
            if on_event:
                on_event(
                    AppsOutput(
                        phase="fetch",
                        app=app,
                        site=None,
                        stream="stderr",
                        text="fatal: couldn't find remote ref nope",
                    )
                )
            report = AppsReport(
                project=project,
                bench_path="/workspace/frappe-bench",
                results=[AppResult(app=app, site=None, action="fetch", ok=False)],
                ok=False,
            )
            return Result(status=Status.WARNING, data=report)

        monkeypatch.setattr(serve_cmd.core_apps, "checkout_app", _checkout)
        monkeypatch.setattr(
            serve_cmd.cache,
            "recache_project",
            lambda project, bench_path=None, warnings=None: recached.append((project, bench_path))
            or True,
        )

        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(
                daemon.base + "/api/v1/actions",
                {"action": "checkout_app", "project": "p", "app": "erpnext", "ref": "nope"},
            )

        # A failed git step is NOT a 200: the envelope's errors[0] is what the
        # page renders, and the report rides along as its outcome.
        assert exc.value.code == 422
        body = json.loads(exc.value.read())
        assert body["errors"][0]["title"] == "app.step_failed"
        assert body["errors"][0]["outcome"]["ok"] is False
        assert "couldn't find remote ref" in body["errors"][0]["message"]
        assert recached == []

    def test_a_successful_checkout_recaches_and_a_failed_recache_is_a_warning(
        self, daemon, monkeypatch
    ):
        def _checkout(project, app, ref, **kwargs):
            report = AppsReport(
                project=project,
                bench_path="/workspace/frappe-bench",
                results=[AppResult(app=app, site=None, action="checkout", ok=True)],
                ok=True,
            )
            return Result(status=Status.OK, data=report)

        recached = []
        monkeypatch.setattr(serve_cmd.core_apps, "checkout_app", _checkout)
        monkeypatch.setattr(
            serve_cmd.cache,
            "recache_project",
            lambda project, bench_path=None, warnings=None: bool(
                recached.append((project, bench_path))
            ),
        )

        status, body = _post(
            daemon.base + "/api/v1/actions",
            {"action": "checkout_app", "project": "p", "app": "erpnext", "ref": "main"},
        )

        assert recached == [("p", "/workspace/frappe-bench")]  # only the checked-out bench
        assert status == 200
        assert "data" in body, "the checkout landed; a failed recache must not fail it"
        assert any(w["code"] == "cache.recache_failed" for w in body["data"]["warnings"])

    def test_refresh_status_rebootstraps_and_reprobes(self, daemon, monkeypatch):
        called = []
        monkeypatch.setattr(daemon.fleet, "bootstrap", lambda: called.append("bootstrap"))
        monkeypatch.setattr(
            daemon.fleet, "probe", lambda project: called.append(f"probe:{project}")
        )

        status, body = _post(
            daemon.base + "/api/v1/actions", {"action": "refresh_status", "project": "p"}
        )

        assert status == 200
        assert "data" in body
        assert body["data"]["outcome"]["project"] == "p"
        assert called == ["bootstrap", "probe:p"]

    def test_refresh_status_of_a_missing_project_is_404(self, daemon):
        with pytest.raises(urllib.error.HTTPError) as e:
            _post(daemon.base + "/api/v1/actions", {"action": "refresh_status", "project": "ghost"})

        assert e.value.code == 404


class TestLogsEndpoint:
    """GET /api/v1/instances/<p>/logs - the bounded read, action-guarded."""

    def _read(self, monkeypatch, result_or_exc, seen=None):
        def _read_logs(project, **kwargs):
            if seen is not None:
                seen.update({"project": project, **kwargs})
            if isinstance(result_or_exc, Exception):
                raise result_or_exc
            return result_or_exc

        monkeypatch.setattr(serve_cmd.core_logs, "read_logs_since", _read_logs)

    def test_it_dispatches_the_query_to_the_core_read(self, daemon, monkeypatch):
        seen: dict = {}
        self._read(
            monkeypatch,
            Result(
                status=Status.OK,
                data=LogsTail(
                    project="p",
                    container_name="p-frappe-1",
                    bench_path="/workspace/frappe-bench",
                    not_cwcli_supervised=False,
                    logs=[
                        ProcessTail(
                            process="web",
                            file="/workspace/frappe-bench/logs/web.supervisor.log",
                            lines=["one", "two"],
                            offset=8,
                        )
                    ],
                ),
            ),
            seen,
        )

        status, body = _get(
            daemon.base + "/api/v1/instances/p/logs?lines=50&bench=0&process=web&after=web:3"
        )

        assert status == 200
        assert body["data"]["outcome"]["logs"][0]["lines"] == ["one", "two"]
        # The cursor rides back so the next poll resumes from it.
        assert body["data"]["outcome"]["logs"][0]["offset"] == 8
        assert seen == {
            "project": "p",
            "bench": "0",
            "process": "web",
            "lines": 50,
            "after": {"web": 3},
        }

    def test_it_is_not_cors_open_and_refuses_cross_origin_reads(self, daemon, monkeypatch):
        self._read(monkeypatch, AssertionError("cross-origin read must not dispatch"))
        req = urllib.request.Request(
            daemon.base + "/api/v1/instances/p/logs",
            headers={"Origin": "https://example.invalid", "Sec-Fetch-Site": "cross-site"},
        )

        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=_TIMEOUT)  # noqa: S310 - fixed localhost

        assert e.value.code == 403

    def test_a_multi_bench_choice_is_409_with_the_options(self, daemon, monkeypatch):
        self._read(
            monkeypatch,
            Result(
                status=Status.NEEDS_CHOICE,
                choice=Choice(
                    kind="select_bench",
                    param="bench",
                    prompt="Project 'p' has multiple benches; select one.",
                    options=[{"value": "0", "label": "/workspace/frappe-bench"}],
                ),
            ),
        )

        with pytest.raises(urllib.error.HTTPError) as e:
            _get(daemon.base + "/api/v1/instances/p/logs")

        assert e.value.code == 409
        body = json.loads(e.value.read())
        assert body["errors"][0]["type"] == "needs_choice"
        assert body["errors"][0]["choice"]["options"][0]["value"] == "0"

    def test_bad_lines_values_are_400_without_dispatch(self, daemon, monkeypatch):
        self._read(monkeypatch, AssertionError("an invalid lines value must not dispatch"))

        for lines in ("abc", "0", "1001", "-5"):
            with pytest.raises(urllib.error.HTTPError) as e:
                _get(daemon.base + f"/api/v1/instances/p/logs?lines={lines}")
            assert e.value.code == 400
            assert json.loads(e.value.read())["errors"][0]["title"] == "logs.lines_invalid"


class TestWhereEndpoint:
    """GET /api/v1/search - the cache search, verified/remembered tokens intact."""

    def test_the_verified_and_project_state_tokens_pass_through(self, daemon, monkeypatch):
        seen: dict = {}

        def _where(term, **kwargs):
            seen["term"] = term
            return Result(
                status=Status.WARNING,
                data=WhereResult(
                    matches=[
                        WhereMatch(
                            type="app",
                            project="gone-project",
                            bench="frappe-bench",
                            name="erpnext",
                            version="16.0.0",
                            branch="version-16",
                            installed=True,
                            project_state="unverified",
                        )
                    ],
                    verified=False,
                ),
            )

        monkeypatch.setattr(serve_cmd.core_where, "where", _where)

        status, body = _get(daemon.base + "/api/v1/search?q=erp")

        assert status == 200
        assert seen["term"] == "erp"
        assert body["data"]["verified"] is False, "an unreachable daemon must not read as verified"
        assert body["data"]["matches"][0]["project_state"] == "unverified"

    def test_an_empty_term_is_400(self, daemon, monkeypatch):
        monkeypatch.setattr(
            serve_cmd.core_where,
            "where",
            lambda term, **kw: pytest.fail("an empty search must not dispatch"),
        )

        for query in ("", "?q=", "?q=%20"):
            with pytest.raises(urllib.error.HTTPError) as e:
                _get(daemon.base + "/api/v1/search" + query)
            assert e.value.code == 400
            assert json.loads(e.value.read())["errors"][0]["title"] == "where.term_required"

    def test_it_refuses_cross_origin_reads(self, daemon, monkeypatch):
        monkeypatch.setattr(
            serve_cmd.core_where,
            "where",
            lambda term, **kw: pytest.fail("cross-origin read must not dispatch"),
        )
        req = urllib.request.Request(
            daemon.base + "/api/v1/search?q=erp",
            headers={"Origin": "https://example.invalid", "Sec-Fetch-Site": "cross-site"},
        )

        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=_TIMEOUT)  # noqa: S310 - fixed localhost

        assert e.value.code == 403


class TestUrlEndpoint:
    """GET /api/v1/instances/<p>/url - the fresh host-URL + reachability probe.

    A pure read, but it execs curl in the container, so it inherits the action
    guards (same-origin only, serialized under the project lock) - the read_logs
    precedent - and a NEEDS_CHOICE from the core surfaces as a 409, never an
    auto-start.
    """

    def _probe(self, monkeypatch, result_or_exc, seen=None):
        def _probe_url(project, **kwargs):
            if seen is not None:
                seen.update({"project": project, **kwargs})
            if isinstance(result_or_exc, Exception):
                raise result_or_exc
            return result_or_exc

        monkeypatch.setattr(serve_cmd.core_url, "probe_url", _probe_url)

    def test_it_dispatches_the_query_to_the_core_probe(self, daemon, monkeypatch):
        seen: dict = {}
        self._probe(
            monkeypatch,
            Result(
                status=Status.OK,
                data=UrlProbe(
                    project="p",
                    bench_path="/workspace/frappe-bench",
                    site="dev.localhost",
                    url="http://dev.localhost:8000",
                    reachable=True,
                    http_code="200",
                ),
            ),
            seen,
        )

        status, body = _get(daemon.base + "/api/v1/instances/p/url?bench=0&site=dev.localhost")

        assert status == 200
        assert "data" in body
        assert body["data"]["outcome"]["url"] == "http://dev.localhost:8000"
        assert body["data"]["outcome"]["reachable"] is True
        assert seen == {"project": "p", "bench": "0", "site": "dev.localhost"}

    def test_an_unresolved_port_is_reported_honestly_not_guessed(self, daemon, monkeypatch):
        self._probe(
            monkeypatch,
            Result(
                status=Status.WARNING,
                data=UrlProbe(
                    project="p",
                    bench_path="/workspace/frappe-bench",
                    site=None,
                    url=None,
                    reachable=False,
                    http_code=None,
                ),
                warnings=[Message("url.port_unknown", "Could not resolve the host port.")],
            ),
        )

        status, body = _get(daemon.base + "/api/v1/instances/p/url")

        assert status == 200
        assert body["data"]["outcome"]["url"] is None
        assert body["data"]["outcome"]["reachable"] is False

    def test_it_refuses_cross_origin_reads(self, daemon, monkeypatch):
        self._probe(monkeypatch, AssertionError("cross-origin read must not dispatch"))
        req = urllib.request.Request(
            daemon.base + "/api/v1/instances/p/url",
            headers={"Origin": "https://example.invalid", "Sec-Fetch-Site": "cross-site"},
        )

        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=_TIMEOUT)  # noqa: S310 - fixed localhost

        assert e.value.code == 403

    def test_a_multi_bench_choice_is_409_not_an_auto_start(self, daemon, monkeypatch):
        self._probe(
            monkeypatch,
            Result(
                status=Status.NEEDS_CHOICE,
                choice=Choice(
                    kind="select_bench",
                    param="bench",
                    prompt="Project 'p' has multiple benches; select one.",
                    options=[{"value": "0", "label": "/workspace/frappe-bench"}],
                ),
            ),
        )

        with pytest.raises(urllib.error.HTTPError) as e:
            _get(daemon.base + "/api/v1/instances/p/url")

        assert e.value.code == 409
        body = json.loads(e.value.read())
        assert body["errors"][0]["type"] == "needs_choice"
        assert body["errors"][0]["choice"]["options"][0]["value"] == "0"

    def test_a_core_error_becomes_the_matching_http_status(self, daemon, monkeypatch):
        self._probe(
            monkeypatch,
            CwcliError(ErrorKind.NOT_FOUND, "instance.not_found", "No instance named 'p'."),
        )

        with pytest.raises(urllib.error.HTTPError) as e:
            _get(daemon.base + "/api/v1/instances/p/url")

        assert e.value.code == 404


class TestDoctorEndpoint:
    """GET /api/v1/doctor - the system-wide read-only preflight."""

    def _report(self, monkeypatch, report):
        monkeypatch.setattr(
            serve_cmd.core_doctor, "run_all", lambda: Result(status=Status.WARNING, data=report)
        )

    def test_it_serializes_every_tier_and_the_freshness_qualifier(self, daemon, monkeypatch):
        self._report(
            monkeypatch,
            DoctorReport(
                checks=[
                    CheckResult(
                        id="docker",
                        title="Docker daemon",
                        group="Docker",
                        status=CheckStatus.PASS,
                        detail="reachable",
                    ),
                    CheckResult(
                        id="version",
                        title="cwcli version",
                        group="cwcli",
                        status=CheckStatus.PASS,
                        detail="1.0.0 installed",
                        version_verified=False,
                    ),
                    CheckResult(
                        id="sendme",
                        title="sendme",
                        group="Optional tools",
                        status=CheckStatus.WARN,
                        detail="not found",
                        fix="install sendme for peer-to-peer transfer",
                    ),
                ],
                passed=2,
                warned=1,
                failed=0,
                ok=True,
            ),
        )

        status, body = _get(daemon.base + "/api/v1/doctor")

        assert status == 200
        assert (
            body["data"]["passed"],
            body["data"]["warned"],
            body["data"]["failed"],
            body["data"]["ok"],
        ) == (2, 1, 0, True)
        # The enum is flattened to its value token, not left unserializable.
        assert [c["status"] for c in body["data"]["checks"]] == ["pass", "pass", "warn"]
        # A version check that could not reach PyPI stays honestly unverified.
        assert body["data"]["checks"][1]["version_verified"] is False
        assert body["data"]["checks"][2]["fix"] == "install sendme for peer-to-peer transfer"

    def test_a_failed_check_carries_through_so_the_page_can_render_action_needed(
        self, daemon, monkeypatch
    ):
        self._report(
            monkeypatch,
            DoctorReport(
                checks=[
                    CheckResult(
                        id="docker",
                        title="Docker daemon",
                        group="Docker",
                        status=CheckStatus.FAIL,
                        detail="daemon unreachable",
                        fix="start Docker",
                    )
                ],
                passed=0,
                warned=0,
                failed=1,
                ok=False,
            ),
        )

        status, body = _get(daemon.base + "/api/v1/doctor")

        assert status == 200
        assert body["data"]["ok"] is False
        assert body["data"]["checks"][0]["status"] == "fail"

    def test_it_is_gated_by_authentication(self, monkeypatch):
        # The read reveals home paths, versions and port collisions, so it sits
        # behind the same auth gate as every other /api read.
        token = "s3cret"
        monkeypatch.setenv("CWCLI_SERVE_TOKEN", token)
        rows = [InstanceDTO(project_name="p", status="running", ports=["8000"])]
        monkeypatch.setattr(
            core_fleet, "list_instances", lambda: Result(status=Status.OK, data=list(rows))
        )
        monkeypatch.setattr(
            core_doctor,
            "run_all",
            lambda: pytest.fail("an unauthenticated caller must not reach doctor"),
        )
        fleet = core_fleet.Fleet()
        hub = serve_cmd._Hub(fleet.set_focus)
        fleet.set_publish(hub.publish)
        fleet.bootstrap()
        httpd = serve_cmd.make_server("127.0.0.1", 0, fleet, hub, token)
        thread = threading.Thread(
            target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        thread.start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            with pytest.raises(urllib.error.HTTPError) as e:
                _get(base + "/api/v1/doctor")
            assert e.value.code in (401, 403)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_it_refuses_cross_origin_reads_without_dispatch_or_cors(self, daemon, monkeypatch):
        monkeypatch.setattr(
            core_doctor,
            "run_all",
            lambda: pytest.fail("cross-origin doctor must not dispatch"),
        )
        req = urllib.request.Request(
            daemon.base + "/api/v1/doctor",
            headers={"Origin": "https://example.invalid", "Sec-Fetch-Site": "cross-site"},
        )

        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=_TIMEOUT)  # noqa: S310 - fixed localhost

        assert e.value.code == 403
        assert e.value.headers.get("Access-Control-Allow-Origin") is None


class TestJobsEndpoint:
    """The HTTP boundary for durable jobs, including the existing security guards."""

    def test_list_and_detail_return_durable_records(self, daemon, monkeypatch):
        records = [{"id": "20261024120000-abcdef123456", "state": "running"}]
        monkeypatch.setattr(serve_cmd.core_jobs, "list_jobs", lambda: records)
        monkeypatch.setattr(
            serve_cmd.core_jobs,
            "read_job",
            lambda job_id, **kwargs: {
                **records[0],
                "id": job_id,
                "events": [{"seq": kwargs["after"] + 1}],
            },
        )

        assert _get(daemon.base + "/api/v1/jobs") == (200, {"data": records})
        status, body = _get(daemon.base + "/api/v1/jobs/20261024120000-abcdef123456?after=4")
        assert status == 200
        assert body["data"]["events"] == [{"seq": 5}]

    def test_create_keeps_secrets_in_the_dedicated_channel(self, daemon, monkeypatch):
        seen = {}

        def create(verb, target, args, **kwargs):
            seen.update({"verb": verb, "target": target, "args": args, **kwargs})
            return {
                "id": "20261024120000-abcdef123456",
                "verb": verb,
                "target": target,
                "args": args,
                "state": "queued",
            }

        monkeypatch.setattr(serve_cmd.core_jobs, "create_job", create)

        status, body = _post(
            daemon.base + "/api/v1/jobs",
            {
                "verb": "init",
                "target": {"project": "new-site"},
                "args": {"version": "16"},
                "secrets": {"admin_password": "not-persisted"},
            },
        )

        assert status == 202
        assert body["data"]["state"] == "queued"
        assert seen["secret_values"] == {"admin_password": "not-persisted"}
        assert seen["args"] == {"version": "16"}
        assert seen["ui_context"]["token"] == _TOKEN
        assert "not-persisted" not in json.dumps(body)

    def test_interrupt_and_detach_dispatch_to_the_named_job(self, daemon, monkeypatch):
        calls = []
        monkeypatch.setattr(
            serve_cmd.core_jobs,
            "interrupt_job",
            lambda job_id: calls.append(("interrupt", job_id)) or {"id": job_id},
        )
        monkeypatch.setattr(
            serve_cmd.core_jobs,
            "detach_job",
            lambda job_id: calls.append(("detach", job_id)) or {"id": job_id},
        )

        job_id = "20261024120000-abcdef123456"
        assert _post(daemon.base + f"/api/v1/jobs/{job_id}/interrupt", {})[0] == 200
        assert _post(daemon.base + f"/api/v1/jobs/{job_id}/detach", {})[0] == 200
        assert calls == [("interrupt", job_id), ("detach", job_id)]

    def test_retry_starts_a_new_job_from_the_named_one(self, daemon, monkeypatch):
        seen = {}

        def retry(job_id, *, ui_context, origin):
            seen.update(job_id=job_id, token=ui_context["token"], role=origin["role"])
            return {"id": "20261024120001-abcdef123456", "state": "queued"}

        monkeypatch.setattr(serve_cmd.core_jobs, "retry_job", retry)

        status, body = _post(daemon.base + "/api/v1/jobs/20261024120000-abcdef123456/retry", {})

        assert status == 202
        assert body["data"]["id"] == "20261024120001-abcdef123456"
        assert seen == {"job_id": "20261024120000-abcdef123456", "token": _TOKEN, "role": "admin"}

    def test_continue_answers_a_waiting_job_without_page_secrets(self, daemon, monkeypatch):
        seen = {}

        def continue_job(job_id, target, args, *, consent, ui_context, origin):
            seen.update(
                job_id=job_id, target=target, args=args, consent=consent, role=origin["role"]
            )
            return {"id": "20261024120001-abcdef123456", "state": "queued"}

        monkeypatch.setattr(serve_cmd.core_jobs, "continue_job", continue_job)

        status, body = _post(
            daemon.base + "/api/v1/jobs/20261024120000-abcdef123456/continue",
            {"target": {"project": "p", "site": "s"}, "args": {}, "consent": True},
        )

        assert status == 202
        assert body["data"]["id"] == "20261024120001-abcdef123456"
        assert seen == {
            "job_id": "20261024120000-abcdef123456",
            "target": {"project": "p", "site": "s"},
            "args": {},
            "consent": True,
            "role": "admin",
        }

    def test_job_changes_reach_the_existing_event_stream(self, daemon, monkeypatch):
        monkeypatch.setattr(serve_cmd.core_jobs, "list_jobs", lambda: [])
        stream = _Stream(daemon.base + "/api/v1/stream")
        try:
            event, snapshot = stream.next_frame()
            assert event == "snapshot" and snapshot["jobs"] == []
            daemon.hub.publish_job({"id": "20261024120000-abcdef123456", "state": "running"})
            event, record = stream.next_frame()
            assert event == "job"
            assert record["state"] == "running"
        finally:
            stream.close()

    def test_cross_origin_create_is_refused_before_dispatch(self, daemon, monkeypatch):
        monkeypatch.setattr(
            serve_cmd.core_jobs,
            "create_job",
            lambda *args, **kwargs: pytest.fail("cross-origin create reached the job runner"),
        )
        request = urllib.request.Request(
            daemon.base + "/api/v1/jobs",
            data=json.dumps({"verb": "backup", "target": {"project": "p"}, "args": {}}).encode(),
            headers={
                "Content-Type": "application/json",
                "Origin": "https://example.invalid",
                "Sec-Fetch-Site": "cross-site",
            },
            method="POST",
        )

        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=_TIMEOUT)  # noqa: S310

        assert error.value.code == 403
        assert error.value.headers.get("Access-Control-Allow-Origin") is None


class TestSettings:
    """``POST /api/v1/settings``: each write lands on the one core function the CLI
    renderer calls, consent stays in the core, and nothing else is reachable."""

    def _post(self, daemon, payload):
        return _post(daemon.base + "/api/v1/settings", payload)

    @pytest.mark.parametrize(
        ("payload", "module", "function", "expected_args", "expected_kwargs"),
        [
            ({"setting": "cache.enable"}, "core_config", "set_cache", (True,), {}),
            ({"setting": "cache.disable"}, "core_config", "set_cache", (False,), {}),
            ({"setting": "tips.enable"}, "core_config", "set_tips", (True,), {}),
            ({"setting": "tips.disable"}, "core_config", "set_tips", (False,), {}),
            ({"setting": "paths.add", "path": "/x"}, "core_config", "add_search_path", ("/x",), {}),
            (
                {"setting": "paths.remove", "path": "/x"},
                "core_config",
                "remove_search_path",
                ("/x",),
                {},
            ),
            ({"setting": "paths.prune"}, "core_config", "prune_search_paths", (), {"apply": False}),
            (
                {"setting": "paths.prune", "apply": True},
                "core_config",
                "prune_search_paths",
                (),
                {"apply": True},
            ),
            (
                {"setting": "paths.prune", "apply": "yes"},
                "core_config",
                "prune_search_paths",
                (),
                {"apply": False},
            ),
            (
                {"setting": "cache.clear", "project": "p"},
                "core_config",
                "clear_cache",
                ("p",),
                {"all_projects": False, "consent": False},
            ),
            (
                {"setting": "cache.clear", "all": True, "consent": True},
                "core_config",
                "clear_cache",
                (None,),
                {"all_projects": True, "consent": True},
            ),
            (
                {"setting": "auto_inspect.enable"},
                "core_auto_inspect",
                "enable",
                (),
                {"interval": None, "at_boot": None},
            ),
            (
                {"setting": "auto_inspect.enable", "interval": 600, "at_boot": True},
                "core_auto_inspect",
                "enable",
                (),
                {"interval": 600, "at_boot": True},
            ),
            ({"setting": "auto_inspect.disable"}, "core_auto_inspect", "disable", (), {}),
            (
                {"setting": "cred_bridge.enable", "at_boot": False},
                "core_cred_bridge",
                "enable",
                (),
                {"at_boot": False},
            ),
            ({"setting": "cred_bridge.disable"}, "core_cred_bridge", "disable", (), {}),
            ({"setting": "cred_bridge.start"}, "core_cred_bridge", "start", (), {}),
            ({"setting": "cred_bridge.restart"}, "core_cred_bridge", "restart", (), {}),
            ({"setting": "auto_inspect.start"}, "core_auto_inspect", "start", (), {}),
            ({"setting": "auto_inspect.restart"}, "core_auto_inspect", "restart", (), {}),
            ({"setting": "agent_hooks.install"}, "agent_hooks", "install", (), {}),
        ],
    )
    def test_each_setting_lands_on_its_core_function(
        self, daemon, monkeypatch, payload, module, function, expected_args, expected_kwargs
    ):
        calls = []

        def fake(*args, **kwargs):
            calls.append((args, kwargs))
            if function == "install":
                return [
                    agent_hooks.HookOutcome("claude-code", "/h/.claude/settings.json", "installed")
                ]
            return Result(status=Status.OK, data={"done": True})

        monkeypatch.setattr(getattr(serve_cmd, module), function, fake)

        status, body = self._post(daemon, payload)

        assert status == 200
        assert calls == [(expected_args, expected_kwargs)]
        assert body["data"]["setting"] == payload["setting"]
        assert body["data"]["status"] == "ok"
        if function == "install":
            assert body["data"]["outcome"] == [
                {
                    "agent": "claude-code",
                    "path": "/h/.claude/settings.json",
                    "status": "installed",
                    "detail": "",
                }
            ]

    def test_clearing_the_whole_cache_keeps_the_cores_consent(self, daemon):
        """The REAL core: `clear_cache(all_projects=True)` without consent is the
        core's own `confirm_clear` choice, rendered 409 and naming the consent
        field, so no page can bypass it by shaping the request."""
        with pytest.raises(urllib.error.HTTPError) as error:
            self._post(daemon, {"setting": "cache.clear", "all": True})

        assert error.value.code == 409
        body = json.loads(error.value.read())["errors"][0]
        assert body["type"] == "needs_choice"
        assert body["title"] == "confirm_clear"
        assert body["choice"]["param"] == "consent"
        assert "consent: true" in body["hint"]

    def test_cache_clear_consent_is_strictly_the_json_boolean_true(self, daemon, monkeypatch):
        calls = []
        monkeypatch.setattr(
            serve_cmd.core_config,
            "clear_cache",
            lambda project, **kwargs: calls.append(kwargs["consent"])
            or Result(status=Status.OK, data={"scope": "all"}),
        )

        for consent in ("yes", 1, "true"):
            self._post(daemon, {"setting": "cache.clear", "all": True, "consent": consent})

        assert calls == [False, False, False]

    def test_a_core_refusal_maps_to_its_http_status(self, daemon):
        """The REAL core refuses a relative search path as USAGE (400); the daemon
        adds no validation of its own and loses none of the core's."""
        with pytest.raises(urllib.error.HTTPError) as error:
            self._post(daemon, {"setting": "paths.add", "path": "relative/bench"})

        assert error.value.code == 400
        assert json.loads(error.value.read())["errors"][0]["title"] == "path.not_absolute"

    @pytest.mark.parametrize(
        ("payload", "title"),
        [
            ({"setting": "auto_inspect.enable", "interval": "600"}, "setting.interval_invalid"),
            ({"setting": "auto_inspect.enable", "interval": True}, "setting.interval_invalid"),
            ({"setting": "auto_inspect.enable", "at_boot": "yes"}, "setting.at_boot_invalid"),
            ({"setting": "cred_bridge.enable", "at_boot": 1}, "setting.at_boot_invalid"),
            ({"setting": "paths.add"}, "action.path_required"),
            ({"setting": "nope"}, "setting.unsupported"),
            ({}, "action.setting_required"),
        ],
    )
    def test_malformed_requests_are_refused_before_any_core_call(
        self, daemon, monkeypatch, payload, title
    ):
        for module, names in (
            ("core_auto_inspect", ("enable",)),
            ("core_cred_bridge", ("enable",)),
            ("core_config", ("add_search_path",)),
        ):
            for name in names:
                monkeypatch.setattr(
                    getattr(serve_cmd, module),
                    name,
                    lambda *a, **k: pytest.fail("a malformed request reached the core"),
                )

        with pytest.raises(urllib.error.HTTPError) as error:
            self._post(daemon, payload)

        assert error.value.code == 400
        assert json.loads(error.value.read())["errors"][0]["title"] == title

    def test_the_unsupported_hint_names_exactly_the_allowed_set(self, daemon):
        with pytest.raises(urllib.error.HTTPError) as error:
            self._post(daemon, {"setting": "nope"})

        hint = json.loads(error.value.read())["errors"][0]["hint"]
        named = set(hint.removeprefix("Allowed settings are ").removesuffix(".").split(", "))
        assert named == set(serve_cmd.SETTINGS)
        # No destructive verb hides here: rm/rm-site/rm-bench/checkout --reset are
        # jobs behind a typed confirmation, and restore/uninstall stay Phase 4.
        assert not {"rm", "restore", "uninstall"} & {n.split(".")[0] for n in named}

    def test_a_cross_origin_settings_write_is_refused_before_dispatch(self, daemon, monkeypatch):
        monkeypatch.setattr(
            serve_cmd.core_config,
            "set_tips",
            lambda *a, **k: pytest.fail("a cross-origin write reached the core"),
        )
        request = urllib.request.Request(
            daemon.base + "/api/v1/settings",
            data=json.dumps({"setting": "tips.enable"}).encode(),
            headers={
                "Content-Type": "application/json",
                "Origin": "https://example.invalid",
                "Sec-Fetch-Site": "cross-site",
            },
            method="POST",
        )

        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=_TIMEOUT)  # noqa: S310

        assert error.value.code == 403

    def test_the_destructive_verbs_stay_out_of_the_synchronous_action_set(self, daemon):
        """They are JOBS (typed confirmation, consent in the spec, the verified
        backup gate in the core), never a synchronous action."""
        assert not {"rm", "rm_instance", "rm_site", "rm_bench", "remove_instance"} & set(
            serve_cmd.ACTIONS
        )
        for verb in ("rm", "rm-site", "rm-bench"):
            assert verb in serve_cmd.core_jobs.ALLOWED_VERBS
