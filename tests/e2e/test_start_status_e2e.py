"""Real-Docker E2E net for the lifecycle commands: start / status / logs / restart.

This is PR 1 of the start/status rework (openspec change ``add-start-status-e2e-net``):
a behavioural net that lands BEFORE the core migration (``migrate-start-status-core``,
PR 2) so that migration is refactor-under-green from its first commit - the same
discipline the ``backup`` slice got from ``test_backup_e2e.py``.

STRUCTURE-AGNOSTIC ON PURPOSE. The migration deliberately CHANGES four observable
things - ``start`` idempotency, ``status`` output shape, ``start``'s multi-bench
policy, and the captured-log location. So this net pins only the invariants the
migration must PRESERVE and abstains from the mechanics it replaces:

  Pinned (preserved)                         | NOT pinned (PR 2 changes it)
  -------------------------------------------|-----------------------------------
  a started instance genuinely serves        | the ``/tmp/bench-<p>.log`` path
  re-running start leaves it serving         | the in-container process count /
                                             |   whether a 2nd honcho spawned; the
                                             |   exit code of a re-run (idempotency)
  ``status`` -> ``offline`` for a stopped inst | the exact ``online`` token for the
  (absent = a distinct non-zero NOT_FOUND)     |   containers-up-but-not-started state
  ``status`` reports running distinctly from   |   the single-curl probe internals
    the not-started state                      | -
  ``logs`` shows the bench stream            | that ``logs`` reads ``/tmp``
  ``restart`` recovers a reachable instance  | the ``stop`` + ``_start_project`` wiring
  honest exit codes                          | -

So the not-started state is asserted by its INVARIANT (containers up, site not
answering), never by string-matching ``online``; only ``offline`` and the running
state - contractually stable across the migration - are matched as tokens.

Two facts about TODAY's implementation shape these tests (verified against the
source + docker-py behaviour, since Docker is unavailable in the authoring env):

- ``cwcli init`` never runs ``bench start``; it only brings the containers up.
  So a freshly-init'd instance has containers up but honcho NOT serving :8000.
  A genuinely-serving instance is produced by ``stop`` -> ``start`` from a stopped
  state (which is exactly when ``_start_project`` runs ``bench start``).
- ``cwcli start`` on a container-UP instance self-conflicts on its own host ports
  (``_check_port_conflicts`` sees the ports held and no OTHER frappe project owns
  them) and exits non-zero WITHOUT re-running ``bench start``. That self-conflict /
  double-start behaviour is precisely the idempotency PR 2 fixes, so its EXIT CODE
  is a migration-transient mechanic this net must not pin - only reachability is.

Multi-bench (task 4.1): the harness has no multi-bench fixture, and standing one
up is an extra expensive ``cwcli init`` out of scope here. Multi-bench start/status
behaviour is covered by PR 2's new-behaviour tests, not this preserved net; any
multi-bench case exercised here would pass an explicit ``--bench <index|label>``
(never the ambiguous default, which is the exact policy PR 2 changes).
"""

from __future__ import annotations

import io
import subprocess

import pytest

from . import harness

pytestmark = pytest.mark.e2e


# --------------------------------------------------------------------------- #
# Serving helpers (the web server actually up on :8000, the real "serves" signal).
#
# ``harness.wait_for_site_ready`` only proves the DB is reachable (``bench
# list-apps``); it does NOT prove the web server is up. The lifecycle invariants
# here mean "genuinely serves", so we poll the web port exactly as ``cwcli status``
# does (a curl inside the container), not just DB readiness.
# --------------------------------------------------------------------------- #
def _web_reachable(project: str) -> bool:
    """True iff the frappe web server answers on :8000 inside the container.

    Mirrors ``status``'s probe: curl exits 0 when it connects to a live server
    (any HTTP code), non-zero when the port is not being served yet.
    """
    code, _ = harness.exec_in_frappe(
        project, "curl -s --max-time 5 -o /dev/null http://localhost:8000"
    )
    return code == 0


def _wait_web_ready(project: str, *, timeout: int = 300) -> None:
    harness.wait_until(
        lambda: _web_reachable(project),
        timeout=timeout,
        interval=5,
        desc=f"{project} web :8000 serving",
    )


