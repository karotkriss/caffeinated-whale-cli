"""`label` E2E - the DB/marker consistency invariant, against a real instance.

ONE mode, non-interactive, deliberately. `label` does NOT prompt: there is no
`questionary`, no `isatty`, and no confirmation in `commands/label.py`, and it
refuses to auto-start rather than offering to. The captain's both-modes standard
is triggered by a prompt, so it does not bite here, and driving a pty against a
command with nothing to answer would prove nothing.

It gets an E2E anyway because it is a mutation across TWO stores - the SQLite
cache and a marker file inside the container - and cwcli has been burned on that
exact line before (CLAUDE.md names "`label --clear` DB/marker consistency"). A
two-store consistency property is precisely what unit fakes cannot prove: the
fakes can only agree with themselves.

Every assertion reads a real outcome back out of the real container and the real
cache, never a string match on cwcli's own success banner.
"""

from __future__ import annotations

import json

import pytest

from . import harness

pytestmark = pytest.mark.e2e

LABEL = "e2e-staging"


def _marker_path(inst) -> str:
    return f"{inst.bench}/.cwcli/.bench-label"


def _marker_label(inst) -> str | None:
    """The label recorded in the in-container marker file, or None if absent."""
    code, out = harness.exec_in_frappe(inst.name, f"cat {_marker_path(inst)} 2>/dev/null")
    if code != 0 or not out.strip():
        return None
    try:
        return json.loads(out.strip()).get("label")
    except (json.JSONDecodeError, ValueError):
        return None


def _cached_label(inst) -> str | None:
    """The label recorded in the SQLite cache, read through `axi benches`.

    Uses the agent surface deliberately: it is the structured read of the cache,
    so this covers the new discovery verb against a real instance at the same time.
    """
    result = harness.run_cwcli("axi", "benches", inst.name)
    assert result.returncode == 0, result.stdout + result.stderr
    out = harness.strip_ansi(result.stdout)
    for line in out.splitlines():
        if LABEL in line:
            return LABEL
    return None


@pytest.fixture()
def inspected_instance(running_instance):
    """The session instance with a populated bench cache (label needs one)."""
    result = harness.run_cwcli("inspect", running_instance.name)
    assert result.returncode == 0, result.stdout + result.stderr
    return running_instance


def _clear_label(inst):
    """Best-effort cleanup so the shared session instance is left as found."""
    harness.run_cwcli("label", inst.name, "0", "--clear")


def test_label_set_then_clear_keeps_both_stores_consistent(inspected_instance):
    """The invariant end to end: set -> BOTH stores carry it; clear -> BOTH lose it."""
    inst = inspected_instance
    try:
        set_result = harness.run_cwcli("label", inst.name, "0", LABEL)
        assert set_result.returncode == 0, set_result.stdout + set_result.stderr

        # BOTH stores, read for real - the marker from inside the container, the
        # cache through the structured verb.
        assert _marker_label(inst) == LABEL, "marker file does not carry the label"
        assert _cached_label(inst) == LABEL, "cache does not carry the label"

        clear_result = harness.run_cwcli("label", inst.name, "0", "--clear")
        assert clear_result.returncode == 0, clear_result.stdout + clear_result.stderr

        assert _marker_label(inst) is None, "marker file survived the clear"
        assert _cached_label(inst) is None, "cache label survived the clear"
    finally:
        _clear_label(inst)


def test_a_cleared_label_is_not_resurrected_by_a_later_inspect(inspected_instance):
    """The reason the clear path writes the marker FIRST.

    The marker is the source of truth for label recovery, so a cleared cache with
    a surviving marker would let a full `inspect` restore a label the user just
    deleted. This proves the ordering holds against a real container: only a real
    inspect reading a real marker can.
    """
    inst = inspected_instance
    try:
        assert harness.run_cwcli("label", inst.name, "0", LABEL).returncode == 0
        assert _marker_label(inst) == LABEL

        assert harness.run_cwcli("label", inst.name, "0", "--clear").returncode == 0

        # A full inspect rebuilds labels from the markers; the marker is gone, so
        # there is nothing to resurrect.
        insp = harness.run_cwcli("inspect", inst.name)
        assert insp.returncode == 0, insp.stdout + insp.stderr

        assert _cached_label(inst) is None, "inspect resurrected the cleared label"
        assert _marker_label(inst) is None
    finally:
        _clear_label(inst)


def test_label_survives_a_cache_wipe_via_the_marker(inspected_instance):
    """The marker's whole purpose: a label outlives the cache it was written to."""
    inst = inspected_instance
    try:
        assert harness.run_cwcli("label", inst.name, "0", LABEL).returncode == 0

        # A full re-inspect re-derives the cache from the live bench; the label
        # comes back only because the marker inside the container carries it.
        insp = harness.run_cwcli("inspect", inst.name)
        assert insp.returncode == 0, insp.stdout + insp.stderr

        assert _cached_label(inst) == LABEL, "label did not survive a re-inspect"
    finally:
        _clear_label(inst)


def test_axi_label_sets_for_real_and_emits_toon(inspected_instance):
    """The agent surface: one TOON document, and a real two-store write."""
    inst = inspected_instance
    try:
        result = harness.run_cwcli("axi", "label", inst.name, "--bench", "0", "--set", LABEL)

        assert result.returncode == 0, result.stdout + result.stderr
        out = harness.strip_ansi(result.stdout)
        assert "cleared: false" in out, out
        assert f"label: {LABEL}" in out, out
        assert _marker_label(inst) == LABEL
    finally:
        _clear_label(inst)


def test_axi_benches_answers_what_bench_accepts(inspected_instance):
    """The dead end this batch exists to close: an agent can discover --bench values."""
    inst = inspected_instance
    result = harness.run_cwcli("axi", "benches", inst.name)

    assert result.returncode == 0, result.stdout + result.stderr
    out = harness.strip_ansi(result.stdout)
    assert "benches[" in out, out
    assert inst.bench in out, out  # the real bench path, from the real cache
    assert "index: 0" in out or "0," in out, out


def test_label_refuses_a_numeric_label(inspected_instance):
    """A numeric label would collide with a bench index; exit non-zero, write nothing."""
    inst = inspected_instance
    result = harness.run_cwcli("label", inst.name, "0", "7")

    assert result.returncode != 0
    assert _marker_label(inst) is None
