"""Packaging E2E: the web UI daemon survives an upgrade of cwcli underneath it.

The defect this guards: after ``uv tool install --force --reinstall .`` the detached
``cwcli ui`` daemon kept serving with the OLD Python code while the browser loaded the
NEW web assets from disk, so a button the new page shipped (Start daemon) posted a
setting the old daemon did not know and failed with "Unsupported setting".

This builds a wheel, installs it as a real ``uv tool``, starts the real detached
daemon, then reinstalls a SAME-VERSION wheel whose code differs (the case a version
number cannot see) and asserts:

* the daemon notices by itself and re-executes onto the new code - same pid, same
  port, same bearer token, and the browser session cookie issued BEFORE the upgrade
  still reads (so the open page reloads instead of being signed out);
* ``/api/v1/meta`` reports a different ``build`` afterwards;
* a daemon recorded without a build (one started by a cwcli that predates the check)
  is replaced by the next ``cwcli ui``.

Marked ``e2e_pkg`` like the other packaging-realism legs. The daemon bootstraps its
fleet from Docker, so it needs a reachable daemon but no instance. Run locally with::

    uv run pytest tests/e2e/test_ui_upgrade_while_running_e2e.py -m e2e_pkg -o addopts=""
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e_pkg

REPO_ROOT = Path(__file__).resolve().parents[2]


# Same opt-out as the other packaging legs: no instance, no ~/.cwcli, no sweep.
@pytest.fixture(scope="module", autouse=True)
def _docker_gate():
    yield


@pytest.fixture(scope="module", autouse=True)
def isolated_home():
    yield


@pytest.fixture(scope="module", autouse=True)
def _teardown_backstop():
    yield


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _record_hash(data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"sha256={digest}"


def _rebuilt_wheel(wheel: Path, out_dir: Path) -> Path:
    """The same wheel, same version, with one module's bytes changed and RECORD updated."""
    out_dir.mkdir()
    target = out_dir / wheel.name
    marker = "caffeinated_whale_cli/__init__.py"
    with zipfile.ZipFile(wheel) as src, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == marker:
                data += b"\n# rebuilt: same version, different code\n"
                changed = (item.filename, _record_hash(data), len(data))
            if item.filename.endswith(".dist-info/RECORD"):
                record_name = item.filename
                continue
            dst.writestr(item, data)
        lines = []
        for line in src.read(record_name).decode().splitlines():
            if line.startswith(marker + ","):
                line = f"{changed[0]},{changed[1]},{changed[2]}"
            lines.append(line)
        dst.writestr(record_name, "\n".join(lines) + "\n")
    return target


class _Tool:
    def __init__(self, root: Path):
        self.root = root
        self.home = root / "home"
        self.home.mkdir()
        self.env = {
            **os.environ,
            "HOME": str(self.home),
            "CWCLI_HOME": str(self.home / ".cwcli"),
            "CWCLI_NO_UPDATE_CHECK": "1",
            "UV_TOOL_DIR": str(root / "tools"),
            "UV_TOOL_BIN_DIR": str(root / "bin"),
        }
        self.env.pop("CWCLI_SERVE_TOKEN", None)
        self.cwcli = root / "bin" / "cwcli"

    def install(self, find_links: Path) -> None:
        done = subprocess.run(
            [
                "uv",
                "tool",
                "install",
                "--force",
                "--reinstall",
                "--find-links",
                str(find_links),
                "caffeinated-whale-cli",
            ],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=600,
        )
        assert done.returncode == 0, f"uv tool install failed:\n{done.stdout}{done.stderr}"

    def run(self, *args: str, timeout: int = 90) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(self.cwcli), *args],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    @property
    def run_dir(self) -> Path:
        return self.home / ".cwcli" / "run"

    def state(self) -> dict:
        return json.loads((self.run_dir / "ui.json").read_text())

    def pid(self) -> int:
        return int((self.run_dir / "ui.pid").read_text().split()[0])


def _meta(port: int) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/meta", timeout=3) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _wait(predicate, *, timeout: float, desc: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for {desc}")


def _cookie_reads(port: int, cookie: str) -> bool:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v1/instances", headers={"Cookie": cookie}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


def _sign_in(port: int, token: str) -> str:
    """Mint a code with the bearer token and redeem it the way the page does."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v1/session/code",
        data=b"{}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        code = json.loads(r.read())["data"]["code"]
    redeem = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v1/session",
        data=json.dumps({"code": code}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(redeem, timeout=5) as r:
        return str(r.headers["Set-Cookie"].split(";", 1)[0])


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is required to install the tool")
@pytest.mark.skipif(os.name != "posix", reason="the daemon re-executes itself on POSIX only")
def test_the_ui_daemon_follows_an_upgrade_while_running(tmp_path):
    wheels_a = tmp_path / "wheels-a"
    built = subprocess.run(
        ["uv", "build", "--wheel", "-o", str(wheels_a)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert built.returncode == 0, f"uv build failed:\n{built.stdout}{built.stderr}"
    (wheel,) = wheels_a.glob("*.whl")
    wheels_b = _rebuilt_wheel(wheel, tmp_path / "wheels-b").parent

    tool = _Tool(tmp_path)
    tool.install(wheels_a)
    port = _free_port()
    daemon_pid = None
    try:
        started = tool.run("ui", "--no-browser", "--port", str(port))
        assert started.returncode == 0, started.stdout + started.stderr
        meta_before = _wait(lambda: _meta(port), timeout=30, desc="the daemon to answer /meta")
        assert meta_before["build"], meta_before
        state = tool.state()
        assert state["build"] == meta_before["build"]
        daemon_pid = tool.pid()
        cookie = _sign_in(port, state["token"])
        assert _cookie_reads(port, cookie)

        # --- the upgrade: same version, different code, while the daemon runs ----
        tool.install(wheels_b)

        def _upgraded():
            meta = _meta(port)
            return meta if meta and meta.get("build") != meta_before["build"] else None

        meta_after = _wait(_upgraded, timeout=60, desc="the daemon to restart onto the new code")
        assert (
            meta_after["version"] == meta_before["version"]
        ), "the point is a same-version rebuild"
        assert meta_after["updating"] is False
        assert tool.pid() == daemon_pid, "the daemon should re-exec in place, not be replaced"
        assert tool.state()["build"] == meta_after["build"]
        assert tool.state()["token"] == state["token"]
        # The page open before the upgrade keeps its session, so it reloads signed in.
        assert _wait(lambda: _cookie_reads(port, cookie), timeout=30, desc="the old cookie")

        # --- a daemon that predates the build check is replaced by `cwcli ui` ----
        stale = tool.state()
        stale.pop("build")
        (tool.run_dir / "ui.json").write_text(json.dumps(stale))
        relaunched = tool.run("ui", "--no-browser", "--port", str(port))
        assert relaunched.returncode == 0, relaunched.stdout + relaunched.stderr
        assert "was updated" in relaunched.stdout
        fresh = _wait(lambda: _meta(port), timeout=30, desc="the replacement daemon")
        assert fresh["build"] == meta_after["build"]
        assert tool.pid() != daemon_pid, "a stale daemon must be replaced, not reused"
        assert "build" in tool.state()
        daemon_pid = tool.pid()
    finally:
        with contextlib.suppress(Exception):
            tool.run("ui", "--stop", timeout=30)
        if daemon_pid:
            with contextlib.suppress(OSError):
                os.kill(daemon_pid, 9)
