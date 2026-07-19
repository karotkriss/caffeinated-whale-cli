"""``core.credbridge`` - a git credential-helper bridge, host ``gh``/``glab`` -> in-container git.

Private GitHub/GitLab Frappe app repos need an authenticated fetch during
``bench get-app`` / ``bench update``, but we want NO ``gh``/``glab`` installed in
the container and the raw token NEVER inside it. This bridges the container's git
back to the HOST's already-authenticated ``gh``/``glab`` over a unix socket placed
in cwcli's existing workspace bind mount (host ``.../data`` <-> container
``/workspace``): a socket a host process holds appears in the container as the same
inode, so git can reach it with no network/compose changes. Proven end to end on a
real bench (``g6-credbridge-proof``).

Three pieces (do not elaborate them):

1. A host listener (an in-process daemon thread) on ``<workspace>/.git-cred.sock``.
   Per connection it dispatches by the request's ``host=`` field - ``github.com``
   -> ``gh``, anything else -> ``glab`` - pipes the request into
   ``<tool> auth git-credential get`` and returns its stdout. The raw token lives
   only in host memory during that exchange; cwcli forwards bytes, never parses or
   stores a token.
2. A container-side shim written into the bind mount
   (``<workspace>/.git-credential-bridge.py``). git calls it as a credential
   helper; it forwards git's stdin over the socket and returns the response.
   ``store``/``erase`` are no-ops, so nothing is ever persisted in the container.
3. One global git-config line for the container ``frappe`` user pointing at the
   shim, in the ``!``-shell form with an absolute interpreter so it is
   PATH-independent. The socket, shim, and config line are all named uniquely per
   invocation (a ``uuid4`` token) and added/removed with ``git config --add``/
   ``--unset <exact value>``, so two concurrent installs/updates against the same
   bench never clobber each other's bridge or credential-helper entry.

git only invokes a credential helper on an HTTP 401, so the bridge is inert for
public repos - which is why :func:`credential_bridge` can safely wrap EVERY
git-URL install and every update without pre-detecting a private repo. It is a
context manager that always tears everything down, including on failure.
"""

from __future__ import annotations

import contextlib
import os
import re
import socket
import subprocess
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path

_SOCK_NAME_FMT = ".git-cred-{}.sock"
_HELPER_NAME_FMT = ".git-credential-bridge-{}.py"

# The container-side shim. It locates its socket as a sibling of itself by default
# (works for any --bench-parent), but the DEFAULT is baked in per invocation (the
# `{sock_name!r}` below) rather than a fixed name, so two concurrent bridges never
# read each other's socket even if CWCLI_CRED_SOCK is unset. Only `get` does
# anything; store/erase exit 0 without persisting, so no credential is ever written
# in the container.
_CONTAINER_HELPER_SRC = """\
import os, socket, sys
if (sys.argv[1] if len(sys.argv) > 1 else "get") != "get":
    sys.exit(0)
sock = os.environ.get("CWCLI_CRED_SOCK") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), {sock_name!r}
)
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(sock)
s.sendall(sys.stdin.buffer.read())
s.shutdown(socket.SHUT_WR)
resp = b""
while True:
    c = s.recv(4096)
    if not c:
        break
    resp += c
sys.stdout.buffer.write(resp)
"""

# How often the accept loop wakes to re-check the stop flag. Small so teardown is
# deterministic and platform-independent (closing a listening socket does not
# reliably interrupt a blocked accept()).
_ACCEPT_TIMEOUT = 0.5


def host_credential(request: bytes) -> bytes:
    """Answer one git-credential request from the host's ``gh``/``glab``.

    Dispatches by the ``host=`` field. The raw token is only ever in this
    process's memory during the ``subprocess`` exchange, then forwarded verbatim -
    it is never parsed here, never stored, never logged.
    """
    host = ""
    for line in request.decode("utf-8", "replace").splitlines():
        if line.startswith("host="):
            host = line[5:].strip()
    tool = "gh" if host.endswith("github.com") else "glab"
    try:
        return subprocess.run(
            [tool, "auth", "git-credential", "get"],
            input=request,
            capture_output=True,
        ).stdout
    except FileNotFoundError:
        # The host lacks gh/glab: return nothing, git falls back to its usual
        # unauthenticated behaviour (which fails for a private repo, as before).
        return b""


def _serve(srv: socket.socket, stop: threading.Event) -> None:
    """Accept connections until ``stop`` is set, answering each from the host tool."""
    while not stop.is_set():
        try:
            conn, _ = srv.accept()
        except TimeoutError:
            continue  # settimeout woke us to re-check `stop`; keep listening
        except OSError:
            break  # socket closed by teardown
        with conn:
            req = b""
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                req += chunk
            with contextlib.suppress(OSError):
                conn.sendall(host_credential(req))


