"""§4.1 init E2E + §5.1/§5.2 version-sensitive init behaviors.

The session fixture already stands up a genuine bench + site non-interactively;
these tests add explicit assertions for both modes, the generated-admin-password
print-once behavior, and the per-version-leg divergences (MariaDB flag,
pyenv/nvm branches). The version-sensitive tests run on every matrix leg; the
version-agnostic interactive test runs only on the v16 leg.
"""

from __future__ import annotations

import io
import shlex

import pytest

from . import harness

pytestmark = pytest.mark.e2e

# Version-agnostic tests run once (on the v16 leg), not three times.
v16_only = pytest.mark.skipif(
    harness.FRAPPE_MAJOR != 16,
    reason="version-agnostic test runs only on the v16 leg",
)


def _in_frappe_ok(project: str, script: str) -> tuple[bool, str]:
    code, out = harness.exec_in_frappe(project, script)
    return code == 0, out


# --- §4.1 non-interactive ------------------------------------------------- #
def test_noninteractive_init_built_real_bench_and_site(session_instance):
    """The non-interactive `cwcli init` really created a bench and ran new-site."""
    inst = session_instance
    assert "Successfully initialized" in inst.init_stdout, inst.init_stdout

    ok, out = _in_frappe_ok(inst.name, f"test -d {shlex.quote(inst.bench)}")
    assert ok, f"bench dir missing: {out}"

    # A real site_config.json is only written by a genuine `bench new-site`.
    site_config = f"{inst.bench}/sites/{inst.site}/site_config.json"
    ok, out = _in_frappe_ok(inst.name, f"test -f {shlex.quote(site_config)}")
    assert ok, f"site_config.json missing (new-site did not run): {out}"


def test_noninteractive_init_did_not_print_generated_password(session_instance):
    """With --admin-password supplied, init must NOT print a generated password
    (it would be a lie - the supplied value was used verbatim)."""
    combined = session_instance.init_stdout + session_instance.init_stderr
    assert "Administrator password (generated):" not in combined


# --- §4.1 interactive + generated-password print-once (v16 leg only) ------- #
@v16_only
def test_interactive_init_generates_and_prints_admin_password_once(port_allocator):
    """Drive init interactively via a real pty: omit the project name so the
    prompt_toolkit prompt fires (await ESC[?2004h, then type the name), and omit
    --admin-password so a strong password is GENERATED and printed exactly once.
    """
    import pexpect

    name = harness.project_name("initgen")
    port = port_allocator.next()
    log = io.StringIO()
    child = harness.spawn_cwcli(
        ["init", "--port", str(port), "--frappe-branch", harness.FRAPPE_BRANCH],
        timeout=harness.INIT_TIMEOUT,
    )
    child.logfile_read = log
    try:
        harness.expect_prompt_ready(child)  # ESC[?2004h before the keystroke
        child.sendline(name)
        # Let the full init run, then assert on the captured output: rich's
        # highlighter injects ANSI codes inside the phrase, so strip them before
        # matching rather than racing a fragile mid-stream regex.
        child.expect(pexpect.EOF, timeout=harness.INIT_TIMEOUT)
    finally:
        child.close(force=True)
        harness.cwcli_rm(name)

    output = harness.strip_ansi(log.getvalue())
    assert "Successfully initialized" in output, output[-2000:]
    assert (
        output.count("Administrator password (generated):") == 1
    ), "generated admin password must be printed exactly once"


# --- §5.1 MariaDB flag divergence (runs on every leg) --------------------- #
def test_mariadb_flag_created_site_on_this_leg(session_instance):
    """The MariaDB flag diverges by version (<=14 --no-mariadb-socket vs 15+
    --mariadb-user-host-login-scope=%); passing the wrong one makes `bench
    new-site` fail. A created site on THIS leg is therefore the proof the
    version-correct flag ran.

    ponytail: created-site is the flag-divergence proof; a direct mysql.user
    host-scope query is the upgrade path if a finer signal is ever needed.
    """
    inst = session_instance
    site_config = f"{inst.bench}/sites/{inst.site}/site_config.json"
    ok, out = _in_frappe_ok(inst.name, f"test -f {shlex.quote(site_config)}")
    assert ok, f"site not created on frappe v{harness.FRAPPE_MAJOR}: {out}"


# --- §5.2 pyenv/nvm version branches (runs on every leg) ------------------ #
def test_venv_python_matches_frappe_version(session_instance):
    """The bench venv Python reflects the version-gated pyenv branch: v14->3.10,
    v15->3.12, v16->the image default (no pyenv override)."""
    inst = session_instance
    code, out = harness.exec_in_frappe(
        inst.name, f"{shlex.quote(inst.bench)}/env/bin/python --version"
    )
    assert code == 0, out
    ver = out.strip()
    if harness.FRAPPE_MAJOR == 14:
        assert "3.10" in ver, ver
    elif harness.FRAPPE_MAJOR == 15:
        assert "3.12" in ver, ver
    else:  # v16: image default, no pyenv override - just a valid 3.x
        assert "Python 3." in ver, ver


def test_v14_provisions_node16_and_yarn(session_instance):
    """v14's bench init runs under nvm node16 with a global yarn (the branch v16
    skips entirely)."""
    if harness.FRAPPE_MAJOR != 14:
        pytest.skip("v14-only pyenv/nvm branch")
    inst = session_instance
    code, out = harness.exec_in_frappe(inst.name, "ls -d ~/.nvm/versions/node/v16* 2>/dev/null")
    assert code == 0 and "v16" in out, f"node16 not provisioned via nvm: {out}"
    code, out = harness.exec_in_frappe(inst.name, "ls ~/.nvm/versions/node/v16*/bin/yarn")
    assert code == 0, f"yarn not installed for node16: {out}"
