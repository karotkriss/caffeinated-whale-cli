"""Persistent credential bridge E2E (phase 2) - the committed proofs from the
scout report's test plan (§7), against a real instance.

Four proofs, each pinning a load-bearing phase-1 contract live:

- **the credential path**: with a PATH-shimmed fake host ``gh`` answering
  deterministic (obviously fake) credentials, the ensure step rides ``cwcli
  start``, and an in-container ``git credential fill`` for github.com comes back
  with the fake credentials - shim -> stable socket -> detached daemon -> host
  ``gh`` - and the audit line lands (never a credential byte);
- **both-modes degradation** (§5.8, THE design contract): daemon stopped with
  the shim + system config line still in place, interactive git falls through to
  git's OWN terminal prompt (pty-driven, a real 401-returning URL) and
  non-interactive git (``GIT_TERMINAL_PROMPT=0``) fails auth cleanly - nonzero,
  fast, and with zero shim noise;
- **recreation self-heal** (§5.6): recreating the frappe container wipes
  ``/etc/gitconfig`` (container layer) while the stable shim survives on the
  workspace bind mount; the next ``cwcli start`` restores the config line and
  the bridge serves again, with no daemon involvement;
- **the `cwcli run` wrap** (§5.9): a bench-level credential probe through
  ``cwcli run`` answers via the per-invocation bridge when the feature is off
  (helper line live DURING the exec, torn down after) and via the stable
  system-level helper when the daemon serves - with NO second, per-invocation
  helper line (the daemon-skip).

Deliberate scoping choice: the tests flip the ``[cred_bridge] enabled`` flag by
writing the isolated ``CWCLI_HOME`` config directly (a ``python -c`` subprocess,
so the import sees the fixture's env) instead of running ``cwcli config
cred-bridge enable``. The verb's fused sweep (``ensure_running_instances``)
wires EVERY running frappe container on the Docker daemon - correct on a user's
box, but on an operator's machine that may include real, non-cwe2e instances the
harness must never touch. Ensure therefore rides ``cwcli start <session
project>`` (exactly how the report words this proof), which scopes every
container mutation to the throwaway instance; ``disable``/``stop`` iterate only
the registry/pid file under the isolated ``CWCLI_HOME``, so the real verbs stay
safe to use for teardown. The ``enable`` verb itself is unit-covered
(``tests/test_core_cred_bridge.py``).

Bridge behavior is version-agnostic, so the module runs once, on the v16 leg,
against the shared session instance (no extra init cost); every mutation is
restored - the bridge fully disabled, the bench shim reverted, the instance left
serving - so sibling shared tests see the state they started with.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from . import harness
from .test_run_e2e import _BENCH_SHIM, _REAL_BENCH, _remove_bench_shim

v16_only = pytest.mark.skipif(
    harness.FRAPPE_MAJOR != 16,
    reason="bridge behavior is version-agnostic; runs only on the v16 leg",
)

pytestmark = [pytest.mark.e2e, v16_only]

# Deterministic, obviously-fake credentials the fake host `gh` answers. The fake
# shadows any real `gh` on PATH for the whole module, so no real host credential
# can ever flow into a socket, log, assertion, or report here.
FAKE_USER = "cwe2e-fake-user"
FAKE_PASSWORD = "cwe2e-fake-password"

# The stable persistent-bridge artifact names (core.credbridge constants; spelled
# out here so the E2E asserts the on-disk contract, not whatever the code says).
STABLE_SHIM = ".cwcli-git-credential.py"
EPHEMERAL_HELPER_MARK = ".git-credential-bridge-"


def _cwcli_home() -> Path:
    return Path(os.environ["CWCLI_HOME"])


def _workspace(project: str) -> Path:
    """The instance's workspace bind-mount host dir (init's ``../data`` form)."""
    return _cwcli_home() / "projects" / project / "data"


def _set_bridge_enabled(value: bool) -> None:
    """Flip ``[cred_bridge] enabled`` in the ISOLATED config, scoped by design.

    A subprocess so the import reads the fixture's ``CWCLI_HOME`` (the package's
    path constants are bound at import time, and the test process imported it
    before the isolation fixture ran). See the module docstring for why this is
    used instead of the ``enable`` verb.
    """
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from caffeinated_whale_cli.utils import config_utils; "
            f"config_utils.set_cred_bridge_enabled({value})",
        ],
        check=True,
        capture_output=True,
        env=os.environ.copy(),
        timeout=60,
    )


def _system_helpers(project: str) -> str:
    """The container's system-level credential.helper values (any user reads them)."""
    _, out = harness.exec_in_frappe(
        project, "git config --system --get-all credential.helper || true"
    )
    return out


