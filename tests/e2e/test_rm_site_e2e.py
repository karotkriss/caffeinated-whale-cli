"""`rm-site` / `axi rm-site` E2E - both modes, against a real instance.

`rm-site` is cwcli's newest destructive, prompting command (a `--yes`-gated
confirm, like `apps uninstall`), so the captain standard requires it proven in
BOTH interactive and non-interactive modes, plus its `axi` sibling. Every
assertion here is a real outcome read back out of the container or the host
filesystem (the site's directory genuinely gone, its database genuinely
dropped, its credential-bearing archive genuinely relocated and pruned - or
genuinely left in place when it could not be verified), never a string match
on cwcli's own claims alone.

Each test creates its OWN throwaway site on the shared `running_instance`
bench and drops only that site, never `inst.site` (the shared instance's own
default site, which every other e2e test in the session still depends on).
"""

from __future__ import annotations

import shlex

import pytest

from . import harness
from .conftest import SESSION_ADMIN_PW

pytestmark = pytest.mark.e2e


def _create_site(inst, site: str) -> None:
    """A real `bench new-site`, both secrets supplied so it never prompts."""
    result = harness.run_cwcli(
        "run",
        inst.name,
        "-i",
        "new-site",
        site,
        "--db-root-password",
        "123",
        "--admin-password",
        SESSION_ADMIN_PW,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _site_exists(inst, site), f"new-site did not create {site}"


def _site_exists(inst, site: str) -> bool:
    code, _ = harness.exec_in_frappe(inst.name, f"test -d {inst.bench}/sites/{shlex.quote(site)}")
    return code == 0


def _archived_path(inst, site: str) -> str:
    return f"{inst.bench}/archived/sites/{site}"


def _archived_exists(inst, site: str) -> bool:
    code, _ = harness.exec_in_frappe(inst.name, f"test -d {_archived_path(inst, site)}")
    return code == 0


def test_rm_site_noninteractive_really_drops_the_site(running_instance):
    """Flags supplied + stdin closed: no prompt, exit 0, and the site is GENUINELY
    gone - its database dropped, its directory gone, and its credential-bearing
    archive relocated out of the container and pruned, not left to accumulate."""
    inst = running_instance
    site = "cwe2e-dropsite-a.localhost"
    _create_site(inst, site)

    result = harness.run_cwcli("rm-site", inst.name, site, "--yes")

    assert result.returncode == 0, result.stdout + result.stderr
    assert not _site_exists(inst, site), "site directory still present after rm-site"
    assert not _archived_exists(inst, site), "bench's own archive was not pruned from the container"
    out = harness.collapse_ws(harness.strip_ansi(result.stdout))
    assert "dropped" in out.lower(), out
    assert "removed from the container" in out, out

    # The shared instance's OWN default site is untouched.
    assert _site_exists(inst, inst.site), "the shared instance's own site was affected"


def test_rm_site_without_yes_refuses_noninteractively(running_instance):
    """A non-TTY without --yes must refuse, never silently drop the site."""
    inst = running_instance
    site = "cwe2e-dropsite-b.localhost"
    _create_site(inst, site)

    result = harness.run_cwcli("rm-site", inst.name, site)

    assert result.returncode != 0, result.stdout + result.stderr
    assert _site_exists(inst, site), "site was dropped despite refusing the confirmation"


def test_rm_site_of_a_nonexistent_site_is_not_found(running_instance):
    """A typo'd site name is a clear refusal, not a silent no-op or a crash."""
    inst = running_instance

    result = harness.run_cwcli("rm-site", inst.name, "cwe2e-dropsite-nope.localhost", "--yes")

    assert result.returncode != 0, result.stdout + result.stderr
    out = harness.collapse_ws(harness.strip_ansi(result.stdout + result.stderr)).lower()
    assert "not found" in out, out


def test_axi_rm_site_without_yes_is_a_usage_error(running_instance):
    """The agent surface never prompts: missing --yes is a structured usage error
    on stdout (exit 2), and the site is left untouched."""
    inst = running_instance
    site = "cwe2e-dropsite-c.localhost"
    _create_site(inst, site)

    result = harness.run_cwcli("axi", "rm-site", inst.name, site)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "error:" in result.stdout, result.stdout
    assert "--yes" in result.stdout, result.stdout
    assert _site_exists(inst, site), "site was dropped despite the missing --yes usage error"


def test_axi_rm_site_emits_toon_and_drops_for_real(running_instance):
    """The agent surface: one TOON document naming the archive's real host path,
    and a real drop - not a mocked or partial one."""
    inst = running_instance
    site = "cwe2e-dropsite-d.localhost"
    _create_site(inst, site)

    result = harness.run_cwcli("axi", "rm-site", inst.name, site, "--yes")

    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout
    assert f"site: {site}" in out, out
    assert "ok: true" in out, out
    assert "archive_pruned_in_container: true" in out, out
    assert "archived_host_path:" in out, out
    assert not _site_exists(inst, site), "site directory still present after axi rm-site"
    assert not _archived_exists(inst, site), "bench's own archive was not pruned from the container"


def test_rm_site_interactive_confirm_over_a_real_pty(running_instance):
    """The real TTY path: the destructive confirm is genuinely shown and genuinely
    awaited (the raw-mode marker proves it), and answering `y` really drops it."""
    import pexpect

    inst = running_instance
    site = "cwe2e-dropsite-e.localhost"
    _create_site(inst, site)

    child = harness.spawn_cwcli(["rm-site", inst.name, site], timeout=300)
    try:
        harness.expect_prompt_ready(child)
        child.send("y")
        child.send("\r")
        child.expect(pexpect.EOF, timeout=300)
    finally:
        child.close(force=True)

    assert not _site_exists(inst, site), "site still present after the interactive confirm"
    assert not _archived_exists(inst, site), "bench's own archive was not pruned from the container"


def test_rm_site_interactive_decline_leaves_the_site_untouched(running_instance):
    """Declining the confirm ('n') must leave the site fully intact."""
    import pexpect

    inst = running_instance
    site = "cwe2e-dropsite-f.localhost"
    _create_site(inst, site)

    child = harness.spawn_cwcli(["rm-site", inst.name, site], timeout=300)
    try:
        harness.expect_prompt_ready(child)
        child.send("n")
        child.send("\r")
        child.expect(pexpect.EOF, timeout=300)
    finally:
        child.close(force=True)

    assert _site_exists(inst, site), "site was dropped despite declining the confirmation"
