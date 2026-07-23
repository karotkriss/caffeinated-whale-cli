"""Real-Docker E2E for the NEW start/status behavior (migrate-start-status-core),
updated for the supervisord per-process supervisor (add-per-process-supervisor).

These sit ON TOP of the structure-agnostic PR-1 net (``test_start_status_e2e.py``,
which stays green unchanged) and assert the migration's ADDED behavior - the
things the net deliberately does NOT pin:

  - ``start`` is genuinely idempotent: a re-run is a no-op that leaves EXACTLY ONE
    supervisord supervisor (the double-start bug stays fixed), reported as
    ``already_running: true`` by ``cwcli axi start``.
  - ``status`` reports REAL per-process health (up/uptime/CPU/RSS/state) + the
    aggregate, via both the human token (stdout) and ``cwcli axi status`` TOON.
  - ``degraded``: with the marker present but the supervisor down, status says
    ``degraded``.
  - the captured logs are per-process files on the bench ``logs/`` volume (not
    ``/tmp``, and no combined ``bench-start.log``).
  - multi-bench DOCUMENT STRUCTURE ONLY (see the caveat below): ``start`` still
    REFUSES with no selector (it mutates one bench, so it must be told which), while
    ``status`` reports every bench in one document, each named, exit 0
    (report-status-per-bench). A bench whose config names no port reports it unknown
    rather than assuming 8000.

**The multi-bench test here is structural and says so.** Its second bench is a
DIRECTORY SKELETON - ``apps/``, ``sites/`` and an empty ``common_site_config.json``,
with no virtualenv, no Procfile, no supervisord and nothing bound to a port. That is
enough to pin the document shape, the surviving ``start`` refusal, and the
unresolved-port path, and it is enough for NOTHING about the web probe: the defects
that motivated the per-bench report (a healthy bench past the first reading
``degraded``, a dead one reading its neighbour's HTTP code, ``start`` waiting on the
wrong port) only exist when a second bench is genuinely answering on its own port.
Those live in ``test_multibench_serving_e2e.py``, which builds a real second bench.
Do not read a green run of this module as covering them.

Runs on the shared session instance; every test that leaves it un-served relies on
``_ensure_serving`` (imported from the net module) to restore sibling tests.
"""

from __future__ import annotations

import io

import pytest

from . import harness
from .test_start_status_e2e import _ensure_serving, _wait_web_ready, _web_reachable

pytestmark = pytest.mark.e2e


