"""``cwcli ui`` against a REAL instance: the three tiers, the page, and the quiet in between.

The daemon is the shipped binary (``cwcli ui --foreground``) in a subprocess and the
client is an ordinary HTTP/SSE reader, so what is measured here is the whole path a
browser takes.

What each tier is proven to do, and why a unit test could not:

* INSTANT - a container stop/start reaches a connected client, and the delta says
  the CONTAINER came up, not that the bench is healthy. Only real Docker emits
  those events.
* FAST - restarting ONE supervisord program produces no Docker event whatsoever,
  so it is invisible to the INSTANT tier by construction. Real supervisord is the
  only thing that can demonstrate that.
* LAZY - the on-demand detail read carries ``core.inspect``'s own freshness
  labels against a real cache.
* Delta suppression - a steady REAL instance publishes nothing, with every
  volatile counter (uptime, cpu, rss, probe duration) genuinely moving underneath.
  A fake's counters do not move, so a fake cannot fail this test.

The reconnect re-bootstrap is unit-tested (``tests/test_core_fleet.py::TestEventLoop``):
inducing a Docker event-stream drop means breaking the daemon's own socket, which
no supported API exposes, and faking it here would test the fake.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from . import harness

pytestmark = [pytest.mark.e2e]

_HTTP_TIMEOUT = 10
# Authentication is always on; a configured token is how a non-browser client gets in.
_TOKEN = "cwe2e-serve-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _Client:
    """An SSE reader on a background thread, recording every frame it receives."""

    def __init__(self, url: str):
        self.frames: list[tuple[str, dict]] = []
        self.ids: list[int] = []
        self.keepalives = 0
        self._resp = urllib.request.urlopen(  # noqa: S310
            urllib.request.Request(url, headers=_AUTH), timeout=_HTTP_TIMEOUT
        )
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        event = None
        try:
            for raw in self._resp:
                line = raw.decode().rstrip("\n")
                if line.startswith(": "):
                    self.keepalives += 1
                elif line.startswith("id: "):
                    self.ids.append(int(line[len("id: ") :]))
                elif line.startswith("event: "):
                    event = line[len("event: ") :]
                elif line.startswith("data: "):
                    self.frames.append((event, json.loads(line[len("data: ") :])))
        except (OSError, ValueError):
            pass

    def deltas(self, tier: str | None = None) -> list[dict]:
        out = [d for e, d in self.frames if e == "delta"]
        return [d for d in out if tier is None or d["tier"] == tier]

    def snapshot(self) -> dict:
        return next(d for e, d in self.frames if e == "snapshot")

    def close(self) -> None:
        self._resp.close()


@pytest.fixture
def daemon(running_instance):
    """The shipped ``cwcli ui --foreground`` in a subprocess, bound to a free local port."""
    port = _free_port()
    proc = subprocess.Popen(
        [
            harness.CWCLI,
            "ui",
            "--foreground",
            "--no-browser",
            "--port",
            str(port),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={**os.environ, "CWCLI_SERVE_TOKEN": _TOKEN},
    )
    base = f"http://127.0.0.1:{port}"

    def _up():
        req = urllib.request.Request(base + "/api/v1/instances", headers=_AUTH)
        with urllib.request.urlopen(req, timeout=2) as r:  # noqa: S310
            return json.loads(r.read())

    try:
        harness.wait_until(_up, timeout=60, interval=0.5, desc="cwcli ui listening")
        yield type("D", (), {"base": base, "proc": proc, "project": running_instance.name})
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            proc.kill()


def _get(url: str):
    req = urllib.request.Request(url, headers=_AUTH)
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:  # noqa: S310
        return json.loads(resp.read())


def _post(url: str, payload: dict):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **_AUTH},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:  # noqa: S310
        return json.loads(resp.read())


def _instance(base: str, project: str) -> dict:
    body = _get(base + "/api/v1/instances")
    return next(i for i in body["data"] if i["project"] == project)


def _wait_for(predicate, *, timeout=90, desc="condition"):
    return harness.wait_until(predicate, timeout=timeout, interval=0.5, desc=desc)


def _job(base: str, job_id: str) -> dict:
    return _get(base + f"/api/v1/jobs/{job_id}")["data"]


def _wait_for_job(base: str, job_id: str, *, timeout=300) -> dict:
    terminal = {"succeeded", "failed", "interrupted", "unknown", "needs_choice"}
    return _wait_for(
        lambda: (record if (record := _job(base, job_id))["state"] in terminal else None),
        timeout=timeout,
        desc=f"job {job_id} to finish",
    )


class TestThePage:
    def test_the_built_ui_is_served_under_the_strict_csp(self, daemon):
        """The wheel/tool install carries ``webui/dist``; ``/`` and deep links serve it."""
        for path in ("/", "/instances/" + daemon.project):
            with urllib.request.urlopen(
                daemon.base + path, timeout=_HTTP_TIMEOUT
            ) as resp:  # noqa: S310
                body = resp.read().decode()
            assert resp.status == 200
            assert '<div id="app"' in body, "the built SPA, not the 'not built' notice"
            csp = resp.headers["Content-Security-Policy"]
            assert "script-src 'self'" in csp and "unsafe-inline" not in csp
            assert "connect-src 'self'" in csp and "frame-ancestors 'none'" in csp
        # The bundle's own assets are reachable and typed.
        script = re.search(r'src="(/assets/[^"]+\.js)"', body)
        assert script, body[:500]
        with urllib.request.urlopen(
            daemon.base + script.group(1), timeout=_HTTP_TIMEOUT
        ) as resp:  # noqa: S310
            assert resp.headers["Content-Type"] == "text/javascript"
            assert "immutable" in resp.headers["Cache-Control"]

    def test_a_foreign_host_is_refused_before_anything_else(self, daemon):
        port = daemon.base.rsplit(":", 1)[1]
        conn = socket.create_connection(("127.0.0.1", int(port)), timeout=_HTTP_TIMEOUT)
        conn.sendall(
            f"GET /api/v1/meta HTTP/1.1\r\nHost: rebind.evil.example:{port}\r\n"
            f"Authorization: Bearer {_TOKEN}\r\nConnection: close\r\n\r\n".encode()
        )
        # Connection: close - read to EOF, the body can arrive after the headers.
        chunks = []
        while chunk := conn.recv(4096):
            chunks.append(chunk)
        conn.close()
        response = b"".join(chunks).decode()
        assert response.startswith("HTTP/1.1 403")
        assert "host.forbidden" in response

    def test_the_login_code_signs_a_browser_in_exactly_once(self, daemon):
        # `cwcli ui` mints through the bearer endpoint, the page redeems it without one.
        minted = _post(daemon.base + "/api/v1/session/code", {})
        code = minted["data"]["code"]
        bare = urllib.request.build_opener()
        req = urllib.request.Request(
            daemon.base + "/api/v1/session",
            data=json.dumps({"code": code}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with bare.open(req, timeout=_HTTP_TIMEOUT) as resp:
            cookie = resp.headers["Set-Cookie"].split(";", 1)[0]
        with pytest.raises(urllib.error.HTTPError) as replay:
            bare.open(req, timeout=_HTTP_TIMEOUT)
        assert replay.value.code == 401
        # The cookie reads the fleet without the token.
        signed = urllib.request.Request(
            daemon.base + "/api/v1/instances", headers={"Cookie": cookie}
        )
        with bare.open(signed, timeout=_HTTP_TIMEOUT) as resp:
            assert resp.status == 200


class TestSnapshot:
    def test_the_real_fleet_is_served_with_live_health(self, daemon):
        state = _wait_for(
            lambda: (
                _instance(daemon.base, daemon.project)
                if _instance(daemon.base, daemon.project)["overall"] != "unknown"
                else None
            ),
            desc="the FAST tier's first probe",
        )

        assert state["container_running"] is True
        assert state["overall"] == "running"
        bench = state["benches"][0]
        assert bench["supervisor_up"] is True
        assert {p["label"] for p in bench["processes"]} >= {"web", "schedule", "worker"}
        assert all(p["up"] for p in bench["processes"])
        assert bench["web_port_verified"] is True and bench["web_port"] is not None

    def test_the_web_check_is_off_until_a_client_opens_the_instance(self, daemon):
        _wait_for(
            lambda: _instance(daemon.base, daemon.project)["overall"] != "unknown",
            desc="the FAST tier's first probe",
        )
        # A lifecycle event opens a window, so wait it out before asserting the
        # steady-state default.
        _wait_for(
            lambda: _instance(daemon.base, daemon.project)["web_probed"] is False,
            desc="the post-event web-probe window to close",
        )
        assert _instance(daemon.base, daemon.project)["benches"][0]["web_http_code"] is None

        client = _Client(daemon.base + f"/api/v1/stream?focus={daemon.project}")
        try:
            state = _wait_for(
                lambda: (
                    _instance(daemon.base, daemon.project)
                    if _instance(daemon.base, daemon.project)["web_probed"]
                    else None
                ),
                desc="focus to turn the web probe on",
            )
            bench = state["benches"][0]
            assert bench["web_http_code"] is not None
            # The probe names a site, so the code it got is attributable.
            assert bench["web_site"] is not None
        finally:
            client.close()


class TestDeltaSuppression:
    def test_a_steady_real_instance_publishes_nothing(self, daemon):
        _wait_for(
            lambda: _instance(daemon.base, daemon.project)["overall"] != "unknown",
            desc="the FAST tier's first probe",
        )
        client = _Client(daemon.base + "/api/v1/stream")
        try:
            _wait_for(lambda: client.frames, desc="the opening snapshot")
            before = _instance(daemon.base, daemon.project)

            # ~6 probe cycles at the 2.5s default. Every process's uptime advances,
            # cpu and rss move, and each probe takes a different number of
            # milliseconds - so this is a real test of the stable-field digest,
            # not of a frozen fixture.
            time.sleep(15)

            after = _instance(daemon.base, daemon.project)
            assert after["probed_at"] > before["probed_at"], "the probe loop must be running"
            # Scoped to this instance: the daemon watches every instance on the
            # Docker host, and on a developer machine another one may genuinely
            # change state during the window.
            ours = [d for d in client.deltas() if d["project"] == daemon.project]
            assert ours == [], "a steady instance must stay silent"
            assert client.ids and client.ids == sorted(client.ids), "every frame is numbered"
        finally:
            client.close()


class TestFastTier:
    def test_restarting_one_program_is_seen_only_by_the_polled_tier(self, daemon):
        _wait_for(
            lambda: _instance(daemon.base, daemon.project)["overall"] != "unknown",
            desc="the FAST tier's first probe",
        )
        before = _instance(daemon.base, daemon.project)
        old_pid = next(p for p in before["benches"][0]["processes"] if p["label"] == "web")["pid"]

        client = _Client(daemon.base + "/api/v1/stream")
        try:
            _wait_for(lambda: client.frames, desc="the opening snapshot")

            # Restores itself: the process restart leaves the program RUNNING, so
            # the shared instance ends this test exactly as it started it.
            result = _post(
                daemon.base + "/api/v1/actions",
                {"action": "restart_process", "project": daemon.project, "process": "web"},
            )
            assert "data" in result

            _wait_for(lambda: client.deltas("fast"), desc="a FAST delta for the restart")

            assert client.deltas("instant") == [], "Docker emits no event for this at all"
            new_pid = next(
                p
                for p in client.deltas("fast")[-1]["instance"]["benches"][0]["processes"]
                if p["label"] == "web"
            )["pid"]
            assert new_pid != old_pid, "the restart must be visible as a new pid"
        finally:
            client.close()


class TestInstantTier:
    def test_a_container_stop_and_start_reaches_a_connected_client(self, daemon):
        _wait_for(
            lambda: _instance(daemon.base, daemon.project)["overall"] != "unknown",
            desc="the FAST tier's first probe",
        )
        client = _Client(daemon.base + "/api/v1/stream")
        try:
            _wait_for(lambda: client.frames, desc="the opening snapshot")

            assert harness.run_cwcli("stop", daemon.project, timeout=300).returncode == 0
            _wait_for(
                lambda: [
                    d
                    for d in client.deltas("instant")
                    if d["instance"] and d["instance"]["overall"] == "offline"
                ],
                desc="an INSTANT delta for the stop",
            )
            offline = [d for d in client.deltas("instant") if d["instance"]][-1]
            assert offline["instance"]["container_running"] is False
            assert offline["instance"]["benches"] == []
            assert offline["cause"]["action"] in ("stop", "die", "kill")

            assert harness.run_cwcli("start", daemon.project, "--yes", timeout=600).returncode == 0
            up = _wait_for(
                lambda: next(
                    (
                        d
                        for d in client.deltas("instant")
                        if d["instance"] and d["instance"]["container_running"]
                    ),
                    None,
                ),
                desc="an INSTANT delta for the start",
            )
            # The whole point of the tier split: container up is NOT bench healthy.
            assert up["instance"]["overall"] == "unknown"
            assert up["instance"]["benches"] == []

            # ...and the FAST tier is what corrects it to a real health token.
            _wait_for(
                lambda: [
                    d
                    for d in client.deltas("fast")
                    if d["instance"]["overall"] in ("running", "degraded", "online")
                ],
                desc="the FAST tier to resolve the new health",
                timeout=180,
            )
        finally:
            client.close()
            # Shared-group convention: leave the instance as this test found it.
            if harness.frappe_container_id(daemon.project) is None:
                harness.run_cwcli("start", daemon.project, "--yes", timeout=600)
            harness.wait_for_site_ready(daemon.project, harness.DEFAULT_SITE)


class TestLazyTier:
    def test_the_detail_endpoint_carries_inspects_own_freshness_labels(self, daemon):
        body = _get(daemon.base + f"/api/v1/instances/{daemon.project}")["data"]

        assert body["project"] == daemon.project
        assert body["served_from"] in ("cache", "partial", "full")
        bench = body["benches"][0]
        assert bench["path"] == harness.DEFAULT_BENCH_PATH
        site = next(s for s in bench["sites"] if s["name"] == harness.DEFAULT_SITE)
        assert any(a.startswith("frappe") for a in site["installed_apps"])
        # The verified-or-remembered token: True only on a full re-inspect.
        assert site["installed_apps_verified"] == (body["served_from"] == "full")
        assert body["cached_at"], "a cached answer says how old it is"

    def test_an_unknown_project_is_a_404_not_an_empty_success(self, daemon):
        with pytest.raises(urllib.error.HTTPError) as e:
            _get(daemon.base + "/api/v1/instances/cwe2e-no-such-project")
        assert e.value.code == 404
        assert json.loads(e.value.read())["errors"][0]["type"] == "not_found"


class TestDetachedJobs:
    """Detached work against the real instance, read back through the shipped API."""

    def test_backup_runs_detached_and_keeps_its_typed_event_log(self, daemon):
        created = _post(
            daemon.base + "/api/v1/jobs",
            {
                "verb": "backup",
                "target": {"project": daemon.project, "site": harness.DEFAULT_SITE},
                "args": {"with_files": False},
            },
        )["data"]

        assert created["pid"] != daemon.proc.pid, "the daemon must not own the long work"
        finished = _wait_for_job(daemon.base, created["id"])
        assert finished["state"] == "succeeded", finished
        assert finished["result"]["site"] == harness.DEFAULT_SITE
        assert finished["result"]["artifact_path"].endswith(".sql.gz")
        event_types = [event["type"] for event in finished["events"]]
        assert "backup_step_start" in event_types
        assert "backup_step_end" in event_types

        # A fresh read after the worker exited is the same durable result. This is
        # the browser reattach path after its tab or SSE stream has gone away.
        attached = _job(daemon.base, created["id"])
        assert attached["state"] == "succeeded"
        assert attached["event_count"] == finished["event_count"]

    def test_stop_and_start_bench_are_jobs_and_leave_the_instance_serving(self, daemon):
        try:
            stopped = _post(
                daemon.base + "/api/v1/jobs",
                {
                    "verb": "stop-bench",
                    "target": {"project": daemon.project, "bench": "0"},
                    "args": {},
                },
            )["data"]
            stop_result = _wait_for_job(daemon.base, stopped["id"])
            assert stop_result["state"] == "succeeded", stop_result
            assert "stop_step_start" in [event["type"] for event in stop_result["events"]]

            started = _post(
                daemon.base + "/api/v1/jobs",
                {
                    "verb": "start",
                    "target": {"project": daemon.project, "bench": "0"},
                    "args": {},
                },
            )["data"]
            start_result = _wait_for_job(daemon.base, started["id"], timeout=600)
            assert start_result["state"] == "succeeded", start_result
            assert "start_step_start" in [event["type"] for event in start_result["events"]]
            harness.wait_for_site_ready(daemon.project, harness.DEFAULT_SITE)
        finally:
            if harness.frappe_container_id(daemon.project) is None:
                harness.run_cwcli("start", daemon.project, "--yes", timeout=600)
            else:
                harness.run_cwcli("start", daemon.project, "--bench", "0", timeout=600)
            harness.wait_for_site_ready(daemon.project, harness.DEFAULT_SITE)


class TestSafeActions:
    """The safe action set against the real instance: each verb round-trips
    through the daemon into real core against a real container, the scale confirm
    provably arrives FROM core (before any mutation), and every test leaves the
    shared instance exactly as it found it."""

    def _prime(self, daemon) -> None:
        # The detail endpoint runs core.inspect(refresh="auto"), so serving it once
        # populates the cache the bench-scoped verbs resolve against.
        body = _get(daemon.base + f"/api/v1/instances/{daemon.project}")["data"]
        assert body["benches"], "the cache must know the bench before bench-scoped verbs"

    def test_set_label_round_trips_and_is_cleared(self, daemon):
        self._prime(daemon)
        try:
            body = _post(
                daemon.base + "/api/v1/actions",
                {"action": "set_label", "project": daemon.project, "label": "tiera"},
            )
            assert body["data"]["outcome"]["label"] == "tiera"

            detail = _get(daemon.base + f"/api/v1/instances/{daemon.project}")["data"]
            assert detail["benches"][0]["label"] == "tiera"
        finally:
            # Shared-group convention: remove the label again (DB and marker).
            result = harness.run_cwcli("axi", "label", daemon.project, "--clear")
            assert result.returncode == 0, result.stderr

    def test_unlock_resolves_the_site_in_core_and_touches_the_real_container(self, daemon):
        # Removing a locks folder is self-restoring state: absent is the clean state.
        body = _post(
            daemon.base + "/api/v1/actions",
            {"action": "unlock_site", "project": daemon.project, "site": harness.DEFAULT_SITE},
        )

        assert body["data"]["outcome"]["site"] == harness.DEFAULT_SITE
        assert body["data"]["outcome"]["locks_path"].endswith(f"{harness.DEFAULT_SITE}/locks")

    def test_clear_maintenance_takes_a_real_site_out_of_maintenance(self, daemon):
        """Put the site INTO maintenance through bench itself, then prove the new
        Phase 1 action takes it out, reading the live flag back both ways."""
        self._prime(daemon)
        site = harness.DEFAULT_SITE
        flag = f"{harness.DEFAULT_BENCH_PATH}/sites/{site}/site_config.json"

        def _maintenance_on() -> bool:
            code, out = harness.exec_in_frappe(daemon.project, f"cat {flag}")
            assert code == 0, out
            return bool(json.loads(out).get("maintenance_mode"))

        code, out = harness.exec_in_frappe(
            daemon.project,
            f"cd {harness.DEFAULT_BENCH_PATH} && bench --site {site} set-maintenance-mode on",
        )
        assert code == 0, out
        try:
            assert _maintenance_on() is True, "the precondition: the site IS in maintenance"
            body = _post(
                daemon.base + "/api/v1/actions",
                {"action": "clear_maintenance", "project": daemon.project, "site": site},
            )
            assert body["data"]["outcome"] == {
                "project": daemon.project,
                "bench_path": harness.DEFAULT_BENCH_PATH,
                "site": site,
                "maintenance_off": True,
            }
            assert _maintenance_on() is False
        finally:
            # Belt and braces: never leave the shared site at 503.
            harness.exec_in_frappe(
                daemon.project,
                f"cd {harness.DEFAULT_BENCH_PATH} && bench --site {site} set-maintenance-mode off",
            )

    def test_stop_bench_then_start_restores_the_shared_bench(self, daemon):
        """``stop_bench`` takes ONE bench's supervisord down and leaves the
        container up; ``start_instance`` with the same bench brings it back."""
        self._prime(daemon)
        _wait_for(
            lambda: _instance(daemon.base, daemon.project)["overall"] == "running",
            desc="a running bench to stop",
        )
        try:
            body = _post(
                daemon.base + "/api/v1/actions",
                {"action": "stop_bench", "project": daemon.project, "bench": "0"},
            )
            outcome = body["data"]["outcome"]
            assert outcome["already_stopped"] is False
            assert "web" in outcome["stopped_processes"]
            state = _wait_for(
                lambda: (
                    _instance(daemon.base, daemon.project)
                    if _instance(daemon.base, daemon.project)["overall"] in ("online", "offline")
                    else None
                ),
                desc="the bench to read as down",
            )
            assert state["container_running"] is True, "only the bench stopped, not the container"
        finally:
            started = _post(
                daemon.base + "/api/v1/actions",
                {"action": "start_instance", "project": daemon.project, "bench": "0"},
            )
            assert "data" in started
            harness.wait_for_site_ready(daemon.project, harness.DEFAULT_SITE)
            _wait_for(
                lambda: _instance(daemon.base, daemon.project)["overall"] == "running",
                desc="the bench to serve again",
                timeout=180,
            )

    def test_the_scale_confirm_arrives_from_core_not_the_page(self, daemon):
        self._prime(daemon)
        # Another shared-instance E2E may already have widened the init default of
        # six ports. Request one beyond the live compose range so expansion is
        # genuinely needed regardless of collection order. Core answers
        # NEEDS_CHOICE/confirm_scale before any mutation, so this remains a read.
        compose_path = (
            Path(os.environ["CWCLI_HOME"])
            / "projects"
            / daemon.project
            / "conf"
            / "docker-compose.yml"
        )
        compose_text = compose_path.read_text()
        web_range = re.search(r"\d+-\d+:8000-(\d+)", compose_text)
        assert web_range is not None, compose_text
        requested_count = int(web_range.group(1)) - 8000 + 2
        with pytest.raises(urllib.error.HTTPError) as e:
            _post(
                daemon.base + "/api/v1/actions",
                {
                    "action": "scale_instance",
                    "project": daemon.project,
                    "to": requested_count,
                },
            )

        assert e.value.code == 409
        error = json.loads(e.value.read())["errors"][0]
        assert error["type"] == "needs_choice"
        assert error["title"] == "confirm_scale"
        assert "RESTARTS" in error["message"]

        # The covering case is a consent-free idempotent no-op that changes nothing.
        noop = _post(
            daemon.base + "/api/v1/actions",
            {"action": "scale_instance", "project": daemon.project},
        )
        assert noop["data"]["outcome"]["expanded"] is False

    def test_the_logs_read_is_resumable_against_real_supervisor_logs(self, daemon):
        body = _get(daemon.base + f"/api/v1/instances/{daemon.project}/logs?lines=20")["data"]

        read = body["outcome"]
        assert read["not_cwcli_supervised"] is False
        processes = {group["process"] for group in read["logs"]}
        assert "web" in processes
        web = next(g for g in read["logs"] if g["process"] == "web")
        assert web["lines"], "a served bench's web log must not be empty"
        assert web["offset"] > 0

        # Resume from the cursor: nothing new is nothing, never the same lines again.
        after = ",".join(f"{g['process']}:{g['offset']}" for g in read["logs"])
        again = _get(daemon.base + f"/api/v1/instances/{daemon.project}/logs?after={after}")["data"]
        web_again = next(g for g in again["outcome"]["logs"] if g["process"] == "web")
        assert web_again["offset"] >= web["offset"]
        assert web["lines"][-1] not in web_again["lines"] or web_again["offset"] > web["offset"]

    def test_search_reports_the_real_instance_as_present_and_verified(self, daemon):
        self._prime(daemon)
        body = _get(daemon.base + "/api/v1/search?q=frappe")["data"]

        assert body["verified"] is True, "with Docker reachable the sweep must verify"
        ours = [m for m in body["matches"] if m["project"] == daemon.project]
        assert ours, "the real instance's frappe app must match"
        assert all(m["project_state"] == "present" for m in ours)

    def test_refresh_and_checkout_round_trip_without_leaving_git_state(self, daemon):
        import shlex

        self._prime(daemon)
        app_dir = f"{harness.DEFAULT_BENCH_PATH}/apps/frappe"
        code, branch = harness.exec_in_frappe(
            daemon.project, f"git -C {shlex.quote(app_dir)} branch --show-current"
        )
        assert code == 0, branch
        branch = branch.strip()
        assert branch
        code, original_head = harness.exec_in_frappe(
            daemon.project, f"git -C {shlex.quote(app_dir)} rev-parse HEAD"
        )
        assert code == 0, original_head
        original_head = original_head.strip()
        code, status = harness.exec_in_frappe(
            daemon.project, f"git -C {shlex.quote(app_dir)} status --porcelain"
        )
        assert code == 0, status
        assert not status.strip(), "the shared checkout must start clean"

        try:
            refreshed = _post(
                daemon.base + "/api/v1/actions",
                {"action": "refresh_status", "project": daemon.project},
            )
            assert refreshed["data"]["outcome"]["project"] == daemon.project
            assert refreshed["data"]["outcome"]["container_running"] is True

            checked_out = _post(
                daemon.base + "/api/v1/actions",
                {
                    "action": "checkout_app",
                    "project": daemon.project,
                    "app": "frappe",
                    "ref": branch,
                },
            )
            assert [row["action"] for row in checked_out["data"]["outcome"]["results"]] == [
                "fetch",
                "checkout",
                "restart-processes",
            ]

            marker = "cwe2e-serve-dirty-tree"
            tracked = f"{app_dir}/README.md"
            code, output = harness.exec_in_frappe(
                daemon.project, f"printf '\\n{marker}\\n' >> {shlex.quote(tracked)}"
            )
            assert code == 0, output
            with pytest.raises(urllib.error.HTTPError) as exc:
                _post(
                    daemon.base + "/api/v1/actions",
                    {
                        "action": "checkout_app",
                        "project": daemon.project,
                        "app": "frappe",
                        "ref": branch,
                    },
                )
            assert exc.value.code == 409
            refusal = json.loads(exc.value.read())["errors"][0]
            assert refusal["title"] == "app.dirty_tree"
            assert "README.md" in refusal["message"]
            code, output = harness.exec_in_frappe(
                daemon.project, f"grep -F {shlex.quote(marker)} {shlex.quote(tracked)}"
            )
            assert code == 0, output
        finally:
            restore = (
                f"git -C {shlex.quote(app_dir)} checkout {shlex.quote(branch)}"
                f" && git -C {shlex.quote(app_dir)} reset --hard {shlex.quote(original_head)}"
            )
            code, output = harness.exec_in_frappe(daemon.project, restore)
            assert code == 0, output
            code, restored_branch = harness.exec_in_frappe(
                daemon.project, f"git -C {shlex.quote(app_dir)} branch --show-current"
            )
            assert code == 0, restored_branch
            assert restored_branch.strip() == branch
            code, restored_head = harness.exec_in_frappe(
                daemon.project, f"git -C {shlex.quote(app_dir)} rev-parse HEAD"
            )
            assert code == 0, restored_head
            assert restored_head.strip() == original_head
            code, restored_status = harness.exec_in_frappe(
                daemon.project, f"git -C {shlex.quote(app_dir)} status --porcelain"
            )
            assert code == 0, restored_status
            assert not restored_status.strip()


class TestGuardedDestructiveJobs:
    """Phase 3: ``rm-site`` as a job against the real instance, both halves of its
    backup gate, through the shipped daemon. Each test creates its OWN throwaway
    site on the shared bench and never touches ``inst.site``. The browser half
    (typed confirmation dialogs) is proven by hand in ``docs/e2e/webui-phase3.md``;
    what CI pins here is that the job reaches the SAME core path as ``cwcli
    rm-site``: consent is the core's own ``confirm_drop_site`` until the spec
    carries it, a succeeded job leaves the verified dump on the host and the site
    genuinely gone, and a failed backup drops nothing."""

    def test_rm_site_job_needs_consent_then_backs_up_and_drops(self, daemon, running_instance):
        from .test_rm_site_e2e import (
            MARKER,
            _create_site,
            _database_exists,
            _db_name,
            _drop_temporary_site,
            _dropped_site_dirs,
            _seed_marker,
            _site_exists,
        )

        inst = running_instance
        site = "cwe2ewebdropa.localhost"
        _create_site(inst, site)
        try:
            _seed_marker(inst, site)
            db_name = _db_name(inst, site)
            target = {"project": inst.name, "site": site}

            # Without consent the job ends on the CORE's choice and nothing is dropped.
            asked = _post(
                daemon.base + "/api/v1/jobs", {"verb": "rm-site", "target": target, "args": {}}
            )["data"]
            asked = _wait_for_job(daemon.base, asked["id"])
            assert asked["state"] == "needs_choice", asked
            assert asked["choice"]["kind"] == "confirm_drop_site"
            assert _site_exists(inst, site)

            created = _post(
                daemon.base + "/api/v1/jobs",
                {"verb": "rm-site", "target": target, "args": {}, "consent": True},
            )["data"]
            assert created["pid"] != daemon.proc.pid
            finished = _wait_for_job(daemon.base, created["id"], timeout=600)
            assert finished["state"] == "succeeded", finished
            assert finished["result"]["ok"] is True
            assert finished["result"]["site"] == site
            assert not _site_exists(inst, site), "site directory still present"
            assert not _database_exists(inst, db_name), "the site's database was not dropped"

            dirs = _dropped_site_dirs(inst, site)
            assert len(dirs) == 1 and finished["result"]["archived_host_path"] == str(dirs[0])
            dumps = [
                f for f in (dirs[0] / "backups").iterdir() if f.name.endswith("-database.sql.gz")
            ]
            assert len(dumps) == 1
            assert MARKER.encode() in gzip.decompress(
                dumps[0].read_bytes()
            ), "record not in the dump"
            # The job record is the same durable artefact the browser reattaches to.
            assert "drop_site_notice" in {event["type"] for event in finished["events"]}
        finally:
            _drop_temporary_site(inst, site)

    def test_rm_site_job_drops_nothing_when_the_backup_fails(self, daemon, running_instance):
        from .test_rm_site_e2e import (
            _count_marker,
            _create_site,
            _database_exists,
            _db_name,
            _drop_temporary_site,
            _dropped_site_dirs,
            _seed_marker,
            _site_exists,
        )

        inst = running_instance
        site = "cwe2ewebdropb.localhost"
        _create_site(inst, site)
        backups = f"{inst.bench}/sites/{site}/private/backups"
        try:
            _seed_marker(inst, site)
            db_name = _db_name(inst, site)
            code, out = harness.exec_in_frappe(
                inst.name, f"mkdir -p {backups} && chmod 0555 {backups}"
            )
            assert code == 0, out

            created = _post(
                daemon.base + "/api/v1/jobs",
                {
                    "verb": "rm-site",
                    "target": {"project": inst.name, "site": site},
                    "args": {},
                    "consent": True,
                },
            )["data"]
            finished = _wait_for_job(daemon.base, created["id"], timeout=600)

            assert finished["state"] == "failed", finished
            assert finished["error"]["code"] == "rm_site.backup_failed"
            assert "nothing was dropped" in finished["error"]["message"]
            assert _site_exists(inst, site), "the site was dropped"
            assert _database_exists(inst, db_name), "the database was dropped"
            assert _count_marker(inst, site) == 1, "the site's data was lost"
            assert _dropped_site_dirs(inst, site) == [], "a half-written host archive was left"
        finally:
            harness.exec_in_frappe(inst.name, f"chmod 0755 {backups}")
            _drop_temporary_site(inst, site)


class TestSettingsWrites:
    """Phase 3 settings writes through the shipped daemon, against the isolated
    ``CWCLI_HOME`` the E2E session owns: each lands in the same config the CLI
    reads back, and every test restores what it changed."""

    def _config(self, daemon) -> dict:
        return _get(daemon.base + "/api/v1/config")["data"]

    def _apply(self, daemon, payload: dict) -> dict:
        return _post(daemon.base + "/api/v1/settings", payload)["data"]

    def test_search_paths_add_prune_and_remove_round_trip(self, daemon):
        path = "/workspace/cwe2e-unreferenced-bench"
        before = self._config(daemon)["search_paths"]
        assert path not in before
        try:
            added = self._apply(daemon, {"setting": "paths.add", "path": path})
            assert added["outcome"]["changed"] is True
            assert path in self._config(daemon)["search_paths"]

            # A dry run names the path; it is prunable only when every instance on
            # this Docker daemon could be checked (a stopped one keeps it, by design).
            plan = self._apply(daemon, {"setting": "paths.prune"})["outcome"]
            row = next(s for s in plan["statuses"] if s["path"] == path)
            assert row["present"] is False
            assert row["prunable"] == (not plan["unchecked_instances"])
            assert plan["applied"] is False
            assert path in self._config(daemon)["search_paths"], "a dry run must not remove"
        finally:
            removed = self._apply(daemon, {"setting": "paths.remove", "path": path})
            assert removed["outcome"]["changed"] is True
            assert self._config(daemon)["search_paths"] == before

    def test_tips_and_cache_toggles_write_the_config_the_cli_reads(self, daemon):
        start = self._config(daemon)
        try:
            self._apply(
                daemon, {"setting": "tips.disable" if start["show_tips"] else "tips.enable"}
            )
            assert self._config(daemon)["show_tips"] is (not start["show_tips"])
            shown = harness.run_cwcli("config", "show", "--json")
            assert json.loads(shown.stdout)["show_tips"] is (not start["show_tips"])

            if not start["cache_env_override"]:
                self._apply(daemon, {"setting": "cache.disable"})
                assert self._config(daemon)["cache_enabled"] is False
                self._apply(daemon, {"setting": "cache.enable"})
                assert self._config(daemon)["cache_enabled"] is True
        finally:
            self._apply(
                daemon, {"setting": "tips.enable" if start["show_tips"] else "tips.disable"}
            )
            assert self._config(daemon)["show_tips"] is start["show_tips"]

    def test_clearing_the_whole_cache_takes_the_cores_consent_first(self, daemon):
        with pytest.raises(urllib.error.HTTPError) as error:
            _post(daemon.base + "/api/v1/settings", {"setting": "cache.clear", "all": True})
        assert error.value.code == 409
        assert json.loads(error.value.read())["errors"][0]["title"] == "confirm_clear"

        # One remembered project can be forgotten without consent; a name the cache
        # never held is an honest found: false, never an error.
        gone = self._apply(daemon, {"setting": "cache.clear", "project": "cwe2e-never-cached"})
        assert gone["outcome"] == {
            "scope": "project",
            "project": "cwe2e-never-cached",
            "found": False,
        }
