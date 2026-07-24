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
from caffeinated_whale_cli.core import fleet as core_fleet
from caffeinated_whale_cli.core import inspect as core_inspect
from caffeinated_whale_cli.core.envelope import Choice, Result, Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from caffeinated_whale_cli.core.inspect import BenchInfo, InspectReport, SiteInfo
from caffeinated_whale_cli.core.list import InstanceDTO
from caffeinated_whale_cli.core.restart import ProcessRestartOutcome
from caffeinated_whale_cli.core.start import ProcessLaunch, StartOutcome
from caffeinated_whale_cli.core.stop import StopOutcome

_TIMEOUT = 5.0


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

    httpd = serve_cmd.make_server("127.0.0.1", 0, fleet, hub)
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
        httpd.shutdown()
        httpd.server_close()


def _get(url):
    with urllib.request.urlopen(url, timeout=_TIMEOUT) as resp:  # noqa: S310 - fixed localhost
        return resp.status, json.loads(resp.read())


def _post(url, payload, headers=None):
    request_headers = {"Content-Type": "application/json"}
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
        status, body = _get(daemon.base + "/api/snapshot")
        assert status == 200
        assert [i["project"] for i in body["instances"]] == ["p"]
        # Nothing has probed yet, and the snapshot says so rather than guessing.
        assert body["instances"][0]["overall"] == core_fleet.UNKNOWN
        assert body["instances"][0]["probe_ms"] is None
        assert body["instances"][0]["probe_failed_at"] is None

    def test_an_unknown_path_is_404(self, daemon):
        with pytest.raises(urllib.error.HTTPError) as e:
            _get(daemon.base + "/api/nope")
        assert e.value.code == 404

    def test_the_root_serves_the_console_ui(self, daemon):
        with urllib.request.urlopen(daemon.base + "/", timeout=_TIMEOUT) as resp:  # noqa: S310
            body = resp.read().decode()
        assert resp.status == 200
        assert resp.headers["Content-Security-Policy"] == "frame-ancestors 'none'"
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert 'data-app="cw-console"' in body
        assert "EventSource" in body
        assert "start_instance" in body
        assert "restart_process" in body
        assert 'role="status" aria-live="polite"' in body
        assert "restoreControlFocus" in body
        assert 'role="tablist" aria-label="Instance details"' in body
        assert 'role="tabpanel"' in body
        assert 'aria-selected="${selectedTab ? "true" : "false"}"' in body
        assert 'aria-controls="detail"' in body
        assert 'event.key === "ArrowRight"' in body
        assert 'role="${lastAction.ok' not in body
        assert 'textContent = lastAction ? lastAction.text : ""' in body
        assert "throwaway test page" not in body
        assert "rm-site" not in body
        assert "Delete instance" not in body

    def test_the_console_ui_keeps_its_tightening_invariants(self, daemon):
        """The phase-4 hardening pins: connection honesty, honest boot state,
        screen-reader churn control, and exposed tree semantics."""
        with urllib.request.urlopen(daemon.base + "/", timeout=_TIMEOUT) as resp:  # noqa: S310
            body = resp.read().decode()
        # A lost connection is shown, not whispered: a visible banner names the
        # stale data and its timestamp, and a dedicated live region announces
        # the loss and the recovery exactly once each.
        assert 'id="conn-banner"' in body
        assert "Connection lost - reconnecting. Showing last known state" in body
        assert "Connection restored - live again." in body
        assert 'id="conn-status"' in body
        # A hard-closed EventSource self-reconnects; every reconnect re-opens
        # /api/events whose first frame is a full snapshot - never a replay.
        assert "EventSource.CLOSED" in body
        assert "since=" not in body
        # Transport readiness is not data readiness: only an applied snapshot
        # restores live state, and callbacks from a replaced source do nothing.
        assert "const es = new EventSource(url);" in body
        assert body.count("if (source !== es) return;") == 4
        open_handler = body.split('es.addEventListener("open"', 1)[1].split(
            'es.addEventListener("snapshot"', 1
        )[0]
        assert 'setConnection("open")' not in open_handler
        snapshot_handler = body.split('es.addEventListener("snapshot"', 1)[1].split(
            'es.addEventListener("delta"', 1
        )[0]
        assert snapshot_handler.index("render();") < snapshot_handler.index(
            'setConnection("open");'
        )
        # "No instances found" is a claim only a snapshot can back; before one
        # arrives the page says it is still waiting.
        assert "Waiting for the daemon - no fleet data yet" in body
        # The event log is a visual feed, NOT a live region: it is re-rendered
        # wholesale on every delta, which a polite region re-announces in full.
        assert '<div id="event-log" class="event-log"></div>' in body
        # Tree semantics are exposed, not just styled.
        assert "aria-expanded" in body
        assert 'aria-current="true"' in body
        # The shell is viewport-fixed so panels scroll, never the whole page.
        assert "height: 100vh" in body


