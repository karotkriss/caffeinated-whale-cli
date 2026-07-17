"""Shared E2E harness: the reusable seam every command E2E builds on.

This automates the manual ``docs/e2e/`` recipe (temp HOME, unique ``cwe2e-``
names, genuine ``cwcli init`` benches, pexpect for TTY, flags for non-TTY,
teardown) so the destructive-path guarantees are enforced by machine. It drives
the real ``cwcli`` console script (subprocess / pexpect), NOT in-process Typer
functions, so Typer parsing is exercised too.

Isolation is layered: a per-session temp ``HOME`` (the belt) plus the
``CWCLI_HOME`` override (the precise control that redirects only cwcli's own
footprint - see ``utils/config_utils.cwcli_home``). Two independent safety
layers guard the operator's real state:

- a hard rail (``enforce_isolation``) that fails closed before any Docker work if
  ``HOME`` is (or nests under) the real home, or if ``CWCLI_HOME`` is unset or does
  not resolve to a location inside that isolated ``HOME``, and a name rail that
  refuses any project name lacking the ``cwe2e-`` prefix;
- an unconditional teardown backstop (``sweep_cwe2e``) that removes every
  ``cwe2e-``-labelled compose project's containers, volumes, and networks even
  when a test crashes before its ``cwcli rm`` teardown fires.

Nothing here touches Docker or the rails at import/collection time; all of that
lives in functions the fixtures/tests call, so collecting these modules under
``-m unit`` (which deselects them) is safe.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time
import uuid
from pathlib import Path

# CSI escape sequences (SGR colours, the ?2004h/l bracketed-paste toggles, etc.).
# rich's default highlighter sprinkles these INSIDE phrases, so string assertions
# on pexpect output must strip them first.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def collapse_ws(text: str) -> str:
    """Collapse whitespace runs to one space.

    rich word-wraps console output to the terminal width, and the wrap point
    shifts with content length (e.g. a project name whose length varies by
    environment - CI's run id is far longer than a local random one). A
    multi-word substring check must not assume its phrase lands on one line.
    """
    return re.sub(r"\s+", " ", text)


CWE2E_PREFIX = "cwe2e-"

# The Frappe major for this leg (the CI matrix sets CWE2E_FRAPPE_MAJOR per job;
# default 16 for a bare local run). The version-sensitive tests key off this.
FRAPPE_MAJOR = int(os.environ.get("CWE2E_FRAPPE_MAJOR", "16"))
FRAPPE_BRANCH = f"version-{FRAPPE_MAJOR}"

# A short run id so parallel/repeat runs never collide on names. The CI job can
# pin it (CWE2E_RUN_ID) so a whole matrix leg shares one prefix for the backstop.
RUN_ID = os.environ.get("CWE2E_RUN_ID") or uuid.uuid4().hex[:6]

DEFAULT_SITE = "development.localhost"
DEFAULT_BENCH_NAME = "frappe-bench"
# init's default --bench-parent is /workspace, so the bench lives here.
DEFAULT_BENCH_PATH = f"/workspace/{DEFAULT_BENCH_NAME}"

# A full `cwcli init` (image pull + bench init build + new-site) is the dominant
# cost; keep the subprocess/pexpect ceiling under the CI job's timeout-minutes so
# a hang surfaces as a captured timeout rather than an opaque job kill.
INIT_TIMEOUT = 2100

# prompt_toolkit's raw-mode readiness marker. Await it before EVERY pexpect
# keystroke so nothing races the prompt (a documented cwcli hazard). Compiled as
# a regex by pexpect: \x1b -> ESC, \[ and \? escape the literal bracket/qmark.
RAW_MODE_MARKER = r"\x1b\[\?2004h"

# The operator's real home, captured at import (before any fixture overrides
# HOME) so the isolation rail can compare against it even after HOME is
# repointed at the temp dir.
_REAL_HOME = os.path.realpath(os.environ.get("HOME", "") or str(Path.home()))

# Resolve the console script once. ``CWCLI_BIN`` wins when set, so the SAME
# harness can be aimed at any cwcli binary - notably a runtime-deps-only
# ``uv tool install .`` build (the packaging-realism leg), which catches an
# undeclared runtime dependency that the all-extras dev venv masks. Otherwise,
# under ``uv run pytest`` the venv bin dir is on PATH so ``cwcli`` resolves;
# fall back to the bare name otherwise.
CWCLI = os.environ.get("CWCLI_BIN") or shutil.which("cwcli") or "cwcli"


# --------------------------------------------------------------------------- #
# Safety rails (fail-closed before any Docker work)
# --------------------------------------------------------------------------- #
def _at_or_under(path: str, ancestor: str) -> bool:
    """True if ``path`` equals or is nested inside ``ancestor`` (both absolute).

    Fail-safe: if the two paths share no common root (``commonpath`` raises), they
    are not nested, so this returns False.
    """
    try:
        return os.path.commonpath([path, ancestor]) == ancestor
    except ValueError:
        return False


def enforce_isolation() -> None:
    """Refuse to run if we are not genuinely isolated. Fail closed, non-zero.

    The one unacceptable failure is touching the operator's real cwcli state, so
    both HOME and CWCLI_HOME are validated before any Docker/instance work:

    - ``HOME`` must not be the operator's real home (nor nested under it).
    - ``CWCLI_HOME`` - where cwcli writes ALL of its on-disk state - must resolve
      to a location INSIDE that isolated ``HOME``. Requiring containment (not just
      "is set") closes the hole where a ``CWCLI_HOME`` pointing at/under the real
      home would pass while cwcli wrote real state.
    """
    raw_home = os.environ.get("HOME", "")
    if not raw_home:
        raise RuntimeError("E2E isolation rail: HOME is unset/empty; refusing to run any E2E.")
    home = os.path.realpath(raw_home)
    if _at_or_under(home, _REAL_HOME):
        raise RuntimeError(
            f"E2E isolation rail: HOME ({home!r}) is at or under the operator's real "
            f"home ({_REAL_HOME!r}); refusing to run any E2E."
        )
    raw_cwcli_home = os.environ.get("CWCLI_HOME", "")
    if not raw_cwcli_home:
        raise RuntimeError("E2E isolation rail: CWCLI_HOME is not set; refusing to run any E2E.")
    cwcli_home = os.path.realpath(raw_cwcli_home)
    if not _at_or_under(cwcli_home, home):
        raise RuntimeError(
            f"E2E isolation rail: CWCLI_HOME ({cwcli_home!r}) is not inside the isolated "
            f"session HOME ({home!r}); cwcli writes all of its state there, so a "
            f"CWCLI_HOME outside the isolated root (e.g. at/under the operator's real "
            f"home) could touch real instances. Refusing to run any E2E."
        )


def assert_prefixed(name: str) -> None:
    if not name.startswith(CWE2E_PREFIX):
        raise RuntimeError(
            f"E2E name rail: project name {name!r} lacks the required "
            f"{CWE2E_PREFIX!r} prefix; refusing to create or operate on it."
        )


def project_name(suffix: str) -> str:
    """A guaranteed-isolated project name: ``cwe2e-<runid>-<suffix>``."""
    name = f"{CWE2E_PREFIX}{RUN_ID}-{suffix}"
    assert_prefixed(name)
    return name


# --------------------------------------------------------------------------- #
# Port allocator
# --------------------------------------------------------------------------- #
# Where the port allocator starts. The default (11000) is clean on an ephemeral
# CI runner, but this harness also runs on operators' own machines, where a real
# instance may already hold that range (its web OR socketio ports). ``CWE2E_PORT_BASE``
# relocates the whole allocation so a local validation run never collides with a
# live instance.
PORT_BASE = int(os.environ.get("CWE2E_PORT_BASE", "11000"))


class PortAllocator:
    """Hand out non-overlapping port bases (>= 1006 apart).

    init maps web ``{port}..{port+5}`` and socketio ``{port+1000}..{port+1005}``;
    a step of 1100 keeps every instance's web AND socketio ranges disjoint.
    """

    def __init__(self, base: int = PORT_BASE, step: int = 1100) -> None:
        self._base = base
        self._step = step
        self._n = 0

    def next(self) -> int:
        port = self._base + self._n * self._step
        self._n += 1
        return port


# --------------------------------------------------------------------------- #
# Docker helpers (via the CLI, so the backstop works purely by compose label)
# --------------------------------------------------------------------------- #
def _docker(*args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def docker_available() -> bool:
    try:
        return _docker("info", timeout=30).returncode == 0
    except Exception:
        return False


def frappe_container_id(project: str) -> str | None:
    r = _docker(
        "ps",
        "-q",
        "--filter",
        f"label=com.docker.compose.project={project}",
        "--filter",
        "label=com.docker.compose.service=frappe",
    )
    ids = r.stdout.split()
    return ids[0] if ids else None


def exec_in_frappe(project: str, script: str, workdir: str | None = None) -> tuple[int, str]:
    """Run a shell script in the project's frappe container. Returns (code, output)."""
    cid = frappe_container_id(project)
    if not cid:
        return (127, f"no running frappe container for {project}")
    args = ["exec"]
    if workdir:
        args += ["-w", workdir]
    args += [cid, "bash", "-lc", script]
    r = _docker(*args, timeout=300)
    return (r.returncode, (r.stdout or "") + (r.stderr or ""))


def docker_cp_out(project: str, container_path: str, host_path: Path) -> bool:
    """Copy a file out of the frappe container to the host. Returns success."""
    cid = frappe_container_id(project)
    if not cid:
        return False
    r = _docker("cp", f"{cid}:{container_path}", str(host_path), timeout=300)
    return r.returncode == 0


# --------------------------------------------------------------------------- #
# Teardown backstop (unconditional; sweeps by cwe2e- label)
# --------------------------------------------------------------------------- #
def cwe2e_projects() -> list[str]:
    """Every distinct ``cwe2e-`` compose project with a live container, volume, or network.

    NETWORKS are discovered too, and that is not symmetry for its own sake: a
    project's network OUTLIVES its containers and volumes. The session teardown
    runs `cwcli rm --yes --volumes` first, so by the time the backstop sweep
    runs, a cleanly-torn-down project has no container and no volume left to be
    discovered by - the project vanished from this list, `sweep_cwe2e`'s
    network-removal loop never ran for it, and its network was orphaned. Every
    SUCCESSFUL e2e run leaked exactly one network that way (20 had accumulated
    when this was found), and Docker's default address pool is finite, so the
    eventual symptom is `could not find an available, non-overlapping IPv4
    address pool` on an unrelated run.
    """
    projs: set[str] = set()
    for cid in _docker("ps", "-aq", "--filter", "label=com.docker.compose.project").stdout.split():
        name = _docker(
            "inspect", "-f", '{{index .Config.Labels "com.docker.compose.project"}}', cid
        ).stdout.strip()
        if name.startswith(CWE2E_PREFIX):
            projs.add(name)
    for vol in _docker(
        "volume", "ls", "-q", "--filter", "label=com.docker.compose.project"
    ).stdout.split():
        name = _docker(
            "volume", "inspect", "-f", '{{index .Labels "com.docker.compose.project"}}', vol
        ).stdout.strip()
        if name.startswith(CWE2E_PREFIX):
            projs.add(name)
    for net in _docker(
        "network", "ls", "-q", "--filter", "label=com.docker.compose.project"
    ).stdout.split():
        name = _docker(
            "network", "inspect", "-f", '{{index .Labels "com.docker.compose.project"}}', net
        ).stdout.strip()
        if name.startswith(CWE2E_PREFIX):
            projs.add(name)
    return sorted(projs)


def sweep_cwe2e(only: str | None = None) -> list[str]:
    """Force-remove ``cwe2e-`` projects' containers, volumes, and networks.

    The unconditional session backstop calls this with no argument to sweep EVERY
    ``cwe2e-`` project (so a crashed test never leaks). ``only`` restricts the
    sweep to one project - identical per-project removal logic, used by the
    leaked-resource test so it can prove the backstop without touching the live
    session instance. Safe to call when nothing matches (returns an empty list).
    """
    projects = cwe2e_projects()
    if only is not None:
        projects = [p for p in projects if p == only]
    swept = []
    for proj in projects:
        filt = f"label=com.docker.compose.project={proj}"
        ids = _docker("ps", "-aq", "--filter", filt).stdout.split()
        if ids:
            _docker("rm", "-f", *ids, timeout=300)
        vols = _docker("volume", "ls", "-q", "--filter", filt).stdout.split()
        if vols:
            _docker("volume", "rm", "-f", *vols, timeout=300)
        for net in _docker("network", "ls", "-q", "--filter", filt).stdout.split():
            _docker("network", "rm", net)
        swept.append(proj)
    return swept


# NOTE: no broad `docker system prune` lives here on purpose. This harness also
# runs on the operator's own machine, where a system-wide prune would remove
# THEIR stopped containers / dangling images. sweep_cwe2e is deliberately scoped
# to cwe2e- resources only. Reclaiming the 14 GB SSD ceiling is a CI concern and
# is done (broadly, safely) on the ephemeral runner in e2e.yml, not here.


# --------------------------------------------------------------------------- #
# Readiness (poll a real signal - never a fixed sleep)
# --------------------------------------------------------------------------- #
def wait_until(predicate, *, timeout: int = 180, interval: float = 3, desc: str = "condition"):
    deadline = time.time() + timeout
    last: object = None
    while time.time() < deadline:
        try:
            last = predicate()
            if last:
                return last
        except Exception as exc:  # noqa: BLE001 - poll-and-retry
            last = exc
        time.sleep(interval)
    raise TimeoutError(f"Timed out after {timeout}s waiting for {desc} (last result: {last!r})")


def wait_for_site_ready(
    project: str, site: str, bench: str = DEFAULT_BENCH_PATH, *, timeout: int = 300
) -> None:
    """Poll until Frappe boots and reaches its DB for ``site`` (real readiness)."""

    def ready() -> bool:
        code, _ = exec_in_frappe(
            project, f"cd {shlex.quote(bench)} && bench --site {shlex.quote(site)} list-apps"
        )
        return code == 0

    wait_until(ready, timeout=timeout, interval=5, desc=f"site {site!r} ready")


# --------------------------------------------------------------------------- #
# Driving the real cwcli binary
# --------------------------------------------------------------------------- #
def run_cwcli(*args: str, timeout: int = 1800, input_text: str | None = None):
    """Run ``cwcli <args>`` non-interactively (stdin closed, a real non-TTY).

    With ``input_text`` given, feeds it on a pipe instead (still non-TTY). Returns
    the CompletedProcess (never raises on non-zero - callers assert on returncode).
    """
    if input_text is None:
        return subprocess.run(
            [CWCLI, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=os.environ.copy(),
        )
    return subprocess.run(
        [CWCLI, *args],
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=os.environ.copy(),
    )


def spawn_cwcli(args, timeout: int = 1800):
    """Spawn ``cwcli <args>`` on a real pty (interactive mode) via pexpect."""
    import pexpect

    return pexpect.spawn(
        CWCLI,
        list(args),
        env=os.environ.copy(),
        encoding="utf-8",
        timeout=timeout,
        dimensions=(50, 140),
    )


def expect_prompt_ready(child, timeout: int = 180) -> None:
    """Await prompt_toolkit's raw-mode marker before sending a keystroke."""
    child.expect(RAW_MODE_MARKER, timeout=timeout)


# --------------------------------------------------------------------------- #
# init / teardown convenience
# --------------------------------------------------------------------------- #
def cwcli_rm(project: str, timeout: int = 600):
    """Tear an instance down: containers + named volumes + project dir.

    Uses ``--no-backup`` because a throwaway teardown does not need a live backup
    (the C1 backup gate itself is covered by the deferred rm E2E).
    """
    assert_prefixed(project)
    return run_cwcli("rm", project, "--yes", "--volumes", "--no-backup", timeout=timeout)