def _teardown(
    container,
    srv: socket.socket | None,
    stop: threading.Event | None,
    thread: threading.Thread | None,
    sock_host: Path,
    helper_host: Path,
    unset_pattern: str,
) -> None:
    """Undo whatever setup got as far as creating: config, listener thread, socket, files.

    ``unset_pattern`` is an anchored regex matching ONLY this invocation's config
    value, so a concurrent bridge's own ``credential.helper`` line (or a
    pre-existing, unrelated one) is left in place.
    """
    with contextlib.suppress(Exception):
        container.exec_run(
            ["git", "config", "--global", "--unset", "credential.helper", unset_pattern]
        )
    if stop is not None:
        stop.set()
    if srv is not None:
        srv.close()
    if thread is not None:
        thread.join(timeout=2)
    for path in (sock_host, helper_host):
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


def _resolve_workspace_mount(container, bench_path: str) -> tuple[Path, str] | None:
    """Find the bind mount the bench lives under: ``(host_dir, container_dir)``.

    Authoritative for old-form (``..:/workspace``) and new-form
    (``../data:/workspace``) instances and any custom ``--bench-parent``, because it
    reads the container's ACTUAL mounts rather than reconstructing the path. Picks
    the longest bind-mount destination that is an ancestor of ``bench_path``.
    """
    best: tuple[Path, str] | None = None
    for mount in getattr(container, "attrs", {}).get("Mounts", []):
        if mount.get("Type") != "bind":
            continue
        dest = (mount.get("Destination") or "").rstrip("/")
        src = mount.get("Source")
        if not dest or not src:
            continue
        if bench_path == dest or bench_path.startswith(dest + "/"):
            if best is None or len(dest) > len(best[1]):
                best = (Path(src), dest)
    return best


@contextlib.contextmanager
def credential_bridge(container, bench_path: str) -> Iterator[None]:
    """Stand up the git credential bridge for the duration of a fetch/update op.

    Resolves the workspace bind mount, writes the container shim into it, starts the
    host listener, and points the container ``frappe`` user's global git at the
    shim. Tears all of it down on exit (socket, shim, listener, git config) whether
    the wrapped op succeeds or raises. A no-op (still yields) when no bind mount can
    be resolved - public fetches keep working, private ones fail as they did before.
    """
    resolved = _resolve_workspace_mount(container, bench_path)
    if resolved is None:
        yield
        return
    host_dir, container_dir = resolved

    token = uuid.uuid4().hex[:12]
    sock_name = _SOCK_NAME_FMT.format(token)
    helper_name = _HELPER_NAME_FMT.format(token)
    sock_host = host_dir / sock_name
    helper_host = host_dir / helper_name
    helper_container = f"{container_dir}/{helper_name}"
    config_value = f"!/usr/bin/python3 {helper_container}"
    unset_pattern = f"^{re.escape(config_value)}$"

    srv: socket.socket | None = None
    stop: threading.Event | None = None
    thread: threading.Thread | None = None
    try:
        helper_host.write_text(_CONTAINER_HELPER_SRC.format(sock_name=sock_name))
        with contextlib.suppress(FileNotFoundError):
            sock_host.unlink()

        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # AF_UNIX sun_path caps at ~108 bytes, and a long CWCLI_HOME easily exceeds it.
        # Bind the short RELATIVE name from inside the mount dir so sun_path stays tiny;
        # the socket file lands at sock_host all the same, and cwd is restored at once
        # (the listening fd does not depend on it afterwards). The container side always
        # connects via its own short /workspace path, so it is never affected.
        prev_cwd = os.getcwd()
        try:
            os.chdir(host_dir)
            srv.bind(sock_name)
        finally:
            os.chdir(prev_cwd)
        # 0666 so the container frappe user connects regardless of whether its uid was
        # aligned to the host's; the socket only exists during the op.
        sock_host.chmod(0o666)
        srv.settimeout(_ACCEPT_TIMEOUT)
        srv.listen(8)

        stop = threading.Event()
        thread = threading.Thread(target=_serve, args=(srv, stop), daemon=True)
        thread.start()

        container.exec_run(
            ["git", "config", "--global", "--add", "credential.helper", config_value]
        )
    except Exception:
        _teardown(container, srv, stop, thread, sock_host, helper_host, unset_pattern)
        raise

    try:
        yield
    finally:
        _teardown(container, srv, stop, thread, sock_host, helper_host, unset_pattern)
