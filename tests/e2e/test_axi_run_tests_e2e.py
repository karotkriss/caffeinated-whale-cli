"""``cwcli axi run-tests`` reports a failed Frappe suite as failed, real Docker.

The reported bug (2026-09-28, a v15 bench): the test output said ``FAILED
(failures=2, errors=3)`` but ``axi run-tests`` returned ``ok: true`` and exit 0.
Root cause, in Frappe rather than cwcli: v14/v15's ``bench run-tests`` computes the
failure count and then exits non-zero only ``if os.environ.get("CI")``, so without
``CI`` the exit code says success for a failed suite (v16 exits 1 itself). cwcli
therefore also reads the unittest summary. ``CI`` is deliberately not set to force
the exit code: Frappe reads it to run background jobs inline and to skip the
``allow_tests`` guard, which would change the conditions the suite runs under.

Unit tests (``tests/test_core_bench_ops.py``) fake the exec; this is the proof
against a real bench, on every Frappe major. The probe suites are plain
``unittest`` modules dropped into the ``frappe`` app tree and removed afterwards -
nothing is installed or registered, so the shared bench cannot be left broken.
"""

from __future__ import annotations

import shlex
from dataclasses import replace

import pytest

from . import harness
from .conftest import SESSION_ADMIN_PW

pytestmark = [pytest.mark.e2e]

_PROBE = "test_cwcli_run_tests_probe"
_TEST_SITE = "cwcli-run-tests.localhost"
_DB_ROOT_PASSWORD = "123"
_FAILING = """\
import unittest


class Probe(unittest.TestCase):
    def test_fails(self):
        self.fail("deliberate failure")

    def test_errors(self):
        raise RuntimeError("deliberate error")

    def test_passes(self):
        pass
"""
_PASSING = """\
import unittest


class Probe(unittest.TestCase):
    def test_passes(self):
        pass
"""


def _site_config(inst) -> str:
    return f"{inst.bench}/sites/{inst.site}/site_config.json"


def _set_allow_tests(inst, enabled: bool) -> None:
    """Add or remove the key by editing the file, so the shared site ends exactly
    as it began (`set-config allow_tests false` would leave the key behind)."""
    script = (
        "import json,sys; p=sys.argv[1]; c=json.load(open(p)); "
        + ("c['allow_tests']=True" if enabled else "c.pop('allow_tests', None)")
        + "; json.dump(c, open(p,'w'), indent=1)"
    )
    code, out = harness.exec_in_frappe(
        inst.name, f"python3 -c {shlex.quote(script)} {shlex.quote(_site_config(inst))}"
    )
    assert code == 0, out


def _probe_path(inst) -> str:
    return f"{inst.bench}/apps/frappe/frappe/tests/{_PROBE}.py"


def _write_probe(inst, source: str) -> None:
    code, out = harness.exec_in_frappe(
        inst.name, f"printf %s {shlex.quote(source)} > {shlex.quote(_probe_path(inst))}"
    )
    assert code == 0, out


def _run_tests(inst):
    res = harness.run_cwcli(
        "axi",
        "run-tests",
        inst.name,
        "--site",
        inst.site,
        "--app",
        "frappe",
        "--module",
        f"frappe.tests.{_PROBE}",
    )
    return res, harness.strip_ansi(res.stdout)


@pytest.fixture()
def probe(dedicated_test_site):
    inst = dedicated_test_site
    try:
        yield inst
    finally:
        harness.exec_in_frappe(inst.name, f"rm -f {shlex.quote(_probe_path(inst))}")
        _set_allow_tests(inst, False)


@pytest.fixture(scope="module")
def dedicated_test_site(session_instance):
    """Keep arbitrary test code and its database effects off the shared site."""
    inst = replace(session_instance, site=_TEST_SITE)
    create = harness.run_cwcli(
        "run",
        inst.name,
        "-i",
        "new-site",
        inst.site,
        "--db-root-password",
        _DB_ROOT_PASSWORD,
        "--admin-password",
        SESSION_ADMIN_PW,
        timeout=300,
    )
    assert create.returncode == 0, create.stdout + create.stderr
    try:
        yield inst
    finally:
        harness.exec_in_frappe(inst.name, f"rm -f {shlex.quote(_probe_path(inst))}")
        remove = harness.run_cwcli("rm-site", inst.name, inst.site, "--yes", timeout=300)
        assert remove.returncode == 0, remove.stdout + remove.stderr


def test_a_failed_and_erroring_suite_is_not_ok_and_exits_nonzero(probe):
    _set_allow_tests(probe, True)
    _write_probe(probe, _FAILING)

    res, out = _run_tests(probe)

    assert "FAILED" in res.stderr, res.stderr  # the failure really happened
    assert res.returncode == 1, out + res.stderr
    assert "ok: false" in out, out
    assert "failures=1" in out and "errors=1" in out, out


def test_a_passing_suite_stays_ok(probe):
    _set_allow_tests(probe, True)
    _write_probe(probe, _PASSING)

    res, out = _run_tests(probe)

    assert res.returncode == 0, out + res.stderr
    assert "ok: true" in out, out


def test_a_site_that_refuses_tests_is_not_reported_as_a_green_run(probe):
    """Without allow_tests v14/v15 print "Testing is disabled" and exit 0 having
    run nothing."""
    _set_allow_tests(probe, False)
    _write_probe(probe, _PASSING)

    res, out = _run_tests(probe)

    assert res.returncode == 1, out + res.stderr
    assert "ok: false" in out, out
    assert "allow_tests" in out, out
