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
from caffeinated_whale_cli.core.logs import LogsRead, ProcessLog
from caffeinated_whale_cli.core.restart import ProcessRestartOutcome
from caffeinated_whale_cli.core.start import ProcessLaunch, StartOutcome
from caffeinated_whale_cli.core.stop import StopOutcome
from caffeinated_whale_cli.core.unlock import UnlockOutcome
from caffeinated_whale_cli.core.url import UrlProbe
from caffeinated_whale_cli.core.where import WhereMatch, WhereResult

_TIMEOUT = 5.0


def _render_template_pattern(source, name):
    tag = source.split(f'name="{name}"', 1)[1].split(">", 1)[0]
    encoded = tag.split('pattern="', 1)[1].split('"', 1)[0]
    rendered = []
    cursor = 0
    while cursor < len(encoded):
        if encoded[cursor] != "\\":
            rendered.append(encoded[cursor])
            cursor += 1
            continue
        cursor += 1
        if cursor == len(encoded):
            rendered.append("\\")
            break
        escaped = encoded[cursor]
        rendered.append({"\\": "\\", "n": "\n", "r": "\r", "t": "\t"}.get(escaped, escaped))
        cursor += 1
    return tag, "".join(rendered)


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
        # The daemon sends the page's OWN content CSP, because in the desktop
        # shell's remote-origin shape Tauri cannot inject one into a remote
        # server's response (Phase 0 report section 8.2). connect-src 'self' is
        # the load-bearing clause: it confines fetch/EventSource to this origin
        # so instance data cannot be exfiltrated to a remote host.
        csp = resp.headers["Content-Security-Policy"]
        assert csp == serve_cmd._CONSOLE_CSP
        assert "default-src 'none'" in csp
        assert "connect-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp
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

    def test_required_action_inputs_reject_whitespace_only_natively(self, daemon):
        """P2-1: a whitespace-only value used to pass ``required``, get dropped
        by the JS ``.trim()`` guard, and close the modal with no action and no
        feedback - two adjacent invalid inputs behaving completely differently.

        The fix makes native validation catch whitespace exactly as it catches
        empty, on every required rail-action input that is then trimmed. The
        ``site`` input is deliberately NOT in this set: blank there means the
        bench's default site, a valid choice, not a silent discard.
        """
        with urllib.request.urlopen(daemon.base + "/", timeout=_TIMEOUT) as resp:  # noqa: S310
            body = resp.read().decode()
        for name in ("label", "app", "ref"):
            tag, rendered_pattern = _render_template_pattern(body, name)
            assert "required" in tag
            assert rendered_pattern == r".*\S.*"
        # The optional site input keeps blank meaning "default site".
        site_tag = body.split('name="site"', 1)[1].split(">", 1)[0]
        assert "required" not in site_tag

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
        # A removal delta can replace the selected instance, so the visible
        # selection and the daemon's per-tab probe focus must move together.
        delta_handler = body.split('es.addEventListener("delta"', 1)[1].split("es.onerror", 1)[0]
        assert delta_handler.index("render();") < delta_handler.index("syncFocus();")
        # "No instances found" is a claim only a snapshot can back; before one
        # arrives the page says it is still waiting.
        assert "Waiting for the daemon - no fleet data yet" in body
        # The event log is a visual feed, NOT a live region: it is re-rendered
        # wholesale on every delta, which a polite region re-announces in full.
        assert '<div id="event-log" class="event-log"></div>' in body
        # Tree semantics are exposed, not just styled.
        assert "aria-expanded" in body
        assert 'aria-current="true"' in body
        # Above 760px the viewport-fixed shell makes panels scroll; the narrow
        # layout deliberately restores page scrolling.
        assert "height: 100vh" in body
        assert ".rail {\n    max-height: none;\n  }" in body
        # The summary reflows when the action rail becomes a bottom band, so
        # long values do not leave a one-letter orphan in the narrow detail pane.
        mid_width_rules = body.split("@media (max-width: 1050px)", 1)[1].split(
            "@media (max-width: 760px)", 1
        )[0]
        assert (
            ".summary-grid {\n" "    grid-template-columns: repeat(2, minmax(0, 1fr));\n" "  }"
        ) in mid_width_rules


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

    def test_the_action_set_is_exactly_tier_a(self, daemon):
        """The allowed set is the captain's Tier A ruling and nothing more: no
        long-running Tier B verb (init/backup/install/update/migrate/run-tests/
        build/rm/rm-site) and no deferred Tier C verb (restore/uninstall/run/
        open/config) may appear here without its own decision."""
        with pytest.raises(urllib.error.HTTPError) as e:
            _post(daemon.base + "/api/action", {"action": "nope", "project": "p"})

        hint = json.loads(e.value.read())["error"]["hint"]
        named = set(hint.removeprefix("Allowed actions are ").removesuffix(".").split(", "))
        assert named == {
            "start_instance",
            "stop_instance",
            "restart_instance",
            "restart_process",
            "refresh_status",
            "set_label",
            "unlock_site",
            "scale_instance",
            "checkout_app",
        }


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
            daemon.base + "/api/action",
            {"action": "set_label", "project": "p", "bench": "0", "label": "dev"},
        )

        assert status == 200
        assert body["ok"] is True
        assert body["outcome"]["label"] == "dev"
        assert called == {"project": "p", "bench": "0", "label": "dev", "probe": "p"}

    def test_set_label_requires_a_label(self, daemon):
        with pytest.raises(urllib.error.HTTPError) as e:
            _post(daemon.base + "/api/action", {"action": "set_label", "project": "p"})

        assert e.value.code == 400
        assert json.loads(e.value.read())["error"]["code"] == "action.label_required"

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
            daemon.base + "/api/action",
            {"action": "unlock_site", "project": "p", "site": "a.localhost"},
        )

        assert status == 200
        assert body["ok"] is True
        assert body["outcome"]["removed"] == ["a.lock"]
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
            _post(daemon.base + "/api/action", {"action": "unlock_site", "project": "p"})

        assert e.value.code == 409
        body = json.loads(e.value.read())
        assert body["error"]["kind"] == "needs_choice"
        assert body["error"]["code"] == "confirm_start"

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
            _post(daemon.base + "/api/action", {"action": "scale_instance", "project": "p"})

        assert e.value.code == 409
        body = json.loads(e.value.read())
        assert body["error"]["kind"] == "needs_choice"
        assert body["error"]["code"] == "confirm_scale"
        assert "RESTARTS" in body["error"]["message"]
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
                daemon.base + "/api/action",
                {"action": "scale_instance", "project": "p", "consent": "yes"},
            )
        with pytest.raises(urllib.error.HTTPError):
            _post(
                daemon.base + "/api/action",
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
                daemon.base + "/api/action",
                {"action": "scale_instance", "project": "p", "to": 8},
            )
        assert calls == [{"to": 8, "consent": False}]

        # JSON true is an int subclass in Python and must not read as "1 bench";
        # a string is not a count either. Both are refused before dispatch.
        for bad in (True, "8"):
            with pytest.raises(urllib.error.HTTPError) as e:
                _post(
                    daemon.base + "/api/action",
                    {"action": "scale_instance", "project": "p", "to": bad},
                )
            assert e.value.code == 400
            assert json.loads(e.value.read())["error"]["code"] == "action.to_invalid"
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
        monkeypatch.setattr(serve_cmd.cache, "recache_project", lambda project: True)

        status, body = _post(
            daemon.base + "/api/action",
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
        assert body["ok"] is True
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
                daemon.base + "/api/action",
                {
                    "action": "checkout_app",
                    "project": "p",
                    "app": "../other",
                    "ref": "main",
                },
            )

        assert exc.value.code == 400
        assert json.loads(exc.value.read())["error"]["code"] == "app.invalid_component"
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
            serve_cmd.cache, "recache_project", lambda project: recached.append(project) or True
        )

        status, body = _post(
            daemon.base + "/api/action",
            {"action": "checkout_app", "project": "p", "app": "erpnext", "ref": "nope"},
        )

        assert status == 200
        assert body["ok"] is False
        assert body["error"]["code"] == "app.step_failed"
        assert "couldn't find remote ref" in body["error"]["message"]
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

        monkeypatch.setattr(serve_cmd.core_apps, "checkout_app", _checkout)
        monkeypatch.setattr(serve_cmd.cache, "recache_project", lambda project: False)

        status, body = _post(
            daemon.base + "/api/action",
            {"action": "checkout_app", "project": "p", "app": "erpnext", "ref": "main"},
        )

        assert status == 200
        assert body["ok"] is True, "the checkout landed; a failed recache must not fail it"
        assert any(w["code"] == "cache.recache_failed" for w in body["warnings"])

    def test_refresh_status_rebootstraps_and_reprobes(self, daemon, monkeypatch):
        called = []
        monkeypatch.setattr(daemon.fleet, "bootstrap", lambda: called.append("bootstrap"))
        monkeypatch.setattr(
            daemon.fleet, "probe", lambda project: called.append(f"probe:{project}")
        )

        status, body = _post(
            daemon.base + "/api/action", {"action": "refresh_status", "project": "p"}
        )

        assert status == 200
        assert body["ok"] is True
        assert body["outcome"]["project"] == "p"
        assert called == ["bootstrap", "probe:p"]

    def test_refresh_status_of_a_missing_project_is_404(self, daemon):
        with pytest.raises(urllib.error.HTTPError) as e:
            _post(daemon.base + "/api/action", {"action": "refresh_status", "project": "ghost"})

        assert e.value.code == 404


