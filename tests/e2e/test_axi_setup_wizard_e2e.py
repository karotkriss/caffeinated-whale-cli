"""``cwcli axi init --complete-setup`` / ``cwcli axi setup-wizard`` E2E, real Docker.

The reported bug (crane staging desk break, 2026-09-22): `cwcli axi init` sets the
admin password but never completes Frappe's setup wizard, so a fresh site lands in
the setup-wizard state (`frappe.is_setup_complete()` False) and a non-System-
Manager desk renders with no navbar, because the desk router force-redirects EVERY
route to `/app/setup-wizard` while `frappe.boot.setup_complete` is falsy
(`frappe/public/js/frappe/router.js`). Unit tests already pin the plumbing
(`tests/test_core_bench_ops.py`, `tests/test_axi_init.py`, `tests/test_axi_bench_ops.py`)
against a faked container; this is the real-Docker proof that the headless RPC
(`frappe.desk.page.setup_wizard.setup_wizard.setup_complete`) genuinely flips that
state on a real bench, and that the exact bytes a browser loads reflect it.

Two throwaway instances, each paying for its own ~10-20 minute provisioning
because the two claims need OPPOSITE initial conditions:

- Instance A: `cwcli axi init --complete-setup` - proves the flag genuinely
  completes the wizard AT CREATION TIME, merged into the SAME `InitReport` TOON
  document, and that the boot payload a real browser session would load for a
  freshly-created NON-System-Manager user no longer carries the falsy
  `setup_complete` that drives the redirect-with-no-navbar.
- Instance B: `cwcli axi init` WITHOUT the flag (left in the wizard state, the
  status quo bug), then the standalone `cwcli axi setup-wizard <project> <site>`
  verb completes it - proving the second entry point works against an EXISTING
  site, and is idempotent on a second call.

Version-agnostic (the RPC and the router redirect do not vary by Frappe major),
so it runs on every leg of the CI matrix (v14/v15/v16), the way other init
behaviors are covered.
"""

from __future__ import annotations

import json
import shlex

import pytest

from . import harness
from .conftest import SESSION_ADMIN_PW

# `standalone`: this file provisions its own instances and never touches the
# shared session instance.
pytestmark = [pytest.mark.e2e, pytest.mark.standalone]

WORKER_EMAIL = "cwe2e-setup-worker@example.com"
WORKER_PASSWORD = "CwE2ESetupWorker-456"


def _init_axi(name: str, port: int, *extra: str):
    return harness.run_cwcli(
        "axi",
        "init",
        name,
        "--port",
        str(port),
        "--frappe-branch",
        harness.FRAPPE_BRANCH,
        "--admin-password",
        SESSION_ADMIN_PW,
        *extra,
        timeout=harness.INIT_TIMEOUT,
    )


def _bench_execute(project: str, site: str, bench: str, method: str, kwargs: dict | None = None):
    parts = ["bench", "--site", shlex.quote(site), "execute", method]
    if kwargs is not None:
        parts += ["--kwargs", shlex.quote(json.dumps(kwargs))]
    script = f"cd {shlex.quote(bench)} && " + " ".join(parts)
    return harness.exec_in_frappe(project, script)


def _is_setup_complete(project: str, site: str, bench: str) -> bool:
    """`bench execute` prints its return value only when truthy, so an EMPTY
    stdout is the honest encoding of `frappe.is_setup_complete()` -> False."""
    code, out = _bench_execute(project, site, bench, "frappe.is_setup_complete")
    assert code == 0, f"could not read frappe.is_setup_complete: {out}"
    return out.strip() != ""


def _toon_field(stdout: str, key: str) -> str:
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{key}:"):
            return stripped.split(":", 1)[1].strip()
    raise AssertionError(f"TOON output has no {key!r} field:\n{stdout}")


def _create_non_system_manager_user(project: str, site: str, bench: str) -> None:
    """A bare System User with NO roles beyond Frappe's own defaults - deliberately
    NOT a System Manager, the exact class of user the bug report named."""
    code, out = _bench_execute(
        project,
        site,
        bench,
        "frappe.client.insert",
        kwargs={
            "doc": {
                "doctype": "User",
                "email": WORKER_EMAIL,
                "first_name": "CwE2E Worker",
                "user_type": "System User",
                "send_welcome_email": 0,
            }
        },
    )
    assert code == 0, f"could not create the non-System-Manager worker user: {out}"

    code, out = _bench_execute(
        project,
        site,
        bench,
        "frappe.utils.password.update_password",
        kwargs={"user": WORKER_EMAIL, "pwd": WORKER_PASSWORD},
    )
    assert code == 0, f"could not set the worker user's password: {out}"


