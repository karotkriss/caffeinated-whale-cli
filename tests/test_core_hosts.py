"""``core.hosts``: the hub's side of a remote-host link. Validation of everything that
reaches ``ssh``, the pinned-key store, the SSH argv, HTTP over a channel, the probe,
and the link state machine, each driven without a network through a fake transport.
Every security lesson the design names has a test here or in the bridge's tests."""

from __future__ import annotations

import base64
import json
import shutil
import stat
import subprocess
import sys
import threading
import time

import pytest

from caffeinated_whale_cli.core import community, hosts, integrations
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from caffeinated_whale_cli.core.protocol import BRIDGE_PROTOCOL, PROTOCOL_VERSION

HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFmBvLshzLAzHjvkIyGPGlSs9uXUe8kK/zwyIT14B3QM"
PAIRING_FIELDS = {
    "v": 1, "hub": "hub-x", "cap": "operator", "user": "frappe", "address": "10.9.8.7",
    "port": 22, "name": "Build box", "host_key": HOST_KEY,
}  # fmt: skip


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("CWCLI_HOME", str(tmp_path / "home"))


class TestValidation:
    @pytest.mark.parametrize("bad", ["-oProxyCommand=touch /tmp/x", "", "host name", "a;b", "ho\nst", "-", "x" * 300])
    def test_a_destination_ssh_could_misread_is_refused(self, bad):
        with pytest.raises(CwcliError) as e:
            hosts.check_hostname(bad)
        assert e.value.kind is ErrorKind.USAGE

    @pytest.mark.parametrize("good", ["box", "box.example.org", "10.0.0.5", "::1", "[fe80::1]", "under_score.host"])
    def test_hostnames_and_ip_literals_pass(self, good):
        assert hosts.check_hostname(good)

    @pytest.mark.parametrize("bad", ["-root", "", "a b", "user@host", "x" * 40, "a/b"])
    def test_a_user_name_ssh_could_misread_is_refused(self, bad):
        with pytest.raises(CwcliError):
            hosts.check_user(bad)

    @pytest.mark.parametrize("bad", [0, 65536, "x", True, None, 2.5])
    def test_ports(self, bad):
        with pytest.raises(CwcliError):
            hosts.check_port(bad)
        assert hosts.check_port("2222") == 2222

    def test_a_public_key_is_parsed_not_pasted(self):
        kind, blob = hosts.parse_public_key(HOST_KEY + " a comment\nssh-rsa AAAAB3NzaC1yc2E= second")
        assert (kind, blob) == tuple(HOST_KEY.split())
        for bad in ('command="x" ' + HOST_KEY, "ssh-rsa " + HOST_KEY.split()[1], "ssh-ed25519 QUJD", "words"):
            with pytest.raises(CwcliError):
                hosts.parse_public_key(bad)

    def test_the_fingerprint_matches_ssh_keygen(self, tmp_path):
        keygen = shutil.which("ssh-keygen")
        if keygen is None:
            pytest.skip("no ssh-keygen")
        subprocess.run([keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(tmp_path / "k")], check=True)
        kind, blob = hosts.parse_public_key((tmp_path / "k.pub").read_text())
        printed = subprocess.run([keygen, "-lf", str(tmp_path / "k.pub")], capture_output=True, text=True, check=True).stdout
        assert hosts.fingerprint(kind, blob) in printed


class TestPairing:
    def test_round_trip(self):
        fields = hosts.parse_pairing(hosts.make_pairing(PAIRING_FIELDS))
        assert fields == {"user": "frappe", "address": "10.9.8.7", "port": 22, "name": "Build box", "hub": "hub-x", "cap": "operator", "host_key": HOST_KEY}

    @pytest.mark.parametrize("text", ["", "cwcli-remote:v1:", "cwcli-remote:v1:!!!", "cwcli-remote:v2:" + base64.urlsafe_b64encode(b"{}").decode(), "some words"])
    def test_a_damaged_or_foreign_string_is_refused(self, text):
        with pytest.raises(CwcliError) as e:
            hosts.parse_pairing(text)
        assert e.value.code == "host.pairing"

    def test_every_field_inside_is_validated(self):
        for key, value in (("address", "-oProxyCommand=x"), ("user", "-x"), ("port", 70000), ("cap", "root"), ("hub", "a b"), ("host_key", "ssh-ed25519 QUJD")):
            with pytest.raises(CwcliError):
                hosts.parse_pairing(hosts.make_pairing({**PAIRING_FIELDS, key: value}))

    def test_a_pairing_without_a_host_key_parses_as_an_empty_pin(self):
        assert hosts.parse_pairing(hosts.make_pairing({**PAIRING_FIELDS, "host_key": None}))["host_key"] is None


