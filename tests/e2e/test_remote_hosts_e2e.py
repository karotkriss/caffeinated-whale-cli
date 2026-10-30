"""Remote hosts over REAL SSH: two shipped ``cwcli ui`` daemons under separate homes
(the hub and the remote), a private ``sshd`` on loopback with its own host key, and the
real bridge as the forced command the hub's key is restricted to.

What only the real path proves: ``sshd`` honours the ``restrict,command=`` line
``cwcli remote allow`` writes (no shell, no port forward), OpenSSH pins the host key the
hub recorded and refuses a different one, the bridge starts in a fresh interpreter per
channel and reaches the remote's loopback daemon with that machine's own token, the
remote lowers the hub's role to the cap, a job started through the hub runs on the
remote and records the hub, revocation reaches a connection that is already open, the
hub's stream carries the remote's snapshot, and a remote that stops answering shows its
last snapshot as unknown with nothing done about it.

No Frappe instance is needed (both daemons see the Docker daemon's instance list, and
the proof is the plumbing and the trust model), so this is ``standalone``. The remote's
grant is written with ``--no-start-daemon`` so the test never spawns a detached daemon on
the machine it runs on.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from . import harness

pytestmark = [pytest.mark.e2e, pytest.mark.standalone]

SSHD = shutil.which("sshd") or ("/usr/sbin/sshd" if Path("/usr/sbin/sshd").exists() else None)
REMOTE_TOKEN = "cwe2e-remote-token"
HUB_TOKEN = "cwe2e-hub-token"
HUB = "cwe2e-hub"
OTHER_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFmBvLshzLAzHjvkIyGPGlSs9uXUe8kK/zwyIT14B3QM other"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _api(base: str, path: str, payload=None, *, token: str | None = None, headers=None, method=None):
    """``(status, body, headers)``; a JSON body is decoded, anything else kept as bytes."""
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    h.update(headers or {})
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 - loopback
            raw = r.read()
            return r.status, (json.loads(raw) if raw[:1] in (b"{", b"[") else raw), r.headers
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw[:1] in (b"{", b"[") else raw), e.headers


def _code(body) -> str | None:
    return (body.get("errors") or [{}])[0].get("title") if isinstance(body, dict) else None


class Lab:
    def __init__(self, root: Path):
        self.root = root
        self.ssh_dir = root / "sshd"
        self.ssh_dir.mkdir(mode=0o700)
        self.ssh_port = _free_port()
        self.remote_port = _free_port()
        self.hub_port = _free_port()
        self.remote_home = Path(os.environ["HOME"]) / "remote-cwcli"
        self.hub_home = Path(os.environ["HOME"]) / "hub-cwcli"
        self.keys = Path(os.environ["HOME"]) / ".ssh" / "authorized_keys"
        self.procs: list[subprocess.Popen] = []
        self.remote_proc: subprocess.Popen | None = None

    # -- the private sshd -------------------------------------------------
    def start_sshd(self) -> None:
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.ssh_dir / "host_ed25519")],
            check=True,
        )
        self.keys.parent.mkdir(mode=0o700, exist_ok=True)
        self.keys.touch(mode=0o600)
        config = self.ssh_dir / "sshd_config"
        config.write_text(
            "\n".join(
                [
                    f"Port {self.ssh_port}",
                    "ListenAddress 127.0.0.1",
                    f"HostKey {self.ssh_dir / 'host_ed25519'}",
                    f"AuthorizedKeysFile {self.keys}",
                    "PasswordAuthentication no",
                    "KbdInteractiveAuthentication no",
                    "PubkeyAuthentication yes",
                    "UsePAM no",
                    f"PidFile {self.ssh_dir / 'sshd.pid'}",
                    "LogLevel VERBOSE",
                    "StrictModes no",
                    f"AllowUsers {os.environ.get('USER') or os.getlogin()}",
                    "",
                ]
            )
        )
        proc = subprocess.Popen(
            [SSHD, "-f", str(config), "-D", "-E", str(self.ssh_dir / "sshd.log")],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.procs.append(proc)
        # The pid file lands once sshd listens; polling the port instead would count as
        # unauthenticated connections toward sshd's per-source penalty.
        harness.wait_until(lambda: (self.ssh_dir / "sshd.pid").exists(), timeout=30, interval=0.5, desc="sshd")
        assert _port_open(self.ssh_port)

    # -- the daemons -------------------------------------------------------
    def env(self, home: Path, token: str) -> dict:
        return {**os.environ, "CWCLI_HOME": str(home), "CWCLI_SERVE_TOKEN": token, "CWCLI_NO_UPDATE_CHECK": "1"}

    def _daemon(self, home: Path, port: int, token: str) -> subprocess.Popen:
        log = open(self.root / f"{home.name}.log", "ab")  # noqa: SIM115 - lives as long as the daemon
        proc = subprocess.Popen(
            [harness.CWCLI, "ui", "--foreground", "--no-browser", "--port", str(port)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=self.env(home, token),
        )
        self.procs.append(proc)
        base = f"http://127.0.0.1:{port}"
        harness.wait_until(lambda: _api(base, "/api/v1/meta")[0] == 200, timeout=90, interval=0.5, desc=f"daemon on {port}")
        return proc

    def start_remote(self) -> None:
        self.remote_proc = self._daemon(self.remote_home, self.remote_port, REMOTE_TOKEN)

    def start_hub(self) -> None:
        self._daemon(self.hub_home, self.hub_port, HUB_TOKEN)

    @property
    def remote(self) -> str:
        return f"http://127.0.0.1:{self.remote_port}"

    @property
    def hub(self) -> str:
        return f"http://127.0.0.1:{self.hub_port}"

    def cwcli(self, home: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [harness.CWCLI, *args],
            env=self.env(home, "unused"),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def allow(self, cap: str = "operator") -> str:
        key = json.loads(self.cwcli(self.hub_home, "remote", "key", "--json").stdout)["public_key"]
        done = self.cwcli(
            self.remote_home, "remote", "allow", "--hub", HUB, "--key", key, "--max-role", cap,
            "--port", str(self.ssh_port), "--address", "127.0.0.1", "--name", "Remote box",
            "--host-key-file", str(self.ssh_dir / "host_ed25519.pub"), "--no-start-daemon", "--json",
        )  # fmt: skip
        assert done.returncode == 0, done.stderr
        return json.loads(done.stdout)["pairing"]

    def ssh_with_hub_key(self, *args: str, stdin: bytes = b"") -> subprocess.CompletedProcess:
        """A plain ``ssh`` with the hub's key, the way an attacker holding it would use it."""
        argv = [
            "ssh", "-F", "/dev/null", "-i", str(self.hub_home / "keys" / "hub_ed25519"),
            "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR", "-o", "ControlMaster=no",
            "-p", str(self.ssh_port), *args,
        ]  # fmt: skip
        return subprocess.run(argv, input=stdin, capture_output=True, timeout=60)

    def host_view(self) -> dict:
        status, body, _ = _api(self.hub, "/api/v1/hosts", token=HUB_TOKEN)
        assert status == 200, body
        (view,) = body["data"]["hosts"]
        return view

    def check(self) -> dict:
        status, body, _ = _api(self.hub, "/api/v1/hosts", {"op": "check", "id": self.host_id}, token=HUB_TOKEN)
        assert status == 200, body
        return body["data"]["host"]

    def ensure_connected(self) -> dict:
        """The link as the next test expects it: granted on the remote and connected."""
        view = self.check()
        if view["state"] == "auth_refused":
            self.allow()
            view = self.check()
        return view

    def stop(self) -> None:
        for proc in reversed(self.procs):
            if proc.poll() is None:
                proc.terminate()
        for proc in self.procs:
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


