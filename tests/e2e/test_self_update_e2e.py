"""Packaging E2E: ``cwcli self-update`` upgrades the install it runs from and brings
every running cwcli service back on the new version - or changes nothing.

Built from real wheels of this tree under two higher-than-PyPI version numbers, so
nothing is published and the upgrade resolves offline from a local wheelhouse:

1. ``uv tool install`` the OLD wheel into a tool folder outside uv's default (the
   way a shared install lives in ``/opt``), start the web UI daemon, auto-inspect
   and the credential bridge, then run ``cwcli self-update`` WITHOUT
   ``UV_TOOL_DIR`` in its environment. Before the fix, uv looked in the caller's
   default tool folder and the running services kept the old code.
2. Assert the install, the web UI's ``/api/v1/meta`` and every daemon's pid moved
   to the new version.
3. Offer a newer wheel but make the bin folder unwritable, so uv replaces the
   packages and then fails linking the executables. Assert exit 1, the reason in
   the error, the install back on the previous version, and no service restarted.

A tiny empty Docker API on a unix socket stands in for Docker, so the web UI
daemon starts without touching any real daemon. Runs as a non-root user (the
failure step relies on a read-only folder). Run locally with::

    uv run pytest tests/e2e/test_self_update_e2e.py -m e2e_pkg -o addopts=""
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import socketserver
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.e2e_pkg,
    pytest.mark.skipif(shutil.which("uv") is None, reason="uv builds and installs the wheels"),
    pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX daemons"),
]

REPO_ROOT = Path(__file__).resolve().parents[2]
OLD, NEW, NEWER = "9999.0.0", "9999.0.1", "9999.0.2"


# Packaging realism, not Docker realism: shadow the e2e conftest's session gates
# (the Docker gate, isolated HOME, cwe2e- sweep), as test_version_build_id_e2e does.
@pytest.fixture(scope="module", autouse=True)
def _docker_gate():
    yield


@pytest.fixture(scope="module", autouse=True)
def isolated_home():
    yield


@pytest.fixture(scope="module", autouse=True)
def _teardown_backstop():
    yield


class _EmptyDocker(BaseHTTPRequestHandler):
    """Answers the Docker Engine API with no containers, volumes or networks."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path.lstrip("/").startswith("v1."):
            path = "/" + path.lstrip("/").split("/", 1)[1]
        bodies: dict[str, object] = {
            "/_ping": "OK",
            "/version": {"Version": "27.0.0", "ApiVersion": "1.47", "MinAPIVersion": "1.24"},
            "/info": {"ID": "e2e", "Containers": 0, "NCPU": 1, "MemTotal": 1},
            "/volumes": {"Volumes": [], "Warnings": []},
            "/containers/json": [],
            "/networks": [],
            "/images/json": [],
        }
        body = bodies.get(path, {"message": "not found"})
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(200 if path in bodies else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Api-Version", "1.47")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_HEAD(self):  # noqa: N802 - the handler method name http.server calls
        self.do_GET()

    def do_POST(self):  # noqa: N802
        self.do_GET()


class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def _wheel(version: str, out: Path, work: Path) -> Path:
    """Build this tree as caffeinated-whale-cli==``version``."""
    src = work / f"src-{version}"
    for name in ("pyproject.toml", "README.md", "LICENSE", "MANIFEST.in"):
        (src / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / name, src / name)
    shutil.copytree(
        REPO_ROOT / "src", src / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    for rel, prefix in (
        ("pyproject.toml", "version = "),
        ("src/caffeinated_whale_cli/__init__.py", "__version__ = "),
    ):
        path = src / rel
        lines = path.read_text().splitlines(keepends=True)
        lines = [f'{prefix}"{version}"\n' if line.startswith(prefix) else line for line in lines]
        path.write_text("".join(lines))
    done = subprocess.run(
        ["uv", "build", "--wheel", "-o", str(out)],
        cwd=src,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    (wheel,) = out.glob(f"*-{version}-*.whl")
    return wheel


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture()
def sandbox(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("the failure step needs a folder root cannot be refused")
    if not (REPO_ROOT / "src/caffeinated_whale_cli/webui/dist/index.html").exists():
        pytest.skip("build the web UI first (cd webui && npm run build)")
    built = tmp_path / "built"
    wheels = {v: _wheel(v, built, tmp_path) for v in (OLD, NEW, NEWER)}
    house = tmp_path / "wheelhouse"
    house.mkdir()
    shutil.copy2(wheels[OLD], house)

    sock = tmp_path / "docker.sock"
    docker = _UnixServer(str(sock), _EmptyDocker)
    threading.Thread(target=docker.serve_forever, daemon=True).start()

    home = tmp_path / "home"
    home.mkdir()
    tools, bins = tmp_path / "opt" / "tools", tmp_path / "opt" / "bin"
    env = {
        "HOME": str(home),
        "PATH": f"{bins}:{os.environ['PATH']}",
        "DOCKER_HOST": f"unix://{sock}",
        "CWCLI_NO_UPDATE_CHECK": "1",
        # The real uv cache (not one under the throwaway HOME), so deps download once.
        "UV_CACHE_DIR": os.environ.get("UV_CACHE_DIR")
        or subprocess.run(["uv", "cache", "dir"], capture_output=True, text=True).stdout.strip(),
    }
    installed = subprocess.run(
        ["uv", "tool", "install", "caffeinated-whale-cli", "--find-links", str(house)],
        env={**env, "UV_TOOL_DIR": str(tools), "UV_TOOL_BIN_DIR": str(bins)},
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    cwcli = str(bins / "cwcli")

    def run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
        done = subprocess.run([cwcli, *args], env=env, capture_output=True, text=True, timeout=600)
        if check:
            assert done.returncode == 0, f"cwcli {args}:\n{done.stdout}{done.stderr}"
        return done

    def latest(version: str) -> None:
        cache = home / ".cwcli" / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        now = time.time()
        (cache / "version_check.json").write_text(
            json.dumps({"latest": version, "checked_at": now, "attempted_at": now})
        )

    port = _free_port()
    try:
        yield {
            "run": run,
            "latest": latest,
            "house": house,
            "wheels": wheels,
            "bins": bins,
            "run_dir": home / ".cwcli" / "run",
            "port": port,
        }
    finally:
        for args in (("ui", "--stop"), ("config", "auto-inspect", "disable")):
            run(*args, check=False)
        run("config", "cred-bridge", "disable", check=False)
        bins.chmod(0o755)
        docker.shutdown()


def _pid(run_dir: Path, name: str) -> int:
    return int((run_dir / f"{name}.pid").read_text().split()[0])


def _meta_version(port: int) -> str:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/meta", timeout=5) as r:
        return str(json.loads(r.read())["version"])


def test_self_update_restarts_every_service_or_changes_nothing(sandbox):
    run, run_dir, port = sandbox["run"], sandbox["run_dir"], sandbox["port"]
    assert OLD in run("--version").stdout
    run("ui", "--no-browser", "--port", str(port))
    run("config", "auto-inspect", "enable")
    run("config", "cred-bridge", "enable")
    before = {n: _pid(run_dir, n) for n in ("ui", "auto-inspect", "credbridge")}
    assert _meta_version(port) == OLD

    # --- a successful upgrade: every running service comes back on NEW --------
    shutil.copy2(sandbox["wheels"][NEW], sandbox["house"])
    sandbox["latest"](NEW)
    done = run("self-update")
    assert f"Upgraded cwcli to {NEW}" in done.stdout
    assert "Restarting 3 running cwcli service(s)" in done.stdout
    assert done.stdout.count("✓") == 3, done.stdout
    assert NEW in run("--version").stdout
    assert _meta_version(port) == NEW
    after = {n: _pid(run_dir, n) for n in ("ui", "auto-inspect", "credbridge")}
    assert all(after[n] != before[n] for n in before), (before, after)

    # --- a failed upgrade: uv replaces the packages, then cannot link ---------
    shutil.copy2(sandbox["wheels"][NEWER], sandbox["house"])
    sandbox["latest"](NEWER)
    sandbox["bins"].chmod(0o555)
    failed = run("self-update", check=False)
    sandbox["bins"].chmod(0o755)
    assert failed.returncode == 1, failed.stdout + failed.stderr
    error = " ".join((failed.stdout + failed.stderr).split())
    assert "Permission denied" in error
    assert f"cwcli {NEW} is still installed, and nothing was restarted" in error
    assert NEW in run("--version").stdout  # rolled back, not left half-upgraded
    assert _meta_version(port) == NEW
    assert {n: _pid(run_dir, n) for n in after} == after