def _ensure_serving(project: str) -> None:
    """Guarantee the web server is up on :8000, idempotently and order-independently.

    A no-op curl check when already serving (so only the FIRST serving test in the
    module pays the supervisor's boot cost); otherwise drives the real ``cwcli stop``
    -> ``cwcli start`` from-stopped path that actually launches the supervisor, then
    waits for the web port. Restores the running state so sibling tests sharing the
    session instance are undisturbed (mirrors ``running_instance``'s guarantee).
    """
    if _web_reachable(project):
        return
    harness.run_cwcli("stop", project)
    result = harness.run_cwcli("start", project, "--yes")
    assert result.returncode == 0, (
        f"`cwcli start {project}` from a stopped state should exit 0 and start "
        f"bench.\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    _wait_web_ready(project)


def _frappe_image(project: str) -> str:
    """The image the project's frappe container runs, for the conflict stub below.

    Reusing the live instance's own image guarantees it is present on the runner
    (no extra pull); falls back to the image the e2e.yml preflight pulls.
    """
    cid = harness.frappe_container_id(project)
    if cid:
        r = subprocess.run(
            ["docker", "inspect", "-f", "{{.Config.Image}}", cid],
            capture_output=True,
            text=True,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    return "frappe/bench:latest"


# --------------------------------------------------------------------------- #
# 1. start (both modes, real side effects)
# --------------------------------------------------------------------------- #
def test_start_noninteractive_from_stopped_serves(running_instance):
    """1.2 Non-interactive: ``cwcli start --yes`` on a STOPPED instance brings the
    containers + bench up and the site becomes genuinely reachable (real outcome,
    not a string match). En route it also proves ``status`` -> ``offline`` for a
    stopped instance (the stopped half of the offline contract)."""
    inst = running_instance

    stop = harness.run_cwcli("stop", inst.name)
    assert stop.returncode == 0, stop.stdout + stop.stderr

    # Stopped containers => offline (the "stopped" half of the offline contract).
    st = harness.run_cwcli("status", inst.name)
    assert st.returncode == 0, st.stdout + st.stderr
    assert st.stdout.strip() == "offline", st.stdout + st.stderr

    start = harness.run_cwcli("start", inst.name, "--yes")
    assert start.returncode == 0, start.stdout + start.stderr

    _wait_web_ready(inst.name)
    assert _web_reachable(inst.name), "site not serving after a from-stopped start"
    assert harness.frappe_container_id(inst.name) is not None


def test_status_is_running_immediately_after_start(running_instance):
    """1.2b Race closed: ``cwcli start`` must not return until the web server is
    actually serving, so a scripted ``cwcli start && cwcli status`` sees
    ``running`` - never a transient ``degraded`` (supervisord up but :8000 not
    yet bound). No ``_wait_web_ready`` between the two calls: that is the point.

    This is the regression the web-readiness wait fixes; before it, the tiny
    window between supervisord launch and ``bench serve`` binding :8000 made an
    immediate status read flake to ``degraded``."""
    inst = running_instance

    stop = harness.run_cwcli("stop", inst.name)
    assert stop.returncode == 0, stop.stdout + stop.stderr

    start = harness.run_cwcli("start", inst.name, "--yes")
    assert start.returncode == 0, start.stdout + start.stderr

    # Immediately, with NO readiness wait: start already blocked on the web.
    st = harness.run_cwcli("status", inst.name)
    assert st.returncode == 0, st.stdout + st.stderr
    assert st.stdout.strip() == "running", (
        "status must read 'running' the instant start returns (the web-readiness "
        f"wait should have closed the race).\n{st.stdout}\n{st.stderr}"
    )


def test_start_rerun_leaves_instance_serving(running_instance):
    """1.3 Re-run invariant: with the instance already serving, run ``cwcli start``
    again -> the site is STILL reachable afterward. Asserts ONLY health, never the
    in-container process count or the re-run's EXIT CODE: on today's code a re-run
    against a container-up instance self-conflicts on its own ports (exit non-zero,
    no second bench started), which is exactly the idempotency mechanic PR 2 fixes -
    so pinning it would be pinning a migration-transient. The preserved invariant is
    that the site keeps serving across a re-run attempt."""
    inst = running_instance
    _ensure_serving(inst.name)

    # Exit code intentionally NOT asserted (see docstring) - only the invariant.
    harness.run_cwcli("start", inst.name, "--yes")

    _wait_web_ready(inst.name)
    assert _web_reachable(inst.name), "re-running start must leave the site serving"


def test_start_nonexistent_fails_honestly():
    """1.4 Honest failure: starting a project that does not exist exits non-zero and
    never reports it as started."""
    ghost = harness.project_name("start-ghost")
    result = harness.run_cwcli("start", ghost, "--yes")
    assert result.returncode != 0, result.stdout + result.stderr
    assert "started" not in (result.stdout + result.stderr).lower(), result.stdout + result.stderr


def test_start_interactive_port_conflict_prompt_is_driven(running_instance):
    """1.5 Interactive: drive ``cwcli start`` through a real pty into its
    port-conflict confirmation prompt, await the ``ESC[?2004h`` raw-mode marker,
    answer it, and confirm the run completes - proving the prompt is genuinely
    SHOWN and ANSWERED, not skipped or auto-answered.

    Setup: the running session instance is the port HOLDER (its host web port is
    live). A throwaway, STOPPED stub compose project is created that publishes the
    same host port - ``docker create`` (not ``run``) records the binding without
    ever binding the live port, and docker-py's full-inspect ``list()`` exposes it
    to ``get_project_ports``, so ``cwcli start <stub>`` detects the conflict and
    prompts to stop the holder. We answer NO, which preserves the shared session
    instance (the isolation discipline) while still exercising the prompt end to
    end via the deterministic decline path. (Requires docker's default
    userland-proxy so the held host port reads as in-use.)
    """
    import pexpect

    inst = running_instance
    assert harness.frappe_container_id(inst.name) is not None, "holder must be up"

    stub = harness.project_name("startconflict-stub")
    image = _frappe_image(inst.name)
    created = subprocess.run(
        [
            "docker",
            "create",
            "--name",
            stub,
            "--label",
            f"com.docker.compose.project={stub}",
            "--label",
            "com.docker.compose.service=frappe",
            "-p",
            f"{inst.port}:8000",
            image,
            "sleep",
            "3600",
        ],
        capture_output=True,
        text=True,
    )
    assert created.returncode == 0, f"could not create conflict stub: {created.stderr}"

    try:
        child = harness.spawn_cwcli(["start", stub], timeout=300)
        log = io.StringIO()
        child.logfile_read = log
        try:
            harness.expect_prompt_ready(child)  # the confirm's ESC[?2004h marker
            child.sendline("n")  # decline: do NOT stop the shared holder
            child.expect(pexpect.EOF, timeout=180)
        finally:
            child.close(force=True)

        out = harness.strip_ansi(log.getvalue())
        # The prompt was genuinely rendered and consumed (not auto-answered):
        assert "Stop project" in out, out[-1500:]
        # Declining refuses the start for the stub (its deterministic outcome):
        assert ("Cannot start" in out) or ("Skipping project" in out), out[-1500:]
        # The real holder was NOT disturbed by the declined start:
        assert harness.frappe_container_id(inst.name) is not None
    finally:
        subprocess.run(["docker", "rm", "-f", stub], capture_output=True)


# --------------------------------------------------------------------------- #
# 2. status (lifecycle-state discrimination)
# --------------------------------------------------------------------------- #
def test_status_nonexistent_fails_honestly():
    """2.1 A truly-nonexistent project (never created - no containers at all) is NOT
    ``offline``: it exits non-zero with a "no such project" error, distinct from a
    real-but-stopped instance (which stays ``offline``/exit 0, covered in 1.2)."""
    ghost = harness.project_name("status-ghost")
    result = harness.run_cwcli("status", ghost)
    assert result.returncode != 0, result.stdout + result.stderr
    combined = (result.stdout + result.stderr).lower()
    assert "no such project" in combined, result.stdout + result.stderr
    # Never the misleading offline token on stdout for a name that doesn't exist.
    assert result.stdout.strip() != "offline", result.stdout + result.stderr


def test_status_reports_running_when_serving(running_instance):
    """2.2 Running state: when the site answers, ``status`` reports the running
    state and exits 0. ``running`` is a token contractually stable across the
    migration, so it is matched directly."""
    inst = running_instance
    _ensure_serving(inst.name)

    result = harness.run_cwcli("status", inst.name)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "running", result.stdout + result.stderr


# --------------------------------------------------------------------------- #
# 3. logs + restart
# --------------------------------------------------------------------------- #
def test_logs_shows_bench_stream(running_instance):
    """3.1 ``cwcli logs --no-follow`` on a started instance surfaces the bench's
    captured output. Asserted by content/exit, NOT by the ``/tmp/bench-<p>.log``
    path (PR 2 relocates it). Driven through a pty because ``logs`` runs
    ``docker exec -it`` (needs a TTY); on a serving instance ``logs`` shows the log
    with no prompt."""
    import pexpect

    inst = running_instance
    _ensure_serving(inst.name)

    child = harness.spawn_cwcli(["logs", inst.name, "--no-follow"], timeout=300)
    log = io.StringIO()
    child.logfile_read = log
    try:
        child.expect(pexpect.EOF, timeout=180)
    finally:
        child.close(force=True)

    out = harness.strip_ansi(log.getvalue())
    # The header only prints once at least one per-process log FILE was found and is
    # being tailed - if none existed, logs errors out before this line.
    assert f"Viewing bench logs for '{inst.name}'" in out, out[-1500:]
    # Real per-process content: the combined view tails each program's supervisord
    # log file, so at least one well-known process/label appears (in a file header
    # and/or its output).
    assert any(
        tok in out for tok in ("web", "redis", "watch", "schedule", "worker", "socketio")
    ), out[-1500:]
    assert child.exitstatus == 0, f"logs --no-follow should exit 0; out:\n{out[-1500:]}"


def test_restart_recovers_reachable_instance(running_instance):
    """3.2 ``cwcli restart`` stops then restarts the instance and the site is
    reachable again afterward (via the real web probe). ``restart`` runs
    ``_start_project`` unconditionally after ``stop`` (no port self-conflict), so it
    exits 0 and re-serves."""
    inst = running_instance

    result = harness.run_cwcli("restart", inst.name)
    assert result.returncode == 0, result.stdout + result.stderr

    _wait_web_ready(inst.name)
    assert _web_reachable(inst.name), "site not reachable again after restart"
    assert harness.frappe_container_id(inst.name) is not None


# Task 2.3 lives here (after the other serving tests) on purpose: it STOPS the
# supervisor, so running it last among serving tests avoids forcing a re-serve (an
# extra supervisor boot) on the CI leg's clock. ``_ensure_serving`` keeps it correct
# in any order.
def test_status_containers_up_bench_down_is_distinct_from_running(running_instance):
    """2.3 Not-started state is DISTINCT from running: with the containers up but
    the supervisor stopped, ``status`` reports neither ``running`` nor ``offline``
    and exits 0. Asserted by the INVARIANT (containers up + site not answering), NOT
    by string-matching the transitional token that the migration may enrich toward
    an aggregate. Leaves the instance un-served but containers up (a valid state;
    nothing later in the session needs it serving)."""
    inst = running_instance
    _ensure_serving(inst.name)  # supervisor up first, so there is something to stop

    # Stop the supervisor itself (NOT an individual program: supervisord would just
    # auto-restart a killed ``bench serve``). SIGTERM to supervisord shuts its whole
    # program group down; then wait until :8000 genuinely stops answering. This only
    # CREATES the state under test; nothing here is asserted.
    harness.exec_in_frappe(inst.name, "pkill -TERM -f supervisord || true")
    harness.wait_until(
        lambda: not _web_reachable(inst.name),
        timeout=120,
        interval=3,
        desc=f"{inst.name} web :8000 stopped after supervisor teardown",
    )

    result = harness.run_cwcli("status", inst.name)
    assert result.returncode == 0, result.stdout + result.stderr
    token = result.stdout.strip()
    assert token not in ("running", "offline"), (
        f"containers-up-but-bench-down must be distinct from running AND offline; " f"got {token!r}"
    )
    # The invariant that DEFINES this state (never a string match on the token):
    assert harness.frappe_container_id(inst.name) is not None, "containers must be up"
    assert not _web_reachable(inst.name), "site must not be answering in this state"


def test_restart_nonexistent_fails_honestly():
    """3.3 Honest failure: restarting a project that does not exist exits non-zero
    and never reports it as started."""
    ghost = harness.project_name("restart-ghost")
    result = harness.run_cwcli("restart", ghost)
    assert result.returncode != 0, result.stdout + result.stderr
    assert f"Instance '{ghost}' started." not in (result.stdout + result.stderr), (
        result.stdout + result.stderr
    )
