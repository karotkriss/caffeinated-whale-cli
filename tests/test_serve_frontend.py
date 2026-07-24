"""``cwcli serve`` - the HTTP/SSE frontend, driven against a REAL bound server.

No Docker: ``core.list_instances`` and ``core.inspect`` are stubbed, but the
socket, the handler, the SSE framing, the fan-out and the disconnect cleanup are
the shipped ones - the parts a mocked handler would never catch.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from caffeinated_whale_cli.commands import serve as serve_cmd
from caffeinated_whale_cli.core import fleet as core_fleet
from caffeinated_whale_cli.core import inspect as core_inspect
from caffeinated_whale_cli.core.envelope import Result, Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from caffeinated_whale_cli.core.inspect import BenchInfo, InspectReport, SiteInfo
from caffeinated_whale_cli.core.list import InstanceDTO

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
        yield type("Daemon", (), {"base": base, "fleet": fleet, "hub": hub, "rows": rows})
    finally:
        httpd.shutdown()
        httpd.server_close()


def _get(url):
    with urllib.request.urlopen(url, timeout=_TIMEOUT) as resp:  # noqa: S310 - fixed localhost
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

    def test_the_root_serves_the_test_page(self, daemon):
        with urllib.request.urlopen(daemon.base + "/", timeout=_TIMEOUT) as resp:  # noqa: S310
            body = resp.read().decode()
        assert resp.status == 200
        assert "EventSource" in body


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

    def test_a_disconnect_retracts_focus_without_waiting_for_keepalive(
        self, daemon, monkeypatch
    ):
        monkeypatch.setattr(serve_cmd, "KEEPALIVE_S", 60.0)
        stream = _Stream(daemon.base + "/api/events?focus=p")
        stream.next_frame()
        _wait_for(lambda: daemon.fleet.should_probe_web("p"), "focus never reached the fleet")

        stream.close()

        _wait_for(
            lambda: not daemon.fleet.should_probe_web("p") and daemon.hub.client_count() == 0,
            "a closed client left its focus and its queue behind",
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
