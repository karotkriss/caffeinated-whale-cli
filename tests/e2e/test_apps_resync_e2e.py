"""Post-mutation resynchronisation E2E - real Docker, real supervisord, real sites.

The defect: changing app code on disk under a bench that is already running leaves
every long-lived process serving the interpreter it booted with. `install` and
`uninstall` were fixed first and only cycled `web`; this proves the two residuals
that fix reported rather than assumed away:

1. `apps checkout` and `apps update` share the root cause and now route through the
   SAME `core.supervision.resync_after_code_change` step.
2. The scheduler and the workers are cycled too, not just `web`. That one cannot be
   proven by a mock at all: it is a statement about which real supervisord programs
   got a new PID, and about which ones deliberately did NOT.

Every assertion asserts the POSITIVE first - the site genuinely serves, the PID
genuinely moved - before any negative. Checking only that no error was raised is
exactly what missed this class of failure the first time.

Mutates the shared session instance (installs an app, restarts its stack), so it
restores both in a `finally`, per the shared-instance convention.
"""

from __future__ import annotations

import json
import re

import pytest

from . import harness
from .test_start_status_e2e import (
    BENCH_PY,
    SUPERVISOR_CFG,
    _ensure_serving,
    _wait_supervised_stack,
)

pytestmark = pytest.mark.e2e

_APP = "payments"

# `<program> RUNNING pid 123, uptime 0:00:11` - the only shape carrying a live PID.
_PID_RE = re.compile(r"^(\S+)\s+RUNNING\s+pid\s+(\d+)", re.MULTILINE)

# What a Frappe Procfile runs that does NOT import a Frappe app: node processes and
# redis servers. Cycling them would buy nothing and would drop the cache and the job
# queue, so the shared step must leave them alone.
_NOT_CODE_BEARING = ("socketio", "watch", "redis_cache", "redis_queue", "redis")


def _program_pids(inst) -> dict[str, int]:
    """Every RUNNING supervised program's live PID, straight from supervisorctl.

    supervisorctl exits non-zero when ANY program is not RUNNING, so the body is
    parsed regardless of the exit code - the state tokens are what we want.
    """
    _code, out = harness.exec_in_frappe(
        inst.name, f"{BENCH_PY} -m supervisor.supervisorctl -c {SUPERVISOR_CFG} status"
    )
    pids = {name: int(pid) for name, pid in _PID_RE.findall(out)}
    assert pids, f"no RUNNING supervised program found for {inst.name}: {out}"
    return pids


def _code_bearing(pids: dict[str, int]) -> dict[str, int]:
    return {
        name: pid
        for name, pid in pids.items()
        if name == "web" or name == "schedule" or name.startswith("worker")
    }


def _site_ping_code(inst) -> str:
    """The HTTP result a real site-routed request gets from this bench's web process."""
    code, config = harness.exec_in_frappe(
        inst.name, f"cat {inst.bench}/sites/common_site_config.json"
    )
    assert code == 0, config
    port = int(json.loads(config)["webserver_port"])
    code, out = harness.exec_in_frappe(
        inst.name,
        f'curl -sS --max-time 10 -o /dev/null -w "%{{http_code}}" '
        f'-H "Host: {inst.site}" http://localhost:{port}/api/method/ping',
    )
    assert code == 0, out
    return out.strip().splitlines()[-1]


def _installed_apps(inst) -> list[str]:
    code, out = harness.exec_in_frappe(
        inst.name, f"cd {inst.bench} && bench --site {inst.site} list-apps"
    )
    assert code == 0, out
    return [line.split()[0] for line in out.strip().splitlines() if line.strip()]


def _remove_app_source(inst, *, required: bool = True) -> None:
    """Remove the fetched app after it has been uninstalled from every site."""
    code, out = harness.exec_in_frappe(
        inst.name, f"cd {inst.bench} && bench remove-app --no-backup {_APP}"
    )
    if required:
        assert code == 0, out


