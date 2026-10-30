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
import shlex
from contextlib import contextmanager

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
        inst.name,
        f"cd {inst.bench} && bench --site {inst.site} " "execute frappe.get_installed_apps",
    )
    assert code == 0, out
    return json.loads(out.strip().splitlines()[-1])


def _remove_app_source(inst, *, required: bool = True) -> None:
    """Remove the fetched app after it has been uninstalled from every site."""
    code, out = harness.exec_in_frappe(
        inst.name, f"cd {inst.bench} && bench remove-app --no-backup {_APP}"
    )
    if required:
        assert code == 0, out


# A pure-Python package no Frappe bench ships, declared as a NEW frappe dependency.
_NEW_DEP = "cowsay"
_NEW_DEP_SPEC = "cowsay~=6.1"
_PROBE_PATCH = "frappe.patches.cwe2e_requirements_probe"


@contextmanager
def _frappe_gains_a_dependency(inst):
    """Reproduce the captain's test-server failure on the shared bench: the bench's
    frappe code moved to a version needing a package the venv lacks (there it was
    ``pyarrow``), and an update of ANOTHER app then migrates.

    frappe declares the package in ``pyproject.toml`` and a new pending patch imports
    it, so ``bench migrate`` dies with ``ModuleNotFoundError`` unless the update
    reinstalled the whole bench's requirements first. A patch module is used rather
    than an edit to a loaded module because ``bench serve`` reloads on changes to
    modules it already imported; a new file is invisible to it. Everything is put
    back in the ``finally``: both files, the package, and the patch's log row.
    """
    frappe_dir = f"{inst.bench}/apps/frappe"
    py = f"{inst.bench}/env/bin/python"
    edit = (
        "import pathlib\n"
        f"p = pathlib.Path('{frappe_dir}/pyproject.toml')\n"
        "s = p.read_text()\n"
        "assert 'dependencies = [' in s\n"
        f"p.write_text(s.replace('dependencies = [', 'dependencies = [\\n    \"{_NEW_DEP_SPEC}\",', 1))\n"
    )
    code, out = harness.exec_in_frappe(
        inst.name,
        f"cd {frappe_dir} && cp pyproject.toml pyproject.toml.cwe2e-bak && "
        "cp frappe/patches.txt frappe/patches.txt.cwe2e-bak && "
        f"python3 -c {shlex.quote(edit)} && "
        f"printf 'import {_NEW_DEP}  # noqa: F401\\n\\n\\ndef execute():\\n    pass\\n' "
        "> frappe/patches/cwe2e_requirements_probe.py && "
        f"printf '\\n{_PROBE_PATCH}\\n' >> frappe/patches.txt",
    )
    try:
        assert code == 0, f"could not stage frappe's new dependency: {out}"
        code, out = harness.exec_in_frappe(inst.name, f"{py} -c 'import {_NEW_DEP}'")
        assert code != 0, f"{_NEW_DEP} is already installed, so the leg proves nothing: {out}"
        yield
    finally:
        harness.exec_in_frappe(
            inst.name,
            f"cd {frappe_dir} && mv -f pyproject.toml.cwe2e-bak pyproject.toml; "
            "mv -f frappe/patches.txt.cwe2e-bak frappe/patches.txt; "
            "rm -f frappe/patches/cwe2e_requirements_probe.py; "
            f"{py} -m pip uninstall -y {_NEW_DEP} >/dev/null 2>&1 || "
            f"uv pip uninstall --python {py} {_NEW_DEP} >/dev/null 2>&1; "
            f"cd {inst.bench} && bench --site {inst.site} execute frappe.db.delete "
            f"--args {shlex.quote(json.dumps(['Patch Log', {'patch': _PROBE_PATCH}]))}",
        )


