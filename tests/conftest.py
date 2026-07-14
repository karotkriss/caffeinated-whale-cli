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
  itself alongside its name, no filename-decoding needed.
- **The E2E ``cwcli init`` / bench-build pole reported SEPARATELY and prominently**
  in ``pytest_terminal_summary``. That one session-scoped step is what makes the
  E2E matrix ~10-30+ min; splitting it out makes "init vs test time" obvious at a
  glance instead of buried in per-test durations. It times itself in
  ``e2e/conftest.py`` and stashes the result on ``config`` - a session-scoped
  fixture's setup is dispatched above the ``tests/`` conftest, so the generic
  ``pytest_fixture_setup`` timer here cannot observe it.
- **Honest setup / call / teardown totals**, and the slowest function/module-scoped
  shared fixtures (``pytest_fixture_setup`` timer), from the per-test phase reports.

``--durations`` (set in ``pyproject.toml`` addopts) gives the built-in
slowest-N view on top. All of this surfaces in CI logs, where the E2E matrix runs.
"""

from __future__ import annotations

import inspect
import time

import pytest

# nodeid -> one-line human description, built at collection time.
_descriptions: dict[str, str] = {}
# fixture argname -> [setup_count, total_setup_seconds, scope], from the setup timer.
_fixture_stats: dict[str, list] = {}
# Wall-honest phase totals (each test's phase duration is measured once by pytest).
_phase_totals: dict[str, float] = {"setup": 0.0, "call": 0.0, "teardown": 0.0}

# Friendly labels for the shared fixtures worth explaining in the timing summary.
# The E2E init pole (session_instance) is timed and reported separately (it is
# session-scoped, which the generic pytest_fixture_setup wrapper below does not
# see - its setup is dispatched above the tests/ conftest); see e2e/conftest.py.
_NOTABLE_FIXTURES = {
    "running_instance": "ensure the frappe container is running",
    "isolated_home": "temp HOME + CWCLI_HOME isolation rail",
    "port_allocator": "hand out non-overlapping port bases",
}


def _describe(item) -> str:
    """A one-line human description of a test: its docstring's first line, else
    its function name humanised."""
    try:
        doc = inspect.getdoc(item.obj)
    except Exception:  # noqa: BLE001 - non-function items (doctests etc.); fall back to name
        doc = None
    if doc:
        return doc.strip().splitlines()[0].strip()
    name = getattr(item, "originalname", None) or item.name
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


@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(fixturedef, request):
    """Time fixture setups so the summary can call out the slow shared ones.

    Catches function/module/class-scoped fixtures; the session-scoped init pole is
    timed explicitly in e2e/conftest.py (its setup is dispatched above this
    conftest, so this wrapper never sees it)."""
    start = time.perf_counter()
    try:
        return (yield)
    finally:
        dur = time.perf_counter() - start
        st = _fixture_stats.setdefault(fixturedef.argname, [0, 0.0, fixturedef.scope])
        st[0] += 1
        st[1] += dur


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    tw = terminalreporter
    tw.write_sep("=", "timing summary", cyan=True)
    tw.write_line(f"  fixture setup total : {_phase_totals['setup']:9.2f}s")
    tw.write_line(f"  test call total     : {_phase_totals['call']:9.2f}s")
    tw.write_line(f"  teardown total      : {_phase_totals['teardown']:9.2f}s")

    # The E2E init pole: separate and unmissable, and flagged as NOT per-test
    # time. Stashed on config by e2e/conftest.py's session_instance fixture.
    init = getattr(config, "_cwcli_e2e_init_seconds", None)
    if init is not None:
        tw.write_sep("-", "E2E session init (the cwcli init / bench-build pole)")
        tw.write_line(
            f"  cwcli init : {init:9.2f}s  "
            f"<- this one build dominates the E2E matrix; it is NOT per-test time",
            yellow=True,
        )

    slow = sorted(
        (item for item in _fixture_stats.items() if item[1][1] >= 0.05),
        key=lambda kv: kv[1][1],
        reverse=True,
    )[:15]
    if slow:
        tw.write_sep("-", "slowest fixtures (cumulative setup)")
        for name, (count, total, scope) in slow:
            label = _NOTABLE_FIXTURES.get(name, "")
            suffix = f"  - {label}" if label else ""
            tw.write_line(f"  {total:9.2f}s  {name} [{scope}, x{count}]{suffix}")
