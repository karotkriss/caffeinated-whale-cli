"""``utils.cred_daemon`` unit tests - the persistent credential-bridge daemon.

Covers the registry, the ensure step (idempotent shim + system git config +
auto-start), the AF_UNIX serving/reconcile path, the audit log, disable hygiene,
and the single most load-bearing property of the whole feature: the generated
persistent shim degrades SILENTLY (exit 0, empty stdout/stderr) against a dead
daemon, so a stopped/crashed/never-started bridge is byte-identical to "no helper
configured" and git falls straight through to its normal prompt (scout §5.8).

No real daemon is ever forked here (``start_daemon`` is faked); the serving path
is exercised by driving ``_reconcile_unix_listeners`` / ``_start_unix_listener``
directly and connecting a real client, the way ``test_core_credbridge`` does.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from caffeinated_whale_cli.core import credbridge
from caffeinated_whale_cli.utils import config_utils, cred_daemon

unix_only = pytest.mark.skipif(
    credbridge._prefer_tcp(),
    reason="AF_UNIX transport is native-Linux only",
)


@pytest.fixture
def run_dir(tmp_path, monkeypatch):
    """Point the daemon's whole footprint AND the config file at a temp dir."""
    rd = tmp_path / "run"
    monkeypatch.setattr(cred_daemon, "PID_DIR", rd)
    monkeypatch.setattr(cred_daemon, "PID_FILE", rd / "credbridge.pid")
    monkeypatch.setattr(cred_daemon, "LOG_FILE", rd / "credbridge.log")
    monkeypatch.setattr(cred_daemon, "AUDIT_FILE", rd / "credbridge-audit.log")
    monkeypatch.setattr(cred_daemon, "REGISTRY_FILE", rd / "credbridge-registry.json")
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config_utils, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config_utils, "CONFIG_FILE", cfg_dir / "config.toml")
    return tmp_path


class FakeContainer:
    """A frappe container with a workspace bind mount and a live /etc/gitconfig
    credential.helper list, so grep-before-add is exercised for real."""

    def __init__(self, host_dir, container_dir="/workspace"):
        self.attrs = {
            "Mounts": [{"Type": "bind", "Source": str(host_dir), "Destination": container_dir}]
        }
        self.exec_calls = []
        self.helpers: list[str] = []

    def exec_run(self, cmd, **kwargs):
        self.exec_calls.append(cmd)
        if cmd[:4] == ["git", "config", "--system", "--get-all"]:
            if self.helpers:
                return (0, ("\n".join(self.helpers) + "\n").encode())
            return (1, b"")  # git returns 1 when the key has no values
        if cmd[:4] == ["git", "config", "--system", "--add"]:
            self.helpers.append(cmd[5])
            return (0, b"")
        if cmd[:5] == ["git", "config", "--system", "--unset-all"]:
            self.helpers = [h for h in self.helpers if cmd[5] not in h]
            return (0, b"")
        return (0, b"")


def _drain(sock: socket.socket) -> bytes:
    resp = b""
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        resp += chunk
    return resp


# ------------------------------------------------------------------- config gate


def test_is_enabled_false_without_config(run_dir):
    assert cred_daemon.is_enabled() is False
    # NO-CREATE: the hot-path read must not write a default config file.
    assert not config_utils.CONFIG_FILE.exists()


def test_is_enabled_true_when_enabled(run_dir):
    config_utils.set_cred_bridge_enabled(True)
    assert cred_daemon.is_enabled() is True


# ---------------------------------------------------------------------- registry


def test_registry_register_read_deregister(run_dir):
    cred_daemon._register("proj-a", "/ws/a")
    cred_daemon._register("proj-b", "/ws/b")
    assert cred_daemon._read_registry() == {"proj-a": "/ws/a", "proj-b": "/ws/b"}
    assert cred_daemon.registered_projects() == ["proj-a", "proj-b"]
    cred_daemon.deregister("proj-a")
    assert cred_daemon._read_registry() == {"proj-b": "/ws/b"}


def test_register_is_atomic_and_idempotent(run_dir, tmp_path):
    cred_daemon._register("p", "/ws")
    cred_daemon._register("p", "/ws")  # no change
    assert cred_daemon._read_registry() == {"p": "/ws"}


