"""``cwcli axi apps list`` - the read verb the agent surface was missing.

Every bench-scoped axi verb takes ``--bench``, and ``axi benches`` answers WHICH
bench - but nothing answered what is ON one. This verb does, and these pin the
three things it must get right: TOON on stdout (never JSON), the forks rendered as
usage errors rather than prompts or auto-starts, and an exit code that reads
``ok`` rather than the envelope status.

``axi apps uninstall`` is deliberately absent (captain-locked, 2026-07-15); there
is a test below that says so, so its absence reads as a decision rather than an
oversight. ``axi apps install`` was held under that same rationale and now ships
scoped to the half the rationale never covered - the tests below pin both the
permitted and the refused path.
"""

from __future__ import annotations

import inspect

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
    monkeypatch.setattr(
        core_apps.supervision,
        "discover_stack",
        lambda *a, **k: core_apps.supervision.StackSnapshot(
            supervisor_up=False, supervisor_pid=None, processes=[]
        ),
    )
    monkeypatch.setattr(
        core_apps.supervision,
        "discover_unsupervised_stack",
        lambda *a, **k: core_apps.supervision.UnsupervisedStack(
            manager_up=False, processes=[]
        ),
    )
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


def test_uninstall_is_deliberately_not_a_verb():
    """Captain-locked 2026-07-15: an agent destroying site data is its own decision.

    This asserts the DEFERRAL, so that adding the verb is a deliberate act that
    updates this test rather than something that quietly slips in.

    The threat that rationale names is precise, and it is worth keeping precise:
    `bench uninstall-app` DROPS THE APP'S TABLES. It is not "agents may not
    mutate" - most live axi verbs mutate, `axi init` provisions a whole instance
    and `axi apps update` runs schema migrations across live sites. So a verb that
    deletes no site data is NOT covered by this deferral and must be judged on its
    own evidence, which is what happened to `checkout` and then to `install`.
    """
    registered = {c.name for c in axi_mod.apps_app.registered_commands}
    assert "list" in registered
    assert "update" in registered
    assert "uninstall" not in registered


def test_apps_install_is_a_verb_scoped_to_the_safe_half():
    """`axi apps install` SHIPPED 2026-07-20; this asserts its presence AND its scope.

    It was deferred alongside `uninstall` on one shared rationale - an agent
    destroying site data. That covers `uninstall` unconditionally and `install`
    only where the app is ALREADY on the site, because then its install hooks
    re-run over existing rows. Installing an app a site does not have creates that
    app's own tables and touches no other app's data, so the verb ships scoped to
    exactly that half.

    The two guards are asserted here as SIGNATURE facts, not just behaviour, so
    widening either one has to edit this test:

    - `--site` is required (no fan-out onto sites the agent never named).
    - There is no bypass flag for the already-installed refusal.
    """
    registered = {c.name for c in axi_mod.apps_app.registered_commands}
    assert "install" in registered

    params = inspect.signature(axi_mod.axi_apps_install).parameters
    # A typer.Option whose default is Ellipsis is a REQUIRED option.
    assert params["site"].default.default is ..., "--site must be required: no fan-out"
    assert not any(
        name in params for name in ("force", "yes", "allow_installed", "reinstall")
    ), "the already-installed refusal must have no bypass flag"


def _record_steps(monkeypatch) -> list[str]:
    """Capture every command `_run_step` would exec, and report each as a success."""
    ran: list[str] = []

    def fake_run_step(_c, cmd, _workdir, *, emit, phase, app=None, site=None):
        ran.append(cmd)
        emit(core_apps.AppsStepEnd(phase=phase, app=app, site=site, ok=True))
        return 0

    monkeypatch.setattr(core_apps, "_run_step", fake_run_step)
    return ran


