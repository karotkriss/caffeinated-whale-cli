"""The S3 backup target integration against a REAL bucket and a REAL site.

The bucket is a throwaway MinIO container; the S3 target is set up the way an
operator sets it up from a terminal (``cwcli backups s3 configure``, the secret from
``CWCLI_S3_SECRET_KEY``), which turns the integration on only when the bucket
answers. Then one site goes the whole way round: captured into the store, copied
off-site encrypted, the local copy deleted, fetched back from the bucket and
verified, and restored over the live site - and the row added before the capture is
back. The shared instance ends as it started: the marker row is deleted again.
"""

from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import uuid

import pytest

from . import harness

pytestmark = pytest.mark.e2e

# The official MinIO images are no longer published to public registries; this is a
# public build of the same server.
IMAGE = "elestio/minio:latest"
ACCESS, SECRET = "cwe2eaccess", "cwe2e-s3-secret-" + uuid.uuid4().hex[:8]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def bucket():
    """A MinIO server: its endpoint URL."""
    name, port = f"cwe2e-s3-{uuid.uuid4().hex[:8]}", _free_port()
    started = subprocess.run(
        [
            "docker", "run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:9000",
            "-e", f"MINIO_ROOT_USER={ACCESS}", "-e", f"MINIO_ROOT_PASSWORD={SECRET}",
            IMAGE, "server", "/data",
        ],
        capture_output=True, text=True, timeout=600,
    )  # fmt: skip
    assert started.returncode == 0, started.stderr
    endpoint = f"http://127.0.0.1:{port}"
    try:
        harness.wait_until(
            lambda: subprocess.run(
                ["curl", "-sf", f"{endpoint}/minio/health/live"], capture_output=True
            ).returncode
            == 0,
            timeout=60,
            interval=1,
            desc="MinIO answering",
        )
        yield endpoint
    finally:
        subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True, timeout=120)


def _cwcli(*args, env=None):
    """The shipped cwcli, stdin closed; ``env`` adds variables (the S3 secret)."""
    return subprocess.run(
        [harness.CWCLI, *args],
        capture_output=True,
        text=True,
        timeout=900,
        stdin=subprocess.DEVNULL,
        env={**os.environ, **(env or {})},
    )


def _execute(inst, method: str, flag: str, value) -> str:
    """``bench execute`` (which commits) with JSON ``--kwargs`` or ``--args``."""
    script = (
        f"cd {shlex.quote(inst.bench)} && bench --site {shlex.quote(inst.site)} execute "
        f"{method} {flag} {shlex.quote(json.dumps(value))}"
    )
    code, out = harness.exec_in_frappe(inst.name, script)
    assert code == 0, f"bench execute {method} failed: {out}"
    return out


def _count(inst, marker: str) -> int:
    out = _execute(
        inst,
        "frappe.client.get_count",
        "--kwargs",
        {"doctype": "ToDo", "filters": {"description": marker}},
    )
    # bench execute prints only a truthy return, so a genuine 0 prints nothing.
    digits = [line.strip() for line in out.splitlines() if line.strip().isdigit()]
    return int(digits[-1]) if digits else 0


def _capture(inst) -> str:
    out = _cwcli("backups", "capture", inst.name, "--site", inst.site, "--no-files", "--json")
    assert out.returncode == 0, out.stdout + out.stderr
    d = json.loads(out.stdout)
    return f"{d['project']}/{d['site']}/{d['id']}"


def test_a_site_goes_off_site_and_back_through_the_s3_integration(running_instance, bucket):
    inst = running_instance
    marker = f"cwe2e-s3-{uuid.uuid4().hex[:8]}"
    configure = ["backups", "s3", "configure", "--endpoint", bucket, "--bucket", "cwe2e"]
    try:
        # Off until set up: an off-site read names where to turn it on.
        off = _cwcli("backups", "s3", "list", inst.name)
        assert off.returncode != 0 and "Integrations" in off.stdout + off.stderr

        # A wrong secret fails the bucket test, and the target stays off.
        bad = _cwcli(*configure, "--access-key", ACCESS, env={"CWCLI_S3_SECRET_KEY": "wrong"})
        assert bad.returncode == 1 and "needs_setup" in bad.stdout, bad.stdout + bad.stderr
        on = _cwcli("backups", "s3", "configure", env={"CWCLI_S3_SECRET_KEY": SECRET})
        assert on.returncode == 0 and "state: on" in on.stdout, on.stdout + on.stderr
        with open(os.path.join(os.environ["CWCLI_HOME"], "integrations.json")) as f:
            assert SECRET not in f.read()

        # The marker row is in the first set only.
        _execute(
            inst,
            "frappe.client.insert",
            "--kwargs",
            {"doc": {"doctype": "ToDo", "description": marker}},
        )
        first = _capture(inst)
        copied = _cwcli("backups", "s3", "copy", first)
        assert copied.returncode == 0, copied.stdout + copied.stderr
        _execute(inst, "frappe.db.delete", "--args", ["ToDo", {"description": marker}])
        assert _count(inst, marker) == 0
        assert _capture(inst) != first

        # The first set now lives only in the bucket, encrypted.
        deleted = _cwcli("backups", "delete", first, "--yes")
        assert deleted.returncode == 0, deleted.stdout + deleted.stderr
        listed = _cwcli("backups", "s3", "list", inst.name, "--json")
        assert listed.returncode == 0, listed.stdout + listed.stderr
        row = next(r for r in json.loads(listed.stdout) if r["key"] == first)
        assert row["local"] is False and row["offsite"]["encrypted"] is True

        # Fetched back it verifies; restored over the site, the row is back.
        fetched = _cwcli("backups", "s3", "fetch", inst.name, first)
        assert fetched.returncode == 0, fetched.stdout + fetched.stderr
        assert f"{first}  verified" in fetched.stdout
        restored = _cwcli(
            "backups", "restore", first, inst.name, "--site", inst.site, "--yes", "--no-migrate"
        )
        assert restored.returncode == 0, restored.stdout + restored.stderr
        assert _count(inst, marker) == 1
    finally:
        # Leave the shared site as it was, and the target off.
        harness.exec_in_frappe(
            inst.name,
            f"cd {shlex.quote(inst.bench)} && bench --site {shlex.quote(inst.site)} execute "
            f"frappe.db.delete --args {shlex.quote(json.dumps(['ToDo', {'description': marker}]))}",
        )
        _cwcli("backups", "s3", "configure", "--disable")