def test_prune_vanished_drops_missing_workspaces(run_dir, tmp_path):
    live = tmp_path / "live"
    live.mkdir()
    reg = {"gone": str(tmp_path / "gone"), "here": str(live)}
    cred_daemon._write_registry(reg)
    pruned = cred_daemon._prune_vanished(cred_daemon._read_registry())
    assert pruned == {"here": str(live)}
    assert cred_daemon._read_registry() == {"here": str(live)}  # persisted


def test_registry_garbage_reads_as_empty(run_dir):
    cred_daemon._ensure_run_dir()
    cred_daemon.REGISTRY_FILE.write_text("not json")
    assert cred_daemon._read_registry() == {}


# ---------------------------------------------------------------- the ensure step


def test_ensure_is_a_noop_when_disabled(run_dir, tmp_path):
    container = FakeContainer(tmp_path)
    assert cred_daemon.ensure_bridge(container, "/workspace/frappe-bench", "proj") is None
    assert container.exec_calls == []  # never touched git config
    assert cred_daemon._read_registry() == {}


def test_ensure_is_a_noop_without_a_bind_mount(run_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(cred_daemon, "is_enabled", lambda: True)
    container = FakeContainer(tmp_path, container_dir="/somewhere-else")
    assert cred_daemon.ensure_bridge(container, "/workspace/frappe-bench", "proj") is None


@unix_only
def test_ensure_writes_shim_registers_configures_and_starts(run_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(cred_daemon, "is_enabled", lambda: True)
    monkeypatch.setattr(cred_daemon, "is_running", lambda: False)
    started = []
    monkeypatch.setattr(cred_daemon, "start_daemon", lambda: started.append(True))
    container = FakeContainer(tmp_path)

    outcome = cred_daemon.ensure_bridge(container, "/workspace/frappe-bench", "proj")

    assert outcome is not None
    assert outcome.project == "proj"
    assert outcome.workspace == str(tmp_path)
    assert outcome.added_config is True
    assert outcome.daemon_started is True
    # stable shim written into the bind mount (no per-invocation uuid)
    shim = tmp_path / credbridge.PERSISTENT_HELPER_NAME
    assert shim.exists()
    # registered
    assert cred_daemon._read_registry() == {"proj": str(tmp_path)}
    # system git config points at the stable shim
    assert outcome.config_value == "!/usr/bin/python3 /workspace/.cwcli-git-credential.py"
    assert ["git", "config", "--system", "--add", "credential.helper", outcome.config_value] in [
        c for c in container.exec_calls
    ]
    assert started == [True]


@unix_only
def test_ensure_is_idempotent_config_added_once(run_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(cred_daemon, "is_enabled", lambda: True)
    monkeypatch.setattr(cred_daemon, "is_running", lambda: True)  # already up
    monkeypatch.setattr(cred_daemon, "start_daemon", lambda: None)
    container = FakeContainer(tmp_path)

    first = cred_daemon.ensure_bridge(container, "/workspace/frappe-bench", "proj")
    second = cred_daemon.ensure_bridge(container, "/workspace/frappe-bench", "proj")

    assert first.added_config is True
    assert second.added_config is False
    adds = [c for c in container.exec_calls if c[:4] == ["git", "config", "--system", "--add"]]
    assert len(adds) == 1  # grep-before-add: exactly one line


def test_ensure_never_raises(run_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(cred_daemon, "is_enabled", lambda: True)
    monkeypatch.setattr(cred_daemon, "is_running", lambda: True)  # don't fork a real daemon

    class Boom(FakeContainer):
        def exec_run(self, cmd, **kwargs):
            raise RuntimeError("docker exploded")

    # A container whose exec blows up must not propagate: the git-config exec is
    # suppressed inside _ensure_container_config, so the outcome still returns
    # because the shim write + registry entry succeeded.
    assert cred_daemon.ensure_bridge(Boom(tmp_path), "/workspace/frappe-bench", "p") is not None


# ================================================================ THE §5.8 PIN
# The generated persistent shim must degrade SILENTLY against a dead daemon.


def _run_shim(shim_path, env=None, action="get", stdin=b"protocol=https\nhost=github.com\n\n"):
    return subprocess.run(
        [sys.executable, str(shim_path), action],
        input=stdin,
        capture_output=True,
        env={**os.environ, **(env or {})},
    )


@unix_only
def test_generated_unix_shim_exits_0_silently_against_a_dead_socket(tmp_path):
    """A dead/absent daemon socket -> the shim exits 0 with empty stdout AND empty
    stderr, so git's fill loop falls through to its prompt exactly as if no helper
    were configured. The single most load-bearing line of the design."""
    shim = tmp_path / "shim.py"
    shim.write_text(credbridge.persistent_helper_unix())
    dead_sock = tmp_path / "nonexistent.sock"

    out = _run_shim(shim, env={"CWCLI_CRED_SOCK": str(dead_sock)})

    assert out.returncode == 0
    assert out.stdout == b""
    assert out.stderr == b""


@unix_only
def test_generated_unix_shim_exits_0_against_a_refused_socket(tmp_path):
    """A socket file that exists but has nothing accepting (ECONNREFUSED) is also
    swallowed to exit 0, empty."""
    shim = tmp_path / "shim.py"
    shim.write_text(credbridge.persistent_helper_unix())
    # bound-but-not-listening: a fresh AF_UNIX socket file with no accept loop
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock_path = tmp_path / "s.sock"
    srv.bind(str(sock_path))
    # deliberately never listen()/accept()

    out = _run_shim(shim, env={"CWCLI_CRED_SOCK": str(sock_path)})
    srv.close()

    assert out.returncode == 0
    assert out.stdout == b""
    assert out.stderr == b""


def test_generated_shim_non_get_action_exits_0(tmp_path):
    """store/erase are no-ops: nothing is ever persisted in the container."""
    shim = tmp_path / "shim.py"
    shim.write_text(credbridge.persistent_helper_unix())
    out = _run_shim(shim, action="store", stdin=b"password=SECRET\n")
    assert out.returncode == 0
    assert out.stdout == b""


# ================================================== serving + audit (AF_UNIX)


@unix_only
def test_reconcile_starts_a_listener_that_answers_and_audits(run_dir, tmp_path, monkeypatch):
    """A registered workspace gets a live listener that answers a git-credential
    request from the host tool AND writes an audit line attributed to the project
    - never a credential byte."""
    monkeypatch.setattr(
        credbridge,
        "host_credential",
        lambda req, audit=None, allowlist=None: (
            audit and audit("github.com", True),
            b"password=TOKEN\n",
        )[1],
    )
    ws = tmp_path / "ws"
    ws.mkdir()
    cred_daemon._register("proj-x", str(ws))

    stop = threading.Event()
    listeners: dict = {}
    cred_daemon._reconcile_unix_listeners(listeners, cred_daemon._read_registry(), stop, credbridge)
    try:
        sock_path = ws / credbridge.PERSISTENT_SOCK_NAME
        assert sock_path.exists()
        # the stable shim was (re)written by the listener startup
        assert (ws / credbridge.PERSISTENT_HELPER_NAME).exists()

        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(sock_path))
        client.sendall(b"protocol=https\nhost=github.com\n\n")
        client.shutdown(socket.SHUT_WR)
        assert _drain(client) == b"password=TOKEN\n"
        client.close()
    finally:
        stop.set()
        for lst in listeners.values():
            lst.srv.close()

    # audit line landed, attributed to the project, no credential material
    audit = cred_daemon.recent_audit()
    assert any("project=proj-x" in ln and "host=github.com" in ln for ln in audit)
    assert all("TOKEN" not in ln and "password" not in ln for ln in audit)


@unix_only
def test_reconcile_stops_a_listener_and_unlinks_when_deregistered(run_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(credbridge, "host_credential", lambda req, audit=None, allowlist=None: b"")
    ws = tmp_path / "ws"
    ws.mkdir()
    cred_daemon._register("p", str(ws))
    stop = threading.Event()
    listeners: dict = {}
    cred_daemon._reconcile_unix_listeners(listeners, {"p": str(ws)}, stop, credbridge)
    assert (ws / credbridge.PERSISTENT_SOCK_NAME).exists()

    cred_daemon._reconcile_unix_listeners(listeners, {}, stop, credbridge)
    stop.set()
    assert listeners == {}
    assert not (ws / credbridge.PERSISTENT_SOCK_NAME).exists()


# ==================================================== the host allowlist (phase 3)


def test_allowed_hosts_defaults_to_the_pair_without_config(run_dir):
    assert cred_daemon.allowed_hosts() == {"github.com", "gitlab.com"}


def test_allowed_hosts_reads_the_config_key(run_dir):
    config_utils.set_cred_bridge_enabled(True)
    config = config_utils.load_config()
    config["cred_bridge"]["allowed_hosts"] = ["github.com", "git.corp.example:8443"]
    config_utils.save_config(config)
    assert cred_daemon.allowed_hosts() == {"github.com", "git.corp.example:8443"}


def test_an_explicit_empty_list_means_answer_nothing(run_dir):
    """[] is the user's own choice, never read as 'use the default' - that reading
    would be the config silently widening, the drift the allowlist guards."""
    config = config_utils.load_config()
    config["cred_bridge"]["allowed_hosts"] = []
    config_utils.save_config(config)
    assert cred_daemon.allowed_hosts() == set()


def test_a_malformed_key_degrades_to_the_default_pair(run_dir):
    config = config_utils.load_config()
    config["cred_bridge"]["allowed_hosts"] = "github.com"  # not a list
    config_utils.save_config(config)
    assert cred_daemon.allowed_hosts() == {"github.com", "gitlab.com"}


@unix_only
def test_daemon_listener_enforces_the_allowlist_and_rereads_it_live(run_dir, tmp_path, monkeypatch):
    """The REAL ``host_credential`` behind a live listener: a non-allowlisted host
    is answered with nothing BEFORE any host tool runs (audit answered=no), an
    allowlisted one answers - and a config edit takes effect on the very next
    request with NO listener restart, because the allowlist is read per request."""
    tool_calls: list[list[str]] = []

    def _fake_run(cmd, **kwargs):
        tool_calls.append(cmd)
        return SimpleNamespace(stdout=b"password=TOKEN\n")

    monkeypatch.setattr(credbridge.subprocess, "run", _fake_run)
    ws = tmp_path / "ws"
    ws.mkdir()
    cred_daemon._register("proj-a", str(ws))

    def ask(host: str) -> bytes:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(ws / credbridge.PERSISTENT_SOCK_NAME))
        client.sendall(f"protocol=https\nhost={host}\n\n".encode())
        client.shutdown(socket.SHUT_WR)
        resp = _drain(client)
        client.close()
        return resp

    stop = threading.Event()
    listeners: dict = {}
    cred_daemon._reconcile_unix_listeners(listeners, cred_daemon._read_registry(), stop, credbridge)
    try:
        # Default allowlist: the foreign host is refused, no tool ever invoked.
        assert ask("git.corp.example") == b""
        assert tool_calls == []
        # An allowlisted host answers.
        assert ask("github.com") == b"password=TOKEN\n"
        assert len(tool_calls) == 1
        # Config edit, SAME listener: the next request already honours it.
        config = config_utils.load_config()
        config["cred_bridge"]["allowed_hosts"] = ["git.corp.example"]
        config_utils.save_config(config)
        assert ask("git.corp.example") == b"password=TOKEN\n"
        assert ask("github.com") == b""  # and github.com is now outside the list
    finally:
        stop.set()
        for lst in listeners.values():
            lst.srv.close()

    audit = cred_daemon.recent_audit()
    assert any("host=git.corp.example answered=no" in ln for ln in audit)
    assert any("host=github.com answered=yes" in ln for ln in audit)
    assert all("TOKEN" not in ln for ln in audit)


# ------------------------------------------------------------------------ audit


def test_audit_writes_host_and_answered_never_a_credential(run_dir):
    cred_daemon._audit("proj", "github.com", True)
    cred_daemon._audit(None, "gitlab.com", False)
    lines = cred_daemon.recent_audit()
    assert "project=proj host=github.com answered=yes" in lines[0]
    assert "project=- host=gitlab.com answered=no" in lines[1]


def test_recent_audit_empty_when_no_log(run_dir):
    assert cred_daemon.recent_audit() == []


# -------------------------------------------------------------- disable hygiene


@unix_only
def test_disable_makes_shims_inert_unlinks_sockets_and_clears_registry(
    run_dir, tmp_path, monkeypatch
):
    monkeypatch.setattr(cred_daemon, "_best_effort_container_unset", lambda project: None)
    ws = tmp_path / "ws"
    ws.mkdir()
    shim = ws / credbridge.PERSISTENT_HELPER_NAME
    sock = ws / credbridge.PERSISTENT_SOCK_NAME
    shim.write_text(credbridge.persistent_helper_unix())
    sock.write_bytes(b"")  # stand-in socket file
    cred_daemon._register("p", str(ws))

    cred_daemon.disable_bridge_artifacts()

    assert shim.read_text() == credbridge.persistent_helper_stub()
    assert not sock.exists()
    assert cred_daemon._read_registry() == {}


# -------------------------------------------------------------------- transport


def test_transport_reports_unix_or_tcp():
    assert cred_daemon.transport() in {"unix", "tcp"}


def test_ensure_running_instances_zero_when_disabled(run_dir, monkeypatch):
    monkeypatch.setattr(cred_daemon, "is_enabled", lambda: False)
    assert cred_daemon.ensure_running_instances() == 0


# ------------------------------------------------------------------- lifecycle


def test_bootstrap_source_is_valid_python():
    source = cred_daemon._bootstrap_source("/home/user/site-packages")
    compile(source, "<bootstrap>", "exec")
    assert "_run_daemon_loop()" in source


def test_start_daemon_is_a_noop_when_already_running(run_dir, monkeypatch):
    monkeypatch.setattr(cred_daemon, "is_running", lambda: True)
    forked = []
    monkeypatch.setattr(cred_daemon.os, "fork", lambda: forked.append(True), raising=False)
    cred_daemon.start_daemon()
    assert forked == []  # never forked over a live daemon


def test_start_daemon_falls_back_to_spawn_without_fork(run_dir, monkeypatch):
    monkeypatch.setattr(cred_daemon, "is_running", lambda: False)
    monkeypatch.delattr(cred_daemon.os, "fork", raising=False)
    monkeypatch.setattr(cred_daemon, "_await_started", lambda timeout=2.0: None)
    spawned = []
    monkeypatch.setattr(cred_daemon, "_spawn_detached", lambda: spawned.append(True))
    cred_daemon.start_daemon()
    assert spawned == [True]


def test_concurrent_start_forks_at_most_one_daemon(run_dir, monkeypatch):
    """The startup lock serializes concurrent start_daemon calls: exactly ONE
    reaches the fork/spawn, the rest re-check is_running() under the lock and
    no-op - never a rival daemon."""
    running = {"up": False}
    monkeypatch.setattr(cred_daemon, "is_running", lambda: running["up"])
    starts = []

    def fake_start(lock_fd):
        starts.append(True)
        time.sleep(0.05)  # widen the window every rival races through
        running["up"] = True

    monkeypatch.setattr(cred_daemon, "_fork_or_spawn", fake_start)

    threads = [threading.Thread(target=cred_daemon.start_daemon) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert starts == [True]


def test_stale_lock_file_does_not_prevent_start(run_dir, monkeypatch):
    """A lock FILE left behind by a dead starter (flock/msvcrt lock long released
    by the kernel) never blocks a fresh start."""
    (run_dir / "run").mkdir(parents=True, exist_ok=True)
    (run_dir / "run" / "credbridge.start.lock").write_text("")
    monkeypatch.setattr(cred_daemon, "is_running", lambda: False)
    started = []
    monkeypatch.setattr(cred_daemon, "_fork_or_spawn", lambda lock_fd: started.append(True))
    cred_daemon.start_daemon()
    assert started == [True]


def test_stop_daemon_is_a_noop_when_not_running(run_dir, monkeypatch):
    monkeypatch.setattr(cred_daemon, "is_running", lambda: False)
    killed = []
    monkeypatch.setattr(cred_daemon.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    cred_daemon.stop_daemon()
    assert killed == []
