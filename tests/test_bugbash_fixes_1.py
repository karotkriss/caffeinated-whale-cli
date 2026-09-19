"""Behavioral coverage for the operator bug-bash fixes (items 1-9) that were not
already pinned by an existing test.

Each test drives a real public/module function and asserts observable behavior
(return value, raised typed error, rendered output) - never source text. The
CLI-surface items (rm not-found, init name-before-password, status/restart
wording, apps-install positional, SIGPIPE) are exercised live end-to-end against
the real ``cwcli`` binary during validation; this file covers the pieces that
need a real Frappe bench to hit through the CLI but whose logic is unit-drivable.
"""

import json

import pytest
import typer

from caffeinated_whale_cli.commands import axi as axi_mod
from caffeinated_whale_cli.commands import update as update_mod
from caffeinated_whale_cli.commands.utils import render_captured_output_tail
from caffeinated_whale_cli.core import resolvers
from caffeinated_whale_cli.core import rm_bench as core_rm_bench
from caffeinated_whale_cli.core import supervision
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind

from caffeinated_whale_cli.commands import status as status_mod

from .test_apps import FakeFrappeContainer
from .test_status_frontend import _report
from .test_update_characterization import _out, _wire


# ------------------------------------------------------------- BUG-6: site resolve
#
# A mutating verb (backup/unlock/migrate/restore) called without --site on a bench
# with no configured default (the shape a fresh `cwcli init` leaves) must fall back
# to the sole site of a single-site bench, still REFUSE a multi-site bench (listing
# the sites), and never emit the misleading `cwcli inspect` tip.


def _patch_sites(monkeypatch, *, default, sites):
    monkeypatch.setattr(resolvers.db_utils, "get_default_site", lambda p, b: default)
    monkeypatch.setattr(
        resolvers.db_utils, "get_all_site_configs", lambda p, b: {s: {} for s in sites}
    )


def test_single_site_bench_falls_back_to_its_sole_site(monkeypatch):
    _patch_sites(monkeypatch, default="", sites=["only.localhost"])
    assert resolvers.resolve_sole_or_require_site("proj", "/w/b") == "only.localhost"


def test_configured_default_wins_over_the_fallback(monkeypatch):
    _patch_sites(monkeypatch, default="chosen.localhost", sites=["a", "b"])
    assert resolvers.resolve_sole_or_require_site("proj", "/w/b") == "chosen.localhost"


def test_multi_site_bench_refuses_and_lists_the_sites_without_an_inspect_tip(monkeypatch):
    _patch_sites(monkeypatch, default="", sites=["b.localhost", "a.localhost"])
    with pytest.raises(CwcliError) as exc:
        resolvers.resolve_sole_or_require_site("proj", "/w/b")
    err = exc.value
    assert err.kind is ErrorKind.NOT_FOUND
    assert err.code == "site.ambiguous"
    # Sites are listed (sorted) so the caller can pick one.
    assert "a.localhost" in err.hint and "b.localhost" in err.hint
    # No misleading inspect tip anywhere; the caller is pointed at --site.
    assert "inspect" not in (err.message + " " + (err.hint or "")).lower()
    assert "--site" in (err.message + " " + (err.hint or ""))


def test_no_sites_at_all_is_no_default_naming_site_flag_not_inspect(monkeypatch):
    _patch_sites(monkeypatch, default="", sites=[])
    with pytest.raises(CwcliError) as exc:
        resolvers.resolve_sole_or_require_site("proj", "/w/b")
    err = exc.value
    assert err.code == "site.no_default"
    assert "inspect" not in (err.message + " " + (err.hint or "")).lower()
    assert "--site" in (err.hint or "")


# ------------------------------------------------ BUG-2: real installer cause shown
#
# When installing supervisor fails, the error must stop asserting a single cause
# and must carry the captured installer output, surfaced on both the human commands
# and the axi surface.