class TestEventStream:
    def test_the_first_frame_is_a_full_snapshot(self, daemon):
        stream = _Stream(daemon.base + "/api/events")
        try:
            event, data = stream.next_frame()
            assert event == "snapshot"
            assert [i["project"] for i in data["instances"]] == ["p"]
        finally:
            stream.close()

    def test_a_fast_delta_reaches_the_client_tagged_with_its_tier(self, daemon):
        stream = _Stream(daemon.base + "/api/events")
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
        stream = _Stream(daemon.base + "/api/events")
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
        stream = _Stream(daemon.base + "/api/events")
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
        streams = [_Stream(daemon.base + "/api/events") for _ in range(3)]
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
        stream = _Stream(daemon.base + "/api/events")
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
        stream = _RawSseSocket(daemon.base, "/api/events?focus=p")
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

        stream = _Stream(daemon.base + "/api/events?focus=p")
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
        stream = _Stream(daemon.base + "/api/events?focus=p")
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

        status, body = _get(daemon.base + "/api/instance/p/detail")

        assert status == 200
        assert body["served_from"] == "partial"
        site = body["benches"][0]["sites"][0]
        assert site["installed_apps_verified"] is False, "a remembered ref must not read as fresh"

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
        _get(daemon.base + "/api/instance/p/detail")

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
            _get(daemon.base + "/api/instance/p/detail")
        assert e.value.code == expected
        assert json.loads(e.value.read())["error"]["code"] == "some.code"


class TestActions:
    """The Console rail exposes only the approved narrow action set."""

    def test_action_preflight_does_not_grant_cross_origin_access(self, daemon):
        req = urllib.request.Request(
            daemon.base + "/api/action",
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
            daemon.base + "/api/action",
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
        assert body["error"]["code"] == "action.origin_forbidden"

    def test_action_body_must_be_json_before_dispatch(self, daemon, monkeypatch):
        monkeypatch.setattr(
            serve_cmd.core_stop,
            "stop",
            lambda project: pytest.fail("non-JSON action must not dispatch"),
        )
        req = urllib.request.Request(
            daemon.base + "/api/action",
            data=b'{"action": "stop_instance", "project": "p"}',
            method="POST",
            headers={"Content-Type": "text/plain"},
        )

        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=_TIMEOUT)  # noqa: S310 - fixed localhost

        assert e.value.code == 415
        body = json.loads(e.value.read())
        assert body["error"]["code"] == "request.content_type_invalid"

    def test_negative_content_length_is_rejected_before_reading(self, daemon):
        host, port = daemon.base.removeprefix("http://").split(":", 1)
        connection = http.client.HTTPConnection(host, int(port), timeout=_TIMEOUT)
        connection.putrequest("POST", "/api/action")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", "-1")
        connection.endheaders()

        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()

        assert response.status == 400
        assert body["error"]["code"] == "request.length_invalid"

    def test_start_instance_dispatches_to_the_core(self, daemon, monkeypatch):
        called = {}
        monkeypatch.setattr(serve_cmd.start_cmd, "_frappe_running", lambda project: True)

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
            daemon.base + "/api/action",
            {"action": "start_instance", "project": "p", "bench": "0"},
        )

        assert status == 200
        assert body["ok"] is True
        assert body["outcome"]["bench_path"] == "/workspace/frappe-bench"
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
            daemon.base + "/api/action",
            {"action": "stop_instance", "project": "p"},
            headers={
                "Origin": daemon.base,
                "Sec-Fetch-Site": "same-origin",
            },
        )

        assert status == 200
        assert body["ok"] is True
        assert body["outcome"]["stopped"] == 3
        assert called["project"] == "p"

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
            daemon.base + "/api/action",
            {"action": "restart_process", "project": "p", "bench": "0", "process": "web"},
        )

        assert status == 200
        assert body["ok"] is True
        assert body["outcome"]["old_pid"] == 11
        assert body["outcome"]["new_pid"] == 12
        assert called == {"project": "p", "process": "web", "bench": "0", "probe": "p"}

    def test_start_refuses_port_conflicts_without_stopping_other_instances(
        self, daemon, monkeypatch
    ):
        monkeypatch.setattr(serve_cmd.start_cmd, "_frappe_running", lambda project: False)
        monkeypatch.setattr(
            serve_cmd.start_cmd,
            "detect_port_conflicts",
            lambda project: (["other"], [8100]),
        )
        monkeypatch.setattr(
            serve_cmd.core_start,
            "start",
            lambda *a, **kw: pytest.fail("start must not run when ports conflict"),
        )

        with pytest.raises(urllib.error.HTTPError) as e:
            _post(daemon.base + "/api/action", {"action": "start_instance", "project": "p"})

        assert e.value.code == 409
        body = json.loads(e.value.read())
        assert body["error"]["code"] == "start.port_conflict"
        assert "other" in body["error"]["message"]
        assert "8100" in body["error"]["message"]

    def test_restart_instance_resolves_bench_before_stopping(self, daemon, monkeypatch):
        choice = Choice(
            kind="select_bench",
            param="bench",
            prompt="Project 'p' has multiple benches; select one.",
            options=[{"value": "0", "label": "/workspace/frappe-bench"}],
        )
        monkeypatch.setattr(serve_cmd.start_cmd, "_frappe_running", lambda project: True)
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
            _post(daemon.base + "/api/action", {"action": "restart_instance", "project": "p"})

        assert e.value.code == 409
        body = json.loads(e.value.read())
        assert body["error"]["code"] == "select_bench"
        assert body["choice"]["options"][0]["value"] == "0"

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
                    daemon.base + "/api/action",
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
            _post(daemon.base + "/api/action", {"action": "rm_instance", "project": "p"})

        assert e.value.code == 400
        body = json.loads(e.value.read())
        assert body["error"]["code"] == "action.unsupported"
