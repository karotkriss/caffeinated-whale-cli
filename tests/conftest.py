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
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

try:
    import resource
except ImportError:  # Windows has no `resource` module.
    resource = None  # type: ignore[assignment]

import pytest

# ------------------------------------------------------------- hermetic session home
#
# cwcli resolves its ENTIRE on-disk footprint (config, projects, run dir, and the
# SQLite cache) from ``config_utils.cwcli_home()``, which reads ``CWCLI_HOME``
# first. ``db_utils`` then CREATES and OPENS the cache DB at import time. So a bare
# ``pytest`` used to open the developer's real ``~/.cwcli/cache/cwc-cache.db`` the
# moment the first test imported the package - the unit tier was not hermetic.
#
# Point ``CWCLI_HOME`` at a throwaway dir for the whole session, BEFORE any test
# module (and therefore the package) is imported. Tests that need their own home
# still override ``CWCLI_HOME`` per-test via monkeypatch. The E2E tier sets its own
# HOME + CWCLI_HOME (tests/e2e/conftest.py), so this default is harmless there.
#
# ``Path.home()`` here is the operator's REAL home (we override CWCLI_HOME, not
# HOME); snapshot the real cache DB so the guard below can fail loudly if any test
# still reaches it.
_REAL_CACHE_DB = Path.home() / ".cwcli" / "cache" / "cwc-cache.db"
_real_cache_db_before = _REAL_CACHE_DB.stat().st_mtime_ns if _REAL_CACHE_DB.exists() else None

_created_session_home: str | None = None
if not os.environ.get("CWCLI_HOME"):
    # Prefer /var/tmp on Linux: it is not a small tmpfs like /tmp, and it keeps the
    # session home out of the ``/tmp/cwcli`` namespace a hardcoded-path test guards
    # against (test_auto_inspect.py::test_pid_dir_honors_cwcli_home).
    _tmproot = "/var/tmp" if os.path.isdir("/var/tmp") else None
    _created_session_home = tempfile.mkdtemp(prefix="cwcli-unit-home-", dir=_tmproot)
    os.environ["CWCLI_HOME"] = _created_session_home

# ----------------------------------------------------------- memory / slow-test guard
#
# The unit tier must stay small and fast. A test that spins a real deadline (a
# no-op ``time.sleep`` against a real ``time.monotonic()`` timeout) while a
# ``MagicMock`` records every call in ``mock_calls`` can climb to gigabytes over
# minutes, then free it at teardown - exactly the 5 GB spike that motivated this
# guard. It fails the unit tier when the pytest process peaks above a ceiling OR
# any single test runs too long, and names the offenders, so that class of
# regression can never land silently. Enforced only on the unit tier (see
# ``pytest_collection_finish``); tune or disable via env.
_RSS_CEILING_MB = float(os.environ.get("CWCLI_TEST_RSS_CEILING_MB", "1024"))
_SLOW_FAIL_S = float(os.environ.get("CWCLI_TEST_SLOW_FAIL_S", "10"))
_SLOW_WARN_S = float(os.environ.get("CWCLI_TEST_SLOW_WARN_S", "5"))
_GUARD_ENABLED = os.environ.get("CWCLI_TEST_GUARD", "1") != "0"

_enforce_guard = False  # set True for the unit tier in pytest_collection_finish
_peak_rss_mb = 0.0
_test_cost: dict[str, tuple[float, float]] = {}  # nodeid -> (wall seconds, rss delta MB)
_guard_failures: list[str] = []
_guard_evaluated = False


def _rss_mb() -> float:
    if resource is None:
        return 0.0
    maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # ``ru_maxrss`` is KiB on Linux, bytes on macOS.
    return maxrss / (1024 * 1024) if sys.platform == "darwin" else maxrss / 1024


