"""Committed E2E: in shared mode a member's fetch uses the SHARED credential first.

On a shared-mode host the machine-wide credential bridge runs as the ``cwcli``
service account, whose gh/glab holds the one shared (read-only) credential the
containers' interactive git uses. A member's ``cwcli init --frappe-url <private>``
(and ``apps install``/``apps update``) used to ask ONLY the invoking member's own
host gh/glab through the per-invocation bridge, so a member who had not signed in
personally failed at ``bench init`` with ``could not read Username``.

The fix: in shared mode ``credential_bridge`` first wires the instance into the
shared bridge (the system-level helper git asks FIRST), so the shared credential
answers; the member's own sign-in is only the automatic fallback, announced with
one stderr line.

Everything runs inside ONE throwaway ``python:3.12-slim`` sandbox as root, with
real users: the ``cwcli`` service account (running the real shared daemon with
its own fake ``glab``), a member ``alice`` (with her own fake ``glab``, signed in
or not), and ``frappe`` (the container git user). A local git smart-HTTP server
requires basic auth and logs WHICH identity authenticated, so the assertions name
the credential that was actually used. The "container" is a stand-in that runs
``git`` in the same sandbox as ``frappe`` (or root, for the system git config) -
the credential path, sockets, shims, permissions, and git's own helper order are
all real. Only obviously fake credentials exist. Marked ``standalone``: it never
touches the shared session instance.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.standalone]

_REPO_ROOT = Path(__file__).resolve().parents[2]

# A git smart-HTTP server (git http-backend behind wsgiref) that demands basic
# auth and appends the authenticated username to /tmp/auth.log.
_GIT_SERVER = r"""
import base64, subprocess, sys
from wsgiref.simple_server import make_server, WSGIRequestHandler

USERS = {"svc-shared": "svc-pass", "alice-own": "alice-pass"}

class Quiet(WSGIRequestHandler):
    def log_message(self, *a):
        pass

def app(environ, start):
    user = None
    auth = environ.get("HTTP_AUTHORIZATION", "")
    if auth.startswith("Basic "):
        name, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
        if USERS.get(name) == pw:
            user = name
    if user is None:
        start("401 Unauthorized", [("WWW-Authenticate", 'Basic realm="git"')])
        return [b"auth required\n"]
    with open("/tmp/auth.log", "a") as f:
        f.write(user + "\n")
    length = int(environ.get("CONTENT_LENGTH") or 0)
    body = environ["wsgi.input"].read(length) if length else b""
    env = {
        "GIT_PROJECT_ROOT": "/srv/git", "GIT_HTTP_EXPORT_ALL": "1",
        "PATH_INFO": environ.get("PATH_INFO", ""),
        "QUERY_STRING": environ.get("QUERY_STRING", ""),
        "REQUEST_METHOD": environ["REQUEST_METHOD"],
        "CONTENT_TYPE": environ.get("CONTENT_TYPE", ""),
        "REMOTE_USER": user, "PATH": "/usr/bin:/bin",
    }
    out = subprocess.run(["git", "http-backend"], input=body, env=env,
                         capture_output=True).stdout
    head, _, payload = out.partition(b"\r\n\r\n")
    status, headers = "200 OK", []
    for line in head.decode().split("\r\n"):
        k, _, v = line.partition(": ")
        if k.lower() == "status":
            status = v
        elif k:
            headers.append((k, v))
    start(status, headers)
    return [payload]

