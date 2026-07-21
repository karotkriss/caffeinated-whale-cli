"""``cwcli axi apps checkout`` - putting a feature branch under test, from an agent.

The verb the agent surface was missing: `apps install` is a fresh clone and
`apps update` is the tracked upstream, so neither fetches ONE named ref into an
existing apps/<app>. With no `axi run` passthrough, this is the only agent-surface
route to that step.

It ships while `axi apps uninstall` stays deferred, and these pin why that is
coherent rather than inconsistent: the deferral names ONE threat, an agent
DESTROYING SITE DATA (`bench uninstall-app` drops the app's tables), and a checkout
runs git inside a source directory. `axi apps install` was held under that same
deferral and has since shipped, scoped to the half it never covered;
`tests/test_axi_apps_list.py` holds both the `uninstall` absence assertion and the
`install` scoped-verb assertion.

What must be right here: TOON on stdout and nothing else (git's own bytes are
load-bearing but belong on stderr), the forks rendered as usage errors rather than
prompts or auto-starts, an exit code that reads `ok` rather than the envelope
status, and the destructive `--reset` staying an explicit, reported opt-in.
"""

from __future__ import annotations

import contextlib

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
    c = FakeContainer(available=["frappe", "payments"])
    monkeypatch.setattr(core_docker, "get_frappe_container", lambda _p: c)
    monkeypatch.setattr(resolvers, "cached_benches", lambda _p: [{"path": BENCH}])

    @contextlib.contextmanager
    def _fake_bridge(_container, _path):
        yield

    monkeypatch.setattr(core_apps.credbridge, "credential_bridge", _fake_bridge)
    # The recache epilogue is a real Docker round trip; default it to a success so
    # each test opts INTO exercising it rather than tripping over it.
    monkeypatch.setattr(axi_mod.cache, "recache_project", lambda *a, **k: True)
    return c


def _checkout(**kwargs):
    params = {"bench": None, "reset": False}
    params.update(kwargs)
    return axi_mod.axi_apps_checkout("proj", "payments", "feature/x", **params)


# ------------------------------------------------------------------- the happy path


def test_a_successful_checkout_is_one_toon_document_and_exits_zero(container, capsys):
    with pytest.raises(typer.Exit) as exc:
        _checkout()

    assert exc.value.exit_code == 0
    out = capsys.readouterr().out
    assert "project: proj" in out
    assert f"bench_path: {BENCH}" in out
    assert "ok: true" in out
    # One row per git step, so an agent can see exactly how far it got.
    assert "fetch" in out and "checkout" in out
    # TOON, never JSON - the axi surface has no --json and must not grow one.
    assert not out.lstrip().startswith("{")


def test_reset_adds_its_own_reported_row(container, capsys):
    """The destructive step is recorded, not silent: --reset is opt-in AND audited."""
    with pytest.raises(typer.Exit) as exc:
        _checkout(reset=True)

    assert exc.value.exit_code == 0
    assert "reset" in capsys.readouterr().out
    assert any("reset --hard FETCH_HEAD" in c for c in container.calls)


def test_without_reset_nothing_hard_resets_the_working_tree(container):
    """The guard is that cwcli never discards local work unless asked to.

    git's own refusal on a dirty tree is the backstop (covered below); this pins
    the half cwcli owns - the reset command is simply never issued.
    """
    with pytest.raises(typer.Exit):
        _checkout()

    assert not any("reset --hard" in c for c in container.calls)


# ---------------------------------------------------------------------- stdout purity


def test_git_bytes_and_the_command_echo_stay_off_stdout(container, capsys):
    """Git's output is load-bearing for diagnosis, so it is narrated - to STDERR.

    On stdout it would corrupt the one-TOON-document contract, since a git message
    carrying a colon would be re-read as a key:value line.
    """
    with pytest.raises(typer.Exit):
        _checkout()

    captured = capsys.readouterr()
    assert "$ git fetch" not in captured.out
    assert "$ git fetch" in captured.err
    assert "$ git checkout -B" in captured.err
    # Every stdout line is TOON key:value or an indented block row, never a `$` echo.
    assert not any(line.startswith("$") for line in captured.out.splitlines())


# ------------------------------------------------------------------- failure and exits


def test_a_failed_git_step_exits_one_with_ok_false(monkeypatch, container, capsys):
    """The dirty-tree refusal's shape: git says no, and the verb reports it honestly."""
    container.fail_on = ["git checkout -B"]

    with pytest.raises(typer.Exit) as exc:
        _checkout()

    # Exit 1, NOT 0: the envelope is WARNING-shaped here, and WARNING maps to 0
    # everywhere else - reading report.ok is what keeps this honest.
    assert exc.value.exit_code == 1
    out = capsys.readouterr().out
    assert "ok: false" in out
    # The agent can still see the fetch landed and the checkout is what failed.
    assert "fetch" in out and "checkout" in out


