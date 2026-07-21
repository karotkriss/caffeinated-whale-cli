"""The surface-wide pin: a reported failure must NEVER exit 0.

The exit code is the only failure signal automation has. Two field reports
(2026-07-19) described `cwcli apps install` and `cwcli run` printing a failure
mark, printing "Completed with errors.", and still exiting 0 - which makes every
wrapper, pipeline step and agent workflow read a failed operation as a completed
one. Neither reproduced at `cd0d298` (the reports were against an older build and
the plumbing was already sound), so this file does not accompany a behaviour fix.
It exists so the plumbing CANNOT silently regress to the reported shape.

What the existing suite already pins, and this file deliberately does not repeat:
`tests/test_apps_characterization.py` covers one apps fan-out partial failure end
to end, and `tests/test_core_run.py` covers `cwcli run` forwarding a bench exit
code verbatim (7 stays 7, 0 stays 0).

What is NEW here is the CROSS-SURFACE guarantee. Those are per-command tests: a
verb added tomorrow, or an exit read quietly reseated from `report.ok` onto
`result.status`, gets no pin from them. So this file drives every frontend that
AGGREGATES per-step results through one parametrized sweep, asserting the same
invariant for all of them at once, and pins the shared renderer they route
through directly.

The invariant has two halves, and both are load-bearing: a failed step must exit
non-zero, AND a clean run must still exit 0. A change that makes everything fail
is not a fix, so every failure case here has a success twin.

Why `report.ok` and not `result.status`: a partial fan-out failure is a
``WARNING``-shaped envelope, and every other verb maps ``WARNING`` to exit 0.
Reading the envelope's status here would report success for an install that half
failed - the exact defect the reports describe. See `commands/apps.py`'s
`_report_and_exit` docstring.
"""

import pytest
import typer

from caffeinated_whale_cli.commands import apps as apps_mod
from caffeinated_whale_cli.core.apps import AppResult, AppsReport
from caffeinated_whale_cli.core.envelope import Result, Status
from caffeinated_whale_cli.utils import docker_utils


def _report(*, ok: bool) -> AppsReport:
    """A fan-out report whose per-step results agree with its ``ok`` aggregate."""
    results = [
        AppResult(app="custom_app", site=None, action="get-app", ok=True),
        AppResult(app="custom_app", site="a.localhost", action="install-app", ok=ok),
    ]
    return AppsReport(project="proj", bench_path="/workspace/frappe-bench", results=results, ok=ok)


def _envelope(report: AppsReport) -> Result:
    """The envelope shape a partial failure really arrives in.

    A failed step yields ``WARNING``, NOT ``ERROR``. That is precisely why the
    exit code must read ``report.ok``: a frontend reading ``result.status`` and
    mapping ``WARNING`` to 0 would swallow the failure.
    """
    return Result(
        status=Status.WARNING if not report.ok else Status.OK,
        data=report,
        warnings=[],
    )


# ------------------------------------------------- the shared renderer, pinned directly


@pytest.mark.parametrize("json_output", [False, True], ids=["human", "json"])
def test_report_and_exit_exits_non_zero_when_a_step_failed(json_output, capsys):
    """`_report_and_exit` is the single exit path for install/uninstall/checkout.

    Pinned in BOTH render modes: `--json` is the agent-facing shape, so a failure
    that exits 0 there is the one automation is likeliest to act on.
    """
    with pytest.raises(typer.Exit) as exc:
        apps_mod._report_and_exit(_report(ok=False), json_output, success_msg="App(s) installed.")

    assert exc.value.exit_code != 0, "a reported failure must never exit 0"
    assert exc.value.exit_code == 1
    capsys.readouterr()


@pytest.mark.parametrize("json_output", [False, True], ids=["human", "json"])
def test_report_and_exit_stays_zero_when_every_step_succeeded(json_output, capsys):
    """The success twin: an all-green report must not be dragged to non-zero."""
    # A clean run returns normally; Typer then exits 0. Raising here would mean
    # the failure path had been widened to swallow successes too.
    apps_mod._report_and_exit(_report(ok=True), json_output, success_msg="App(s) installed.")
    capsys.readouterr()


# ------------------------------------------------- every aggregating verb, swept at once


