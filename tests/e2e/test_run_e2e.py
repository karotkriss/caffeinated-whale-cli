"""`run` E2E - both modes, against a real instance.

`run` is a prompting command (the auto-start confirm, gated by `--yes`) and had
NO E2E and no command unit tests at all: `tests/README.md` recorded it as the one
genuinely untested command in cwcli. This batch also refactors it onto new
streaming machinery, so this is not a nice-to-have - it is the safety floor for
the refactor.

The `>32KB unicode` leg is the one that cannot be faked. Docker frames the exec
socket at 32KB and `bench` (CPython on a pipe) flushes in 8KB blocks, so a chunk
boundary lands at an arbitrary BYTE offset - routinely mid-character. Only a real
socket produces that split; a fake stream chooses its own boundaries. The same
leg pins the honest exit code against a real daemon.
"""

from __future__ import annotations

import pytest

from . import harness

pytestmark = pytest.mark.e2e

# A stand-in `bench` that emits well over one 32KB socket read of unicode. The
# real `bench migrate` would do this too, but this costs seconds instead of
# minutes and lets us pin the exit code exactly.
#
# `/home/frappe/.local/bin` precedes `/usr/local/bin` on the image's PATH, so a
# script placed there shadows the real bench for this test only.
_BENCH_SHIM = "/home/frappe/.local/bin/bench"
_REAL_BENCH = "/home/frappe/.local/bin/bench.cwe2e-real"
_LINES = 3000
_LINE = "Building… ✓ app {} ─────│└ ⚠"


def _install_bench_shim(inst, *, exit_code: int) -> None:
    """Shadow `bench` with a python3 program emitting ~120KB of unicode.

    python3 specifically: `bench` IS a Python CLI, and CPython block-buffers when
    its stdout is a pipe (which `exec_create(tty=False)` makes it). That buffering
    is what produces the >32KB writes that split mid-character. A shell `printf`
    loop would emit one small write per line and never reproduce it.
    """
    script = (
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"for i in range({_LINES}):\n"
        f'    print("{_LINE}".format(i))\n'
        f"sys.exit({exit_code})\n"
    )
    code, out = harness.exec_in_frappe(
        inst.name,
        # Keep the real bench aside so the shim can be reverted.
        f"[ -f {_REAL_BENCH} ] || cp {_BENCH_SHIM} {_REAL_BENCH}; "
        f"cat > {_BENCH_SHIM} <<'CWE2E_EOF'\n{script}CWE2E_EOF\n"
        f"chmod +x {_BENCH_SHIM}",
    )
    assert code == 0, f"could not install the bench shim: {out}"


def _remove_bench_shim(inst) -> None:
    harness.exec_in_frappe(inst.name, f"[ -f {_REAL_BENCH} ] && mv {_REAL_BENCH} {_BENCH_SHIM}")


@pytest.fixture()
def bench_shim(running_instance):
    """Install the unicode-emitting bench shim, and always put the real one back."""

    def _install(exit_code=0):
        _install_bench_shim(running_instance, exit_code=exit_code)
        return running_instance

    try:
        yield _install
    finally:
        _remove_bench_shim(running_instance)


def _expected_output() -> str:
    return "".join(f"{_LINE.format(i)}\n" for i in range(_LINES))


# ------------------------------------------------------------------ the contract, for real


def test_run_streams_a_large_unicode_payload_intact(bench_shim):
    """>32KB of unicode across real socket reads, delivered byte-for-byte.

    This is the leg no fake can produce. Decoding each chunk independently used
    to either raise `UnicodeDecodeError` (delivering ZERO bytes and a traceback)
    or silently corrupt the split character to U+FFFD.
    """
    inst = bench_shim(exit_code=0)

    result = harness.run_cwcli("run", inst.name, "migrate", "--yes")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "�" not in result.stdout, "a character was corrupted at a chunk boundary"
    assert "UnicodeDecodeError" not in result.stderr, result.stderr
    assert result.stdout == _expected_output(), (
        f"expected {len(_expected_output())} bytes of output, " f"got {len(result.stdout)}"
    )
    # The payload really did exceed one 32KB socket read - otherwise this leg
    # proves nothing about chunk boundaries.
    assert len(_expected_output().encode()) > 32768


def test_run_reports_a_real_nonzero_exit_code(bench_shim):
    """The fail-open fix, against a real daemon.

    `run.py` read `result.get("ExitCode", 1)` - whose default is dead code,
    because the key is present and holds None while the exec is still going - and
    raised `typer.Exit(code=None)`, which exits 0.
    """
    inst = bench_shim(exit_code=42)

    result = harness.run_cwcli("run", inst.name, "migrate", "--yes")

    assert (
        result.returncode == 42
    ), f"expected the bench command's real exit code 42, got {result.returncode}"
    assert result.stdout == _expected_output()


def test_run_executes_a_real_bench_command(running_instance):
    """No shim: the genuine `bench --version` in the genuine bench.

    `--yes` goes BEFORE the separator: after `--`, everything is a bench arg.
    """
    inst = running_instance

    result = harness.run_cwcli("run", inst.name, "--yes", "--", "--version")

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip(), "bench --version produced no output"


def test_run_passes_bench_flags_after_a_separator(running_instance):
    """The documented `--` form: this command's parser claims flags otherwise."""
    inst = running_instance

    result = harness.run_cwcli("run", inst.name, "--yes", "--", "--site", inst.site, "list-apps")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "frappe" in result.stdout


def test_run_reports_a_failing_bench_command_nonzero(running_instance):
    """A real bench failure must not report success."""
    inst = running_instance

    result = harness.run_cwcli(
        "run", inst.name, "--yes", "--", "--site", "no-such-site", "list-apps"
    )

    assert result.returncode != 0, result.stdout + result.stderr


# ------------------------------------------------------------------ both modes


def test_run_noninteractive_without_yes_refuses_rather_than_hanging(session_instance):
    """Non-TTY + stopped containers + no `--yes`: refuse non-zero.

    Never hang on an un-answerable prompt, never silently proceed.
    """
    inst = session_instance

    stop = harness.run_cwcli("stop", inst.name)
    assert stop.returncode == 0, stop.stdout + stop.stderr

    try:
        # stdin closed (run_cwcli passes no input_text): there is no way to answer.
        result = harness.run_cwcli("run", inst.name, "--", "--version", timeout=300)

        assert result.returncode != 0, result.stdout + result.stderr
        assert "not running" in harness.strip_ansi(result.stderr).lower(), result.stderr
    finally:
        harness.run_cwcli("start", inst.name, "--yes")
        harness.wait_for_site_ready(inst.name, inst.site)


def test_run_interactive_auto_start_prompt_is_genuinely_shown(session_instance):
    """The prompting half of the both-modes standard.

    With the container stopped, interactive `cwcli run` MUST genuinely display the
    auto-start confirm and genuinely await input. Reaching prompt_toolkit's
    raw-mode marker proves it really is waiting for a keystroke; the command only
    completes after we answer `y`.

    Restores the instance to running and site-ready, so the shared session
    instance is left as it was found.
    """
    import io

    import pexpect

    inst = session_instance

    stop = harness.run_cwcli("stop", inst.name)
    assert stop.returncode == 0, stop.stdout + stop.stderr

    try:
        log = io.StringIO()
        child = harness.spawn_cwcli(["run", inst.name, "--", "--version"], timeout=600)
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