def _supervisord_count(project: str) -> int:
    """How many supervisord supervisors are live in the container (double-start guard)."""
    code, out = harness.exec_in_frappe(project, "ps -eo args | grep -c '[s]upervisord' || true")
    if code != 0:
        return -1
    try:
        return int(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return -1


# --------------------------------------------------------------------------- #
# 1. idempotent start - exactly one supervisor, reported as already_running
# --------------------------------------------------------------------------- #
def test_start_rerun_is_idempotent_single_supervisor(running_instance):
    inst = running_instance
    _ensure_serving(inst.name)
    assert _supervisord_count(inst.name) == 1, "setup: exactly one supervisord after serving"

    # axi start on an already-running bench is a clean no-op (exit 0, TOON, no prompt).
    res = harness.run_cwcli("axi", "start", inst.name, "--yes")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "already_running: true" in res.stdout, res.stdout
    assert "supervisor: supervisord" in res.stdout, res.stdout
    # stdout stays TOON-clean (no progress chatter).
    assert "Starting" not in res.stdout

    # The load-bearing invariant: NO second supervisord was spawned.
    assert _supervisord_count(inst.name) == 1, "a re-run must not spawn a second supervisord"
    assert _web_reachable(inst.name), "the instance is still serving after the no-op"


# --------------------------------------------------------------------------- #
# 2. status reports real per-process health
# --------------------------------------------------------------------------- #
def test_axi_status_reports_per_process_health(running_instance):
    inst = running_instance
    _ensure_serving(inst.name)

    res = harness.run_cwcli("axi", "status", inst.name)
    assert res.returncode == 0, res.stdout + res.stderr
    lines = res.stdout.splitlines()
    # overall aggregate leads the TOON document.
    assert lines[0] == "overall: running", res.stdout
    # Uniform shape: the per-bench list, always, even for one bench.
    assert any(line.startswith("benches[") for line in lines), res.stdout
    # A real per-process table with resource columns, now NESTED under its bench (so
    # it is indented, not at column 0), and at least the web row up.
    assert any(
        line.lstrip().startswith("processes[") and "uptime_s" in line for line in lines
    ), res.stdout
    assert "supervisor_up: true" in res.stdout
    # The bench names itself and names the port its HTTP code was measured on - the
    # whole point: an unattributed web_http_code is how one bench's answer stood in
    # for another's.
    assert "bench_path: " in res.stdout
    assert "web_port_verified: true" in res.stdout


def test_human_status_running_token_with_detail_on_stderr(running_instance):
    inst = running_instance
    _ensure_serving(inst.name)

    res = harness.run_cwcli("status", inst.name, "-v")
    assert res.returncode == 0, res.stdout + res.stderr
    # stdout is ONLY the aggregate token (the PR-1 net contract, preserved).
    assert res.stdout.strip() == "running", res.stdout
    # the per-process breakdown is on stderr.
    assert any(tok in res.stderr for tok in ("web", "redis", "worker", "schedule")), res.stderr


# --------------------------------------------------------------------------- #
# 3. degraded when the supervisor is down but the marker is present
# --------------------------------------------------------------------------- #
def test_status_degraded_when_supervisor_down(running_instance):
    inst = running_instance
    _ensure_serving(inst.name)  # writes the supervisor marker

    # Kill supervisord itself (it shuts its program group down); the marker stays.
    # (Killing one program would auto-restart, so target the supervisor.)
    harness.exec_in_frappe(inst.name, "pkill -TERM -f '[s]upervisord' || true")
    harness.wait_until(
        lambda: not _web_reachable(inst.name),
        timeout=120,
        interval=3,
        desc=f"{inst.name} supervisord down",
    )
    # `web` unreachable only means ITS child died; supervisord's own master
    # process (what `supervisor_up` actually reads via `ps`) keeps running while
    # it drains the other Procfile programs, and its control socket can already be
    # gone by then too (the `state` reads null above). Wait for the master itself
    # to actually exit, or the status assertions below race a mid-shutdown
    # supervisord that still shows up in `ps`.
    harness.wait_until(
        lambda: _supervisord_count(inst.name) == 0,
        timeout=60,
        interval=2,
        desc=f"{inst.name} supervisord master process exited",
    )

    res = harness.run_cwcli("status", inst.name)
    assert res.returncode == 0, res.stdout + res.stderr
    assert res.stdout.strip() == "degraded", res.stdout

    axi = harness.run_cwcli("axi", "status", inst.name)
    assert axi.stdout.splitlines()[0] == "overall: degraded", axi.stdout
    assert "supervisor_up: false" in axi.stdout


# --------------------------------------------------------------------------- #
# 4. logs are per-process files on the workspace volume, not /tmp or a combined log
# --------------------------------------------------------------------------- #
def test_logs_are_per_process_files_on_the_volume(running_instance):
    inst = running_instance
    _ensure_serving(inst.name)

    bench = harness.DEFAULT_BENCH_PATH
    # supervisord writes one <program>.supervisor.log per Procfile program.
    code, _ = harness.exec_in_frappe(inst.name, f"test -f {bench}/logs/web.supervisor.log")
    assert code == 0, f"web's per-process log must exist at {bench}/logs/web.supervisor.log"
    # The old honcho combined log and the ephemeral /tmp path are no longer used.
    code_combined, _ = harness.exec_in_frappe(inst.name, f"test -f {bench}/logs/bench-start.log")
    assert code_combined != 0, "the old combined bench-start.log must no longer be written"
    code_tmp, _ = harness.exec_in_frappe(inst.name, f"test -f /tmp/bench-{inst.name}.log")
    assert code_tmp != 0, "the old /tmp/bench-<project>.log must no longer be written"

    # cwcli logs --process tails one program's file and shows real content.
    import pexpect

    child = harness.spawn_cwcli(["logs", inst.name, "--process", "web", "--no-follow"], timeout=300)
    log = io.StringIO()
    child.logfile_read = log
    try:
        child.expect(pexpect.EOF, timeout=180)
    finally:
        child.close(force=True)
    out = harness.strip_ansi(log.getvalue())
    assert f"Viewing bench logs for '{inst.name}'" in out, out[-1500:]


# --------------------------------------------------------------------------- #
# 5. multi-bench DOCUMENT STRUCTURE over a bench SKELETON (not a serving bench)
# --------------------------------------------------------------------------- #
def test_multibench_document_structure_over_a_bench_skeleton(running_instance):
    """Structure only: the shape of the multi-bench document, the surviving
    ``start`` refusal, and the unresolved-port path.

    The second bench here is a directory skeleton with nothing running in it, so
    this test says nothing about the per-bench web probe - by construction, since
    a skeleton binds no port for a probe to read. The runtime behaviour (F3/F4/F5)
    is proven against two genuinely serving benches in
    ``test_multibench_serving_e2e.py``.
    """
    inst = running_instance
    _ensure_serving(inst.name)
    second = "/workspace/cwe2e-second-bench"

    # A cheap SECOND bench skeleton that inspect's detector recognizes
    # (apps/ + sites/ + sites/common_site_config.json). inspect discovers benches
    # ONLY under registered search roots (`cwcli init` self-registers each bench's
    # path via `config add-path`; there is no broad filesystem scan), so a bare
    # mkdir is invisible - register the skeleton's path so `cwcli inspect` finds it
    # and the cache reports two benches.
    harness.exec_in_frappe(
        inst.name,
        f"mkdir -p {second}/apps {second}/sites && echo '{{}}' > {second}/sites/common_site_config.json",
    )
    try:
        reg = harness.run_cwcli("config", "add-path", second)
        assert reg.returncode == 0, reg.stdout + reg.stderr

        # Populate the cache with BOTH benches. Force a full re-scan (--update):
        # a plain inspect only re-verifies benches ALREADY in the cache (Tier 2
        # partial refresh) and never re-discovers a brand-new bench unless that
        # forces drift on a known bench - by the time this test runs, the shared
        # instance's cache may already hold the original bench from an earlier
        # test, and that bench hasn't drifted, so a plain inspect would silently
        # keep serving the stale single-bench cache.
        insp = harness.run_cwcli("inspect", inst.name, "--update")
        assert insp.returncode == 0, insp.stdout + insp.stderr

        # axi start with no --bench: a usage error naming --bench, exit 2, never
        # prompts. UNCHANGED - start MUTATES one bench, so it must be told which.
        res = harness.run_cwcli("axi", "start", inst.name, "--yes")
        assert res.returncode == 2, res.stdout + res.stderr
        assert "--bench" in res.stdout, res.stdout

        # status is a READ, and it no longer refuses: the bare form reports every
        # bench in ONE document, each named, exit 0. It is non-prompting on every
        # path now, so TTY and non-TTY behave identically here.
        axi = harness.run_cwcli("axi", "status", inst.name)
        assert axi.returncode == 0, axi.stdout + axi.stderr
        assert "benches[2]:" in axi.stdout, axi.stdout
        assert second in axi.stdout, axi.stdout

        # The skeleton bench's common_site_config.json is `{}` - it parses, but it
        # names no webserver_port. That is EXACTLY the defaulted-port hole: cwcli
        # must report the port unknown rather than assume Frappe's 8000 and probe
        # the REAL bench's server while claiming to describe this one.
        skeleton = axi.stdout.split(second, 1)[1]
        assert "web_port: null" in skeleton, axi.stdout
        assert "web_port_verified: false" in skeleton, axi.stdout

        # Human status: still exactly one token on stdout (the instance fold), with
        # the per-bench detail on stderr. Exit 0, no prompt, from a non-TTY.
        st = harness.run_cwcli("status", inst.name)
        assert st.returncode == 0, st.stdout + st.stderr
        assert st.stdout.strip() in ("running", "degraded", "online"), st.stdout
        assert len(st.stdout.split()) == 1, st.stdout

        # --bench still answers for exactly one bench, unchanged.
        one = harness.run_cwcli("axi", "status", inst.name, "--bench", "0")
        assert one.returncode == 0, one.stdout + one.stderr
        assert "benches[1]:" in one.stdout, one.stdout
    finally:
        # Restore the single-bench state so sibling tests are undisturbed: drop the
        # registered path + the skeleton, then re-inspect back to one bench.
        harness.run_cwcli("config", "remove-path", second)
        harness.exec_in_frappe(inst.name, f"rm -rf {second}")
        harness.run_cwcli("inspect", inst.name)
        # Make sure the shared instance is still serving for whatever runs next.
        _ensure_serving(inst.name)
        _wait_web_ready(inst.name)
