"""Top-level pytest config for the two-tier suite.

The suite is split into a fast ``unit`` tier (no Docker) and a real-Docker
``e2e`` / ``e2e_p2p`` tier (``tests/e2e/``). Rather than hand-mark every legacy
test, this hook auto-applies the ``unit`` marker to any collected test that is
not already marked ``e2e`` / ``e2e_p2p``. That way:

- a bare ``pytest`` (default ``-m "not e2e and not e2e_p2p and not e2e_pkg"``) runs the unit tier,
- the unit CI job (``-m unit``) selects the same set, and
- ``-m e2e`` selects only the real-Docker tier.

During the parallel-run migration off the mock suite, the legacy container-mock
tests are carried in the ``unit`` tier alongside the permanent mock-free
pure-logic tests; they are retired per command as each command's real E2E lands
(see ``openspec/changes/rebuild-e2e-test-suite``). The end state is a ``unit``
tier of only mock-free pure-logic tests.

Quiet per-file reporting (test-tooling only, no behaviour change)
-----------------------------------------------------------------
Nearly every run is green, so a green run says as little as it can: one line per
test FILE (path, verdict, wall seconds) plus one total coverage %. A red run
additionally gets pytest's own stock FAILURES section - the failing test's name
and its traceback - because a red run that does not say what broke just costs a
second run.

The mechanics are subtractive:

- ``pytest_report_teststatus`` returns an empty letter AND word, which makes
  pytest's terminal reporter return before writing any per-test progress - but
  only after it has counted the report, so every total and the FAILURES /
  ERRORS sections stay intact. The category must still be the one pytest would
  have picked, or the report lands in the wrong bucket.
- ``-q`` (in ``pyproject.toml`` addopts) drops the session header and the
  per-file path prefix that pytest writes at verbosity 0.
- The per-file line goes through pytest's own terminal writer, so colour appears
  only when it genuinely owns a TTY. Never set ``FORCE_COLOR`` to get colour into
  CI logs: it leaks ANSI into the app-under-test's own stdout and breaks
  string-match assertions (e.g. ``test_where.py::...::test_no_match_plain_message``).

Only the fast tier is trimmed. Anything at verbosity >= 0 (``-v``) is handed back
to pytest's stock reporting untouched, which is how the minutes-long E2E tier
keeps its per-test liveness - ``e2e.yml`` runs it with ``-o addopts="" -v``, so a
long silence there would read the same as a hang.
"""

from __future__ import annotations

import io

import pytest

# Set once in pytest_configure; everything below is a no-op unless it is True.
_quiet = False
_reporter = None
# test file -> [total seconds, passed count, failed count]
_files: dict[str, list[float]] = {}
# test file -> nodeid of its last selected test, i.e. when to print its line.
_last_nodeid: dict[str, str] = {}


@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    # trylast: a conftest is registered after the core plugins, so it is called
    # FIRST by default - before _pytest.terminal has registered its reporter.
    global _quiet, _reporter
    _quiet = config.option.verbose < 0
    _reporter = config.pluginmanager.get_plugin("terminalreporter")


def pytest_collection_modifyitems(config, items):
    for item in items:
        if not (
            item.get_closest_marker("e2e")
            or item.get_closest_marker("e2e_p2p")
            or item.get_closest_marker("e2e_pkg")
        ):
            item.add_marker(pytest.mark.unit)


def pytest_collection_finish(session):
    # session.items is post-deselection, so the last nodeid per file is one that
    # will actually run.
    for item in session.items:
        _last_nodeid[item.nodeid.split("::")[0]] = item.nodeid


def pytest_report_teststatus(report, config):
    """Drop pytest's per-test progress output; the per-file line replaces it.

    Returns exactly what ``_pytest.runner`` / ``_pytest.terminal`` would have,
    with only the LETTER - the per-test progress character - blanked. The other
    two fields are load-bearing and must not be trimmed:

    - the category feeds the run totals, and a passing setup/teardown MUST stay
      uncategorised, or every test is counted once per phase and 994 passed
      reads as 2982;
    - the word is what ``short_test_summary`` prints as each failure's
      ``FAILED`` / ``ERROR`` prefix, so blanking it makes a red run unable to
      tell a failure from an error.
    """
    if not _quiet or hasattr(report, "wasxfail"):
        return None
    if report.when != "call":
        if report.failed:
            return "error", "", "ERROR"
        if report.skipped:
            return "skipped", "", "SKIPPED"
        return "", "", ""
    return report.outcome, "", report.outcome.upper()


def pytest_runtest_logreport(report):
    if not _quiet:
        return
    stat = _files.setdefault(report.nodeid.split("::")[0], [0.0, 0, 0])
    stat[0] += report.duration
    if report.failed:
        stat[2] += 1
    elif report.when == "call" and report.passed:
        stat[1] += 1


def pytest_runtest_logfinish(nodeid):
    if not _quiet or _reporter is None:
        return
    path = nodeid.split("::")[0]
    if _last_nodeid.get(path) != nodeid:
        return
    seconds, passed, failed = _files.get(path, [0.0, 0, 0])
    if failed:
        verdict, markup = "FAIL", {"red": True}
    elif passed:
        verdict, markup = "PASS", {"green": True}
    else:
        verdict, markup = "SKIP", {"yellow": True}
    _reporter.write_line(f"{path:<58}{verdict:<6}{seconds:6.2f}s", **markup)


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """One total coverage % for the run, in place of pytest-cov's per-file table.

    ``--cov-report=`` (empty, in addopts) silences the plugin's own report; the
    total is read back off the same coverage data it already collected, which
    ``cov_controller.finish()`` has written by the time summaries run.
    """
    controller = getattr(config.pluginmanager.get_plugin("_cov"), "cov_controller", None)
    if controller is None:
        return  # no --cov on this run
    total = controller.cov.report(ignore_errors=True, file=io.StringIO())
    terminalreporter.write_line(f"coverage: {total:.2f}%")