def test_the_reason_a_step_failed_reaches_stderr(monkeypatch, container, capsys):
    """Without git's own bytes a failure is a bare `ok: false` an agent cannot act on.

    This is why the narrator forwards AppsOutput where `axi init`'s deliberately
    does not: there is no `cwcli logs` for a git step, so these bytes are the only
    place the reason exists.
    """
    container.fail_on = ["git checkout -B"]

    with pytest.raises(typer.Exit):
        _checkout()

    assert "boom" in capsys.readouterr().err


def test_a_failed_fetch_never_runs_the_checkout(container):
    """A failed fetch makes the checkout meaningless, so it must not move the tree."""
    container.fail_on = ["git fetch"]

    with pytest.raises(typer.Exit) as exc:
        _checkout()

    assert exc.value.exit_code == 1
    assert not any("git checkout" in c for c in container.calls)


# --------------------------------------------------------------------- the forks


def test_a_stopped_project_is_a_usage_error_that_starts_nothing(container, capsys):
    """No --yes on the agent surface: an agent composes `cwcli axi start` first."""
    container.status = "exited"

    with pytest.raises(typer.Exit) as exc:
        _checkout()

    assert exc.value.exit_code == 2
    out = capsys.readouterr().out
    assert out.startswith("error:")
    assert "cwcli start" in out
    # Nothing was started, and no git ran.
    assert not any("git" in c for c in container.calls)


def test_a_multi_bench_project_with_no_selector_names_the_flag(monkeypatch, container, capsys):
    monkeypatch.setattr(
        resolvers,
        "cached_benches",
        lambda _p: [{"path": BENCH}, {"path": "/workspace/other-bench"}],
    )

    with pytest.raises(typer.Exit) as exc:
        _checkout()

    assert exc.value.exit_code == 2
    out = capsys.readouterr().out
    assert "--bench" in out


def test_an_app_that_is_not_a_git_checkout_errors_and_installs_nothing(container, capsys):
    """A typo'd app name must not read as a no-op, and must never imply an install."""
    container.fail_on = ["git remote"]  # apps/ghost is not a git checkout

    with pytest.raises(typer.Exit) as exc:
        axi_mod.axi_apps_checkout("proj", "ghost", "feature/x", bench=None, reset=False)

    assert exc.value.exit_code == 1
    out = capsys.readouterr().out
    assert out.startswith("error:")
    assert "cwcli apps list" in out
    # No clone, no get-app: this verb never creates an app.
    assert not any("get-app" in c for c in container.calls)


# ------------------------------------------------------------------ recache epilogue


def test_a_successful_checkout_refreshes_the_cache(monkeypatch, container):
    """A checkout changes the app's git state, so a stale cache would make the
    agent's own confirming read (`axi apps list`) lie."""
    calls: list[str] = []
    monkeypatch.setattr(
        axi_mod.cache, "recache_project", lambda p, *a, **k: calls.append(p) or True
    )

    with pytest.raises(typer.Exit):
        _checkout()

    assert calls == ["proj"]


def test_a_failed_recache_warns_but_does_not_fail_the_checkout(monkeypatch, container, capsys):
    """The mutation already landed; a non-zero exit would make an agent retry it."""
    monkeypatch.setattr(axi_mod.cache, "recache_project", lambda *a, **k: False)

    with pytest.raises(typer.Exit) as exc:
        _checkout()

    assert exc.value.exit_code == 0
    captured = capsys.readouterr()
    assert "re-caching" in captured.err
    assert "ok: true" in captured.out


def test_no_recache_when_every_step_failed(monkeypatch, container):
    """Nothing moved, so there is nothing to re-read."""
    container.fail_on = ["git fetch"]
    calls: list[str] = []
    monkeypatch.setattr(
        axi_mod.cache, "recache_project", lambda p, *a, **k: calls.append(p) or True
    )

    with pytest.raises(typer.Exit):
        _checkout()

    assert calls == []


# ------------------------------------------------------------------------ the flag set


def test_the_verb_carries_no_yes_json_path_or_verbose_flag():
    """Each absence is a decision, not an omission.

    --yes would open a start-from-axi path no bench-scoped axi verb has; --json
    would break the TOON-only contract; --path is the human verb's lower-level
    alternative to --bench with no axi precedent; --verbose is meaningless when
    stdout is always TOON and narration always goes to stderr.
    """
    command = next(c for c in axi_mod.apps_app.registered_commands if c.name == "checkout")
    names = {p for p in command.callback.__annotations__}

    assert "bench" in names
    assert "reset" in names
    for absent in ("yes", "json_output", "bench_path", "verbose"):
        assert absent not in names