class TestStore:
    def test_add_pins_the_host_key_under_an_alias_in_the_hubs_own_known_hosts(self):
        rec = hosts.add_host(hosts.make_pairing(PAIRING_FIELDS), by="t")
        assert rec.id == "build-box" and rec.transport == "ssh" and rec.key_alias == "cwcli-host-build-box"
        assert hosts.known_hosts_path().read_text() == f"cwcli-host-build-box {HOST_KEY}\n"
        assert stat.S_IMODE(hosts.store_path().stat().st_mode) == 0o600
        assert stat.S_IMODE(hosts.known_hosts_path().stat().st_mode) == 0o600

    def test_nothing_is_added_without_a_host_key(self):
        with pytest.raises(CwcliError) as e:
            hosts.add_host(hosts.make_pairing({**PAIRING_FIELDS, "host_key": None}))
        assert e.value.code == "host.key_required"
        assert hosts.list_hosts() == []

    def test_explicit_values_override_the_pairing_and_a_pasted_key_fills_an_empty_pin(self):
        rec = hosts.add_host(hosts.make_pairing({**PAIRING_FIELDS, "host_key": None}), address="build.lan", port=2222, host_key=HOST_KEY + " comment", name="Build")
        assert (rec.address, rec.port, rec.host_key, rec.name) == ("build.lan", 2222, HOST_KEY, "Build")

    def test_the_record_holds_no_credential_of_the_remote(self):
        hosts.add_host(hosts.make_pairing(PAIRING_FIELDS))
        stored = json.loads(hosts.store_path().read_text())["hosts"]["build-box"]
        assert set(stored) == {"name", "transport", "address", "port", "user", "host_key", "added_at", "added_by"}

    def test_the_same_destination_twice_is_a_conflict_and_ids_stay_unique(self):
        hosts.add_host(hosts.make_pairing(PAIRING_FIELDS))
        with pytest.raises(CwcliError) as e:
            hosts.add_host(hosts.make_pairing(PAIRING_FIELDS))
        assert e.value.code == "host.exists"
        second = hosts.add_host(hosts.make_pairing({**PAIRING_FIELDS, "address": "10.9.8.8"}))
        assert second.id == "build-box-2"

    def test_remove_and_repin_rewrite_the_pins(self):
        rec = hosts.add_host(hosts.make_pairing(PAIRING_FIELDS))
        other = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAoLvGEvGjS97I/0qCfGZ07VM9w5bY4Gd4/g9hbWOwYm"
        hosts.repin_host(rec.id, other)
        assert hosts.known_hosts_path().read_text() == f"cwcli-host-build-box {other}\n"
        hosts.remove_host(rec.id)
        assert hosts.known_hosts_path().read_text() == "" and hosts.list_hosts() == []
        with pytest.raises(CwcliError):
            hosts.get_host(rec.id)

    def test_store_changes_notify_a_running_manager(self):
        seen = []
        hosts.register_change_listener(lambda: seen.append(1))
        try:
            hosts.add_host(hosts.make_pairing(PAIRING_FIELDS))
        finally:
            hosts.unregister_change_listener(seen.append)
        assert seen