# This module creates no cwe2e- instance (two daemons and an sshd only), so the
# session's cwe2e- sweep has nothing of ours to remove; shadowing it keeps a local run
# of just this file from force-removing a sibling's cwe2e- instance on a shared box.
# The Docker gate and the isolated HOME stay in force.
@pytest.fixture(scope="module", autouse=True)
def _teardown_backstop():
    yield


@pytest.fixture(scope="module")
def lab(tmp_path_factory):
    if SSHD is None:
        pytest.skip("SSHD UNAVAILABLE - no sshd binary on this machine; the real-SSH leg cannot run")
    if shutil.which("ssh") is None or shutil.which("ssh-keygen") is None:
        pytest.skip("SSH CLIENT UNAVAILABLE - ssh/ssh-keygen missing")
    harness.enforce_isolation()
    lab = Lab(tmp_path_factory.mktemp("remote-lab"))
    try:
        lab.start_sshd()
        lab.start_remote()
        lab.start_hub()
        # The hub turns the SSH transport on (its test creates the hub's key pair).
        status, body, _ = _api(
            lab.hub, "/api/v1/integrations",
            {"op": "configure", "id": "remote_ssh", "settings": {"hub_name": HUB}, "enable": True},
            token=HUB_TOKEN,
        )  # fmt: skip
        assert status == 200 and body["data"]["integration"]["state"] == "on", body
        pairing = lab.allow()
        status, body, _ = _api(lab.hub, "/api/v1/hosts", {"op": "add", "pairing": pairing}, token=HUB_TOKEN)
        assert status == 200, body
        lab.host_id = body["data"]["host"]["id"]
        yield lab
    finally:
        lab.stop()


