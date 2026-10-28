"""A community backup target carrying a REAL site's backup off-site and back.

A throwaway community integration whose ``backup_target`` hook stores copies in a
local directory is added from a git repository and built with the real pip, then an
Admin turns it on through the web UI API (its connection test runs first). From
there it is the same path the S3 bucket takes: one site's set is captured, copied
off-site encrypted, deleted locally, fetched back and verified, and restored over the
live site - and the row added before the capture is back. Turned off, nothing goes
off-site; removed, nothing of it is left. The shared instance ends as it started.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import uuid
from pathlib import Path

import pytest

from . import harness

pytestmark = pytest.mark.e2e

TOKEN = "cwe2e-community-target-token"

PYPROJECT = """\
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "cwcli-integration-localdir"
version = "0.1.0"
requires-python = ">=3.10"

[project.entry-points."cwcli.integrations"]
localdir = "cwcli_localdir:INTEGRATION"

[tool.cwcli.integration]
name = "Local directory backup target"
publisher = "cwcli E2E"
description = "Keeps off-site copies in a directory on this machine."
provides = ["backup_target"]
"""

MODULE = """\
import os
import shutil
from pathlib import Path

from caffeinated_whale_cli.core.integrations import COMMUNITY, Check, Field, Integration


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.location = str(self.root)

    def key(self, *parts):
        return "/".join(p.strip("/") for p in parts if p and p.strip("/"))

    def ensure(self):
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key):
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def put_file(self, key, path):
        shutil.copyfile(path, self._path(key))

    def put_bytes(self, key, data):
        self._path(key).write_bytes(data)

    def get_to_file(self, key, path):
        shutil.copyfile(self.root / key, path)
        return Path(path).stat().st_size

    def get_bytes(self, key):
        return (self.root / key).read_bytes()

    def delete(self, key):
        (self.root / key).unlink(missing_ok=True)

    def list(self, prefix=""):
        files = sorted(p for p in self.root.rglob("*") if p.is_file())
        keys = [p.relative_to(self.root).as_posix() for p in files]
        return [{"key": k} for k in keys if k.startswith(prefix)]


def _test(settings, secrets):
    path = Path(settings["path"])
    if not path.is_absolute():
        return Check(False, "The directory must be an absolute path.")
    path.mkdir(parents=True, exist_ok=True)
    if not os.access(path, os.W_OK):
        return Check(False, f"{path} is not writable.")
    return Check(True, f"{path} is writable.")