def _credential_fill(project: str) -> tuple[int, str]:
    """Ask in-container git to fill github.com credentials, helpers only.

    ``GIT_TERMINAL_PROMPT=0`` so a non-answering helper chain fails fast and
    clean instead of trying to prompt a terminal that is not there - the
    non-interactive half of the degradation contract, and a bounded probe for
    the serving half.
    """
    return harness.exec_in_frappe(
        project,
        "printf 'protocol=https\\nhost=github.com\\npath=cwe2e/fake-private.git\\n' | "
        "GIT_TERMINAL_PROMPT=0 git credential fill",
    )


def _wait_bridge_serving(project: str, timeout: int = 60) -> str:
    """Poll until the bridge genuinely answers (the daemon's registry poll binds
    the socket within ~0.5s of the ensure; retry instead of racing it)."""

    def answered():
        code, out = _credential_fill(project)
        return out if code == 0 and f"username={FAKE_USER}" in out else None

    return harness.wait_until(answered, timeout=timeout, interval=2, desc="bridge serving")


@pytest.fixture(scope="module")
def fake_gh(tmp_path_factory):
    """Shadow the host ``gh`` with a deterministic credential answerer.

    Both bridges dispatch a github.com request to ``gh auth git-credential get``
    on the HOST - the detached daemon and the per-invocation bridge alike inherit
    PATH from the cwcli invocation that starts them - so putting the fake first
    on PATH makes the whole credential path deterministic and keeps any real,
    authenticated ``gh`` out of the exchange entirely.
    """
    bindir = tmp_path_factory.mktemp("cwe2e-fake-gh")
    gh = bindir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        '[ "$1" = auth ] && [ "$2" = git-credential ] && [ "$3" = get ] || exit 1\n'
        "cat >/dev/null\n"
        f"printf 'username={FAKE_USER}\\npassword={FAKE_PASSWORD}\\n'\n"
    )
    gh.chmod(0o755)
    saved = os.environ["PATH"]
    os.environ["PATH"] = f"{bindir}{os.pathsep}{saved}"
    try:
        yield
    finally:
        os.environ["PATH"] = saved


@pytest.fixture(scope="module", autouse=True)
def _bridge_backstop():
    """A crashed test must never leak the detached daemon (or an enabled flag)
    past this module: ``disable`` stops the daemon, inert-stubs every registered
    shim, unsets the config line in the registered (session) container, and
    writes enabled = false - all scoped to the isolated registry."""
    yield
    harness.run_cwcli("config", "cred-bridge", "disable")


@pytest.fixture()
def bridge_enabled(running_instance, fake_gh):
    """The bridge enabled and ENSURED for the session instance via ``cwcli
    start`` (the report's wording of this proof, and the per-launch ensure slot),
    then fully disabled again so sibling tests see the default-off state."""
    inst = running_instance
    _set_bridge_enabled(True)
    try:
        r = harness.run_cwcli("start", inst.name, "--yes")
        assert r.returncode == 0, r.stdout + r.stderr
        yield inst
    finally:
        d = harness.run_cwcli("config", "cred-bridge", "disable")
        assert d.returncode == 0, d.stdout + d.stderr