class TestTheLink:
    def test_the_hub_links_to_the_remote_and_reads_its_instances_through_the_bridge(self, lab):
        view = lab.host_view()
        assert view["state"] == "connected", view
        assert view["cap"] == "operator" and view["actions_allowed"] is True
        assert view["remote"]["protocol"] == _api(lab.hub, "/api/v1/meta")[1]["protocol"]
        assert view["host_key_fingerprint"].startswith("SHA256:")
        assert "token" not in json.dumps(view) and "PRIVATE" not in json.dumps(view)
        # The forward answers what the remote itself answers.
        status, through, headers = _api(lab.hub, f"/api/v1/hosts/{lab.host_id}/instances", token=HUB_TOKEN)
        direct = _api(lab.remote, "/api/v1/instances", token=REMOTE_TOKEN)[1]
        assert status == 200
        assert sorted(i["project"] for i in through["data"]) == sorted(i["project"] for i in direct["data"])
        # ... as data only.
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["Content-Security-Policy"] == "default-src 'none'; sandbox"
        assert headers.get("Set-Cookie") is None and headers["X-Cwcli-Host"] == lab.host_id

    def test_the_hubs_stream_carries_the_remotes_snapshot(self, lab):
        req = urllib.request.Request(lab.hub + "/api/v1/stream", headers={"Authorization": f"Bearer {HUB_TOKEN}"})
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
            line = b""
            while not line.startswith(b"data: "):
                line = resp.readline()
        snapshot = json.loads(line[6:])
        (host,) = snapshot["hosts"]
        assert host["id"] == lab.host_id and host["state"] == "connected"
        assert sorted(i["project"] for i in host["instances"]) == sorted(
            i["project"] for i in _api(lab.remote, "/api/v1/instances", token=REMOTE_TOKEN)[1]["data"]
        )

    def test_the_remotes_session_stream_and_hosts_are_never_forwarded(self, lab):
        for inner in ("session", "stream", "hosts"):
            assert _api(lab.hub, f"/api/v1/hosts/{lab.host_id}/{inner}", token=HUB_TOKEN)[0] == 404


class TestTheKeyReachesOnlyTheApi:
    def test_the_restricted_key_runs_no_shell(self, lab):
        done = lab.ssh_with_hub_key("--", f"{os.environ.get('USER') or os.getlogin()}@127.0.0.1", "cat /etc/hostname; id")
        assert done.returncode == 2, done
        assert b"restricted to the cwcli bridge" in done.stderr
        assert b"uid=" not in done.stdout

    def test_the_restricted_key_forwards_no_port(self, lab):
        local = _free_port()
        # -N: no command; the forward is the only request, and `restrict` refuses it.
        proc = subprocess.Popen(
            [
                "ssh", "-F", "/dev/null", "-i", str(lab.hub_home / "keys" / "hub_ed25519"),
                "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null", "-o", "ExitOnForwardFailure=yes", "-o", "ControlMaster=no",
                "-N", "-L", f"{local}:127.0.0.1:{lab.remote_port}", "-p", str(lab.ssh_port),
                "--", f"{os.environ.get('USER') or os.getlogin()}@127.0.0.1",
            ],  # fmt: skip
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            time.sleep(2)
            # Either sshd closed the session outright, or the tunnel is open but every use is refused.
            if proc.poll() is None:
                with socket.socket() as s:
                    s.settimeout(5)
                    s.connect(("127.0.0.1", local))
                    s.sendall(b"GET /api/v1/meta HTTP/1.1\r\nHost: x\r\n\r\n")
                    try:
                        answer = s.recv(200)
                    except OSError:
                        answer = b""
                assert b"HTTP/1.1 200" not in answer
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)

    def test_stdin_that_is_not_an_http_request_is_refused(self, lab):
        done = lab.ssh_with_hub_key("--", f"{os.environ.get('USER') or os.getlogin()}@127.0.0.1", stdin=b"id\n")
        assert done.returncode == 2 and b"expected an HTTP request" in done.stderr


