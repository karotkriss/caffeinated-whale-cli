"""``core.inspect._get_installed_apps`` must never cache a failure sentinel.

Root cause
----------
On ``bench list-apps`` failure ``_get_installed_apps`` used to return
``["Error fetching apps for site <site>"]``. That sentinel string was persisted
verbatim into the project cache (``inspect`` writes ``installed_apps`` straight
from this return), re-emitted forever by the read-only partial-refresh path, and
rendered as a fake green "app" in the inspect tree.

The fix: on a non-zero exit, return ``[]`` (the honest "nothing to record /
unknown" state - identical to a site that genuinely has no apps) and surface the
failure OUT OF BAND instead of into the returned/cached data.

Changed BY DESIGN in the ``migrate-inspect-core`` migration: the helper now lives
on the core and surfaces the failure as a typed ``InspectWarning`` EVENT (the core
never prints); the frontend renders that event as the same stderr warning as
before, which the rendering test below pins.
"""

from __future__ import annotations

from caffeinated_whale_cli.commands import inspect as inspect_cmd_mod
from caffeinated_whale_cli.core import inspect as core_inspect

BENCH = "/home/frappe/frappe-bench"


class _Container:
    """Minimal frappe container returning a fixed (exit_code, output) for list-apps."""

    def __init__(self, exit_code: int, output: bytes):
        self._result = (exit_code, output)

    def exec_run(self, cmd, workdir=None):
        return self._result


def _get_installed_apps(container, site):
    events: list = []
    apps = core_inspect._get_installed_apps(container, BENCH, site, events.append)
    return apps, events


def test_error_path_returns_empty_not_sentinel():
    apps, events = _get_installed_apps(_Container(1, b"Traceback: boom"), "site.local")
    # The honest unknown state - never a poisoned sentinel string.
    assert apps == []
    # The failure is surfaced as a warning event, not folded into the returned data.
    warnings = [e for e in events if isinstance(e, core_inspect.InspectWarning)]
    assert len(warnings) == 1
    assert "site.local" in warnings[0].text
    assert not any("Error fetching apps" in a for a in apps)


def test_warning_event_renders_as_todays_stderr_warning(capsys):
    """The frontend renders the warning UNCONDITIONALLY (not -v-gated), with
    today's exact wording."""
    inspect_cmd_mod._render_event(
        core_inspect.InspectWarning(text="Failed to list apps for site 'site.local'."),
        verbose=False,
    )
    err = capsys.readouterr().err
    assert "Warning:" in err
    assert "Failed to list apps for site 'site.local'." in err


def test_success_path_parses_app_lines():
    apps, _events = _get_installed_apps(_Container(0, b"frappe\nerpnext\n"), "site.local")
    assert apps == ["frappe", "erpnext"]


def test_genuinely_no_apps_and_failure_both_cache_as_empty():
    # A site with no apps (exit 0, empty output) and a site whose list-apps failed
    # (exit != 0) must be indistinguishable in the cached data: both [].
    no_apps, _ = _get_installed_apps(_Container(0, b""), "empty.local")
    failed, _ = _get_installed_apps(_Container(1, b"boom"), "broken.local")
    assert no_apps == failed == []