class TestLogsEndpoint:
    """GET /api/instance/<p>/logs - the bounded read, action-guarded."""

    def _read(self, monkeypatch, result_or_exc, seen=None):
        def _read_logs(project, **kwargs):
            if seen is not None:
                seen.update({"project": project, **kwargs})
            if isinstance(result_or_exc, Exception):
                raise result_or_exc
            return result_or_exc

        monkeypatch.setattr(serve_cmd.core_logs, "read_logs", _read_logs)

    def test_it_dispatches_the_query_to_the_core_read(self, daemon, monkeypatch):
        seen: dict = {}
        self._read(
            monkeypatch,
            Result(
                status=Status.OK,
                data=LogsRead(
                    project="p",
                    container_name="p-frappe-1",
                    bench_path="/workspace/frappe-bench",
                    lines_requested=50,
                    not_cwcli_supervised=False,
                    logs=[
                        ProcessLog(
                            process="web",
                            file="/workspace/frappe-bench/logs/web.supervisor.log",
                            lines=["one", "two"],
                        )
                    ],
                ),
            ),
            seen,
        )

        status, body = _get(daemon.base + "/api/instance/p/logs?lines=50&bench=0&process=web")

        assert status == 200
        assert body["ok"] is True
        assert body["outcome"]["logs"][0]["lines"] == ["one", "two"]
        assert seen == {"project": "p", "bench": "0", "process": "web", "lines": 50}

    def test_it_is_not_cors_open_and_refuses_cross_origin_reads(self, daemon, monkeypatch):
        self._read(monkeypatch, AssertionError("cross-origin read must not dispatch"))
        req = urllib.request.Request(
            daemon.base + "/api/instance/p/logs",
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
            _get(daemon.base + "/api/instance/p/logs")

        assert e.value.code == 409
        body = json.loads(e.value.read())
        assert body["error"]["kind"] == "needs_choice"
        assert body["choice"]["options"][0]["value"] == "0"

    def test_bad_lines_values_are_400_without_dispatch(self, daemon, monkeypatch):
        self._read(monkeypatch, AssertionError("an invalid lines value must not dispatch"))

        for lines in ("abc", "0", "1001", "-5"):
            with pytest.raises(urllib.error.HTTPError) as e:
                _get(daemon.base + f"/api/instance/p/logs?lines={lines}")
            assert e.value.code == 400
            assert json.loads(e.value.read())["error"]["code"] == "logs.lines_invalid"


class TestWhereEndpoint:
    """GET /api/where - the cache search, verified/remembered tokens intact."""

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

        status, body = _get(daemon.base + "/api/where?q=erp")

        assert status == 200
        assert seen["term"] == "erp"
        assert body["verified"] is False, "an unreachable daemon must not read as verified"
        assert body["matches"][0]["project_state"] == "unverified"

    def test_an_empty_term_is_400(self, daemon, monkeypatch):
        monkeypatch.setattr(
            serve_cmd.core_where,
            "where",
            lambda term, **kw: pytest.fail("an empty search must not dispatch"),
        )

        for query in ("", "?q=", "?q=%20"):
            with pytest.raises(urllib.error.HTTPError) as e:
                _get(daemon.base + "/api/where" + query)
            assert e.value.code == 400
            assert json.loads(e.value.read())["error"]["code"] == "where.term_required"

    def test_it_refuses_cross_origin_reads(self, daemon, monkeypatch):
        monkeypatch.setattr(
            serve_cmd.core_where,
            "where",
            lambda term, **kw: pytest.fail("cross-origin read must not dispatch"),
        )
        req = urllib.request.Request(
            daemon.base + "/api/where?q=erp",
            headers={"Origin": "https://example.invalid", "Sec-Fetch-Site": "cross-site"},
        )

        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=_TIMEOUT)  # noqa: S310 - fixed localhost

        assert e.value.code == 403