make_server("127.0.0.1", 8123, app, handler_class=Quiet).serve_forever()
"""

# The member's fetch: the REAL credential_bridge around a git ls-remote run by the
# container stand-in, exactly as init/apps wrap their bench git fetches.
_MEMBER_FETCH = r'''
import subprocess, sys
from caffeinated_whale_cli.core import credbridge

WS = "/var/lib/cwcli/projects/demo/data"

class Container:
    """A stand-in for the frappe container: same filesystem, identity bind mount."""
    labels = {"com.docker.compose.project": "demo"}
    attrs = {"Mounts": [{"Type": "bind", "Source": WS, "Destination": WS}]}
    def exec_run(self, cmd, user=None, **kw):
        p = subprocess.run(["sudo", "-n", "-H", "-u", user or "frappe", "--"] + list(cmd),
                           capture_output=True)
        return p.returncode, p.stdout + p.stderr

c = Container()
with credbridge.credential_bridge(c, WS + "/frappe-bench"):
    code, out = c.exec_run(["env", "GIT_TERMINAL_PROMPT=0", "git", "ls-remote",
                            "http://127.0.0.1:8123/private.git"])
print("FETCH_EXIT=%d" % code)
sys.stdout.write(out.decode(errors="replace"))
'''

_GLAB = """#!/bin/sh
[ "$1 $2 $3" = "auth git-credential get" ] || exit 1
cat >/dev/null
{guard}
echo username={user}
echo password={password}
"""

_SCRIPT = r"""
set -e
apt-get -qq update >/dev/null && apt-get -qq install -y git sudo >/dev/null 2>&1
ln -sf /usr/local/bin/python3 /usr/bin/python3
cp -r /src /build
rm -rf /build/.venv /build/build /build/*.egg-info /build/webui/node_modules 2>/dev/null || true
pip install -q /build 2>&1 | tail -1

useradd -m -s /bin/bash alice
useradd -m -s /bin/bash frappe
cwcli setup shared --users alice,frappe --cred-bridge >/dev/null 2>&1 || true
python3 - <<'PY'
import toml
p = "/var/lib/cwcli/config/config.toml"
d = toml.load(p)
d["cred_bridge"]["allowed_hosts"] = ["127.0.0.1:8123"]
open(p, "w").write(toml.dumps(d))
PY
# The member's "docker exec" into the stand-in container.
echo 'alice ALL=(root,frappe) NOPASSWD: ALL' > /etc/sudoers.d/alice

# A private repo behind basic auth.
git init -q --bare /srv/git/private.git
git -C /tmp init -q seed && git -C /tmp/seed -c user.email=e@x -c user.name=e \
  commit -q --allow-empty -m seed && git -C /tmp/seed push -q /srv/git/private.git HEAD:main
python3 /sandbox/git_server.py & sleep 1

# The instance workspace, as the shared projects tree lays it out.
mkdir -p /var/lib/cwcli/projects/demo/data
chgrp -R cwcli /var/lib/cwcli/projects && chmod -R 2770 /var/lib/cwcli/projects

# Two identities: the service account's glab, and the member's own glab.
mkdir -p /opt/glab-svc /opt/glab-alice
cp /sandbox/glab-svc /opt/glab-svc/glab && cp /sandbox/glab-alice /opt/glab-alice/glab
chmod 755 /opt/glab-svc/glab /opt/glab-alice/glab

start_shared() {
  runuser -u cwcli -- env PATH=/opt/glab-svc:$PATH HOME=/var/lib/cwcli python3 -c \
    'from caffeinated_whale_cli.utils import cred_daemon; cred_daemon.start_daemon()'
}
stop_shared() {
  runuser -u cwcli -- env HOME=/var/lib/cwcli python3 -c \
    'from caffeinated_whale_cli.utils import cred_daemon; cred_daemon.stop_daemon()'
}
fetch() {  # $1 = scenario label
  : > /tmp/auth.log
  su - alice -c "PATH=/opt/glab-alice:\$PATH python3 /sandbox/member_fetch.py" \
    > /tmp/$1.out 2> /tmp/$1.err || true
  echo "== $1"
  grep -m1 FETCH_EXIT /tmp/$1.out
  echo "AUTH_USER=$(tail -1 /tmp/auth.log)"
  echo "NOTICE=$(grep -c 'shared credential' /tmp/$1.err || true)"
  grep 'shared credential' /tmp/$1.err || true
  grep -h 'could not read Username' /tmp/$1.out || true
}

start_shared

# 1. Fresh instance, member NOT signed in: the reported failure.
fetch fresh_member_signed_out
# 2. Member signed in personally: the shared credential still goes first.
touch /tmp/alice-signed-in
fetch member_signed_in
# 3. Shared bridge does not serve this host: the member's own sign-in is used, said once.
python3 - <<'PY'
import toml
p = "/var/lib/cwcli/config/config.toml"
d = toml.load(p); d["cred_bridge"]["allowed_hosts"] = []; open(p, "w").write(toml.dumps(d))
PY
fetch shared_refuses_host
# 4. Shared daemon down: the member's own sign-in is used, said once.
stop_shared
fetch shared_daemon_down

# No credential byte ever lands in cwcli's state or logs.
if grep -rqs -e svc-pass -e alice-pass /var/lib/cwcli; then echo SECRET_LEAKED; else echo NO_SECRET_IN_STATE; fi
"""


def run_sandbox(src: Path = _REPO_ROOT) -> str:
    """Run the whole scenario in one throwaway container; returns its combined output."""
    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp)
        (sandbox / "git_server.py").write_text(_GIT_SERVER)
        (sandbox / "member_fetch.py").write_text(_MEMBER_FETCH)
        (sandbox / "glab-svc").write_text(
            _GLAB.format(guard="", user="svc-shared", password="svc-pass")
        )
        (sandbox / "glab-alice").write_text(
            _GLAB.format(
                guard="[ -e /tmp/alice-signed-in ] || exit 1",
                user="alice-own",
                password="alice-pass",
            )
        )
        proc = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{src}:/src:ro",
                "-v",
                f"{sandbox}:/sandbox:ro",
                "python:3.12-slim",
                "bash",
                "-c",
                _SCRIPT,
            ],
            capture_output=True,
            text=True,
            timeout=900,
        )
    return proc.stdout + proc.stderr


def _section(output: str, label: str) -> str:
    return output.split(f"== {label}\n", 1)[1].split("\n== ", 1)[0]


def test_shared_mode_member_fetch_uses_the_shared_credential_first():
    output = run_sandbox()

    # The reported failure: a member who never signed in personally can fetch.
    fresh = _section(output, "fresh_member_signed_out")
    assert "FETCH_EXIT=0" in fresh, output
    assert "AUTH_USER=svc-shared" in fresh, output
    assert "NOTICE=0" in fresh, output

    # Shared is the default even when the member has their own sign-in.
    signed_in = _section(output, "member_signed_in")
    assert "FETCH_EXIT=0" in signed_in, output
    assert "AUTH_USER=svc-shared" in signed_in, output
    assert "NOTICE=0" in signed_in, output

    # Automatic fallback to the member's own sign-in, announced in one line.
    for label in ("shared_refuses_host", "shared_daemon_down"):
        fallback = _section(output, label)
        assert "FETCH_EXIT=0" in fallback, output
        assert "AUTH_USER=alice-own" in fallback, output
        assert "NOTICE=1" in fallback, output

    assert "NO_SECRET_IN_STATE" in output, output
