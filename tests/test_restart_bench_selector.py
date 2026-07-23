"""``cwcli restart --bench N`` acts on the bench it was given.

The whole-stack restart takes every container down and brings ONE bench back up.
``--bench`` was threaded only into the ``--process`` branch, so on the whole-stack
path the selector was accepted without complaint and then ignored: the resolver
fell back to ``on_ambiguous="first"`` and relaunched bench 0 while the bench the
user NAMED stayed dead - at exit 0, with no error. ``start``, ``status`` and
``logs`` all honor the same flag, which is what made the silent divergence a trap
rather than a documented limitation.
"""

from __future__ import annotations

import pytest

from caffeinated_whale_cli.commands import restart as restart_mod


@pytest.fixture
def wired(monkeypatch):
    """Capture what the whole-stack path asks ``_start_project`` to start."""
    monkeypatch.setattr(restart_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        restart_mod, "get_project_containers", lambda name: [_RunningContainer()]
    )
    monkeypatch.setattr(
        restart_mod,
        "resolve_bench_path",
        lambda name, selector, *a, **k: "/w/b1" if selector == "1" else "/w/b0",
    )
    monkeypatch.setattr(restart_mod, "stop_project_best_effort", lambda name, verbose=False: 1)
    seen: list[dict] = []

    def fake_start_project(project_name, **kwargs):
        seen.append({"project": project_name, **kwargs})
        return "/w/b1/logs"

    monkeypatch.setattr(restart_mod, "_start_project", fake_start_project)
    return seen


class _RunningContainer:
    status = "running"
    labels = {"com.docker.compose.service": "frappe"}


def test_the_named_bench_is_the_one_relaunched(wired):
    restart_mod.restart(
        ctx=None, verbose=False, process=None, bench="1", project_name=["proj"]
    )

    assert len(wired) == 1
    assert wired[0]["bench_path_override"] == "/w/b1"


def test_the_selector_survives_being_written_after_the_project_name(wired):
    # A variadic Argument eats trailing options, so `cwcli restart proj --bench 1`
    # has to be recovered by hand - and it must reach the start the same way.
    restart_mod.restart(
        ctx=None, verbose=False, process=None, bench=None, project_name=["proj", "--bench", "1"]
    )

    assert wired[0]["bench_path_override"] == "/w/b1"


def test_no_selector_still_leaves_the_resolver_to_choose(wired):
    # Unchanged behaviour without --bench: the resolver picks (and says so).
    restart_mod.restart(
        ctx=None, verbose=False, process=None, bench=None, project_name=["proj"]
    )

    assert wired[0]["bench_path_override"] == "/w/b0"


def test_an_invalid_selector_is_rejected_before_the_project_is_stopped(monkeypatch):
    monkeypatch.setattr(
        restart_mod, "get_project_containers", lambda name: [_RunningContainer()]
    )
    monkeypatch.setattr(
        restart_mod,
        "resolve_bench_path",
        lambda *a, **k: (_ for _ in ()).throw(restart_mod.typer.Exit(code=1)),
    )
    monkeypatch.setattr(
        restart_mod,
        "stop_project_best_effort",
        lambda *a, **k: pytest.fail("selector validation must precede teardown"),
    )

    with pytest.raises(restart_mod.typer.Exit):
        restart_mod._restart_project("proj", bench="nope")


@pytest.mark.parametrize(
    "option", ["--bench", "--bench=", "--process", "--process=", "-p"]
)
def test_a_trailing_value_option_is_a_usage_error(option, monkeypatch):
    monkeypatch.setattr(restart_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        restart_mod,
        "_restart_project",
        lambda *a, **k: pytest.fail("malformed options must not restart anything"),
    )

    with pytest.raises(restart_mod.typer.Exit) as exc:
        restart_mod.restart(
            ctx=None,
            verbose=False,
            process=None,
            bench=None,
            project_name=["proj", option],
        )

    assert exc.value.exit_code == 2
