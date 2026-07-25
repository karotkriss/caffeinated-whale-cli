"""`apps update` E2E - both modes, against a real instance.

There was NO E2E for `apps` or `update` at all, which is a standing gap against
the captain standard: `apps update` prompts (the auto-start confirm, gated by
`--yes`), and `apps update <proj> frappe` runs `bench update --reset` - which
resets every app repo, pulls, migrates every site and rebuilds - gated by
NOTHING. No `--yes`, no confirmation. That is the path that most needs real
coverage, and it is the one this batch changed most.

`bench` is shimmed for the legs that need deterministic output, exactly as
`test_run_e2e.py` does and for the same reasons: a real `bench update --reset`
clones every app from GitHub and takes many minutes, and what this batch changed
is cwcli's OUTPUT ROUTING and exit code, not bench's reset. The shim also lets
these legs prove the property that matters - stdout purity - which an empty
bench output cannot: with no bytes to stream, streaming and not streaming look
identical. That is precisely why the pre-existing unit tests could not see the
hardcoded `verbose=True` this batch removed.

The unshimmed legs (a real missing app, the real prompts) run against the
genuine bench.
"""

from __future__ import annotations

import io
import json

import pytest

from . import harness

pytestmark = pytest.mark.e2e

# `/home/frappe/.local/bin` precedes `/usr/local/bin` on the image's PATH, so a
# script placed there shadows the real bench for these tests only.
_BENCH_SHIM = "/home/frappe/.local/bin/bench"
_REAL_BENCH = "/home/frappe/.local/bin/bench.cwe2e-real"

# Unicode, because Frappe's own output is full of it and a decode bug here is not
# hypothetical (it shipped once already).
_NOISE = "Updating apps… ✓ frappe updated ─────│└ ⚠"


def _install_bench_shim(inst, *, exit_code: int) -> None:
    """Shim only ``bench update --reset`` and delegate every other bench command.

    App updates now resynchronise the running Procfile programs before returning.
    Those programs launch through this same executable, so a catch-all shim makes
    web, schedule, and workers exit immediately and supervisord reports a spawn
    error. Delegating every other argument preserves the real running bench while
    keeping the reset output deterministic.
    """
    script = (
        "#!/usr/bin/env python3\n"
        "import os\n"
        "import sys\n"
        'if sys.argv[1:] == ["update", "--reset"]:\n'
        f'    print("{_NOISE}")\n'
        f"    sys.exit({exit_code})\n"
        f'os.execv("{_REAL_BENCH}", ["{_REAL_BENCH}", *sys.argv[1:]])\n'
    )
    code, out = harness.exec_in_frappe(
        inst.name,
        f"[ -f {_REAL_BENCH} ] || cp {_BENCH_SHIM} {_REAL_BENCH}; "
        f"cat > {_BENCH_SHIM} <<'CWE2E_EOF'\n{script}CWE2E_EOF\n"
        f"chmod +x {_BENCH_SHIM}",
    )
    assert code == 0, f"could not install the bench shim: {out}"


def _remove_bench_shim(inst) -> None:
    harness.exec_in_frappe(inst.name, f"[ -f {_REAL_BENCH} ] && mv {_REAL_BENCH} {_BENCH_SHIM}")


@pytest.fixture()
def bench_shim(running_instance):
    """Install the output-emitting bench shim, and ALWAYS put the real one back."""

    def _install(exit_code=0):
        _install_bench_shim(running_instance, exit_code=exit_code)
        return running_instance

    try:
        yield _install
    finally:
        _remove_bench_shim(running_instance)


# ------------------------------------------------- stdout purity (the batch's point)


