"""A failed ``bench list-apps`` must never cache a failure sentinel.

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

Since the batched full read, a failed read is ``None`` in the bench facts
(``core.bench_read.SiteRead.list_apps``) with its cause in ``SiteRead.error``, and
the dict builder ``_bench_dict`` turns it into ``[]`` plus the warning naming the
bench and the cause. These tests drive that builder.
"""

from __future__ import annotations

from caffeinated_whale_cli.commands import inspect as inspect_cmd_mod
from caffeinated_whale_cli.core import bench_read
from caffeinated_whale_cli.core import inspect as core_inspect

BENCH = "/home/frappe/frappe-bench"


def _get_installed_apps(site, list_apps, error=None):
    events: list = []
    read = bench_read.BenchRead(
        path=BENCH,
        available_apps=[],
        app_imports={},
        common_site_config=None,
        sites=[
            bench_read.SiteRead(
                name=site, site_config=None, list_apps=list_apps, installed=None, error=error
            )
        ],
        current_site=None,
        label=None,
    )
    bench = core_inspect._bench_dict(read, events.append)
    return bench["sites"][0]["installed_apps"], events


def test_error_path_returns_empty_not_sentinel():
    apps, events = _get_installed_apps(
        "site.local", None, error="frappe.connect failed (OperationalError)"
    )
    # The honest unknown state - never a poisoned sentinel string.
    assert apps == []
    # The failure is surfaced as a warning event, not folded into the returned data.
    warnings = [e for e in events if isinstance(e, core_inspect.InspectWarning)]
    assert len(warnings) == 1
    assert warnings[0].text == (
        f"Failed to list apps for site 'site.local' "
        f"({BENCH}: frappe.connect failed (OperationalError))."
    )
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
    apps, events = _get_installed_apps("site.local", ["frappe", "erpnext"])
    assert apps == ["frappe", "erpnext"]
    assert not any(isinstance(e, core_inspect.InspectWarning) for e in events)


def test_genuinely_no_apps_and_failure_both_cache_as_empty():
    # A site with no apps (exit 0, empty output) and a site whose list-apps failed
    # (exit != 0) must be indistinguishable in the cached data: both [].
    no_apps, _ = _get_installed_apps("empty.local", [])
    failed, _ = _get_installed_apps("broken.local", None, error="list-apps failed (KeyError)")
    assert no_apps == failed == []
