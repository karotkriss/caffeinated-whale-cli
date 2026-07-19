"""``cwcli axi apps list`` - the read verb the agent surface was missing.

Every bench-scoped axi verb takes ``--bench``, and ``axi benches`` answers WHICH
bench - but nothing answered what is ON one. This verb does, and these pin the
three things it must get right: TOON on stdout (never JSON), the forks rendered as
usage errors rather than prompts or auto-starts, and an exit code that reads
``ok`` rather than the envelope status.

``axi apps install``/``axi apps uninstall`` are deliberately absent
(captain-locked, 2026-07-15); there is a test below that says so, so their absence
reads as a decision rather than an oversight.
"""

from __future__ import annotations

import pytest
import typer

from caffeinated_whale_cli.commands import axi as axi_mod
from caffeinated_whale_cli.core import apps as core_apps
from caffeinated_whale_cli.core import docker as core_docker
from caffeinated_whale_cli.core import resolvers

from .test_core_apps import FakeContainer

BENCH = "/workspace/frappe-bench"


@pytest.fixture()
def container(monkeypatch):
    c = FakeContainer(
        available=["frappe", "payments"],
        installed={"a.localhost": ["frappe 15.0.0 version-15", "payments 1.2.3 version-15"]},
    )
    monkeypatch.setattr(core_docker, "get_frappe_container", lambda _p: c)
    monkeypatch.setattr(core_apps.bench_sites, "list_sites", lambda *a, **k: ["a.localhost"])
    monkeypatch.setattr(resolvers, "cached_benches", lambda _p: [{"path": BENCH}])
    return c


def test_available_apps_are_one_toon_document_on_stdout(container, capsys):
    with pytest.raises(typer.Exit) as exc:
        axi_mod.axi_apps_list("proj", bench=None, sites=None, installed=False)

    assert exc.value.exit_code == 0
    out = capsys.readouterr().out
    assert "project: proj" in out
    assert "frappe" in out and "payments" in out
    # TOON, never JSON - the axi surface has no --json and must not grow one.
    assert not out.lstrip().startswith("{")


def test_installed_per_site_reports_only_app_names(container, capsys):
    """A real bench prints `<name> <version> <branch>`; the agent gets the name."""
    with pytest.raises(typer.Exit) as exc:
        axi_mod.axi_apps_list("proj", bench=None, sites=None, installed=True)

    assert exc.value.exit_code == 0
    out = capsys.readouterr().out
    assert "a.localhost" in out
    assert "15.0.0" not in out


def test_a_failed_site_read_exits_one_and_still_emits_the_document(container, capsys):
    """Reads `ok`, not the envelope status: a partial read is a WARNING envelope,
    and WARNING maps to exit 0 on every other verb."""
    container.fail_on = ["list-apps"]

    with pytest.raises(typer.Exit) as exc:
        axi_mod.axi_apps_list("proj", bench=None, sites=None, installed=True)

    assert exc.value.exit_code == 1
    assert "a.localhost" in capsys.readouterr().out


def test_a_stopped_project_is_a_usage_error_naming_start(monkeypatch, container, capsys):
    """Never an auto-start: there is no --yes on this verb, by design."""
    container.status = "exited"

    with pytest.raises(typer.Exit) as exc:
        axi_mod.axi_apps_list("proj", bench=None, sites=None, installed=False)

    assert exc.value.exit_code == 2
    out = capsys.readouterr().out
    assert "error:" in out
    assert "cwcli start" in out
    assert container.calls == []


def test_multibench_with_no_selector_is_a_usage_error_listing_the_benches(
    monkeypatch, container, capsys
):
    monkeypatch.setattr(
        resolvers, "cached_benches", lambda _p: [{"path": BENCH}, {"path": "/workspace/other"}]
    )

    with pytest.raises(typer.Exit) as exc:
        axi_mod.axi_apps_list("proj", bench=None, sites=None, installed=False)

    assert exc.value.exit_code == 2
    out = capsys.readouterr().out
    assert "error:" in out
    assert "options[2]:" in out


def test_a_missing_project_is_a_structured_error_exit_one(monkeypatch, capsys):
    from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind

    def _boom(_p):
        raise CwcliError(ErrorKind.NOT_FOUND, "project.not_found", "Project 'nope' not found.")

    monkeypatch.setattr(core_docker, "get_frappe_container", _boom)

    with pytest.raises(typer.Exit) as exc:
        axi_mod.axi_apps_list("nope", bench=None, sites=None, installed=False)

    assert exc.value.exit_code == 1
    assert "error:" in capsys.readouterr().out


def test_the_verbose_command_trace_never_reaches_the_document(container, capsys):
    """The trace rides the event channel, and axi passes no callback.

    It is a debug echo, not a note an agent acts on; routing it through `warnings`
    would put `$ ls -1 apps` in the structured document.
    """
    with pytest.raises(typer.Exit):
        axi_mod.axi_apps_list("proj", bench=None, sites=None, installed=True)

    out = capsys.readouterr().out
    assert "ls -1 apps" not in out
    assert "-> exit 0" not in out


def test_the_destructive_mutations_are_deliberately_not_verbs():
    """Captain-locked 2026-07-15: an agent destroying site data is its own decision.

    This asserts the DEFERRAL, so that adding either verb is a deliberate act that
    updates this test rather than something that quietly slips in.
    """
    registered = {c.name for c in axi_mod.apps_app.registered_commands}
    assert "list" in registered
    assert "update" in registered
    assert "install" not in registered
    assert "uninstall" not in registered
    # `apps checkout` mutates the in-instance checkout, so like install/uninstall it
    # is a HUMAN verb only - no axi surface. Keep its absence a decision.
    assert "checkout" not in registered