def _fetch_desk_boot_as_worker(project: str, site: str) -> str:
    """Log in as the non-System-Manager worker over HTTP and fetch `/app`, the
    exact bytes a browser loads. `frappe/www/desk.py` server-renders
    `frappe.boot = {...}` straight into that HTML (`boot = frappe.sessions.get()`),
    and the desk router (`router.js`) force-redirects every route to
    `setup-wizard` - hiding the navbar - whenever that boot's `setup_complete`
    is falsy. Runs entirely inside the container against the bench's own port 8000,
    the same probe shape `core.supervision.web_http_code` uses (`-H Host: <site>`
    routes Frappe's multi-tenant dispatch)."""
    cookie_jar = "/tmp/cwe2e-setup-wizard-cookie.txt"
    script = (
        f"curl -s -c {cookie_jar} -X POST http://localhost:8000/api/method/login "
        f"-H 'Host: {site}' "
        f"--data-urlencode 'usr={WORKER_EMAIL}' --data-urlencode 'pwd={WORKER_PASSWORD}' "
        f"-o /dev/null -w 'login:%{{http_code}}\\n' "
        f"&& curl -s -b {cookie_jar} http://localhost:8000/app -H 'Host: {site}' "
        f"&& rm -f {cookie_jar}"
    )
    code, out = harness.exec_in_frappe(project, script)
    assert code == 0, f"logging in and fetching /app as the worker user failed: {out}"
    login_line, _, body = out.partition("\n")
    assert login_line.strip() == "login:200", f"worker login did not succeed: {out[:500]}"
    return body


def test_axi_init_complete_setup_finishes_the_wizard_on_a_real_bench(port_allocator):
    """The `--complete-setup` half: a FRESH site, real bench, real RPC."""
    # Lowercase suffix: Docker Compose (and cwcli's own validate_project_slug)
    # normalize a project name to lowercase, so the name cwcli echoes back and the
    # label it creates are lowercase. Holding a lowercase name here keeps the test's
    # own assertions and cleanup consistent with that. (The normalization itself is
    # pinned at the seam level by tests/test_axi_init.py::TestProjectNameNormalization.)
    project = harness.project_name("setupa")
    site = harness.DEFAULT_SITE
    bench = harness.DEFAULT_BENCH_PATH

    try:
        result = _init_axi(
            project,
            port_allocator.next(),
            "--complete-setup",
            "--country",
            "United States",
            "--currency",
            "USD",
            "--timezone",
            "UTC",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        stdout = result.stdout

        # The InitReport's own fields AND the merged setup_wizard block, in the
        # SAME TOON document (one `toon.encode()` call, not two).
        assert f"project: {project}" in stdout, stdout
        assert "setup_wizard:" in stdout, stdout
        assert "already_complete: false" in stdout, stdout
        assert "setup_complete: true" in stdout, stdout
        assert "ok: true" in stdout, stdout

        harness.wait_for_site_ready(project, site)

        # Independently re-derive the same fact straight from Frappe, not by
        # trusting cwcli's own report alone.
        assert _is_setup_complete(
            project, site, bench
        ), "frappe.is_setup_complete() is still False after --complete-setup"

        # The end-user-realistic proof: the exact bytes a non-System-Manager
        # user's browser would load carry a truthy setup_complete, so the desk
        # router does not redirect every route to setup-wizard (hiding the
        # navbar) for this user.
        _create_non_system_manager_user(project, site, bench)
        boot_html = _fetch_desk_boot_as_worker(project, site)
        assert '"setup_complete":true' in boot_html, (
            "the desk boot payload served to a non-System-Manager user still "
            f"reports setup_complete falsy (navbar-hiding redirect would fire): "
            f"{boot_html[:2000]}"
        )
    finally:
        harness.cwcli_rm(project)


def test_axi_setup_wizard_completes_an_existing_wizard_state_site(port_allocator):
    """The standalone verb: a site left in the (bug-report) wizard state by a
    plain `cwcli axi init` with no `--complete-setup`, completed afterward - and
    proven idempotent on a second call."""
    project = harness.project_name("setupb")  # lowercase: see the note in test A
    site = harness.DEFAULT_SITE
    bench = harness.DEFAULT_BENCH_PATH

    try:
        result = _init_axi(project, port_allocator.next())
        assert result.returncode == 0, result.stdout + result.stderr
        assert "setup_wizard:" not in result.stdout, result.stdout
        harness.wait_for_site_ready(project, site)

        # Reproduces the reported bug: a fresh site is left incomplete.
        assert not _is_setup_complete(project, site, bench), (
            "a fresh site created without --complete-setup is unexpectedly "
            "already setup-complete; the standalone verb's precondition is gone"
        )

        first = harness.run_cwcli("axi", "setup-wizard", project, site)
        assert first.returncode == 0, first.stdout + first.stderr
        assert _toon_field(first.stdout, "already_complete") == "false", first.stdout
        assert _toon_field(first.stdout, "setup_complete") == "true", first.stdout
        assert _toon_field(first.stdout, "ok") == "true", first.stdout
        assert _is_setup_complete(
            project, site, bench
        ), "frappe.is_setup_complete() is still False after cwcli axi setup-wizard"

        # Idempotent: Frappe's own setup_complete no-ops on an already-done site.
        second = harness.run_cwcli("axi", "setup-wizard", project, site)
        assert second.returncode == 0, second.stdout + second.stderr
        assert _toon_field(second.stdout, "already_complete") == "true", second.stdout
        assert _toon_field(second.stdout, "setup_complete") == "true", second.stdout
    finally:
        harness.cwcli_rm(project)