# --------------------------------------------------------------------------- #
# §7 proof 1: the credential path, end to end
# --------------------------------------------------------------------------- #
def test_credential_path_end_to_end_through_the_persistent_bridge(bridge_enabled):
    """Fake host gh -> daemon -> stable socket -> shim -> in-container git: the
    deterministic credentials arrive, and the audit line lands without ever
    carrying a credential byte."""
    inst = bridge_enabled

    # The ensure step wrote the stable shim into the workspace bind mount and
    # pointed the container's SYSTEM git config at it (any user, lowest priority).
    assert (_workspace(inst.name) / STABLE_SHIM).exists(), "stable shim not in the workspace"
    assert STABLE_SHIM in _system_helpers(inst.name), "system config line missing"

    # The daemon is genuinely up and knows this instance.
    s = harness.run_cwcli("config", "cred-bridge", "status", "--json")
    assert s.returncode == 0, s.stdout + s.stderr
    state = json.loads(s.stdout)
    assert state["enabled"] is True
    assert state["daemon_running"] is True
    assert inst.name in state["registered_projects"]

    # The fill answers with the fake gh's credentials, forwarded verbatim.
    out = _wait_bridge_serving(inst.name)
    assert f"password={FAKE_PASSWORD}" in out

    # One audit line per request: project, host, answered - never a credential.
    audit = (_cwcli_home() / "run" / "credbridge-audit.log").read_text()
    assert "host=github.com answered=yes" in audit
    assert f"project={inst.name}" in audit
    assert FAKE_USER not in audit and FAKE_PASSWORD not in audit


# --------------------------------------------------------------------------- #
# §7 proof 2: both-modes degradation (§5.8, the non-negotiable contract)
# --------------------------------------------------------------------------- #
def test_dead_daemon_degrades_to_gits_own_behavior_in_both_modes(bridge_enabled):
    """Daemon stopped, shim + config line STILL in place - the exact half-state
    the silent-shim contract exists for. Interactive git reaches its own
    terminal prompt; non-interactive git fails auth cleanly. Byte-identical to
    'no helper configured', zero added noise."""
    inst = bridge_enabled
    _wait_bridge_serving(inst.name)  # precondition: it served before it died

    r = harness.run_cwcli("config", "cred-bridge", "stop")
    assert r.returncode == 0, r.stdout + r.stderr

    # The dangerous half-state is real: the helper line survived the daemon.
    assert STABLE_SHIM in _system_helpers(inst.name)

    # Non-interactive: a clean, fast auth failure - nonzero, no hang (the exec
    # is bounded), and no shim noise on the combined output.
    code, out = _credential_fill(inst.name)
    assert code != 0, f"a dead daemon must not answer credentials: {out}"
    assert "could not read username" in out.lower(), out
    assert "Traceback" not in out
    assert STABLE_SHIM not in out

    # Interactive: a pty git fetch against a 401-returning URL (GitHub answers
    # 401 for a nonexistent repo, hiding private-repo existence) falls through
    # the silent shim to git's OWN username prompt.
    import pexpect

    cid = harness.frappe_container_id(inst.name)
    assert cid, "session frappe container must be up"
    url = f"https://github.com/{harness.CWE2E_PREFIX}{harness.RUN_ID}/nonexistent-private.git"
    child = pexpect.spawn(
        "docker",
        ["exec", "-it", cid, "git", "ls-remote", url],
        encoding="utf-8",
        timeout=180,
        dimensions=(50, 140),
    )
    try:
        child.expect(r"Username for 'https://github\.com'")
        assert "Traceback" not in (child.before or "")
    finally:
        child.close(force=True)


