"""``cwcli axi migrate`` / ``cwcli axi run-tests`` - the execution verbs.

The gap frappemate reported: `bench migrate` and `bench run-tests` are the two
commands every Frappe proof runs, and every execution step was dropping to raw
`cwcli run` because neither had an agent form. The hole also broke the workflow
`axi apps checkout` shipped for - the only agent route to a migrate was
`axi apps update`, which `git pull`s every named app FIRST and would move the ref
the checkout just pinned.

They ship as two TOP-LEVEL verbs (captain ruling, Option A: 16 -> 18), not as an
`axi bench` group, because `apps` groups by domain noun with a statable boundary
while a `bench` group would group by mechanism and complete at passthrough by
accretion. `tests/test_axi.py::TestNoAxiRunVerb` holds the passthrough absence.

What must be right here: TOON on stdout and nothing else (the command's own bytes
are load-bearing but belong on stderr), the forks rendered as usage errors rather
than prompts or auto-starts, an exit code that reads `report.ok` rather than the
envelope status, and `run-tests`'s required target that no default may erode.
"""

from __future__ import annotations

import pytest
import typer

from caffeinated_whale_cli.commands import axi as axi_mod
from caffeinated_whale_cli.core import bench_ops as core_bench_ops
from caffeinated_whale_cli.core import docker as core_docker
from caffeinated_whale_cli.core import resolvers

from .test_core_apps import FakeContainer

BENCH = "/workspace/frappe-bench"
SITE = "a.localhost"


@pytest.fixture()
def container(monkeypatch):
    c = FakeContainer()
    monkeypatch.setattr(core_docker, "get_frappe_container", lambda _p: c)
    monkeypatch.setattr(resolvers, "cached_benches", lambda _p: [{"path": BENCH}])
    monkeypatch.setattr(resolvers, "resolve_default_site", lambda *a, **k: SITE)
    return c


def _migrate(**kwargs):
    params = {"site": SITE, "bench": None}
    params.update(kwargs)
    return axi_mod.axi_migrate("proj", **params)


def _run_tests(**kwargs):
    params = {"site": SITE, "app_name": "payments", "bench": None}
    params.update(kwargs)
    return axi_mod.axi_run_tests("proj", **params)


# --------------------------------------------------------------------- axi migrate


def test_a_successful_migrate_is_one_toon_document_and_exits_zero(container, capsys):
    with pytest.raises(typer.Exit) as exc:
        _migrate()

    assert exc.value.exit_code == 0
    out = capsys.readouterr().out
    assert "project: proj" in out
    assert f"bench_path: {BENCH}" in out
    assert f"site: {SITE}" in out
    assert "ok: true" in out


def test_the_resolved_site_is_always_readable_back(container, capsys):
    """The gap `apps checkout` left open and this deliberately does not repeat: an
    agent that cannot read back what it acted on cannot verify its own work."""
    with pytest.raises(typer.Exit):
        _migrate(site=None)

    assert f"site: {SITE}" in capsys.readouterr().out


def test_a_failed_migrate_exits_one_on_a_warning_shaped_envelope(container, capsys):
    """The exit code reads `report.ok`, NEVER `result.status`: a failed step is a
    WARNING-shaped envelope, and WARNING maps to exit 0 everywhere else."""
    container.fail_on = [" migrate"]

    with pytest.raises(typer.Exit) as exc:
        _migrate()

    assert exc.value.exit_code == 1
    assert "ok: false" in capsys.readouterr().out


def test_a_refused_maintenance_enable_exits_one_and_migrates_nothing(container, capsys):
    container.fail_on = ["set-maintenance-mode on"]

    with pytest.raises(typer.Exit) as exc:
        _migrate()

    assert exc.value.exit_code == 1
    assert "ok: false" in capsys.readouterr().out
    assert not [c for c in container.calls if c.endswith("migrate")]


def test_a_site_left_in_maintenance_is_reported_in_the_document(container, capsys):
    container.fail_on = ["set-maintenance-mode off"]

    with pytest.raises(typer.Exit) as exc:
        _migrate()

    assert exc.value.exit_code == 1
    assert "maintenance_left_on: true" in capsys.readouterr().out


def test_a_failure_carries_a_help_line_and_a_success_does_not(container, capsys):
    """AXI section 9: a successful migrate fully answers the query, so a suggestion
    there is noise. A failure's next step is not obvious, so it is named."""
    with pytest.raises(typer.Exit):
        _migrate()
    assert "help:" not in capsys.readouterr().out

    container.fail_on = [" migrate"]
    with pytest.raises(typer.Exit):
        _migrate()
    assert "cwcli axi logs proj" in capsys.readouterr().out


def test_there_is_no_skip_maintenance_flag_on_the_verb(container):
    """Captain ruling M1, pinned at the frontend as well as the core."""
    import inspect

    params = set(inspect.signature(axi_mod.axi_migrate).parameters)
    assert "skip_maintenance" not in params
    assert params == {"project", "site", "bench"}


# ------------------------------------------------------------------- axi run-tests


def test_a_passing_suite_is_one_toon_document_and_exits_zero(container, capsys):
    with pytest.raises(typer.Exit) as exc:
        _run_tests()

    assert exc.value.exit_code == 0
    out = capsys.readouterr().out
    assert f"site: {SITE}" in out
    assert "app: payments" in out
    assert "ok: true" in out