class TestTheRemoteCapsTheHub:
    def test_an_admin_at_the_hub_is_an_operator_on_the_remote(self, lab):
        # The hub's bearer is Admin at the hub; the remote granted Operator.
        status, body, _ = _api(lab.hub, f"/api/v1/hosts/{lab.host_id}/settings", {"setting": "tips.enable"}, token=HUB_TOKEN)
        assert status == 403 and _code(body) == "auth.role_forbidden" and body["errors"][0]["role"] == "operator"
        status, body, _ = _api(lab.hub, f"/api/v1/hosts/{lab.host_id}/integrations", token=HUB_TOKEN)
        assert status == 403 and _code(body) == "auth.role_forbidden"
        # The remote's own settings are untouched.
        assert _api(lab.remote, "/api/v1/config", token=REMOTE_TOKEN)[0] == 200

    def test_even_the_remotes_real_token_and_an_admin_claim_through_the_bridge_stay_capped(self, lab):
        request = (
            "POST /api/v1/settings HTTP/1.1\r\nHost: evil\r\n"
            f"Authorization: Bearer {REMOTE_TOKEN}\r\nX-Cwcli-Delegated-Role: admin\r\nX-Cwcli-Delegated-User: root\r\n"
            "Content-Type: application/json\r\nContent-Length: 25\r\n\r\n"
            '{"setting":"tips.enable"}'
        ).encode()
        done = lab.ssh_with_hub_key("--", f"{os.environ.get('USER') or os.getlogin()}@127.0.0.1", stdin=request)
        assert done.returncode == 0, done.stderr
        head, _, body = done.stdout.partition(b"\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 403")
        assert json.loads(body)["errors"][0]["role"] == "operator"

    def test_a_lowered_cap_reaches_the_connection_that_is_already_open(self, lab):
        """sshd binds the forced command, and the cap in it, when a connection
        authenticates, and the hub keeps that connection open: re-granting at a lower
        cap must still take effect for it, not wait for the connection to drop."""
        assert lab.ensure_connected()["cap"] == "operator"
        start = {"verb": "start", "target": {"project": "cwe2e-no-such-instance-remote"}, "args": {}}

        def cap_is(cap):
            return lambda: (lambda v: v if v["state"] == "connected" and v["cap"] == cap else None)(lab.host_view())

        try:
            lab.allow(cap="viewer")
            harness.wait_until(cap_is("viewer"), timeout=60, interval=0.5, desc="the hub to read the lowered cap")
            status, body, _ = _api(lab.hub, f"/api/v1/hosts/{lab.host_id}/jobs", start, token=HUB_TOKEN)
            assert status == 403 and _code(body) == "auth.role_forbidden" and body["errors"][0]["role"] == "viewer", body
        finally:
            lab.allow()
        harness.wait_until(cap_is("operator"), timeout=60, interval=0.5, desc="the hub to read the restored cap")

    def test_a_job_started_through_the_hub_runs_on_the_remote_and_records_the_hub(self, lab):
        payload = {"verb": "start", "target": {"project": "cwe2e-no-such-instance-remote"}, "args": {}}
        status, body, _ = _api(lab.hub, f"/api/v1/hosts/{lab.host_id}/jobs", payload, token=HUB_TOKEN)
        assert status == 202, body
        job_id = body["data"]["id"]
        # The remote's own store holds it, attributed to the hub's user.
        record = harness.wait_until(
            lambda: (lambda r: r if r[0] == 200 and r[1]["data"]["state"] not in ("queued", "running") else None)(
                _api(lab.remote, f"/api/v1/jobs/{job_id}", token=REMOTE_TOKEN)
            ),
            timeout=120,
            interval=1,
            desc="the remote job to finish",
        )[1]["data"]
        assert record["origin"]["via"] == "hub" and record["origin"]["hub"] == HUB
        assert record["origin"]["account"] == f"api@{HUB}" and record["origin"]["account_source"] == "hub"
        assert record["state"] == "failed"  # no such instance there: the remote's own answer
        # ... and the hub reads it back through the forward, as a remote job.
        status, through, _ = _api(lab.hub, f"/api/v1/hosts/{lab.host_id}/jobs/{job_id}", token=HUB_TOKEN)
        assert status == 200 and through["data"]["id"] == job_id
        # Only an Admin at the hub may ask for a removal there, and the remote refuses it anyway.
        status, body, _ = _api(lab.hub, f"/api/v1/hosts/{lab.host_id}/jobs", {"verb": "rm", "target": {"project": "x"}, "consent": True}, token=HUB_TOKEN)
        assert status == 403 and _code(body) == "auth.role_forbidden"