def _assert_resynchronised(inst, before: dict[str, int], *, verb: str) -> dict[str, int]:
    """Every code-bearing program got a new PID, the untouched ones kept theirs, and
    the site genuinely serves. Returns the new PID map for the next leg."""
    _wait_supervised_stack(inst.name)
    after = _program_pids(inst)

    # POSITIVE FIRST: the site really answers, so the restart produced a working
    # bench rather than merely a different set of PIDs.
    assert _site_ping_code(inst) == "200", f"the site does not serve after {verb}"

    cycled = _code_bearing(before)
    assert cycled, "the fixture bench runs no code-bearing program to cycle"
    for name, pid in cycled.items():
        assert name in after, f"{verb}: program {name!r} is gone after the resync"
        assert after[name] != pid, (
            f"{verb}: {name!r} kept PID {pid}, so it is still running the code that "
            "was on disk before the change - the exact defect this closes"
        )

    for name in _NOT_CODE_BEARING:
        if name in before and name in after:
            assert after[name] == before[name], (
                f"{verb}: {name!r} was restarted, but it imports no Frappe app - "
                "cycling redis drops the cache and the job queue for nothing"
            )
    return after


def test_every_app_code_change_resynchronises_the_whole_bench(running_instance):
    """One arc over the three verbs that change app code on disk.

    Kept as ONE test deliberately: the expensive part is the real `bench get-app`,
    and `checkout` and `update` need an app that is not `frappe` (updating frappe
    diverts to the bench-wide `bench update --reset` path, a different state
    machine). Installing once and driving all three legs off it is the honest cost.
    """
    inst = running_instance
    _ensure_serving(inst.name)
    _wait_supervised_stack(inst.name)
    assert _APP not in _installed_apps(inst), "fixture already has payments installed"
    assert _site_ping_code(inst) == "200", "the running fixture must serve before the mutation"

    before = _program_pids(inst)
    assert "web" in before, f"no supervised web program to cycle: {before}"
    # The residual under test only exists if the fixture actually runs background
    # programs; an assertion that silently covers nothing is worse than none.
    assert any(
        name == "schedule" or name.startswith("worker") for name in before
    ), f"the fixture runs no scheduler or worker, so the residual cannot be proven: {before}"

    restored = False
    try:
        install = harness.run_cwcli(
            "axi",
            "apps",
            "install",
            inst.name,
            _APP,
            "--site",
            inst.site,
            "--branch",
            harness.FRAPPE_BRANCH,
        )
        assert install.returncode == 0, install.stdout + install.stderr
        assert "restart-processes" in install.stdout
        assert _APP in _installed_apps(inst)
        before = _assert_resynchronised(inst, before, verb="apps install")

        # --- checkout: the verb whose "no target site to verify against" was true of
        # its arguments and false of its effect. The sites it changes are the ones
        # with the app installed, and it now proves they serve.
        code, out = harness.exec_in_frappe(
            inst.name, f"git -C {inst.bench}/apps/{_APP} branch --show-current"
        )
        assert code == 0, out
        branch = out.strip()

        checkout = harness.run_cwcli("axi", "apps", "checkout", inst.name, _APP, branch)
        assert checkout.returncode == 0, checkout.stdout + checkout.stderr
        assert "restart-processes" in checkout.stdout
        before = _assert_resynchronised(inst, before, verb="apps checkout")

        # --- update: the resync has to land AFTER maintenance mode is disabled, or
        # the site answers 503 and the proof can never be obtained. That ordering is
        # invisible to a mocked probe and is the whole reason this leg is real.
        update = harness.run_cwcli("axi", "apps", "update", inst.name, _APP)
        assert update.returncode == 0, update.stdout + update.stderr
        assert "restarted_processes" in update.stdout
        assert "unserved_sites" in update.stdout
        assert "resync_error: null" in update.stdout
        _assert_resynchronised(inst, before, verb="apps update")

        uninstall = harness.run_cwcli(
            "apps", "uninstall", inst.name, _APP, "--site", inst.site, "--yes"
        )
        assert uninstall.returncode == 0, uninstall.stdout + uninstall.stderr
        assert _site_ping_code(inst) == "200"
        assert _APP not in _installed_apps(inst)
        _remove_app_source(inst)
        restored = True
    finally:
        if not restored:
            # Best-effort cleanup that never masks the real assertion failure.
            if _APP in _installed_apps(inst):
                harness.run_cwcli(
                    "apps", "uninstall", inst.name, _APP, "--site", inst.site, "--yes"
                )
            _remove_app_source(inst, required=False)
        _ensure_serving(inst.name)