INTEGRATION = Integration(
    id="localdir",
    name="Local directory backup target",
    publisher=COMMUNITY,
    description="Keeps off-site copies in a directory on this machine.",
    provides=("backup_target",),
    fields=(Field("path", "Directory", required=True),),
    normalize=lambda s: {"path": str(s.get("path") or "").strip()},
    test=_test,
    hooks={"backup_target": lambda settings, secrets: Store(settings["path"])},
)
"""


def _git(cwd: Path, *args: str) -> None:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
    )


def _repo(root: Path) -> str:
    (root / "src" / "cwcli_localdir").mkdir(parents=True)
    (root / "pyproject.toml").write_text(PYPROJECT)
    (root / "src" / "cwcli_localdir" / "__init__.py").write_text(MODULE)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "localdir backup target")
    return root.as_uri()


@pytest.fixture(scope="module", autouse=True)
def _file_repositories():
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("CWCLI_TEST_ONLY_ALLOW_FILE_REPOSITORY_URLS", "1")
        yield


def _cwcli(*args):
    return subprocess.run(
        [harness.CWCLI, *args],
        capture_output=True,
        text=True,
        timeout=900,
        stdin=subprocess.DEVNULL,
    )


def _execute(inst, method: str, flag: str, value) -> str:
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
    digits = [line.strip() for line in out.splitlines() if line.strip().isdigit()]
    return int(digits[-1]) if digits else 0


def _capture(inst) -> str:
    out = _cwcli("backups", "capture", inst.name, "--site", inst.site, "--no-files", "--json")
    assert out.returncode == 0, out.stdout + out.stderr
    d = json.loads(out.stdout)
    return f"{d['project']}/{d['site']}/{d['id']}"


def _integration(base: str, payload: dict) -> tuple[int, dict]:
    return harness.api_call(base, "/api/v1/integrations", payload, token=TOKEN)


def test_a_site_goes_off_site_and_back_through_a_community_backup_target(
    running_instance, tmp_path
):
    inst = running_instance
    marker = f"cwe2e-community-{uuid.uuid4().hex[:8]}"
    offsite = tmp_path / "offsite"
    added = _cwcli("integrations", "add", _repo(tmp_path / "repo"), "--yes")
    assert added.returncode == 0, added.stdout + added.stderr
    try:
        with harness.ui_daemon(TOKEN) as base:
            # Installed, it is off: off-site copies still have nowhere to go.
            off = _cwcli("backups", "s3", "list", inst.name)
            assert off.returncode != 0 and "Integrations" in off.stdout + off.stderr
            # Its own connection test gates turning it on.
            bad = {"op": "configure", "id": "localdir", "settings": {"path": "rel"}}
            status, body = _integration(base, {**bad, "enable": True})
            assert status == 200 and body["data"]["check"]["ok"] is False, body
            assert body["data"]["integration"]["state"] != "on"
            good = {"op": "configure", "id": "localdir", "settings": {"path": str(offsite)}}
            status, body = _integration(base, {**good, "enable": True})
            assert status == 200 and body["data"]["integration"]["state"] == "on", body

        _execute(
            inst,
            "frappe.client.insert",
            "--kwargs",
            {"doc": {"doctype": "ToDo", "description": marker}},
        )
        first = _capture(inst)
        copied = _cwcli("backups", "s3", "copy", first)
        assert copied.returncode == 0, copied.stdout + copied.stderr
        assert str(offsite) in copied.stdout + copied.stderr
        stored = [p for p in offsite.rglob("*") if p.is_file()]
        assert any(p.name == "set.json" for p in stored)
        assert all(p.name == "set.json" or p.name.endswith(".cwenc") for p in stored)
        _execute(inst, "frappe.db.delete", "--args", ["ToDo", {"description": marker}])
        assert _count(inst, marker) == 0
        assert _capture(inst) != first  # the last good backup is never deleted

        deleted = _cwcli("backups", "delete", first, "--yes")
        assert deleted.returncode == 0, deleted.stdout + deleted.stderr
        listed = _cwcli("backups", "s3", "list", inst.name, "--json")
        assert listed.returncode == 0, listed.stdout + listed.stderr
        row = next(r for r in json.loads(listed.stdout) if r["key"] == first)
        assert row["local"] is False and row["offsite"]["target"] == "localdir"

        fetched = _cwcli("backups", "s3", "fetch", inst.name, first)
        assert fetched.returncode == 0, fetched.stdout + fetched.stderr
        assert f"{first}  verified" in fetched.stdout
        restored = _cwcli(
            "backups", "restore", first, inst.name, "--site", inst.site, "--yes", "--no-migrate"
        )
        assert restored.returncode == 0, restored.stdout + restored.stderr
        assert _count(inst, marker) == 1

        with harness.ui_daemon(TOKEN) as base:
            assert _integration(base, {"op": "disable", "id": "localdir"})[0] == 200
        after = _cwcli("backups", "s3", "list", inst.name)
        assert after.returncode != 0  # off: nothing goes off-site any more
    finally:
        harness.exec_in_frappe(
            inst.name,
            f"cd {shlex.quote(inst.bench)} && bench --site {shlex.quote(inst.site)} execute "
            f"frappe.db.delete --args {shlex.quote(json.dumps(['ToDo', {'description': marker}]))}",
        )
        removed = _cwcli("integrations", "remove", "localdir", "--yes")
    assert removed.returncode == 0, removed.stdout + removed.stderr
    assert "localdir" not in _cwcli("integrations", "list").stdout