class TestTierAConsoleUi:
    """The page's Tier A pins: exact button set, core-driven confirm, honest reads."""

    @pytest.fixture
    def page(self, daemon):
        with urllib.request.urlopen(daemon.base + "/", timeout=_TIMEOUT) as resp:  # noqa: S310
            return resp.read().decode()

    def test_the_rail_renders_exactly_the_tier_a_buttons(self, page):
        """A greyed-out or dead button for an excluded verb counts as building
        it; the button set is pinned to exactly the allowed action set."""
        import re

        rendered = set(re.findall(r'actionButton\("([a-z_]+)"', page))
        assert rendered == {
            "start_instance",
            "stop_instance",
            "restart_instance",
            "restart_process",
            "refresh_status",
            "set_label",
            "unlock_site",
            "scale_instance",
            "checkout_app",
        }

    def test_no_excluded_tier_verb_has_a_surface(self, page):
        # Tier B waits on the job backend; Tier C is deferred per-verb. None of
        # them may appear as a button label or an action token.
        for token in (
            "Backup",
            "Migrate",
            "Restore",
            "Uninstall",
            "Install app",
            "Update app",
            "Run tests",
            "Build assets",
            "Remove instance",
            "Drop site",
            "rm-site",
            "Delete instance",
        ):
            assert token not in page, f"excluded verb surfaced: {token}"

    def test_the_scale_confirm_is_core_driven_and_typed_name(self, page):
        # The modal opens ONLY off core's 409 confirm_scale, renders core's own
        # message, and the confirm button stays disabled until the project name
        # is typed back exactly. Its retry stays bound to the project whose
        # request produced that confirmation, even if selection changes.
        assert 'error.code === "confirm_scale"' in page
        assert "openScaleConfirm(payload.project, error.message" in page
        assert "confirm.disabled = input.value !== project;" in page
        assert "Object.assign({consent: true}" in page
        assert "Object.assign({consent: false}" in page
        assert "async function runAction(action, extra, targetProject)" in page
        assert "targetProject ? model.get(targetProject)" in page
        consent_retry = page.split("Object.assign({consent: true}", 1)[1].split(");", 1)[0]
        assert "project" in consent_retry

    def test_the_checkout_form_sends_only_app_and_ref(self, page):
        assert 'runAction("checkout_app", {app, ref})' in page
        assert "reset: " not in page, "the page must never send a reset flag"

    def test_the_new_read_surfaces_render_honest_unknowns(self, page):
        # where: the three project_state tokens each have a distinct rendering,
        # and an unverified sweep is announced rather than dressed as live.
        assert "could not verify" in page
        assert "instance gone" in page
        assert "matches are remembered, not verified" in page
        # logs: an empty read and a not-cwcli-supervised bench say what they are.
        assert "No logs written yet" in page
        assert "raw bench logs - not cwcli supervised" in page
        # apps detail: a cache row missing version/branch says "not recorded".
        assert "not recorded" in page

    def test_the_fleet_search_lives_outside_the_rerendered_rail(self, page):
        # The rail body re-renders on every delta; an input inside it would
        # lose its text and focus mid-typing. The search panel is static.
        rail_body_render = page.split('el("rail-body").innerHTML')[1].split("restoreControlFocus")[
            0
        ]
        assert "where-input" not in rail_body_render
        assert '<form id="where-form"' in page
        assert 'id="where-input"' in page


