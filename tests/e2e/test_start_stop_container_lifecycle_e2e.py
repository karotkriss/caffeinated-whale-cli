"""Committed real-Docker E2E for three start/stop container-lifecycle defects.

1. ``cwcli start`` on a project whose frappe container exits right after it is
   started (a devcontainer-style instance whose PID 1 is ``bench start`` and dies,
   e.g. on a ``logs/bench.log`` PermissionError) used to crash with a Docker ``409
   Conflict ... is not running`` traceback out of ``supervision._ps_rows``. It must
   report a clean ``frappe container exited`` error naming ``docker logs``.
2. Shared-mode ``align_container_user_to_host`` edited ``/etc/passwd`` BEFORE
   re-owning the bench. On an instance whose PID 1 runs as ``frappe`` the edit
   kills PID 1, the container exits, the exec dies with it, and the bench is never
   re-owned - so every later start dies on the same PermissionError (the staging
   brick of 2026-09-29). The re-own must come first, and PID 1 must be frozen for
   the whole remap so it cannot die mid-chown either, so the remap always lands
   whole and the container restarts.
3. ``cwcli stop`` stopped MariaDB with the daemon's default grace (1s on Docker
   Desktop, 10s elsewhere), so a DB still flushing InnoDB was SIGKILLed while cwcli
   reported success. The DB must get a real grace period, and a DB that exited
   non-zero (137: killed) must never read as a successful stop.

Each is reproduced with REAL containers (no Frappe bench needed): lightweight
``python:3.12-slim`` stand-ins carry the compose labels cwcli discovers projects
by, and the stop tests run the real ``mariadb`` image. MariaDB's slow shutdown is
made deterministic by freezing its PID 1 with SIGSTOP (delivered from the host, so
the namespace-init signal exemption does not apply): the SIGTERM ``docker stop``
sends stays pending until SIGCONT, exactly like a shutdown that needs longer than
the grace period.

Marked `standalone`: it never touches the shared session instance.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time

import pytest
from docker.errors import APIError

from . import harness

pytestmark = [pytest.mark.e2e, pytest.mark.standalone]

_SLIM = "python:3.12-slim"
_MARIADB = "mariadb:11.8"
_BENCH = harness.DEFAULT_BENCH_PATH  # where `cwcli start` looks with nothing cached
_OLD_UID = 54321  # the "pre-shared" uid the devcontainer workspace was built under


def _docker():
    import docker

    return docker.from_env()


def _labels(project: str, service: str) -> dict[str, str]:
    return {
        "com.docker.compose.project": project,
        "com.docker.compose.service": service,
        "com.docker.compose.container-number": "1",
    }


def _create(project: str, service: str, image: str, command, **kwargs):
    harness.assert_prefixed(project)
    return _docker().containers.create(
        image,
        command,
        name=f"{project}-{service}-1",
        labels=_labels(project, service),
        **kwargs,
    )


def _output(proc) -> str:
    return harness.strip_ansi((proc.stdout or "") + (proc.stderr or ""))


# --------------------------------------------------------------------------- #
# 1. start on a frappe container that exits right away
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("surface", ["human", "axi"])
def test_start_reports_an_exited_frappe_container_cleanly(surface):
    project = harness.project_name(f"exit-{surface}")
    frappe = _create(
        project,
        "frappe",
        _SLIM,
        [
            "sh",
            "-c",
            f'echo "PermissionError: [Errno 13] Permission denied: '
            f"'{_BENCH}/logs/bench.log'\" >&2; exit 1",
        ],
    )
    try:
        args = ["start", project] if surface == "human" else ["axi", "start", project]
        proc = harness.run_cwcli(*args, timeout=300)
        out = _output(proc)
        assert proc.returncode != 0, f"a start whose frappe container died exited 0:\n{out}"
        assert (
            "Traceback" not in out and "409" not in out
        ), f"start leaked a Docker traceback instead of a clean error:\n{out}"
        assert "exited" in out and f"docker logs {frappe.name}" in out, out
    finally:
        harness.sweep_cwe2e(only=project)


# --------------------------------------------------------------------------- #
# 2. shared-mode uid remap on a devcontainer-style instance (PID 1 runs as frappe)
# --------------------------------------------------------------------------- #
# PID 1 provisions a `frappe` user + bench at the pre-shared uid on first boot,
# then drops to `frappe` and behaves like `bench start`: it opens logs/bench.log
# (a PermissionError exits 1, as bench's setup_logging does) and dies the moment
# its uid loses its passwd row (pymysql's getpass `No username set`). Lots of small
# files under the home cache make align's home re-own take long enough that a
# passwd edit placed BEFORE the bench re-own is always interrupted first.
_DEVCONTAINER_PID1 = f"""
set -e
if ! id frappe >/dev/null 2>&1; then
  groupadd -g {_OLD_UID} frappe
  useradd -u {_OLD_UID} -g {_OLD_UID} -m -d /home/frappe frappe
  mkdir -p {_BENCH}/logs /home/frappe/.cache/pad
  python3 -c "