def test_a_failed_install_step_exits_one(container, monkeypatch, capsys):
    """Reads `report.ok`, NOT the envelope status: a failed step is a WARNING
    envelope, and WARNING maps to exit 0 on every other verb."""

    def failing_step(_c, cmd, _workdir, *, emit, phase, app=None, site=None):
        emit(core_apps.AppsStepEnd(phase=phase, app=app, site=site, ok=False))
        return 1

    monkeypatch.setattr(core_apps, "_run_step", failing_step)

    with pytest.raises(typer.Exit) as exit_info:
        axi_mod.axi_apps_install("proj", "hrms", site="a.localhost", bench=None, branch=None)

    assert exit_info.value.exit_code == 1
    # The document is still emitted, so the agent can see WHICH step failed.
    assert "get-app" in capsys.readouterr().out


def test_install_of_an_absent_app_is_permitted(container, monkeypatch, capsys):
    """The safe half: the app is not on the site, so the install runs and reports."""
    ran = _record_steps(monkeypatch)

    with pytest.raises(typer.Exit) as exit_info:
        axi_mod.axi_apps_install(
            "proj", "hrms", site="a.localhost", bench=None, branch="version-15"
        )

    assert exit_info.value.exit_code == 0
    assert any("bench get-app" in c and "hrms" in c for c in ran)
    assert any("install-app" in c and "hrms" in c for c in ran)

    out = capsys.readouterr().out
    # Structured and actionable: a per-step row an agent reads, not prose.
    assert "results[" in out
    assert "get-app" in out
    assert "install-app" in out
    assert "ok: true" in out.lower()


def test_install_over_an_already_installed_app_is_refused(container, monkeypatch, capsys):
    """The dangerous half: refused, nothing fetched, and the refusal explains itself.

    `payments` is already installed on a.localhost in the fixture. Re-installing
    would re-run its install hooks against that site's existing rows, which is the
    exact case the original deferral was protecting.
    """
    ran = _record_steps(monkeypatch)

    with pytest.raises(typer.Exit) as exit_info:
        axi_mod.axi_apps_install(
            "proj", "payments", site="a.localhost", bench=None, branch=None
        )

    assert exit_info.value.exit_code == 1
    # Refused BEFORE any mutation: not even the fetch ran.
    assert not any("get-app" in c for c in ran)

    out = capsys.readouterr().out
    assert out.startswith("error:")
    assert "already installed" in out
    # Honest refusal: it names what to do instead, per axi spec section 6.
    assert "help:" in out
    assert "apps checkout" in out
    assert "apps update" in out


def test_install_refuses_when_the_sites_app_list_cannot_be_read(container, monkeypatch, capsys):
    """Fail closed: an unreadable site must never degrade to "nothing is installed"."""
    monkeypatch.setattr(
        core_apps, "_installed_apps", lambda *a, **k: ("bench list-apps -> exit 1", False, [])
    )
    ran = _record_steps(monkeypatch)

    with pytest.raises(typer.Exit) as exit_info:
        axi_mod.axi_apps_install("proj", "hrms", site="a.localhost", bench=None, branch=None)

    assert exit_info.value.exit_code == 1
    assert not any("get-app" in c for c in ran)
    assert "cannot be confirmed" in capsys.readouterr().out


def test_apps_checkout_is_a_verb_decided_on_its_own_evidence():
    """`axi apps checkout` SHIPPED 2026-07-20; this asserts its presence.

    It used to be asserted ABSENT here, on the reasoning that it "mutates the
    in-instance checkout, so like install/uninstall it is a HUMAN verb only".
    That inherited the wrong rationale: install/uninstall were held for DESTROYING
    SITE DATA (see the test above), and `core.checkout_app` runs `git fetch` then
    `git checkout -B` inside apps/<app> - no bench command, no site, no SQL, no
    table. Judged on its own evidence and approved.

    Kept as an assertion so the decision stays a decision in both directions:
    removing the verb should also be deliberate.
    See `openspec/changes/add-axi-apps-checkout-verb/`.
    """
    registered = {c.name for c in axi_mod.apps_app.registered_commands}
    assert "checkout" in registered