class TestUrlEndpoint:
    """GET /api/instance/<p>/url - the fresh host-URL + reachability probe.

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

        status, body = _get(daemon.base + "/api/instance/p/url?bench=0&site=dev.localhost")

        assert status == 200
        assert body["ok"] is True
        assert body["outcome"]["url"] == "http://dev.localhost:8000"
        assert body["outcome"]["reachable"] is True
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

        status, body = _get(daemon.base + "/api/instance/p/url")

        assert status == 200
        assert body["outcome"]["url"] is None
        assert body["outcome"]["reachable"] is False

    def test_it_refuses_cross_origin_reads(self, daemon, monkeypatch):
        self._probe(monkeypatch, AssertionError("cross-origin read must not dispatch"))
        req = urllib.request.Request(
            daemon.base + "/api/instance/p/url",
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
            _get(daemon.base + "/api/instance/p/url")

        assert e.value.code == 409
        body = json.loads(e.value.read())
        assert body["error"]["kind"] == "needs_choice"
        assert body["choice"]["options"][0]["value"] == "0"

    def test_a_core_error_becomes_the_matching_http_status(self, daemon, monkeypatch):
        self._probe(
            monkeypatch,
            CwcliError(ErrorKind.NOT_FOUND, "instance.not_found", "No instance named 'p'."),
        )

        with pytest.raises(urllib.error.HTTPError) as e:
            _get(daemon.base + "/api/instance/p/url")

        assert e.value.code == 404


class TestDoctorEndpoint:
    """GET /api/doctor - the system-wide read-only preflight."""

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

        status, body = _get(daemon.base + "/api/doctor")

        assert status == 200
        assert (body["passed"], body["warned"], body["failed"], body["ok"]) == (2, 1, 0, True)
        # The enum is flattened to its value token, not left unserializable.
        assert [c["status"] for c in body["checks"]] == ["pass", "pass", "warn"]
        # A version check that could not reach PyPI stays honestly unverified.
        assert body["checks"][1]["version_verified"] is False
        assert body["checks"][2]["fix"] == "install sendme for peer-to-peer transfer"

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

        status, body = _get(daemon.base + "/api/doctor")

        assert status == 200
        assert body["ok"] is False
        assert body["checks"][0]["status"] == "fail"

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
                _get(base + "/api/doctor")
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
            daemon.base + "/api/doctor",
            headers={"Origin": "https://example.invalid", "Sec-Fetch-Site": "cross-site"},
        )

        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=_TIMEOUT)  # noqa: S310 - fixed localhost

        assert e.value.code == 403
        assert e.value.headers.get("Access-Control-Allow-Origin") is None


class TestPhase1ReadSurfaces:
    """The page carries the doctor and url READ surfaces, kept off the pinned
    Tier A action-button set (a read is not a mutating action)."""

    @pytest.fixture
    def page(self, daemon):
        with urllib.request.urlopen(daemon.base + "/", timeout=_TIMEOUT) as resp:  # noqa: S310
            return resp.read().decode()

    def test_the_doctor_screen_is_reachable_and_renders_all_three_tiers(self, page):
        assert 'id="doctor-button"' in page
        assert "async function runDoctor()" in page
        assert 'fetch("/api/doctor")' in page
        # pass/warn/fail all have a pill class, so no tier renders as a blank.
        for token in (".pill.pass", ".pill.warn", ".pill.fail"):
            assert token in page

    def test_the_url_read_is_a_read_not_a_tier_a_action(self, page):
        assert "function runUrlProbe(" in page
        assert "/url${query}" in page
        # It must NOT be built as an actionButton - that surface is pinned exact.
        assert 'actionButton("probe_url"' not in page
        assert 'readButton("url"' in page

    def test_the_url_read_preserves_focus_and_announces_its_lifecycle(self, page):
        assert "return `read:${node.dataset.read}`" in page
        assert "URL check in progress for ${inst.project}" in page
        assert 'el("action-status").textContent = lastAction ? lastAction.text : "";' in page

    def test_the_url_read_binds_results_to_the_originating_selection(self, page):
        assert "benchKey: benchKey(bench)" in page
        assert "selected.project === origin.project" in page
        assert '(selected.benchKey || "") === origin.benchKey' in page
        assert "lastAction = selectionStillMatches ? probeResult : null;" in page
        assert "`${p.project} / ${p.bench_path}:" in page

    def test_no_later_phase_mutation_button_is_rendered(self, page):
        # Phase 1 wires reads + lifecycle; a greyed slot for a later-phase verb
        # would still be building its surface (the Tier A rail ruling).
        for token in ("apps_install", "migrate_site", "backup_restore", "remove_instance"):
            assert f'data-action="{token}"' not in page
