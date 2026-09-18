"""Committed two-uid E2E for the opt-in shared/multi-user home.

Proves, with REAL distinct uids, the two properties the shared-home design exists
to deliver (scout report `cwcli-shared-home-multiuser` §2 / §7 Tests):

1. a second user (in the ``cwcli`` group) can drive shared cwcli state - read the
   config, read AND write the cache - with NO permission failure, and a third
   user reads state the second one wrote (the cross-user ``EACCES`` the per-user
   0700/0600 modes cause today, repro rows 1-3);
2. the credential socket is group-gated: a ``cwcli``-group member connects, a
   non-member is REFUSED at ``connect()`` - closing the world-connectable
   ``0666`` socket leak (repro row 5).

Everything runs inside ONE throwaway ``python:3.12-slim`` container where the
test process is root (so it can create the group + three real uids and provision
the system tree), mirroring the report's reproduction. It exercises the real
cwcli code in shared mode - ``cwcli setup shared`` builds the tree, ``cwcli
config``/``where`` write it as the second user, and ``cred_daemon`` stands up the
socket - not a hand-rolled stand-in. No managed Frappe instance and no host root
are touched. Marked ``standalone``: it never uses the shared session instance.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

# The script the container runs. ``set -e`` makes any unexpected failure abort
# with a non-zero exit the test asserts against; the explicit markers below are
# what the assertions match on.
_SCRIPT = r"""
set -e
# Install cwcli from the mounted worktree (copied out so the read-only mount and
# its .venv/build artifacts never interfere with the build).
cp -r /src /build
rm -rf /build/.venv /build/build /build/*.egg-info 2>/dev/null || true
pip install -q /build 2>&1 | tail -1

# A dedicated group, two members, and one NON-member (the trust boundary).
groupadd --system cwcli
useradd -m -s /bin/bash alice && usermod -aG cwcli alice
useradd -m -s /bin/bash bob   && usermod -aG cwcli bob
useradd -m -s /bin/bash mallory

# Provision the shared tree as root (state dir is the fixed /var/lib/cwcli).
cwcli setup shared --users alice,bob >/dev/null

# (1) The second user drives shared state with no permission failure.
su - bob -c 'cwcli config show >/dev/null 2>/tmp/bob.err && echo BOB_CONFIG_OK || { echo BOB_CONFIG_FAIL; cat /tmp/bob.err; }'
su - bob -c 'cwcli where nonexistent >/dev/null 2>&1; echo "BOB_WHERE_EXIT=$?"'
# A third group member reads the cache the second one just wrote.
su - alice -c 'cwcli where nonexistent >/dev/null 2>/tmp/alice.err && echo ALICE_CACHE_OK || { echo ALICE_CACHE_FAIL; cat /tmp/alice.err; }'

# (2) Socket gating: stand up the real cwcli credential socket in the shared
# tree, then connect as a group member vs a non-member.
mkdir -p /var/lib/cwcli/projects/p/data
chgrp -R cwcli /var/lib/cwcli/projects && chmod -R 2770 /var/lib/cwcli/projects
cat > /tmp/serve.py <<'PY'
import os, stat, threading, time
from caffeinated_whale_cli.core import credbridge
from caffeinated_whale_cli.utils import cred_daemon, shared_home
assert shared_home.shared_mode(), "expected shared mode on inside the container"
stop = threading.Event()
cred_daemon._start_unix_listener("/var/lib/cwcli/projects/p/data", "p", stop, credbridge)
sp = "/var/lib/cwcli/projects/p/data/" + credbridge.PERSISTENT_SOCK_NAME
st = os.stat(sp)
print("SOCKET_MODE=" + oct(stat.S_IMODE(st.st_mode)), flush=True)
time.sleep(25)
PY
python /tmp/serve.py &
sleep 3
CONN='import socket
s=socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
try:
    s.connect("/var/lib/cwcli/projects/p/data/.cwcli-git-cred.sock"); print("CONNECT_OK")
except PermissionError: print("CONNECT_DENIED")
except Exception as e: print("CONNECT_ERR:" + type(e).__name__)'
printf 'BOB_SOCKET='; su - bob     -c "python -c '$CONN'"
printf 'MALLORY_SOCKET='; su - mallory -c "python -c '$CONN'"
kill %1 2>/dev/null || true
echo DONE
"""

pytestmark = [pytest.mark.e2e, pytest.mark.standalone]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_two_uids_share_state_and_the_socket_is_group_gated():
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
        f.write(_SCRIPT)
        script_path = f.name

    proc = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{_repo_root()}:/src:ro",
            "-v",
            f"{script_path}:/smoke.sh:ro",
            "python:3.12-slim",
            "bash",
            "/smoke.sh",
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    out = proc.stdout + "\n" + proc.stderr
    assert proc.returncode == 0, f"container script failed:\n{out}"
    assert "DONE" in out, f"script did not run to completion:\n{out}"

    # (1) second user drives shared state; third reads what the second wrote.
    assert "BOB_CONFIG_OK" in out and "BOB_CONFIG_FAIL" not in out, out
    assert "BOB_WHERE_EXIT=0" in out, out
    assert "ALICE_CACHE_OK" in out and "ALICE_CACHE_FAIL" not in out, out

    # (2) socket is group-gated: 0660, member allowed, non-member refused.
    assert "SOCKET_MODE=0o660" in out, out
    assert "BOB_SOCKET=CONNECT_OK" in out, out
    assert "MALLORY_SOCKET=CONNECT_DENIED" in out, out
