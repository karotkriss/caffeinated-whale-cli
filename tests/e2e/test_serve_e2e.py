"""``cwcli serve`` against a REAL instance: the three tiers, and the quiet in between.

The daemon is the shipped binary in a subprocess and the client is an ordinary
HTTP/SSE reader, so what is measured here is the whole path a browser takes.

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

import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from . import harness

pytestmark = pytest.mark.e2e

_HTTP_TIMEOUT = 10


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _Client:
    """An SSE reader on a background thread, recording every frame it receives."""

    def __init__(self, url: str):
        self.frames: list[tuple[str, dict]] = []
        self.keepalives = 0
        self._resp = urllib.request.urlopen(url, timeout=_HTTP_TIMEOUT)  # noqa: S310
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        event = None
        try:
            for raw in self._resp:
                line = raw.decode().rstrip("\n")
                if line.startswith(": "):
                    self.keepalives += 1
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
    """The shipped ``cwcli serve`` in a subprocess, bound to a free local port."""
    port = _free_port()
    proc = subprocess.Popen(
        [harness.CWCLI, "serve", "--port", str(port), "--host", "127.0.0.1", "--interval", "1"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=os.environ.copy(),
    )
    base = f"http://127.0.0.1:{port}"

    def _up():
        with urllib.request.urlopen(base + "/api/snapshot", timeout=2) as r:  # noqa: S310
            return json.loads(r.read())

    try:
        harness.wait_until(_up, timeout=60, interval=0.5, desc="cwcli serve listening")
        yield type("D", (), {"base": base, "proc": proc, "project": running_instance.name})
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            proc.kill()


def _get(url: str):
    with urllib.request.urlopen(url, timeout=_HTTP_TIMEOUT) as resp:  # noqa: S310
        return json.loads(resp.read())


def _post(url: str, payload: dict):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:  # noqa: S310
        return json.loads(resp.read())


def _instance(base: str, project: str) -> dict:
    body = _get(base + "/api/snapshot")
    return next(i for i in body["instances"] if i["project"] == project)


def _wait_for(predicate, *, timeout=90, desc="condition"):
    return harness.wait_until(predicate, timeout=timeout, interval=0.5, desc=desc)


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

        client = _Client(daemon.base + f"/api/events?focus={daemon.project}")
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
        client = _Client(daemon.base + "/api/events")
        try:
            _wait_for(lambda: client.frames, desc="the opening snapshot")
            before = _instance(daemon.base, daemon.project)

            # ~15 probe cycles at --interval 1. Every process's uptime advances,
            # cpu and rss move, and each probe takes a different number of
            # milliseconds - so this is a real test of the stable-field digest,
            # not of a frozen fixture.
            time.sleep(15)

            after = _instance(daemon.base, daemon.project)
            assert after["probed_at"] > before["probed_at"], "the probe loop must be running"
            assert client.deltas() == [], "a steady instance must stay silent"
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

        client = _Client(daemon.base + "/api/events")
        try:
            _wait_for(lambda: client.frames, desc="the opening snapshot")

            # Restores itself: the Console rail's process restart leaves the program
            # RUNNING, so the shared instance ends this test exactly as it started it.
            result = _post(
                daemon.base + "/api/action",
                {"action": "restart_process", "project": daemon.project, "process": "web"},
            )
            assert result["ok"] is True

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
        client = _Client(daemon.base + "/api/events")
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
        body = _get(daemon.base + f"/api/instance/{daemon.project}/detail")

        assert body["project"] == daemon.project
        assert body["served_from"] in ("cache", "partial", "full")
        bench = body["benches"][0]
        assert bench["path"] == harness.DEFAULT_BENCH_PATH
        site = next(s for s in bench["sites"] if s["name"] == harness.DEFAULT_SITE)
        assert any(a.startswith("frappe") for a in site["installed_apps"])
        # The verified-or-remembered token: True only on a full re-inspect.
        assert site["installed_apps_verified"] == (body["served_from"] == "full")

    def test_an_unknown_project_is_a_404_not_an_empty_success(self, daemon):
        import urllib.error

        with pytest.raises(urllib.error.HTTPError) as e:
            _get(daemon.base + "/api/instance/cwe2e-no-such-project/detail")
        assert e.value.code == 404


class TestTierAActions:
    """The Tier A rail expansion against the real instance: each verb round-trips
    through the daemon into real core against a real container, the scale confirm
    provably arrives FROM core (before any mutation), and every test leaves the
    shared instance exactly as it found it."""

    def _prime(self, daemon) -> None:
        # The detail endpoint runs core.inspect(refresh="auto"), so serving it once
        # populates the cache the bench-scoped verbs resolve against.
        body = _get(daemon.base + f"/api/instance/{daemon.project}/detail")
        assert body["benches"], "the cache must know the bench before bench-scoped verbs"

    def test_set_label_round_trips_and_is_cleared(self, daemon):
        self._prime(daemon)
        try:
            body = _post(
                daemon.base + "/api/action",
                {"action": "set_label", "project": daemon.project, "label": "tiera"},
            )
            assert body["ok"] is True
            assert body["outcome"]["label"] == "tiera"

            detail = _get(daemon.base + f"/api/instance/{daemon.project}/detail")
            assert detail["benches"][0]["label"] == "tiera"
        finally:
            # Shared-group convention: remove the label again (DB and marker).
            result = harness.run_cwcli("axi", "label", daemon.project, "--clear")
            assert result.returncode == 0, result.stderr

    def test_unlock_resolves_the_site_in_core_and_touches_the_real_container(self, daemon):
        # Removing a locks folder is self-restoring state: absent is the clean state.
        body = _post(
            daemon.base + "/api/action",
            {"action": "unlock_site", "project": daemon.project, "site": harness.DEFAULT_SITE},
        )

        assert body["ok"] is True
        assert body["outcome"]["site"] == harness.DEFAULT_SITE
        assert body["outcome"]["locks_path"].endswith(f"{harness.DEFAULT_SITE}/locks")

    def test_the_scale_confirm_arrives_from_core_not_the_page(self, daemon):
        import urllib.error

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
                daemon.base + "/api/action",
                {
                    "action": "scale_instance",
                    "project": daemon.project,
                    "to": requested_count,
                },
            )

        assert e.value.code == 409
        body = json.loads(e.value.read())
        assert body["error"]["kind"] == "needs_choice"
        assert body["error"]["code"] == "confirm_scale"
        assert "RESTARTS" in body["error"]["message"]

        # The covering case is a consent-free idempotent no-op that changes nothing.
        noop = _post(
            daemon.base + "/api/action",
            {"action": "scale_instance", "project": daemon.project},
        )
        assert noop["ok"] is True
        assert noop["outcome"]["expanded"] is False

    def test_the_logs_read_serves_real_supervisor_logs(self, daemon):
        body = _get(daemon.base + f"/api/instance/{daemon.project}/logs?lines=20")

        assert body["ok"] is True
        read = body["outcome"]
        assert read["not_cwcli_supervised"] is False
        processes = {group["process"] for group in read["logs"]}
        assert "web" in processes
        web = next(g for g in read["logs"] if g["process"] == "web")
        assert web["lines"], "a served bench's web log must not be empty"

    def test_where_reports_the_real_instance_as_present_and_verified(self, daemon):
        self._prime(daemon)
        body = _get(daemon.base + "/api/where?q=frappe")

        assert body["ok"] is True
        assert body["verified"] is True, "with Docker reachable the sweep must verify"
        ours = [m for m in body["matches"] if m["project"] == daemon.project]
        assert ours, "the real instance's frappe app must match"
        assert all(m["project_state"] == "present" for m in ours)

    def test_refresh_and_checkout_round_trip_without_leaving_git_state(self, daemon):
        import shlex
        import urllib.error

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
                daemon.base + "/api/action",
                {"action": "refresh_status", "project": daemon.project},
            )
            assert refreshed["ok"] is True
            assert refreshed["outcome"]["project"] == daemon.project
            assert refreshed["outcome"]["container_running"] is True

            checked_out = _post(
                daemon.base + "/api/action",
                {
                    "action": "checkout_app",
                    "project": daemon.project,
                    "app": "frappe",
                    "ref": branch,
                },
            )
            assert checked_out["ok"] is True
            assert [row["action"] for row in checked_out["outcome"]["results"]] == [
                "fetch",
                "checkout",
            ]

            marker = "cwe2e-serve-dirty-tree"
            tracked = f"{app_dir}/README.md"
            code, output = harness.exec_in_frappe(
                daemon.project, f"printf '\\n{marker}\\n' >> {shlex.quote(tracked)}"
            )
            assert code == 0, output
            with pytest.raises(urllib.error.HTTPError) as exc:
                _post(
                    daemon.base + "/api/action",
                    {
                        "action": "checkout_app",
                        "project": daemon.project,
                        "app": "frappe",
                        "ref": branch,
                    },
                )
            assert exc.value.code == 409
            refusal = json.loads(exc.value.read())
            assert refusal["error"]["code"] == "app.dirty_tree"
            assert "README.md" in refusal["error"]["message"]
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