def _real_home_touch_failure() -> str | None:
    after = _REAL_CACHE_DB.stat().st_mtime_ns if _REAL_CACHE_DB.exists() else None
    if after == _real_cache_db_before:
        return None
    return (
        f"HERMETICITY VIOLATION: the unit tier touched the real cache DB {_REAL_CACHE_DB} "
        f"(mtime {_real_cache_db_before} -> {after}). A test resolved cwcli_home() to the "
        f"real ~/.cwcli instead of the session CWCLI_HOME; give it its own "
        f"CWCLI_HOME (monkeypatch.setenv) pointed at tmp_path."
    )


def _evaluate_guard() -> None:
    """Populate ``_guard_failures`` once. Called by the summary and sessionfinish."""
    global _guard_evaluated
    if _guard_evaluated:
        return
    _guard_evaluated = True
    if not (_GUARD_ENABLED and _enforce_guard):
        return
    if _peak_rss_mb > _RSS_CEILING_MB:
        _guard_failures.append(
            f"peak RSS {_peak_rss_mb:.0f}MB exceeded the {_RSS_CEILING_MB:.0f}MB unit-tier "
            f"ceiling (override with CWCLI_TEST_RSS_CEILING_MB)"
        )
    for nodeid, (wall, _delta) in _test_cost.items():
        if wall >= _SLOW_FAIL_S:
            _guard_failures.append(
                f"unit test ran {wall:.1f}s (> {_SLOW_FAIL_S:.0f}s; a unit test must control "
                f"time, not wait a real deadline): {nodeid}"
            )
    touch = _real_home_touch_failure()
    if touch:
        _guard_failures.append(touch)


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
    # The memory/slow guard is a unit-tier contract: the E2E, packaging, and
    # standalone tiers legitimately run long and are driven through subprocesses,
    # so enforce only when NOTHING selected carries one of those markers.
    global _enforce_guard
    _off = ("e2e", "e2e_p2p", "e2e_pkg", "standalone")
    _enforce_guard = not any(item.get_closest_marker(m) for item in session.items for m in _off)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    """Sample the process peak RSS and wall time around each unit test."""
    if not (_GUARD_ENABLED and _enforce_guard):
        yield
        return
    before = _rss_mb()
    start = time.monotonic()
    yield
    wall = time.monotonic() - start
    after = _rss_mb()
    global _peak_rss_mb
    _peak_rss_mb = max(_peak_rss_mb, after)
    _test_cost[item.nodeid] = (wall, max(0.0, after - before))


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
    if controller is not None:  # only when --cov collected data
        total = controller.cov.report(ignore_errors=True, file=io.StringIO())
        terminalreporter.write_line(f"coverage: {total:.2f}%")

    # Memory / slow-test guard (unit tier only).
    _evaluate_guard()
    if not (_GUARD_ENABLED and _enforce_guard):
        return
    slow = sorted(
        (wall, nodeid) for nodeid, (wall, _d) in _test_cost.items() if wall >= _SLOW_WARN_S
    )
    if slow:
        terminalreporter.write_line(f"slow unit tests (>= {_SLOW_WARN_S:.0f}s):", yellow=True)
        for wall, nodeid in sorted(slow, reverse=True)[:10]:
            terminalreporter.write_line(f"  {wall:6.1f}s  {nodeid}")
    if _guard_failures:
        terminalreporter.write_line("MEMORY/SLOW GUARD FAILED:", red=True)
        for msg in _guard_failures:
            terminalreporter.write_line(f"  - {msg}", red=True)
        heavy = sorted(
            (delta, wall, nodeid) for nodeid, (wall, delta) in _test_cost.items() if delta >= 50
        )
        if heavy:
            terminalreporter.write_line("heaviest tests (peak RSS delta):", red=True)
            for delta, wall, nodeid in sorted(heavy, reverse=True)[:10]:
                terminalreporter.write_line(f"  +{delta:7.0f}MB  {wall:6.1f}s  {nodeid}")
        terminalreporter.write_line(f"peak process RSS this run: {_peak_rss_mb:.0f}MB")


def pytest_sessionfinish(session, exitstatus):
    """Fail the unit tier when the memory/slow guard tripped; drop the temp home."""
    _evaluate_guard()
    if _guard_failures and session.exitstatus == 0:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    if _created_session_home:
        shutil.rmtree(_created_session_home, ignore_errors=True)