def test_apps_update_json_emits_one_document_and_no_bench_output(bench_shim):
    """`apps update --json` - the surface that could not exist before.

    `_run_frappe_update_reset` hardcoded `verbose=True`, so this path wrote bench
    output to stdout whatever the caller asked. A structured surface whose stdout
    must hold exactly one document cannot survive that, which is why `apps update`
    was the only `apps` subcommand with no `--json`.
    """
    inst = bench_shim(exit_code=0)

    result = harness.run_cwcli(
        "apps", "update", inst.name, "frappe", "--json", "--no-recache", "--yes"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    doc = json.loads(result.stdout)  # raises if ANYTHING else reached stdout
    assert doc["project"] == inst.name
    assert doc["frappe_reset"] is True
    assert doc["ok"] is True
    assert _NOISE not in result.stdout


def test_axi_apps_update_emits_one_toon_document(bench_shim):
    """`cwcli axi apps update` - one TOON document, exactly like `axi backup`."""
    inst = bench_shim(exit_code=0)

    result = harness.run_cwcli("axi", "apps", "update", inst.name, "frappe", "--no-recache")

    assert result.returncode == 0, result.stdout + result.stderr
    assert _NOISE not in result.stdout, "bench output corrupted the TOON document"
    assert f"project: {inst.name}" in result.stdout
    assert "ok: true" in result.stdout
    assert "frappe_reset: true" in result.stdout


def test_apps_update_streams_bench_output_when_verbose(bench_shim):
    """The other half of the contract: `verbose` decides whether to RENDER.

    Without this leg the purity tests above could pass by never streaming at all.
    """
    inst = bench_shim(exit_code=0)

    result = harness.run_cwcli(
        "apps", "update", inst.name, "frappe", "--verbose", "--no-recache", "--yes"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert _NOISE in result.stdout, "verbose must still stream bench output"


def test_apps_update_quiet_does_not_stream_bench_output(bench_shim):
    """Non-verbose runs under a spinner: the reset's output must not appear."""
    inst = bench_shim(exit_code=0)

    result = harness.run_cwcli("apps", "update", inst.name, "frappe", "--no-recache", "--yes")

    assert result.returncode == 0, result.stdout + result.stderr
    assert _NOISE not in result.stdout


# ------------------------------------------------------------------ honest exit codes


def test_apps_update_reports_a_failing_reset_nonzero(bench_shim):
    """A real non-zero `bench update --reset` must never report success."""
    inst = bench_shim(exit_code=1)

    result = harness.run_cwcli("apps", "update", inst.name, "frappe", "--no-recache", "--yes")

    assert result.returncode == 1, result.stdout + result.stderr


def test_apps_update_json_reports_a_failing_reset_in_the_document(bench_shim):
    inst = bench_shim(exit_code=1)

    result = harness.run_cwcli(
        "apps", "update", inst.name, "frappe", "--json", "--no-recache", "--yes"
    )

    assert result.returncode == 1
    doc = json.loads(result.stdout)
    assert doc["ok"] is False
    assert doc["failed_apps"] == ["frappe"]


def test_apps_update_missing_app_is_reported_nonzero(running_instance):
    """No shim: the REAL bench, and an app that genuinely is not there.

    Exercises the real resolve + the real two-directory probe + the real report.
    """
    inst = running_instance

    result = harness.run_cwcli(
        "apps", "update", inst.name, "cwe2e-no-such-app", "--no-recache", "--yes"
    )

    assert result.returncode != 0, result.stdout + result.stderr
    text = harness.collapse_ws(harness.strip_ansi(result.stdout + result.stderr))
    assert "cwe2e-no-such-app" in text
    assert "not found" in text.lower()


# ---------------------------------------------------------- the deprecated alias


def test_deprecated_update_alias_keeps_its_app_option(bench_shim):
    """`cwcli update <p> --app frappe` - the alias is NOT interface-identical.

    It takes apps as a repeatable `--app`/`-a` OPTION where `apps update` takes
    them positionally. Anyone assuming a pure passthrough would unify the two and
    break every existing invocation, so the option form is pinned against the real
    binary, along with the stderr-only deprecation warning.
    """
    inst = bench_shim(exit_code=0)

    result = harness.run_cwcli("update", inst.name, "--app", "frappe", "--no-recache", "--yes")

    assert result.returncode == 0, result.stdout + result.stderr
    stderr_text = harness.collapse_ws(harness.strip_ansi(result.stderr)).lower()
    assert "deprecated" in stderr_text
    # stderr-only: the warning must never touch stdout.
    assert "deprecated" not in harness.strip_ansi(result.stdout).lower()


# ------------------------------------------------------------------ both modes


def test_apps_update_noninteractive_without_yes_refuses_rather_than_hanging(session_instance):
    """Non-TTY + stopped containers + no `--yes`: refuse non-zero.

    Never hang on an un-answerable prompt, never silently proceed.
    """
    inst = session_instance

    stop = harness.run_cwcli("stop", inst.name)
    assert stop.returncode == 0, stop.stdout + stop.stderr

    try:
        # stdin closed (run_cwcli passes no input_text): there is no way to answer.
        result = harness.run_cwcli("apps", "update", inst.name, "frappe", timeout=300)

        assert result.returncode != 0, result.stdout + result.stderr
        stderr_text = harness.collapse_ws(harness.strip_ansi(result.stderr)).lower()
        assert "not running" in stderr_text, result.stderr
    finally:
        harness.run_cwcli("start", inst.name, "--yes")
        harness.wait_for_site_ready(inst.name, inst.site)


def test_axi_apps_update_never_prompts_on_a_stopped_project(session_instance):
    """The agent surface must refuse structurally, never wait for a human.

    There is deliberately no --yes on this verb (starting is UI-coupled), so a
    stopped project is a documented usage error, exit 2, naming `cwcli start`.
    """
    inst = session_instance

    stop = harness.run_cwcli("stop", inst.name)
    assert stop.returncode == 0, stop.stdout + stop.stderr

    try:
        result = harness.run_cwcli("axi", "apps", "update", inst.name, "frappe", timeout=300)

        assert result.returncode == 2, result.stdout + result.stderr
        # Still TOON on stdout, even refusing.
        assert result.stdout.startswith("error:"), result.stdout
        assert "cwcli start" in result.stdout
    finally:
        harness.run_cwcli("start", inst.name, "--yes")
        harness.wait_for_site_ready(inst.name, inst.site)


def test_apps_update_interactive_auto_start_prompt_is_genuinely_shown(session_instance):
    """The prompting half of the both-modes standard.

    With the container stopped, interactive `cwcli apps update` MUST genuinely
    display the auto-start confirm and genuinely await input - a prompt that is
    skipped, or returns empty without waiting, is a bug. Reaching prompt_toolkit's
    raw-mode marker proves it really is waiting for a keystroke.

    Restores the instance to running and site-ready, so the shared session instance
    is left as it was found.
    """
    import pexpect

    inst = session_instance

    stop = harness.run_cwcli("stop", inst.name)
    assert stop.returncode == 0, stop.stdout + stop.stderr

    try:
        log = io.StringIO()
        child = harness.spawn_cwcli(
            ["apps", "update", inst.name, "cwe2e-no-such-app", "--no-recache"], timeout=600
        )
        child.logfile_read = log
        try:
            # The marker means the confirm entered raw mode: it is genuinely waiting.
            harness.expect_prompt_ready(child)
            child.sendline("y")  # `y` THEN Enter, as a human types it (auto_enter=False)
            child.expect(pexpect.EOF, timeout=600)
        finally:
            child.close(force=True)

        out = harness.strip_ansi(log.getvalue())
        assert "is not running" in out, out  # the warning was shown
        assert "Would you like to start the containers" in out, out  # the prompt was shown
    finally:
        harness.run_cwcli("start", inst.name, "--yes")
        harness.wait_for_site_ready(inst.name, inst.site)