class TestTrustIsPinnedBothWays:
    def test_a_changed_host_key_is_a_hard_stop_until_an_admin_repins(self, lab):
        real = lab.host_view()
        status, body, _ = _api(lab.hub, "/api/v1/hosts", {"op": "repin", "id": lab.host_id, "host_key": OTHER_KEY}, token=HUB_TOKEN)
        assert status == 200, body
        view = body["data"]["host"]
        if view["state"] == "connecting":
            view = lab.check()
        assert view["state"] == "host_key_changed", view
        assert view["actions_allowed"] is False
        status, body, _ = _api(lab.hub, f"/api/v1/hosts/{lab.host_id}/jobs", {"verb": "start", "target": {"project": "p"}}, token=HUB_TOKEN)
        assert status == 409 and _code(body) == "host.not_connected"
        # It never reconnects by itself: a check later still says so.
        assert lab.check()["state"] == "host_key_changed"
        # The Admin pins the real key again.
        host_key = (lab.ssh_dir / "host_ed25519.pub").read_text()
        status, body, _ = _api(lab.hub, "/api/v1/hosts", {"op": "repin", "id": lab.host_id, "host_key": host_key}, token=HUB_TOKEN)
        assert status == 200, body
        view = body["data"]["host"] if body["data"]["host"]["state"] != "connecting" else lab.check()
        assert view["state"] == "connected", view
        assert view["host_key_fingerprint"] == real["host_key_fingerprint"]

    def test_revoke_on_the_remote_reaches_the_connection_that_is_already_open(self, lab):
        revoked = lab.cwcli(lab.remote_home, "remote", "revoke", "--hub", HUB, "--json")
        assert revoked.returncode == 0, revoked.stderr
        assert json.loads(revoked.stdout)["removed"] == 1
        assert lab.cwcli(lab.remote_home, "remote", "hubs", "--json").stdout.strip() == "[]"
        # The hub's multiplexed connection is still open; the bridge re-reads the grant and
        # answers 403 bridge.revoked, the hub drops that connection, and the fresh one sshd
        # has to authenticate is refused one level lower (host.auth_refused). Once the link
        # itself shows the refusal, a read is answered without dialling (host.not_connected).
        status, body, _ = _api(lab.hub, f"/api/v1/hosts/{lab.host_id}/instances", token=HUB_TOKEN)
        assert (status, _code(body)) in (
            (403, "bridge.revoked"), (502, "host.auth_refused"), (502, "host.unreachable"), (409, "host.not_connected"),
        ), body  # fmt: skip
        view = lab.check()
        assert view["state"] == "auth_refused", view
        # Granting it again brings the link back.
        lab.allow()
        assert lab.check()["state"] == "connected"
        assert _api(lab.hub, f"/api/v1/hosts/{lab.host_id}/instances", token=HUB_TOKEN)[0] == 200


class TestPresenceIsReportedNeverActedOn:
    def test_a_quiet_remote_keeps_its_last_snapshot_as_unknown_and_nothing_runs(self, lab):
        before = lab.ensure_connected()
        assert before["state"] == "connected", before
        jobs_before = _api(lab.remote, "/api/v1/jobs", token=REMOTE_TOKEN)[1]["data"]
        lab.remote_proc.terminate()
        lab.remote_proc.wait(timeout=30)
        view = lab.check()
        assert view["state"] == "daemon_down", view
        assert view["last_seen"] is not None and view["actions_allowed"] is False
        # The hub's stream still carries the remote's instances, each honestly unknown.
        req = urllib.request.Request(lab.hub + "/api/v1/stream", headers={"Authorization": f"Bearer {HUB_TOKEN}"})
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
            line = b""
            while not line.startswith(b"data: "):
                line = resp.readline()
        (host,) = json.loads(line[6:])["hosts"]
        assert host["state"] == "daemon_down"
        assert host["instances"] and {i["overall"] for i in host["instances"]} == {"unknown"}
        # ... down to every bench, with the reason a page shows over the last readings.
        assert all(i["probe_error"] and {b["overall"] for b in i["benches"]} <= {"unknown"} for i in host["instances"])
        status, body, _ = _api(lab.hub, f"/api/v1/hosts/{lab.host_id}/jobs", {"verb": "start", "target": {"project": "p"}}, token=HUB_TOKEN)
        assert status == 409 and _code(body) == "host.not_connected"
        # Back up: the link recovers on its own and no job appeared on the remote meanwhile.
        lab.start_remote()
        view = lab.check()
        assert view["state"] == "connected", view
        jobs_after = _api(lab.remote, "/api/v1/jobs", token=REMOTE_TOKEN)[1]["data"]
        assert [j["id"] for j in jobs_after] == [j["id"] for j in jobs_before]
