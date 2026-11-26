"""The web UI's receive job installs sendme on first use, for real.

Backups > Receive in the web UI runs a detached ``receive-backup`` job. It used to
refuse with "sendme is not installed" on a machine where only the command line had
ever installed it. This drives that exact job, through ``jobs.create_job`` and the
detached worker the daemon spawns, under a fresh HOME with no sendme anywhere, and
proves the job downloads the real release into ``$HOME/.local/bin`` and records an
"Installing sendme" step. The ticket is a well-formed fake, so the download itself
fails afterwards; what matters is that it fails at sendme, not for want of it.

Needs the network (GitHub releases), which CI has; no instance, no Docker work.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from caffeinated_whale_cli.utils import sendme_utils

pytestmark = [pytest.mark.e2e, pytest.mark.standalone]

_DRIVER = r"""
import json, sys, time
from caffeinated_whale_cli import jobs

job = jobs.create_job("receive-backup", {}, {"ticket": sys.argv[1]})
deadline = time.monotonic() + 600
installed_at = None
interrupted = False
while True:
    record = jobs.read_job(job["id"], include_events=True)
    if record["state"] in jobs.TERMINAL_STATES:
        break
    now = time.monotonic()
    if installed_at is None and any(
        e.get("phase") == "install" and e.get("status") == "ok" for e in record["events"]
    ):
        installed_at = now
    if interrupted and now > deadline:
        break
    if not interrupted and (now > deadline or (installed_at and now - installed_at > 120)):
        # sendme is installed by now; a fake ticket sendme waits on is not this test's concern
        jobs.interrupt_job(job["id"])
        interrupted, deadline = True, now + 60
    time.sleep(0.5)
print(json.dumps(record))
"""


def _path_without_sendme() -> str:
    keep = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry and not os.path.exists(os.path.join(entry, "sendme")):
            keep.append(entry)
    return os.pathsep.join(keep)


def test_the_receive_job_installs_sendme_where_none_exists(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "CWCLI_HOME": str(home / ".cwcli"),
        "CWCLI_SHARED_MARKER": str(tmp_path / "no-shared-marker.toml"),
        "PATH": _path_without_sendme(),
    }
    binary = home / ".local" / "bin" / "sendme"
    assert not binary.exists()

    proc = subprocess.run(
        [sys.executable, "-c", _DRIVER, "blob" + "a" * 60],
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert proc.returncode == 0, proc.stderr
    record = json.loads(proc.stdout.strip().splitlines()[-1])
    steps = [
        (e.get("status"), e.get("message")) for e in record["events"] if e.get("phase") == "install"
    ]

    assert any(
        status == "start" and message.startswith("Installing sendme") for status, message in steps
    ), record
    assert ("ok", f"Installed sendme to {binary}") in steps, record
    assert binary.exists() and os.access(binary, os.X_OK)
    assert sendme_utils.is_pinned_sendme(binary), "the installed sendme is not the pinned release"
    assert (record.get("error") or {}).get("code") != "transfer.sendme_missing", record
    version = subprocess.run([str(binary), "--version"], capture_output=True, text=True, env=env)
    assert version.returncode == 0 and "sendme" in version.stdout.lower(), version