class _FailingInstallContainer:
    """supervisor never importable; the pip install fails with a real cause."""

    def exec_run(self, cmd, **kwargs):
        script = cmd[2] if isinstance(cmd, (list, tuple)) else cmd
        if "pip install supervisor" in script:
            return (1, b"ERROR: [Errno 13] Permission denied: '/workspace/frappe-bench/env'")
        return (1, b"")  # `import supervisor` check: not installed


def _install_failure_error() -> CwcliError:
    with pytest.raises(CwcliError) as exc:
        supervision.ensure_supervisor_installed(_FailingInstallContainer(), "/workspace/frappe-bench")
    return exc.value


def test_supervisor_install_failure_no_longer_asserts_only_no_network():
    err = _install_failure_error()
    assert err.code == "supervisor.install_failed"
    # It offers more than the single "network required" guess.
    assert "not writable" in err.message and "captured output" in err.message
    # The real installer output is captured for surfacing.
    assert "Permission denied" in (err.detail or {}).get("output", "")


def test_human_handler_prints_the_tail_to_stderr(capsys):
    err = _install_failure_error()
    render_captured_output_tail(err)
    out = capsys.readouterr()
    assert "Permission denied" in out.err
    assert "Captured output" in out.err


def test_axi_surface_emits_the_captured_output_as_a_block(capsys):
    err = _install_failure_error()
    axi_mod.emit_axi_error(err)
    out = capsys.readouterr().out
    assert "output[" in out  # the TOON block header
    assert "Permission denied" in out


# ------------------------------------------------------ BUG-8: git error tail shown
#
# A failed pull's default (non-verbose) summary must include a short tail of git's
# real error, not just "Git pull failed".


@pytest.mark.parametrize("verbose", [False, True])
def test_failed_pull_summary_includes_gits_real_error(monkeypatch, capsys, verbose):
    container = FakeFrappeContainer(available_apps=["frappe", "payments"])
    _wire(monkeypatch, container)
    container.fail_on = ["git pull"]

    with pytest.raises(typer.Exit) as exc:
        update_mod.run_app_update("proj", ["payments"], verbose=verbose)

    assert exc.value.exit_code == 1
    out = _out(capsys)
    assert "payments: Git pull failed" in out
    # The fake emits `error running: <cmd>` on failure; that tail is now folded in.
    assert "error running" in out and "git pull" in out


# ------------------------------------------------- 9c: rm-bench refusal real selector
#
# The running-bench refusal names the concrete `--bench <index>` even when --bench
# was omitted, instead of a literal `<index|label>` placeholder.


def test_rm_bench_selector_substitutes_the_real_cached_index(monkeypatch):
    monkeypatch.setattr(
        resolvers,
        "cached_benches",
        lambda p: [{"path": "/w/b0", "index": 0, "label": None},
                   {"path": "/w/b1", "index": 3, "label": "prod"}],
    )
    assert core_rm_bench._selector_for_path("proj", "/w/b1") == "3"


def test_rm_bench_selector_falls_back_to_placeholder_when_uncached(monkeypatch):
    monkeypatch.setattr(resolvers, "cached_benches", lambda p: [])
    assert core_rm_bench._selector_for_path("proj", "/w/unknown") == "<index|label>"


# ------------------------------------------- 9a: stopped instance titles its benches
#
# A stopped instance returns an empty bench list (nothing probed), but the title
# must report the CACHED benches as "not running" rather than "(0 benches)", which
# reads as if the bench vanished.


def test_stopped_instance_title_reports_cached_benches_as_not_running(monkeypatch):
    monkeypatch.setattr(
        status_mod.resolvers,
        "cached_benches",
        lambda p: [{"path": "/w/b0", "index": 0}, {"path": "/w/b1", "index": 1}],
    )
    title = status_mod._title(_report("offline"))
    assert "not running" in title
    assert "2 benches" in title
    assert "0 benches" not in title


def test_stopped_instance_with_no_cached_benches_has_no_count_suffix(monkeypatch):
    monkeypatch.setattr(status_mod.resolvers, "cached_benches", lambda p: [])
    title = status_mod._title(_report("offline"))
    assert "benches" not in title
    assert "0 benches" not in title
