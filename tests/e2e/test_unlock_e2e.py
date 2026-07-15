"""`unlock` E2E - both modes, against a real instance.

`unlock` is a prompting command (the auto-start confirm) with a `--yes` flag, so
the captain standard requires it to be proven in BOTH interactive and
non-interactive modes; it had no E2E at all before the core migration, which
would have meant refactoring it blind. This is that safety floor.

Every assertion is a real outcome read back out of the container (the locks
directory genuinely gone), never a string match on cwcli's own output.
"""

from __future__ import annotations

import pytest

from . import harness

pytestmark = pytest.mark.e2e


def _locks_path(inst) -> str:
    return f"{inst.bench}/sites/{inst.site}/locks"


def _seed_locks(inst) -> str:
    """Create a real locks directory with real lock files inside the container."""
    locks = _locks_path(inst)
    code, out = harness.exec_in_frappe(
        inst.name,
        f"mkdir -p {locks} && touch {locks}/doctype.lock {locks}/queue.lock && ls -1 {locks}",
    )
    assert code == 0, f"could not seed locks dir: {out}"
    assert _locks_exist(inst), "seeded locks dir is not present"
    return locks


def _locks_exist(inst) -> bool:
    code, _ = harness.exec_in_frappe(inst.name, f"test -d {_locks_path(inst)}")
    return code == 0


def test_unlock_noninteractive_really_removes_the_locks_dir(running_instance):
    """Flags supplied + stdin closed: no prompt, exit 0, and the dir is GENUINELY gone."""
    inst = running_instance
    _seed_locks(inst)

    result = harness.run_cwcli("unlock", inst.name, "--site", inst.site, "--yes")

    assert result.returncode == 0, result.stdout + result.stderr
    assert not _locks_exist(inst), "locks directory still present after unlock"


def test_unlock_verbose_reports_the_removed_paths(running_instance):
    """`--verbose` still reports each removed path (now from the structured
    `removed` list at completion, rather than a live stream)."""
    inst = running_instance
    locks = _seed_locks(inst)

    result = harness.run_cwcli("unlock", inst.name, "--site", inst.site, "--yes", "--verbose")

    assert result.returncode == 0, result.stdout + result.stderr
    # Emitted as plain stdout (not through a rich console), so the long path is
    # neither wrapped at the terminal width nor highlighted.
    out = harness.strip_ansi(result.stdout)
    assert f"removed '{locks}/doctype.lock'" in out, out
    assert f"removed '{locks}/queue.lock'" in out, out
    assert not _locks_exist(inst)


def test_unlock_of_an_unlocked_site_is_a_success_not_an_error(running_instance):
    """An absent locks dir is a CLEAN SUCCESS: `rm -rf` always exited 0 on a missing
    target, and a site that simply is not locked is what the caller wanted. Pins
    that the migration did not turn this into a NOT_FOUND."""
    inst = running_instance
    _seed_locks(inst)
    first = harness.run_cwcli("unlock", inst.name, "--site", inst.site, "--yes")
    assert first.returncode == 0, first.stdout + first.stderr

    # Second run: nothing left to remove.
    second = harness.run_cwcli("unlock", inst.name, "--site", inst.site, "--yes")

    assert second.returncode == 0, second.stdout + second.stderr
    assert "already unlocked" in harness.strip_ansi(second.stdout).lower()


def test_axi_unlock_emits_toon_and_removes_for_real(running_instance):
    """The agent surface: one TOON document, a structured `removed` list, real removal."""
    inst = running_instance
    _seed_locks(inst)

    result = harness.run_cwcli("axi", "unlock", inst.name, "--site", inst.site)

    assert result.returncode == 0, result.stdout + result.stderr
    out = harness.strip_ansi(result.stdout)
    assert "removed[" in out, out  # a TOON block, not an opaque blob
    assert "already_unlocked: false" in out, out
    assert not _locks_exist(inst)


def test_unlock_interactive_over_a_real_pty(running_instance):
    """The real TTY path: a running instance needs no prompt, so unlock runs straight
    through and the removal genuinely lands."""
    import pexpect

    inst = running_instance
    _seed_locks(inst)

    child = harness.spawn_cwcli(["unlock", inst.name, "--site", inst.site], timeout=300)
    try:
        child.expect("Successfully unlocked site", timeout=300)
        child.expect(pexpect.EOF, timeout=120)
    finally:
        child.close(force=True)

    assert not _locks_exist(inst)


def test_unlock_interactive_auto_start_prompt_is_genuinely_shown(session_instance):
    """The prompting half of the both-modes standard.

    With the container stopped, interactive `cwcli unlock` MUST genuinely display
    the auto-start confirm and genuinely await input - a prompt that is skipped, or
    that returns without waiting, is the bug this pins. Reaching prompt_toolkit's
    raw-mode marker proves it really is waiting for a keystroke; the command only
    completes after we answer `y`.

    Runs last and restores the instance to a running, site-ready state, so the
    shared session instance is left as it was found.
    """
    import io

    import pexpect

    inst = session_instance
    _seed_locks(inst)

    stop = harness.run_cwcli("stop", inst.name)
    assert stop.returncode == 0, stop.stdout + stop.stderr

    try:
        log = io.StringIO()
        child = harness.spawn_cwcli(["unlock", inst.name, "--site", inst.site], timeout=600)
        child.logfile_read = log
        try:
            # The marker means the confirm entered raw mode: it is genuinely waiting.
            harness.expect_prompt_ready(child)
            child.sendline("y")  # `y` THEN Enter, as a human types it (auto_enter=False)
            # Assert on the captured output after EOF rather than racing a mid-stream
            # regex: rich's highlighter injects ANSI codes inside the phrases.
            child.expect(pexpect.EOF, timeout=600)
        finally:
            child.close(force=True)

        out = harness.strip_ansi(log.getvalue())
        assert "is not running" in out, out  # the warning was shown
        assert "Would you like to start the containers" in out, out  # the prompt was shown
        assert "Successfully unlocked site" in out, out
        assert not _locks_exist(inst), "locks dir still present after auto-start unlock"
    finally:
        # Leave the shared instance as we found it: running and site-ready.
        harness.run_cwcli("start", inst.name, "--yes")
        harness.wait_for_site_ready(inst.name, inst.site)