import os
for i in range(20000):
    open(f'/home/frappe/.cache/pad/{{i}}', 'w').close()
"
  chown -R {_OLD_UID}:{_OLD_UID} /home/frappe {_BENCH}
fi
exec setpriv --reuid="$(id -u frappe)" --regid="$(id -g frappe)" --clear-groups \\
  python3 -c "
import os, pwd, sys, time
try:
    log = open('{_BENCH}/logs/bench.log', 'a')
except PermissionError as e:
    print('PermissionError:', e, file=sys.stderr, flush=True)
    sys.exit(1)
log.write('bench started\\n'); log.flush()
print('BENCH_UP', flush=True)
while True:
    try:
        pwd.getpwuid(os.getuid())
    except KeyError:
        print('OSError: No username set in the environment', file=sys.stderr, flush=True)
        sys.exit(1)
    time.sleep(0.01)
"
"""


def _wait_logs(container, needle: str, timeout: float = 120) -> None:
    def seen() -> bool:
        return needle in container.logs().decode("utf-8", "replace")

    harness.wait_until(seen, timeout=timeout, interval=0.5, desc=f"'{needle}' in logs")


def _shared_start(project: str, marker) -> subprocess.CompletedProcess:
    env_before = os.environ.get("CWCLI_SHARED_MARKER")
    os.environ["CWCLI_SHARED_MARKER"] = str(marker)
    try:
        return harness.run_cwcli("start", project, timeout=300)
    finally:
        if env_before is None:
            os.environ.pop("CWCLI_SHARED_MARKER", None)
        else:
            os.environ["CWCLI_SHARED_MARKER"] = env_before


def _shared_start_in_background(project: str, marker) -> subprocess.Popen:
    env = os.environ.copy()
    env["CWCLI_SHARED_MARKER"] = str(marker)
    return subprocess.Popen(
        [harness.CWCLI, "start", project],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def test_shared_mode_remap_interrupted_by_container_exit_stays_restartable(tmp_path):
    marker = tmp_path / "shared.toml"
    marker.write_text("enabled = true\n")
    host_uid = os.getuid()  # no `cwcli` service account here, so shared mode targets it
    assert host_uid != _OLD_UID

    project = harness.project_name("remap")
    frappe = _create(project, "frappe", _SLIM, ["bash", "-c", _DEVCONTAINER_PID1])
    try:
        frappe.start()
        _wait_logs(frappe, "BENCH_UP")
        frappe.stop(timeout=1)  # the instance is now stopped, as after a host restart

        # The user-facing path: a shared-mode `cwcli start` remaps `frappe` out from
        # under the live PID 1, which kills the container mid-align.
        proc = _shared_start(project, marker)
        out = _output(proc)
        frappe.reload()
        assert frappe.status != "running", "the remap was expected to kill PID 1"

        # The interrupted remap must still leave a restartable instance: the bench
        # was re-owned BEFORE the passwd edit, so PID 1 (now the remapped `frappe`)
        # can write logs/bench.log on the next start.
        frappe.start()
        time.sleep(3)
        frappe.reload()
        logs = frappe.logs().decode("utf-8", "replace")
        assert (
            frappe.status == "running"
        ), f"the instance is bricked after an interrupted remap:\n{logs[-2000:]}"
        code, owner = frappe.exec_run(["stat", "-c", "%u", f"{_BENCH}/logs/bench.log"])
        assert code == 0 and owner.decode().strip() == str(host_uid), owner
        # ...and the start that died with it said so cleanly.
        assert proc.returncode != 0, out
        assert "Traceback" not in out and "exited" in out, f"start was not clean:\n{out}"
    finally:
        harness.sweep_cwe2e(only=project)


# PID 1 here re-opens a probe file in each of 50 padded bench dirs every 10ms and
# exits on the first PermissionError, like a bench whose processes keep opening
# log files. `chown -R <bench>` re-owns those dirs one after another, so PID 1 dies
# as soon as the first probe changes hands - mid-chown, with the other dirs, the
# groupmod, and the passwd edit still queued - unless the remap freezes it.
_PROBE_DIRS = 50
_MID_CHOWN_PID1 = f"""
set -e
if ! id frappe >/dev/null 2>&1; then
  groupadd -g {_OLD_UID} frappe
  useradd -u {_OLD_UID} -g {_OLD_UID} -m -d /home/frappe frappe
  mkdir -p {_BENCH}/logs
  python3 -c "
