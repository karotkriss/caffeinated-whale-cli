"""Top-level pytest config for the two-tier suite.

The suite is split into a fast ``unit`` tier (no Docker) and a real-Docker
``e2e`` / ``e2e_p2p`` tier (``tests/e2e/``). Rather than hand-mark every legacy
test, this hook auto-applies the ``unit`` marker to any collected test that is
not already marked ``e2e`` / ``e2e_p2p``. That way:

- a bare ``pytest`` (default ``-m "not e2e and not e2e_p2p"``) runs the unit tier,
- the unit CI job (``-m unit``) selects the same set, and
- ``-m e2e`` selects only the real-Docker tier.

During the parallel-run migration off the mock suite, the legacy container-mock
tests are carried in the ``unit`` tier alongside the permanent mock-free
pure-logic tests; they are retired per command as each command's real E2E lands
(see ``openspec/changes/rebuild-e2e-test-suite``). The end state is a ``unit``
tier of only mock-free pure-logic tests.

Timing & description reporting (test-tooling only, no behaviour change)
----------------------------------------------------------------------
The suite's raw ``file::test_name`` output is opaque to a newcomer and hides how
long the slow bits take, so this file also layers pure-observability reporting on
top (nothing here changes what a test asserts):

- **Per-test time + a human-readable description, inline** in the ``-v`` line
  (``pytest_report_teststatus``). The description is the test's docstring first
  line when it has one, else its function name humanised (``test_stopped_container
  _returns_false`` -> "stopped container returns false"). So every test explains
  itself alongside its name, no filename-decoding needed. Docstring-first is the
  convention now; the reworded-name fallback is the FLOOR so nothing regresses.
- **A single "two-faced" end-of-run summary** (``pytest_terminal_summary`` ->
  ``tests/_reporting.py``): one report, two faces from one code path. Cockpit
  (colour) when stdout is a colour-capable terminal, Ledger (plain aligned
  columns) otherwise, chosen from the reporter's own markup capability. Colour is
  forced only on the reporter's own ``rich.Console``, never via ``FORCE_COLOR``
  (which leaks ANSI into the app-under-test's stdout). Its sections: a header
  verdict line, the E2E ``cwcli init`` / bench-build pole (highlighted, and only
  shown when ``config._cwcli_e2e_init_seconds`` was recorded - the fast tier has
  no bench build, so it is absent there), the slowest tests with descriptions,
  a per-file rollup, and honest setup/call/teardown phase totals.
- The init pole times itself in ``e2e/conftest.py`` and stashes the result on
  ``config``; a session-scoped fixture's setup is dispatched above the ``tests/``
  conftest, so a generic per-test timer here cannot observe it.

``--durations`` (set in ``pyproject.toml`` addopts) gives the built-in
slowest-N view on top. All of this surfaces in CI logs, where the E2E matrix runs.
"""

from __future__ import annotations

import inspect

import pytest

from ._reporting import build_report, render

# nodeid -> one-line human description, built at collection time.
_descriptions: dict[str, str] = {}
# nodeid -> total measured seconds (setup + call + teardown), for the summary.
_test_durations: dict[str, float] = {}
# Wall-honest phase totals (each test's phase duration is measured once by pytest).
_phase_totals: dict[str, float] = {"setup": 0.0, "call": 0.0, "teardown": 0.0}


def _describe(item) -> str:
    """A one-line human description of a test: its docstring's first line, else
    its function name humanised."""
    try:
        doc = inspect.getdoc(item.obj)
    except Exception:  # noqa: BLE001 - non-function items (doctests etc.); fall back to name
        doc = None
    if doc:
        return doc.strip().splitlines()[0].strip()
    name: str = getattr(item, "originalname", None) or item.name
    stem = name[5:] if name.startswith("test_") else name
    return stem.replace("_", " ").strip()


def pytest_collection_modifyitems(config, items):
    for item in items:
        if not (item.get_closest_marker("e2e") or item.get_closest_marker("e2e_p2p")):
            item.add_marker(pytest.mark.unit)
        _descriptions[item.nodeid] = _describe(item)


def pytest_report_teststatus(report, config):
    """Append per-test time + description to the ``-v`` PASSED line, so the
    duration and a plain-English summary sit right alongside the test name.

    Only the passed call-phase line is augmented; failures, skips, and
    xfail/xpass keep pytest's default rendering (and its counters) untouched.
    """
    if report.when != "call" or not report.passed or hasattr(report, "wasxfail"):
        return None
    word = f"PASSED {report.duration:6.3f}s"
    desc = _descriptions.get(report.nodeid)
    if desc:
        word += f"  · {desc}"
    return "passed", ".", (word, {"green": True})


def pytest_runtest_logreport(report):
    _phase_totals[report.when] = _phase_totals.get(report.when, 0.0) + report.duration
    # Per-test total across all three phases, for the slowest-tests table.
    _test_durations[report.nodeid] = _test_durations.get(report.nodeid, 0.0) + report.duration


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Render the single two-faced summary (see tests/_reporting.py).

    Colour vs plain is decided from the reporter's own markup capability, so
    colour is confined to our own rich Console and never leaks via FORCE_COLOR.
    """
    report = build_report(terminalreporter, config, _phase_totals, _test_durations, _descriptions)
    tw = terminalreporter._tw
    block = render(report, color=tw.hasmarkup, width=tw.fullwidth)
    terminalreporter.write("\n")
    terminalreporter.write(block)
