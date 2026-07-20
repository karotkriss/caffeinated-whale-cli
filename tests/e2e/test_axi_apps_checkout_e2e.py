"""`cwcli axi apps checkout` E2E - real Docker, real git, real bench.

Validates the missing real-instance leg (task 6.2 in
`openspec/changes/add-axi-apps-checkout-verb/`) that the unit suite's
`FakeContainer` cannot: that `core.checkout_app`'s `git fetch` / `git checkout -B`
genuinely run inside a real frappe container against a real `apps/frappe`
checkout, that a genuinely dirty tree is genuinely refused by git (not a cwcli
pre-check), and that `--reset` genuinely discards that edit.

Uses the `frappe` app itself as the checkout target - every bench already has
`apps/frappe` as a real git checkout, so no extra app install is needed.
"""

from __future__ import annotations

import pytest

from . import harness

pytestmark = pytest.mark.e2e

_APP_DIR = f"{harness.DEFAULT_BENCH_PATH}/apps/frappe"


def _current_branch(inst) -> str:
    code, out = harness.exec_in_frappe(inst.name, f"git -C {_APP_DIR} branch --show-current")
    assert code == 0, out
    return out.strip()


def test_axi_apps_checkout_fetches_and_checks_out_a_real_ref(running_instance):
    """The happy path against a real bench: TOON on stdout, git bytes on stderr,
    and the app's branch genuinely moves."""
    inst = running_instance
    original_branch = _current_branch(inst)

    result = harness.run_cwcli("axi", "apps", "checkout", inst.name, "frappe", original_branch)

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"project: {inst.name}" in result.stdout
    assert "ok: true" in result.stdout
    assert "fetch" in result.stdout and "checkout" in result.stdout
    assert not result.stdout.lstrip().startswith("{")  # TOON, never JSON

    # Git's own bytes are narrated to stderr, never stdout.
    assert "$ git fetch" in result.stderr
    assert "$ git checkout -B" in result.stderr
    assert "$ git fetch" not in result.stdout

    # The checkout genuinely ran: still on the same branch name, now tracking FETCH_HEAD.
    assert _current_branch(inst) == original_branch


def test_axi_apps_checkout_without_reset_is_refused_by_git_on_a_dirty_tree(running_instance):
    """No cwcli-side dirty-tree pre-check: GIT refuses, and cwcli reports it honestly.

    A tracked file is dirtied in-container, then checkout runs without --reset.
    Git's own refusal must reach stderr, the edit must survive untouched, and the
    exit code must be 1 (report.ok is False), not 0.
    """
    inst = running_instance
    tracked_file = f"{_APP_DIR}/README.md"
    marker = "cwe2e-dirty-tree-marker"

    code, out = harness.exec_in_frappe(inst.name, f"echo '{marker}' >> {tracked_file}")
    assert code == 0, out

    try:
        branch = _current_branch(inst)
        result = harness.run_cwcli("axi", "apps", "checkout", inst.name, "frappe", branch)

        assert result.returncode == 1, result.stdout + result.stderr
        assert "ok: false" in result.stdout
        stderr_text = result.stderr.lower()
        assert "would be overwritten" in stderr_text or "local changes" in stderr_text

        # The edit is intact: git's refusal, not a discard.
        code, out = harness.exec_in_frappe(inst.name, f"cat {tracked_file}")
        assert code == 0
        assert marker in out
    finally:
        harness.exec_in_frappe(inst.name, f"git -C {_APP_DIR} checkout -- README.md")


def test_axi_apps_checkout_reset_discards_the_dirty_edit(running_instance):
    """--reset is the explicit, reported opt-in that gets an agent out of the
    dirty tree the previous test proved git refuses to enter automatically."""
    inst = running_instance
    tracked_file = f"{_APP_DIR}/README.md"
    marker = "cwe2e-dirty-tree-marker-reset"

    code, out = harness.exec_in_frappe(inst.name, f"echo '{marker}' >> {tracked_file}")
    assert code == 0, out

    branch = _current_branch(inst)
    result = harness.run_cwcli("axi", "apps", "checkout", inst.name, "frappe", branch, "--reset")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok: true" in result.stdout
    assert "reset" in result.stdout  # its own reported row

    code, out = harness.exec_in_frappe(inst.name, f"cat {tracked_file}")
    assert code == 0
    assert marker not in out  # genuinely discarded


def test_axi_apps_checkout_unknown_app_is_a_real_nonzero_error(running_instance):
    """A typo'd app name against a real bench: no clone, no silent no-op."""
    inst = running_instance

    result = harness.run_cwcli("axi", "apps", "checkout", inst.name, "cwe2e-no-such-app", "main")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "error:" in result.stdout or "ok: false" in result.stdout


def test_axi_apps_checkout_on_a_stopped_project_refuses_without_starting(session_instance):
    """No --yes on this verb: a stopped project is a usage error naming
    `cwcli start`, and nothing is started or fetched."""
    inst = session_instance

    stop = harness.run_cwcli("stop", inst.name)
    assert stop.returncode == 0, stop.stdout + stop.stderr

    try:
        result = harness.run_cwcli(
            "axi", "apps", "checkout", inst.name, "frappe", "main", timeout=300
        )

        assert result.returncode == 2, result.stdout + result.stderr
        assert result.stdout.startswith("error:")
        assert "cwcli start" in result.stdout
    finally:
        harness.run_cwcli("start", inst.name, "--yes")
        harness.wait_for_site_ready(inst.name, inst.site)