import os
for d in range({_PROBE_DIRS}):
    os.makedirs(f'{_BENCH}/d{{d}}')
    for i in range(400):
        open(f'{_BENCH}/d{{d}}/{{i}}', 'w').close()
"
  chown -R {_OLD_UID}:{_OLD_UID} /home/frappe {_BENCH}
fi
exec setpriv --reuid="$(id -u frappe)" --regid="$(id -g frappe)" --clear-groups \\
  python3 -c "
import sys, time
print('BENCH_UP', flush=True)
while True:
    for d in range({_PROBE_DIRS}):
        try:
            open(f'{_BENCH}/d{{d}}/0', 'a').close()
        except PermissionError as e:
            print('PermissionError:', e, file=sys.stderr, flush=True)
            sys.exit(1)
    time.sleep(0.01)
"
"""


def test_shared_mode_remap_cannot_be_cut_short_mid_chown(tmp_path):
    marker = tmp_path / "shared.toml"
    marker.write_text("enabled = true\n")
    host_uid = os.getuid()
    assert host_uid != _OLD_UID

    project = harness.project_name("remapchown")
    frappe = _create(project, "frappe", _SLIM, ["bash", "-c", _MID_CHOWN_PID1])
    try:
        frappe.start()
        _wait_logs(frappe, "BENCH_UP", timeout=300)
        frappe.stop(timeout=1)

        first = _output(_shared_start(project, marker))
        assert "Traceback" not in first, first
        assert "boot it remapped" in " ".join(first.split()), first

        # The remap landed whole, so the next start boots PID 1 as the remapped
        # `frappe` against a bench it owns - instead of dying on boot for good.
        second = _output(_shared_start(project, marker))
        time.sleep(3)
        frappe.reload()
        logs = frappe.logs().decode("utf-8", "replace")
        assert frappe.status == "running", (
            f"the instance is bricked after a remap cut short mid-chown:\n{second}\n"
            f"{logs[-2000:]}"
        )
        assert "exited (exit code" not in second, second
        code, strays = frappe.exec_run(["find", _BENCH, "!", "-user", str(host_uid)])
        assert code == 0 and strays.decode().strip() == "", strays.decode()[:2000]
    finally:
        harness.sweep_cwe2e(only=project)


def test_shared_mode_remap_survives_client_interrupt_mid_chown(tmp_path):
    marker = tmp_path / "shared.toml"
    marker.write_text("enabled = true\n")
    host_uid = os.getuid()
    assert host_uid != _OLD_UID

    project = harness.project_name("remapsignal")
    frappe = _create(project, "frappe", _SLIM, ["bash", "-c", _MID_CHOWN_PID1])
    try:
        frappe.start()
        _wait_logs(frappe, "BENCH_UP", timeout=300)
        chown_marker = "/tmp/cwcli-bench-chown-running"
        chown_wrapper = f"""#!/bin/sh
for last do :; done
if [ "$1" = "-R" ] && [ "$last" = "{_BENCH}" ]; then
  touch {chown_marker}
  sleep 30
