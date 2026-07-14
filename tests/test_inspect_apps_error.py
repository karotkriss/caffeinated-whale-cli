"""``inspect._get_installed_apps`` must never cache a failure sentinel.

Root cause
----------
On ``bench list-apps`` failure ``_get_installed_apps`` used to return
``["Error fetching apps for site <site>"]``. That sentinel string was persisted
verbatim into the project cache (``inspect`` writes ``installed_apps`` straight
from this return), re-emitted forever by the read-only partial-refresh path, and
rendered as a fake green "app" in the inspect tree.

The fix: on a non-zero exit, return ``[]`` (the honest "nothing to record /
unknown" state - identical to a site that genuinely has no apps) and surface the
failure to stderr instead of into the returned/cached data.
"""

from __future__ import annotations

from caffeinated_whale_cli.commands import inspect as inspect_mod

BENCH = "/home/frappe/frappe-bench"


class _Container:
    """Minimal frappe container returning a fixed (exit_code, output) for list-apps."""

    def __init__(self, exit_code: int, output: bytes):
        self._result = (exit_code, output)

    def exec_run(self, cmd, workdir=None):
        return self._result


def test_error_path_returns_empty_not_sentinel(capsys):
    c = _Container(1, b"Traceback: boom")
    apps = inspect_mod._get_installed_apps(c, BENCH, "site.local")
    # The honest unknown state - never a poisoned sentinel string.
    assert apps == []
    # The failure is surfaced to stderr, not folded into the returned data.
    err = capsys.readouterr().err
    assert "site.local" in err
    assert not any("Error fetching apps" in a for a in apps)


def test_success_path_parses_app_lines():
    c = _Container(0, b"frappe\nerpnext\n")
    assert inspect_mod._get_installed_apps(c, BENCH, "site.local") == ["frappe", "erpnext"]


def test_genuinely_no_apps_and_failure_both_cache_as_empty():
    # A site with no apps (exit 0, empty output) and a site whose list-apps failed
    # (exit != 0) must be indistinguishable in the cached data: both [].
    no_apps = inspect_mod._get_installed_apps(_Container(0, b""), BENCH, "empty.local")
    failed = inspect_mod._get_installed_apps(_Container(1, b"boom"), BENCH, "broken.local")
    assert no_apps == failed == []