class TestSshArgv:
    def _rec(self, **kw):
        base = dict(id="b", name="b", transport="ssh", address="10.0.0.9", port=2200, user="frappe", host_key=HOST_KEY, added_at=0.0, added_by="")
        return hosts.HostRecord(**{**base, **kw})

    def test_the_argv_pins_batches_multiplexes_and_puts_the_destination_after_a_dash_dash(self, tmp_path):
        t = hosts.SshTransport(ssh="/usr/bin/ssh", key_path=tmp_path / "k", known_hosts=tmp_path / "kh", control=tmp_path / "c")
        argv = t.argv(self._rec())
        opts = {argv[i + 1] for i, a in enumerate(argv) if a == "-o"}
        assert {"StrictHostKeyChecking=yes", "BatchMode=yes", "IdentitiesOnly=yes", "UpdateHostKeys=no", "PasswordAuthentication=no", "KbdInteractiveAuthentication=no", "ControlMaster=auto", "ControlPersist=120", "ServerAliveInterval=15", "ServerAliveCountMax=3", "HostKeyAlias=cwcli-host-b", f"UserKnownHostsFile={tmp_path / 'kh'}", "GlobalKnownHostsFile=/dev/null"} <= opts
        assert argv[argv.index("-i") + 1] == str(tmp_path / "k")
        assert argv[-2:] == ["--", "frappe@10.0.0.9"] and argv[argv.index("-p") + 1] == "2200"
        assert argv[1:3] == ["-F", "/dev/null"]

    def test_a_hostile_destination_never_reaches_ssh(self, tmp_path):
        t = hosts.SshTransport(ssh="/usr/bin/ssh", key_path=tmp_path / "k", known_hosts=tmp_path / "kh", control=tmp_path / "c")
        with pytest.raises(CwcliError):
            t.argv(self._rec(address="-oProxyCommand=touch /tmp/pwn"))
        with pytest.raises(CwcliError):
            t.argv(self._rec(user="-oProxyCommand=x"))

    def test_a_failed_ssh_is_classified_from_its_own_stderr_through_a_real_pipe(self):
        """A regression the fakes could not see: the channel's stderr was read AFTER the
        channel was closed, so every ssh failure read as a message-less ``unreachable``
        (and the read raised into the handler). A real process stands in for ssh."""
        script = "import sys; sys.stderr.write('Host key verification failed.\\n'); sys.exit(255)"
        proc = subprocess.Popen(
            [sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        with pytest.raises(hosts.LinkError) as e:
            hosts.exchange(hosts.SshChannel(proc), "GET", "/api/v1/bridge", [])
        assert e.value.state == hosts.HOST_KEY_CHANGED

    @pytest.mark.parametrize(
        "stderr, state",
        [
            ("@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@\nHost key verification failed.", hosts.HOST_KEY_CHANGED),
            ("Host key verification failed.", hosts.HOST_KEY_CHANGED),
            ("frappe@10.0.0.9: Permission denied (publickey).", hosts.AUTH_REFUSED),
            ("ssh: connect to host 10.0.0.9 port 2200: Connection timed out", hosts.UNREACHABLE),
            ("", hosts.UNREACHABLE),
        ],
    )
    def test_ssh_failures_map_to_link_states(self, stderr, state):
        assert hosts.classify_ssh_failure(255, stderr).state == state


class FakeChannel:
    """A scripted byte stream: ``chunks`` arrive in order, then EOF; ``failure`` is what
    the channel reports when it ended without an HTTP answer."""

    def __init__(self, chunks, failure=None, hang_after=None):
        self.chunks = list(chunks)
        self.sent = b""
        self.closed = False
        self._failure = failure
        self._hang_after = hang_after
        self._reads = 0

    def write(self, data):
        self.sent += data

    def close_write(self):
        pass

    def read(self, size, timeout):
        if self._hang_after is not None and self._reads >= self._hang_after:
            return None
        self._reads += 1
        return self.chunks.pop(0) if self.chunks else b""

    def failure(self):
        return self._failure

    def close(self):
        self.closed = True


def _http(status, body, ctype="application/json", length=True, extra=""):
    head = f"HTTP/1.1 {status} X\r\nContent-Type: {ctype}\r\n{extra}" + (f"Content-Length: {len(body)}\r\n" if length else "")
    return head.encode() + b"\r\n" + body


class TestExchange:
    def test_a_content_length_body_is_read_exactly(self):
        ch = FakeChannel([_http(200, b'{"a":1}')[:10], _http(200, b'{"a":1}')[10:], b"trailing-garbage"])
        resp = hosts.exchange(ch, "GET", "/api/v1/x", [("Accept", "a")])
        assert resp.status == 200 and resp.read() == b'{"a":1}'
        assert ch.sent.startswith(b"GET /api/v1/x HTTP/1.1\r\nAccept: a\r\n")

    def test_a_stream_without_a_length_ends_at_eof_and_times_out_when_silent(self):
        ch = FakeChannel([_http(200, b"", "text/event-stream", length=False), b"data: 1\n\n", b"data: 2\n\n"])
        resp = hosts.exchange(ch, "GET", "/api/v1/stream", [])
        assert b"".join(resp.iter_body()) == b"data: 1\n\ndata: 2\n\n"
        silent = FakeChannel([_http(200, b"", "text/event-stream", length=False), b"x"], hang_after=2)
        resp = hosts.exchange(silent, "GET", "/api/v1/stream", [])
        with pytest.raises(hosts.LinkError) as e:
            list(resp.iter_body(idle_timeout=0.01))
        assert "keepalive" in e.value.reason

    def test_a_channel_that_ends_without_an_answer_raises_its_own_failure(self):
        ch = FakeChannel([], failure=hosts.LinkError(hosts.AUTH_REFUSED, "denied"))
        with pytest.raises(hosts.LinkError) as e:
            hosts.exchange(ch, "GET", "/api/v1/bridge", [])
        assert e.value.state == hosts.AUTH_REFUSED and ch.closed

    def test_a_non_http_answer_is_unreachable_not_a_crash(self):
        with pytest.raises(hosts.LinkError) as e:
            hosts.exchange(FakeChannel([b"garbage\r\n\r\n"]), "GET", "/x", [])
        assert e.value.state == hosts.UNREACHABLE

    def test_sse_frames_and_keepalives(self):
        frames, rest = hosts.parse_sse(b": keepalive\n\nid: 3\nevent: delta\ndata: {\"a\":1}\n\nevent: part")
        assert frames == [(None, "", None), ("delta", '{"a":1}', "3")] and rest == b"event: part"


class FakeTransport:
    """Answers each ``open`` with the next scripted channel."""

    id = "ssh"

    def __init__(self, channels):
        self.channels = list(channels)
        self.opened = 0
        self.closed = []

    def open(self, host):
        self.opened += 1
        if not self.channels:
            raise hosts.LinkError(hosts.UNREACHABLE, "script exhausted")
        item = self.channels.pop(0)
        if isinstance(item, hosts.LinkError):
            raise item
        return item

    def close(self, host):
        self.closed.append(host.id)


def _bridge_answer(daemon_protocol=PROTOCOL_VERSION, cap="operator", bridge_protocol=BRIDGE_PROTOCOL, daemon=True, error=None):
    data = {
        "bridge_protocol": bridge_protocol,
        "hub": "hub-x",
        "max_role": cap,
        "daemon": {"version": "3.5.0", "protocol": daemon_protocol, "build": "b"} if daemon else None,
        "daemon_error": error,
    }
    return _http(200, json.dumps({"data": data}).encode())


def _rec():
    return hosts.HostRecord(id="build-box", name="Build box", transport="ssh", address="10.9.8.7", port=22, user="frappe", host_key=HOST_KEY, added_at=0.0, added_by="")


class TestProbe:
    def test_connected_when_the_bridge_and_daemon_answer_and_the_protocol_matches(self):
        probe = hosts.probe_host(FakeTransport([FakeChannel([_bridge_answer()])]), _rec())
        assert probe.state == hosts.CONNECTED and probe.cap == "operator" and probe.remote["protocol"] == PROTOCOL_VERSION

    def test_a_different_protocol_is_a_version_mismatch_that_names_the_side_to_update(self):
        older = hosts.probe_host(FakeTransport([FakeChannel([_bridge_answer(daemon_protocol=PROTOCOL_VERSION - 1)])]), _rec())
        assert older.state == hosts.VERSION_MISMATCH and "update cwcli on the remote host" in older.reason
        newer = hosts.probe_host(FakeTransport([FakeChannel([_bridge_answer(daemon_protocol=PROTOCOL_VERSION + 1)])]), _rec())
        assert newer.state == hosts.VERSION_MISMATCH and "update cwcli on this hub" in newer.reason

    def test_a_daemon_that_is_down_is_its_own_state(self):
        probe = hosts.probe_host(FakeTransport([FakeChannel([_bridge_answer(daemon=False, error="not running")])]), _rec())
        assert probe.state == hosts.DAEMON_DOWN and probe.reason == "not running" and probe.cap == "operator"

    def test_a_revoked_grant_reads_as_auth_refused(self):
        body = json.dumps({"errors": [{"title": "bridge.revoked"}]}).encode()
        probe = hosts.probe_host(FakeTransport([FakeChannel([_http(403, body)])]), _rec())
        assert probe.state == hosts.AUTH_REFUSED

    def test_a_channel_failure_propagates_as_the_link_error(self):
        with pytest.raises(hosts.LinkError) as e:
            hosts.probe_host(FakeTransport([hosts.LinkError(hosts.HOST_KEY_CHANGED, "changed")]), _rec())
        assert e.value.state == hosts.HOST_KEY_CHANGED


class TestLinkStateMachine:
    def _link(self, transport, clock=None):
        states, frames = [], []
        now = [1000.0]
        link = hosts.HostLink(
            _rec(), transport,
            on_state=lambda lk: states.append((lk.status.state, lk.status.reason)),
            on_frame=lambda lk, ev, data: frames.append((ev, data)),
            clock=clock or (lambda: now[0]), jitter=lambda: 0.5,
        )  # fmt: skip
        return link, states, frames, now

    def test_a_changed_host_key_is_a_hard_stop_that_never_reconnects_by_itself(self):
        transport = FakeTransport([hosts.LinkError(hosts.HOST_KEY_CHANGED, "mismatch"), FakeChannel([_bridge_answer()])])
        link, states, _, _ = self._link(transport)
        link.step()
        assert states[-1] == (hosts.HOST_KEY_CHANGED, "mismatch")
        assert link.step() == hosts.MAX_BACKOFF_S and transport.opened == 1  # no second probe
        link.request_check()  # an Admin re-pinned: the next step probes again
        link.step()
        assert states[-1][0] == hosts.CONNECTED

    def test_frames_are_republished_and_a_drop_gets_one_grace_probe_before_unreachable(self):
        stream = FakeChannel([_http(200, b"", "text/event-stream", length=False), b'event: snapshot\ndata: {"instances": [], "jobs": []}\n\n: keepalive\n\n', b'event: delta\ndata: {"project": "p"}\n\n'])
        transport = FakeTransport([FakeChannel([_bridge_answer()]), stream, hosts.LinkError(hosts.UNREACHABLE, "timed out")])
        link, states, frames, now = self._link(transport)
        delay = link.step()
        assert [ev for ev, _ in frames] == ["snapshot", "delta"]
        assert states == [(hosts.CONNECTED, None)]  # the stream ended, nothing flashed yet
        assert delay == 0.0 and link.status.last_seen == 1000.0
        delay = link.step()  # the grace probe fails: now it is unreachable, with backoff
        assert states[-1] == (hosts.UNREACHABLE, "timed out") and delay > 0
        assert link.view()["drops_10m"] == 1

    def test_a_grace_probe_that_succeeds_never_shows_a_flash(self):
        stream1 = FakeChannel([_http(200, b"", "text/event-stream", length=False), b": keepalive\n\n"])
        stream2 = FakeChannel([_http(200, b"", "text/event-stream", length=False)])
        transport = FakeTransport([FakeChannel([_bridge_answer()]), stream1, FakeChannel([_bridge_answer()]), stream2])
        link, states, _, _ = self._link(transport)
        link.step()
        link.step()
        assert states == [(hosts.CONNECTED, None)]

    def test_backoff_grows_with_jitter_and_resets_on_success(self):
        transport = FakeTransport([hosts.LinkError(hosts.UNREACHABLE, "x")] * 3)
        link, _, _, _ = self._link(transport)
        delays = [link.step() for _ in range(3)]
        assert delays == [2.0, 4.0, 8.0]  # 2**n with the fixed 0.5 jitter -> 2**n * 1.0

    def test_auth_refused_retries_slowly_and_a_daemon_down_is_reported_as_such(self):
        transport = FakeTransport([hosts.LinkError(hosts.AUTH_REFUSED, "denied"), FakeChannel([_bridge_answer(daemon=False, error="down")])])
        link, states, _, _ = self._link(transport)
        assert link.step() == hosts.MAX_BACKOFF_S
        link.step()
        assert states[-1] == (hosts.DAEMON_DOWN, "down")

    def test_a_version_mismatch_still_holds_the_stream_for_the_read_only_view(self):
        stream = FakeChannel([_http(200, b"", "text/event-stream", length=False), b'event: snapshot\ndata: {"instances": []}\n\n'])
        transport = FakeTransport([FakeChannel([_bridge_answer(daemon_protocol=PROTOCOL_VERSION - 1)]), stream])
        link, states, frames, _ = self._link(transport)
        link.step()
        assert states[0][0] == hosts.VERSION_MISMATCH and frames and not link.view()["actions_allowed"] and link.view()["readable"]

    def test_the_view_carries_what_the_page_needs_and_no_secret(self):
        link, _, _, _ = self._link(FakeTransport([]))
        view = link.view()
        assert {"id", "name", "address", "port", "user", "state", "reason", "last_seen", "cap", "remote", "drops_10m", "actions_allowed", "readable", "host_key_fingerprint"} <= set(view)
        assert "host_key" not in view and "token" not in json.dumps(view)


class TestManager:
    def _manager(self, transport, published):
        return hosts.HostManager(publish=lambda ev, data: published.append((ev, data)), transports_fn=lambda: {"ssh": transport})

    def test_a_quiet_host_keeps_its_last_snapshot_as_unknown_with_last_seen(self):
        published = []
        transport = FakeTransport([])
        manager = self._manager(transport, published)
        link = hosts.HostLink(_rec(), transport, on_state=manager._state_changed, on_frame=manager._frame, clock=lambda: 5.0)
        manager._links[link.host.id] = link
        link.status.state = hosts.CONNECTED
        link.touch()
        manager._frame(link, "snapshot", {"instances": [{"project": "p", "overall": "running", "web_probed": True}], "jobs": [{"id": "j1"}]})
        manager._frame(link, "delta", {"project": "q", "instance": {"project": "q", "overall": "degraded"}, "removed": False})
        manager._frame(link, "job", {"id": "j2", "state": "running"})
        manager._frame(link, "metrics", {"x": 1})
        assert [ev for ev, _ in published] == ["host_snapshot", "host_delta", "host_job", "host_event"]
        assert published[-1][1] == {"host": "build-box", "event": "metrics", "data": {"x": 1}}
        published.clear()
        link._set(hosts.UNREACHABLE, "gone")
        events = dict(published)
        assert set(events) == {"host_link", "host_snapshot"}  # nothing starts, stops or retries work
        assert events["host_link"]["state"] == hosts.UNREACHABLE and events["host_link"]["last_seen"] == 5.0
        snap = events["host_snapshot"]
        assert snap["stale"] is True and {i["overall"] for i in snap["instances"]} == {"unknown"}
        assert {i["project"] for i in snap["instances"]} == {"p", "q"} and [j["id"] for j in snap["jobs"]] == ["j1", "j2"]
        assert {i["overall"] for i in manager.snapshot()[0]["instances"]} == {"unknown"}

    def test_a_host_whose_transport_is_off_is_shown_as_such_and_never_dialled(self):
        published = []
        hosts.add_host(hosts.make_pairing(PAIRING_FIELDS))
        manager = hosts.HostManager(publish=lambda ev, data: published.append((ev, data)), transports_fn=lambda: {})
        manager.resync()
        (view,) = manager.views()
        assert view["state"] == hosts.TRANSPORT_OFF and view["actions_allowed"] is False
        assert manager.view()["hub"]["transport_on"] is False
        with pytest.raises(hosts.LinkError) as e:
            manager.request("build-box", "GET", "/api/v1/instances", [])
        assert e.value.state == hosts.TRANSPORT_OFF

    def test_checking_a_connected_host_probes_out_of_band_and_ends_a_stale_stream(self):
        """The held stream cannot say a daemon died until it notices; Check now must."""
        published = []
        transport = FakeTransport([FakeChannel([_bridge_answer(daemon=False, error="not running")])])
        manager = self._manager(transport, published)
        link = hosts.HostLink(_rec(), transport, on_state=manager._state_changed, on_frame=manager._frame)
        manager._links[link.host.id] = link
        link.status.state = hosts.CONNECTED
        link.status.cap = "operator"
        stale = FakeChannel([])
        link._current = hosts.HttpResponse(stale, 200, "OK", [], b"")
        view = manager.check("build-box")
        assert view["state"] == hosts.DAEMON_DOWN and view["reason"] == "not running"
        assert stale.closed is True and transport.opened == 1
        assert published[0][0] == "host_link"
        # Still connected according to a fresh probe: nothing is touched.
        transport.channels.append(FakeChannel([_bridge_answer()]))
        link.status.state = hosts.CONNECTED
        link._current = hosts.HttpResponse(FakeChannel([]), 200, "OK", [], b"")
        assert manager.check("build-box")["state"] == hosts.CONNECTED and not link._current._channel.closed

    def test_resync_starts_links_for_stored_hosts_and_stops_removed_ones(self):
        published = []
        transport = FakeTransport([])
        rec = hosts.add_host(hosts.make_pairing(PAIRING_FIELDS))
        manager = self._manager(transport, published)
        manager.resync()
        link = manager.get(rec.id)
        assert link is not None and link.thread is not None
        hosts.remove_host(rec.id)
        manager.resync()
        assert manager.get(rec.id) is None and transport.closed == [rec.id]
        assert ("host_removed", {"host": rec.id}) in published
        manager.stop()


class TestTransportHookPoint:
    @pytest.fixture
    def fakes(self, monkeypatch):
        def integ(iid, provides):
            return integrations.Integration(
                id=iid, name=iid, publisher=integrations.OFFICIAL, description="", provides=provides,
                fields=(), normalize=lambda s: {}, test=lambda s, x: integrations.Check(True, "ok"),
                hooks={h: (lambda s, x, _i=iid: type("T", (), {"id": _i})()) for h in provides},
            )  # fmt: skip
        reg = {"t1": integ("t1", ("host_transport",)), "t2": integ("t2", ("host_transport",)), "s1": integ("s1", ("sign_in",)), "s2": integ("s2", ("sign_in",))}
        monkeypatch.setattr(integrations, "official", lambda: reg)
        monkeypatch.setattr(integrations, "registry", lambda: reg)
        return reg

    def test_several_transport_integrations_may_be_on_at_once(self, fakes):
        integrations.configure("t1", enable=True)
        integrations.configure("t2", enable=True)  # no hook_taken
        assert [i.id for i, _s, _x in integrations.providers("host_transport")] == ["t1", "t2"]
        assert set(hosts.transports()) == {"t1", "t2"}

    def test_the_one_at_a_time_rule_still_holds_for_the_other_hook_points(self, fakes):
        integrations.configure("s1", enable=True)
        with pytest.raises(CwcliError) as e:
            integrations.configure("s2", enable=True)
        assert e.value.code == "integration.hook_taken"

    def test_a_community_integration_cannot_declare_a_transport(self):
        assert "host_transport" not in community.HOOK_POINTS

    def test_a_community_transport_would_be_ignored_even_if_one_got_in(self, monkeypatch):
        rogue = integrations.Integration(
            id="rogue", name="rogue", publisher=integrations.COMMUNITY, description="", provides=("host_transport",),
            fields=(), normalize=lambda s: {}, test=lambda s, x: integrations.Check(True, "ok"),
            hooks={"host_transport": lambda s, x: type("T", (), {"id": "ssh"})()},
        )  # fmt: skip
        monkeypatch.setattr(integrations, "providers", lambda hook: [(rogue, {}, {})])
        assert hosts.transports() == {}


class TestHubKey:
    def test_the_official_card_describes_the_hub_and_exposes_no_private_half(self, tmp_path):
        if shutil.which("ssh-keygen") is None:
            pytest.skip("no ssh-keygen")
        check = hosts.SSH_INTEGRATION.test({"hub_name": "hub-t"}, {})
        assert check.ok, check.message
        private = hosts.hub_key_path()
        assert stat.S_IMODE(private.stat().st_mode) == 0o600
        public = hosts.hub_public_key()
        assert public.startswith("ssh-ed25519 ") and public.endswith("cwcli-hub:hub-t")
        card = json.dumps(integrations.view("remote_ssh"))
        assert "PRIVATE KEY" not in card and public in card
        assert hosts.ensure_hub_key() == public  # idempotent
        assert hosts.allow_command().startswith("cwcli remote allow --hub ")

    def test_the_hub_name_is_validated_and_defaults_to_the_hostname(self):
        assert hosts.SSH_INTEGRATION.normalize({"hub_name": ""})["hub_name"] == hosts.default_hub_name()
        with pytest.raises(CwcliError):
            hosts.SSH_INTEGRATION.normalize({"hub_name": "a b"})

    def test_the_control_socket_dir_stays_short_and_private(self):
        d = hosts.control_dir()
        assert len(str(d / ("x" * 40))) < 108
        assert stat.S_IMODE(d.stat().st_mode) == 0o700


def test_links_run_on_their_own_thread_and_stop_cleanly():
    gate = threading.Event()

    class Blocking(FakeTransport):
        def open(self, host):
            gate.wait(2)
            return super().open(host)

    link = hosts.HostLink(_rec(), Blocking([hosts.LinkError(hosts.UNREACHABLE, "x")] * 50), on_state=lambda lk: None, on_frame=lambda *a: None)
    link.start()
    gate.set()
    time.sleep(0.05)
    link.stop()
    link.thread.join(3)
    assert not link.thread.is_alive()