fi
exec /usr/bin/chown "$@"
"""
        code, out = frappe.exec_run(
            [
                "python3",
                "-c",
                "import os, pathlib; "
                f"p = pathlib.Path('/usr/local/bin/chown'); p.write_text({chown_wrapper!r}); "
                "os.chmod(p, 0o755)",
            ],
            user="root",
        )
        assert code == 0, out
        frappe.stop(timeout=1)

        child = _shared_start_in_background(project, marker)

        def chown_is_in_progress() -> bool:
            frappe.reload()
            if frappe.status != "running":
                return False
            code, _ = frappe.exec_run(["test", "-f", chown_marker])
            return code == 0

        harness.wait_until(
            chown_is_in_progress,
            timeout=300,
            interval=0.01,
            desc="shared bench chown in progress",
        )
        child.send_signal(signal.SIGTERM)
        stdout, stderr = child.communicate(timeout=60)
        interrupted = harness.strip_ansi(stdout + stderr)
        assert child.returncode != 0, interrupted

        def remap_finished() -> bool:
            frappe.reload()
            return frappe.status != "running"

        harness.wait_until(remap_finished, timeout=300, interval=0.1, desc="remap completion")

        restarted = _output(_shared_start(project, marker))
        time.sleep(3)
        frappe.reload()
        logs = frappe.logs().decode("utf-8", "replace")
        assert frappe.status == "running", (
            f"the instance is bricked after the client interrupted the remap:\n{restarted}\n"
            f"{logs[-2000:]}"
        )
        code, strays = frappe.exec_run(["find", _BENCH, "!", "-user", str(host_uid)])
        assert code == 0 and strays.decode().strip() == "", strays.decode()[:2000]
    finally:
        harness.sweep_cwe2e(only=project)


# --------------------------------------------------------------------------- #
# 3. stop gives the database a real grace period, and never lies about it
# --------------------------------------------------------------------------- #
def _db_project(suffix: str):
    project = harness.project_name(suffix)
    db = _create(
        project,
        "mariadb",
        _MARIADB,
        None,
        environment={"MARIADB_ROOT_PASSWORD": "123"},
    )
    frappe = _create(project, "frappe", _SLIM, ["sleep", "infinity"])
    db.start()
    frappe.start()
    # The entrypoint's first-boot init server listens on no port; the real one
    # announces `port: 3306`.
    _wait_logs(db, "port: 3306", timeout=240)
    return project, db


def _frozen_until_sigterm(db) -> None:
    """Freeze MariaDB, then block until ``docker stop``'s SIGTERM is pending on it."""
    db.kill(signal="SIGSTOP")

    def sigterm_pending() -> bool:
        db.reload()
        if db.status != "running":  # already gone (killed by a too-short grace)
            return True
        code, out = db.exec_run(["grep", "-E", "^(SigPnd|ShdPnd):", "/proc/1/status"])
        if code != 0:
            return False
        return any(int(line.split()[1], 16) & (1 << 14) for line in out.decode().splitlines())

    harness.wait_until(sigterm_pending, timeout=120, interval=0.2, desc="SIGTERM pending on DB")


def _run_stop_in_background(args: list[str]) -> subprocess.Popen:
    return subprocess.Popen(
        [harness.CWCLI, *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=os.environ.copy(),
    )


def test_stop_waits_for_a_slow_database_shutdown():
    project, db = _db_project("dbslow")
    try:
        started = time.time()
        db.kill(signal="SIGSTOP")
        child = _run_stop_in_background(["stop", project])
        _frozen_until_sigterm(db)
        # A shutdown that needs 15s: longer than Docker's default grace on every
        # daemon (1s on Docker Desktop, 10s elsewhere), well inside cwcli's.
        time.sleep(15)
        try:
            db.kill(signal="SIGCONT")
        except APIError:  # already killed by a too-short grace: the bug under test
            pass
        stdout, stderr = child.communicate(timeout=300)
        out = harness.strip_ansi(stdout + stderr)
        db.reload()
        tail = db.logs(since=int(started) - 1).decode("utf-8", "replace")
        assert db.attrs["State"]["ExitCode"] == 0, (
            f"MariaDB was killed mid-shutdown (exit {db.attrs['State']['ExitCode']}); "
            f"cwcli said:\n{out}\nDB log:\n{tail[-2000:]}"
        )
        assert "Shutdown complete" in tail, tail[-2000:]
        assert child.returncode == 0, out
    finally:
        harness.sweep_cwe2e(only=project)


@pytest.mark.parametrize("surface", ["human", "axi"])
def test_stop_never_reports_success_for_a_killed_database(surface):
    project, db = _db_project(f"dbkill-{surface}")
    try:
        args = ["stop", project] if surface == "human" else ["axi", "stop", project]
        db.kill(signal="SIGSTOP")
        child = _run_stop_in_background(args)
        _frozen_until_sigterm(db)
        # The DB dies without ever finishing its shutdown (an OOM kill, a host
        # stop, or a grace period that ran out all look like this to cwcli).
        db.kill(signal="SIGKILL")
        stdout, stderr = child.communicate(timeout=300)
        out = harness.strip_ansi(stdout + stderr)
        db.reload()
        assert db.attrs["State"]["ExitCode"] != 0  # precondition: it really was killed
        assert child.returncode != 0, f"stop reported success for a killed database:\n{out}"
        assert "exited with code 137" in " ".join(out.split()), out  # names the real cause
    finally:
        harness.sweep_cwe2e(only=project)