# --------------------------------------------------------------------------- #
# §7 proof 3: container recreation self-heals on the next start
# --------------------------------------------------------------------------- #
def test_container_recreation_self_heals_on_the_next_start(bridge_enabled):
    """Recreation wipes /etc/gitconfig with the container layer; the stable shim
    survives on the workspace bind mount; the next ``cwcli start`` re-ensures
    both and the bridge serves again - no daemon involvement (§5.6)."""
    inst = bridge_enabled
    _wait_bridge_serving(inst.name)

    old_cid = harness.frappe_container_id(inst.name)
    conf_dir = _cwcli_home() / "projects" / inst.name / "conf"
    # The scale path's compose shape: --no-deps is load-bearing (MariaDB/Redis
    # and the DB volume untouched); --force-recreate because, unlike scale,
    # nothing in the compose file changed.
    r = subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            inst.name,
            "-f",
            "docker-compose.yml",
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "frappe",
        ],
        cwd=conf_dir,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    new_cid = harness.frappe_container_id(inst.name)
    assert new_cid and new_cid != old_cid, "frappe was not recreated"

    # The wipe is real - and the shim is not wiped with it (it lives host-side).
    assert STABLE_SHIM not in _system_helpers(inst.name), "recreation should wipe /etc/gitconfig"
    assert (_workspace(inst.name) / STABLE_SHIM).exists()

    # The self-heal: one ordinary start restores the config line and serving.
    r = harness.run_cwcli("start", inst.name, "--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    # Shared-instance discipline: the instance is left genuinely healthy.
    harness.wait_for_site_ready(inst.name, inst.site)

    assert STABLE_SHIM in _system_helpers(inst.name), "start did not re-ensure the config line"
    _wait_bridge_serving(inst.name)


# --------------------------------------------------------------------------- #
# §7 proof 4: the `cwcli run` wrap - ephemeral path and daemon-skip path
# --------------------------------------------------------------------------- #
# Every `cwcli run` executes `bench <args>` (core.run_plan hard-prefixes
# `bench`), so the real credential-needing invocations are bench subcommands
# that fetch: `cwcli run <p> get-app <private-url>`, `cwcli run <p> update
# --pull`. Proving one of those against a GENUINE private repo needs real host
# auth, which CI cannot supply hermetically (the phase-1 model test records the
# same limit), so this drives the report's "`git credential fill` equivalent
# through a bench-level op": shadow `bench` with a script (the same shim
# mechanism test_run_e2e uses) that first lists the frappe user's --global
# helpers (the per-invocation bridge's config level - its presence DURING the
# exec is the discriminator between the ephemeral path and the daemon-skip),
# then asks git to fill github.com credentials, helpers only - the exact
# exchange a private `get-app` fetch performs on its 401. Network-free and
# deterministic; the invocation shape stays `cwcli run <p> <bench-subcommand>`.
_PROBE = (
    "#!/bin/sh\n"
    "git config --global --get-all credential.helper 2>/dev/null\n"
    "printf 'protocol=https\\nhost=github.com\\npath=cwe2e/fake-private.git\\n' | "
    "GIT_TERMINAL_PROMPT=0 git credential fill\n"
)


def _install_probe_shim(inst) -> None:
    """Shadow `bench` with the credential probe (test_run_e2e's shim mechanism)."""
    code, out = harness.exec_in_frappe(
        inst.name,
        f"[ -f {_REAL_BENCH} ] || cp {_BENCH_SHIM} {_REAL_BENCH}; "
        f"cat > {_BENCH_SHIM} <<'CWE2E_EOF'\n{_PROBE}CWE2E_EOF\n"
        f"chmod +x {_BENCH_SHIM}",
    )
    assert code == 0, f"could not install the probe shim: {out}"


def test_run_wraps_the_ephemeral_bridge_when_the_daemon_is_off(running_instance, fake_gh):
    """Feature off (the default in this isolated home): `cwcli run` stands up the
    SAME per-invocation bridge `apps install` uses - the helper line exists
    DURING the exec, the fill answers the host fake gh, and the helper is torn
    down after (the exact-value unset), leaving no trace."""
    inst = running_instance
    _install_probe_shim(inst)
    try:
        r = harness.run_cwcli("run", inst.name, "credential-probe")
        assert r.returncode == 0, r.stdout + r.stderr
        assert f"username={FAKE_USER}" in r.stdout, r.stdout
        assert EPHEMERAL_HELPER_MARK in r.stdout, (
            f"the per-invocation helper line was not configured during the exec: {r.stdout}"
        )
        _, out = harness.exec_in_frappe(
            inst.name, "git config --global --get-all credential.helper || true"
        )
        assert EPHEMERAL_HELPER_MARK not in out, "the ephemeral helper was not torn down"
    finally:
        _remove_bench_shim(inst)


def test_run_skips_the_ephemeral_bridge_when_the_daemon_serves(bridge_enabled):
    """The daemon-skip (§5.9): with the persistent bridge serving this instance,
    `run` must NOT add a second, per-invocation helper line - and the fill still
    answers, through the stable system-level helper."""
    inst = bridge_enabled
    _wait_bridge_serving(inst.name)
    _install_probe_shim(inst)
    try:
        r = harness.run_cwcli("run", inst.name, "credential-probe")
        assert r.returncode == 0, r.stdout + r.stderr
        assert f"username={FAKE_USER}" in r.stdout, r.stdout
        assert EPHEMERAL_HELPER_MARK not in r.stdout, (
            f"run stood up a redundant second bridge under the daemon: {r.stdout}"
        )
    finally:
        _remove_bench_shim(inst)