def test_a_failing_suite_exits_one(container, capsys):
    container.fail_on = ["run-tests"]

    with pytest.raises(typer.Exit) as exc:
        _run_tests()

    assert exc.value.exit_code == 1
    assert "ok: false" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("missing", "kwargs"),
    [
        ("--site", {"site": None}),
        ("--app", {"app_name": None}),
        ("--site, --app", {"site": None, "app_name": None}),
    ],
)
def test_a_missing_target_flag_is_a_usage_error_that_runs_nothing(
    container, capsys, missing, kwargs
):
    """Captain ruling S1: --site and --app are REQUIRED with NO default-site
    fallback, deliberately diverging from `axi backup`/`unlock`/`migrate`. For
    those cwcli can state exactly what the operation does to the site; run-tests
    executes the repository's own code, so the target must be an explicit act."""
    with pytest.raises(typer.Exit) as exc:
        _run_tests(**kwargs)

    assert exc.value.exit_code == 2
    out = capsys.readouterr().out
    assert missing in out
    assert "help:" in out
    assert not container.calls


# ------------------------------------------------------- shared forks and stdout purity


@pytest.mark.parametrize("verb", ["migrate", "run-tests"])
def test_a_stopped_project_is_a_usage_error_naming_cwcli_start(container, capsys, verb):
    container.status = "exited"

    with pytest.raises(typer.Exit) as exc:
        _migrate() if verb == "migrate" else _run_tests()

    assert exc.value.exit_code == 2
    out = capsys.readouterr().out
    assert "not running" in out
    assert "cwcli start" in out
    assert not container.calls  # nothing was started


@pytest.mark.parametrize("verb", ["migrate", "run-tests"])
def test_a_multi_bench_project_with_no_selector_names_bench(monkeypatch, container, capsys, verb):
    monkeypatch.setattr(
        resolvers, "cached_benches", lambda _p: [{"path": BENCH}, {"path": "/workspace/other"}]
    )

    with pytest.raises(typer.Exit) as exc:
        _migrate() if verb == "migrate" else _run_tests()

    assert exc.value.exit_code == 2
    assert "--bench" in capsys.readouterr().out


@pytest.mark.parametrize("verb", ["migrate", "run-tests"])
def test_the_commands_own_bytes_go_to_stderr_and_stdout_stays_toon(container, capsys, verb):
    """The `_checkout_narrate` reasoning: neither op is a supervised process, so
    neither logs anywhere afterwards, and the failing patch (or assertion) names
    itself ONLY in those bytes. They are load-bearing - and they are not stdout."""
    with pytest.raises(typer.Exit):
        _migrate() if verb == "migrate" else _run_tests()

    captured = capsys.readouterr()
    assert "$ bench " in captured.err
    assert "$ bench " not in captured.out
    # stdout is exactly one TOON document: every line is a key:value or a block row.
    assert captured.out.strip()
    assert not any(line.startswith("$") for line in captured.out.splitlines())


def test_a_failing_run_names_where_the_reason_is(container, capsys):
    container.fail_on = ["run-tests"]

    with pytest.raises(typer.Exit):
        _run_tests()

    assert "stderr" in capsys.readouterr().out


# ------------------------------------------------------------------ registry shape


def test_both_verbs_are_registered_at_the_top_level(container):
    """Captain ruling: Option A, two TOP-LEVEL verbs, NOT an `axi bench` group.
    A `bench` group would group by mechanism, and every bench subcommand qualifies
    by construction, so its completion state is passthrough reached by accretion."""
    registered = {c.name for c in axi_mod.app.registered_commands}
    assert "migrate" in registered
    assert "run-tests" in registered
    assert "bench" not in {g.typer_instance.info.name for g in axi_mod.app.registered_groups}


def test_neither_verb_accepts_a_free_form_command_string(container):
    """These are narrow verbs, not the deferred passthrough. The distinction is who
    AUTHORS the command: every parameter here is a typed, individually-quoted value,
    and none is variadic or free-form, so no second command can be expressed."""
    import inspect

    for fn in (axi_mod.axi_migrate, axi_mod.axi_run_tests):
        for param in inspect.signature(fn).parameters.values():
            assert param.kind is not inspect.Parameter.VAR_POSITIONAL
            # `from __future__ import annotations` makes these strings, so compare
            # by name: every parameter is a plain str, never a list or a variadic.
            assert param.annotation == "str", f"{fn.__name__}:{param.name}"
            assert param.name not in {"cmd", "command", "args", "bench_args"}


def test_the_core_functions_are_called_with_auto_start_false(monkeypatch, container):
    """No agent-surface verb starts containers a user deliberately stopped."""
    seen: list[dict] = []

    def _spy(*_a, **kwargs):
        seen.append(kwargs)
        raise typer.Exit(0)

    monkeypatch.setattr(core_bench_ops, "migrate_site", _spy)
    monkeypatch.setattr(core_bench_ops, "run_tests", _spy)

    for call in (_migrate, _run_tests):
        with pytest.raises(typer.Exit):
            call()

    assert [k["auto_start"] for k in seen] == [False, False]