@pytest.fixture
def no_docker(monkeypatch):
    """Defuse the preflight, the interactive prologue and the recache epilogue.

    Only the exit plumbing is under test here, so the container work is stubbed
    out entirely - this suite must stay in the fast, Docker-free unit tier.
    """
    # Bypass the @handle_docker_errors preflight on every apps command (the
    # tests/test_apps.py pattern): it checks for a real `docker` binary and a
    # live daemon before the function body runs, so without this a runner with
    # no Docker installed raises typer.Exit(1) here regardless of what the
    # verb's own logic would have done.
    monkeypatch.setattr(docker_utils.shutil, "which", lambda name: "/usr/bin/docker")

    class _Client:
        def ping(self):
            return True

    monkeypatch.setattr(docker_utils.docker, "from_env", lambda: _Client())

    monkeypatch.setattr(apps_mod, "ensure_containers_running", lambda *a, **k: None)
    monkeypatch.setattr(apps_mod, "_resolve_bench", lambda *a, **k: "/workspace/frappe-bench")
    # The post-mutation recache is a frontend epilogue that runs BEFORE the exit
    # read whenever any step succeeded. It degrades to a warning by design, so it
    # must not be able to influence the exit code either way.
    monkeypatch.setattr(apps_mod, "_refresh_cache", lambda *a, **k: None)


# Every apps verb that aggregates per-step results, with the core call it routes
# through and the kwargs that reach it. Adding a verb here is cheaper than
# discovering in production that its exit code was wired to the wrong field.
AGGREGATING_VERBS = [
    pytest.param(
        "install_apps",
        "install_apps",
        dict(
            project_name="proj",
            apps=["custom_app"],
            bench=None,
            bench_path=None,
            sites=["a.localhost"],
            branch=None,
            fetch_only=False,
            yes=True,
            verbose=False,
        ),
        id="apps-install",
    ),
    pytest.param(
        "uninstall_apps",
        "uninstall_apps",
        dict(
            project_name="proj",
            apps=["custom_app"],
            bench=None,
            bench_path=None,
            sites=["a.localhost"],
            yes=True,
            verbose=False,
        ),
        id="apps-uninstall",
    ),
    pytest.param(
        "checkout_app",
        "checkout_app",
        dict(
            project_name="proj",
            app="custom_app",
            ref="feature-branch",
            bench=None,
            bench_path=None,
            reset=False,
            yes=True,
            verbose=False,
        ),
        id="apps-checkout",
    ),
]


@pytest.mark.parametrize(("verb", "core_name", "kwargs"), AGGREGATING_VERBS)
@pytest.mark.parametrize("json_output", [False, True], ids=["human", "json"])
def test_aggregating_verb_exits_non_zero_when_a_step_failed(
    verb, core_name, kwargs, json_output, monkeypatch, no_docker, capsys
):
    """The regression this file exists for: a failed step, a non-zero exit.

    Driven through the real Typer command function so the assertion covers the
    whole frontend - the core call, the recache epilogue, and the exit read - not
    just the renderer in isolation.
    """
    monkeypatch.setattr(apps_mod.core_apps, core_name, lambda *a, **k: _envelope(_report(ok=False)))

    with pytest.raises(typer.Exit) as exc:
        getattr(apps_mod, verb)(json_output=json_output, **kwargs)

    assert exc.value.exit_code != 0, f"{verb} reported a failed step but exited 0"
    capsys.readouterr()


@pytest.mark.parametrize(("verb", "core_name", "kwargs"), AGGREGATING_VERBS)
def test_aggregating_verb_exits_zero_when_every_step_succeeded(
    verb, core_name, kwargs, monkeypatch, no_docker, capsys
):
    """The success twin, per verb: a green run must still be exit 0."""
    monkeypatch.setattr(apps_mod.core_apps, core_name, lambda *a, **k: _envelope(_report(ok=True)))

    getattr(apps_mod, verb)(json_output=False, **kwargs)
    capsys.readouterr()


def test_warning_status_alone_never_decides_the_exit_code(monkeypatch, no_docker, capsys):
    """The specific trap: `WARNING` + `ok=False` must still exit non-zero.

    A frontend reseated onto `result.status` would map this envelope to 0, which
    is exactly how a half-finished install reads as a clean success. Pinned as its
    own case so the reason survives even if the sweep above is ever rewritten.
    """
    envelope = _envelope(_report(ok=False))
    assert envelope.status is Status.WARNING  # the shape the trap depends on

    monkeypatch.setattr(apps_mod.core_apps, "install_apps", lambda *a, **k: envelope)

    with pytest.raises(typer.Exit) as exc:
        apps_mod.install_apps(
            project_name="proj",
            apps=["custom_app"],
            bench=None,
            bench_path=None,
            sites=["a.localhost"],
            branch=None,
            fetch_only=False,
            json_output=False,
            yes=True,
            verbose=False,
        )

    assert exc.value.exit_code != 0
    capsys.readouterr()