def _assert_resynchronised(
    inst, before: dict[str, int], *, verb: str, watcher_quiesced: bool = False
) -> dict[str, int]:
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
            if name == "watch" and watcher_quiesced:
                continue
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
        with harness.quiesce_v14_asset_watcher(inst.name, inst.bench) as watcher_quiesced:
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
        before = _assert_resynchronised(
            inst, before, verb="apps install", watcher_quiesced=watcher_quiesced
        )

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
        # It runs while frappe needs a package the venv lacks: the update must
        # reinstall the WHOLE bench's requirements (not only payments') before the
        # migrate, or that migrate fails with ModuleNotFoundError.
        with _frappe_gains_a_dependency(inst):
            update = harness.run_cwcli("axi", "apps", "update", inst.name, _APP)
            assert update.returncode == 0, update.stdout + update.stderr
            assert "requirements: ok" in update.stdout
            code, out = harness.exec_in_frappe(
                inst.name, f"{inst.bench}/env/bin/python -c 'import {_NEW_DEP}'"
            )
            assert code == 0, f"the update did not install frappe's new dependency: {out}"
        assert "restarted_processes" in update.stdout
        assert "unserved_sites" in update.stdout
        assert "resync_error: null" in update.stdout
        _assert_resynchronised(inst, before, verb="apps update")

        uninstall = harness.run_cwcli(
            "apps", "uninstall", inst.name, "--app", _APP, "--site", inst.site, "--yes"
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
                    "apps", "uninstall", inst.name, "--app", _APP, "--site", inst.site, "--yes"
                )
            _remove_app_source(inst, required=False)
        _ensure_serving(inst.name)


def _read_common_config(inst) -> dict:
    code, out = harness.exec_in_frappe(inst.name, f"cat {inst.bench}/sites/common_site_config.json")
    assert code == 0, out
    return json.loads(out)


def _write_common_config(inst, config: dict) -> None:
    payload = shlex.quote(json.dumps(config))
    code, out = harness.exec_in_frappe(
        inst.name, f"echo {payload} > {inst.bench}/sites/common_site_config.json"
    )
    assert code == 0, out


@contextmanager
def _without_webserver_port(inst):
    """Temporarily remove ``webserver_port`` - a real, supported configuration a
    DNS-multitenant bench uses deliberately (many domain-named sites, where writing
    a port there breaks Host-based routing), restored unconditionally per the
    shared-instance convention."""
    original = _read_common_config(inst)
    assert "webserver_port" in original, "fixture bench already has no webserver_port"
    without_port = {k: v for k, v in original.items() if k != "webserver_port"}
    _write_common_config(inst, without_port)
    try:
        yield
    finally:
        _write_common_config(inst, original)


def test_apps_checkout_confirms_serving_when_webserver_port_is_absent(running_instance):
    """Regression for the reported false positive: a bench whose
    ``common_site_config.json`` OMITS ``webserver_port`` (a DNS-multitenant bench
    serving many domain-named sites removes it deliberately so Frappe routes purely
    by Host header) must not have its resync step fail with "the bench's assigned
    port could not be read" - the update itself succeeds and the real web process
    is still bound to a real port, which cwcli must determine from evidence rather
    than declaring the config unreadable.
    """
    inst = running_instance
    _ensure_serving(inst.name)
    _wait_supervised_stack(inst.name)
    code, out = harness.exec_in_frappe(
        inst.name, f"git -C {inst.bench}/apps/frappe branch --show-current"
    )
    assert code == 0, out
    branch = out.strip()

    with _without_webserver_port(inst):
        checkout = harness.run_cwcli("axi", "apps", "checkout", inst.name, "frappe", branch)

    # `axi apps checkout`'s exit code reads the report's `ok`, which is False iff
    # ANY step - including the shared resync - failed, so a 0 here already proves
    # the resync did not hit the false "assigned port could not be read" failure.
    assert checkout.returncode == 0, checkout.stdout + checkout.stderr
    assert "restart-processes" in checkout.stdout
    assert "assigned port could not be read" not in checkout.stdout
    assert _site_ping_code(inst) == "200"
