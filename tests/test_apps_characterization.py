"""Characterization tests for ``apps list``/``install``/``uninstall``, written BEFORE
they move onto the logic core (openspec `migrate-apps-core`, tasks 1.2-1.8).

**These must pass unchanged before AND after the migration.** That is what makes
"refactor under green" true here rather than aspirational.

They pin the branches the existing suite does NOT reach. Measured first-hand at
`30cf57c` (task 1.1): `commands/apps.py` was 195 stmts / 91.28%, and the 17 missing
lines are exactly the interesting ones - the whole git-URL half of the app-name
derivation (`:176-177`, the batch's stated risk concentration), the get-app failure
branch (`:342-343`), both empty-site-set notes (`:353`, `:430-431`), the
"Completed with errors." aggregate (`:216`), the failed-recache warning (`:189`),
the unreadable-``apps/`` read (`:133`), and every ``--verbose`` echo.

They drive the three Typer commands with ALL params explicit, the way
`tests/test_apps.py` already does, and assert only on OBSERVABLE behaviour -
the commands the container was asked to run, what reached stdout/stderr, and the
exit code. Deliberately, nothing here names ``_derive_app_name``,
``_report_and_exit``, ``_run_bench``, ``_capture_bench``, ``_stream_bench``,
``_list_available_apps``, ``_list_installed_apps`` or ``_resolve_target_sites``:
every one of those MOVES to ``core/apps.py`` or dies in this batch, so a test bound
to one could not survive the migration unchanged. The four app-name shapes are
therefore pinned THROUGH ``install_apps`` (by the ``install-app <name>`` the
container actually receives), which is both migration-proof and a truer statement
of the behaviour than calling the helper directly.

Unlike batch 4's characterization file, this one may drive the Typer commands
directly: that batch was itself adding ``--json`` to ``update_apps``, which would
have forced every call to change, whereas this batch preserves the trio's
signatures byte-for-byte.
"""

import json

import pytest
import typer

from caffeinated_whale_cli.commands import apps as apps_mod
from caffeinated_whale_cli.core import docker as core_docker

from .test_apps import FakeFrappeContainer, _set_tty, wired  # noqa: F401

_BENCH = "/workspace/frappe-bench"


def _wire_container(monkeypatch, container):
    monkeypatch.setattr(core_docker, "get_frappe_container", lambda name: container)
    return container


# ------------------------------------------------- install: the app-name derivation


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        # A plain name is itself.
        ("custom_app", "custom_app"),
        # A git URL clones into apps/<repo-basename>, minus a trailing .git.
        ("https://github.com/example/custom_app.git", "custom_app"),
        # A URL with no .git suffix, and with a trailing slash to strip.
        ("https://github.com/example/custom_app/", "custom_app"),
        # An scp-style ssh remote: the "@" form, not "://".
        ("git@github.com:example/custom_app.git", "custom_app"),
    ],
)
def test_install_derives_the_app_name_from_every_target_shape(
    wired, monkeypatch, target, expected  # noqa: F811
):
    """The fallback name derivation, pinned through the install-app it produces.

    Reached whenever the apps/ before/after diff is not exactly one new dir - here
    because get-app adds nothing (the already-present-app case). This is the
    batch's risk concentration and the git-URL half of it had NO coverage.
    """
    container = _wire_container(monkeypatch, FakeFrappeContainer(available_apps=["frappe"]))

    apps_mod.install_apps(
        "proj",
        [target],
        bench=None,
        bench_path=None,
        sites=["a.localhost"],
        branch=None,
        fetch_only=False,
        json_output=True,
        yes=False,
        verbose=False,
    )

    assert any(f"install-app {expected}" in c for c in container.calls), container.calls


def test_install_get_app_failure_reports_it_and_skips_that_apps_install(
    wired, monkeypatch, capsys  # noqa: F811
):
    """A failed get-app is reported ok=False and NEVER installed on any site."""
    container = _wire_container(
        monkeypatch,
        FakeFrappeContainer(available_apps=["frappe"], fail_on=["get-app"]),
    )

    with pytest.raises(typer.Exit) as exc:
        apps_mod.install_apps(
            "proj",
            ["custom_app"],
            bench=None,
            bench_path=None,
            sites=["a.localhost"],
            branch=None,
            fetch_only=False,
            json_output=True,
            yes=False,
            verbose=False,
        )

    assert exc.value.exit_code == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["ok"] is False
    assert doc["results"] == [{"app": "custom_app", "site": None, "action": "get-app", "ok": False}]
    assert not any("install-app" in c for c in container.calls)


def test_install_streams_bench_output_to_stdout_in_human_mode(
    wired, monkeypatch, capsys  # noqa: F811
):
    """Human (non-JSON) mode streams bench's own output through to stdout."""
    _wire_container(
        monkeypatch,
        FakeFrappeContainer(available_apps=["frappe"], fail_on=["get-app"]),
    )

    with pytest.raises(typer.Exit):
        apps_mod.install_apps(
            "proj",
            ["custom_app"],
            bench=None,
            bench_path=None,
            sites=["a.localhost"],
            branch=None,
            fetch_only=False,
            json_output=False,
            yes=False,
            verbose=False,
        )

    assert "error running: bench get-app custom_app" in capsys.readouterr().out


def test_install_with_no_sites_on_the_bench_fetches_and_notes_it(
    wired, monkeypatch, capsys  # noqa: F811
):
    """No sites to install on: fetch, say so, and do NOT claim an install."""
    wired.sites = []
    container = _wire_container(monkeypatch, FakeFrappeContainer(available_apps=["frappe"]))

    apps_mod.install_apps(
        "proj",
        ["custom_app"],
        bench=None,
        bench_path=None,
        sites=[],
        branch=None,
        fetch_only=False,
        json_output=False,
        yes=False,
        verbose=False,
    )

    captured = capsys.readouterr()
    assert "no sites on the bench to install on" in captured.err
    assert not any("install-app" in c for c in container.calls)
    assert "fetched" in captured.out.lower()


def test_install_verbose_echoes_the_bench_command_to_stderr(
    wired, monkeypatch, capsys  # noqa: F811
):
    """--verbose echoes each bench command, and to STDERR (stdout stays the output)."""
    container = _wire_container(monkeypatch, FakeFrappeContainer(available_apps=["frappe"]))

    apps_mod.install_apps(
        "proj",
        ["custom_app"],
        bench=None,
        bench_path=None,
        sites=["a.localhost"],
        branch=None,
        fetch_only=False,
        json_output=True,
        yes=False,
        verbose=True,
    )

    assert "$ bench get-app custom_app" in capsys.readouterr().err
    assert container.calls


def test_install_partial_failure_prints_completed_with_errors_and_no_banner(
    wired, monkeypatch, capsys  # noqa: F811
):
    """One site failing: report all, say "Completed with errors.", exit 1, no banner."""
    _wire_container(
        monkeypatch,
        FakeFrappeContainer(
            available_apps=["frappe"],
            fail_on=["--site b.localhost install-app"],
        ),
    )

    with pytest.raises(typer.Exit) as exc:
        apps_mod.install_apps(
            "proj",
            ["custom_app"],
            bench=None,
            bench_path=None,
            sites=["a.localhost", "b.localhost"],
            branch=None,
            fetch_only=False,
            json_output=False,
            yes=False,
            verbose=False,
        )

    assert exc.value.exit_code == 1
    captured = capsys.readouterr()
    assert "Completed with errors." in captured.err
    assert "App(s) installed." not in captured.out


def test_install_warns_when_the_post_mutation_recache_fails(
    wired, monkeypatch, capsys  # noqa: F811
):
    """A failed recache degrades to a warning: the mutation itself already succeeded."""
    _wire_container(monkeypatch, FakeFrappeContainer(available_apps=["frappe"]))
    monkeypatch.setattr(apps_mod.cache, "recache_project", lambda *a, **k: False)

    apps_mod.install_apps(
        "proj",
        ["custom_app"],
        bench=None,
        bench_path=None,
        sites=["a.localhost"],
        branch=None,
        fetch_only=False,
        json_output=True,
        yes=False,
        verbose=False,
    )

    assert "refreshing the cache failed" in capsys.readouterr().err


# ------------------------------------------------------------------------- uninstall


def test_uninstall_proceeds_after_an_interactive_confirmation(
    wired, monkeypatch, capsys  # noqa: F811
):
    """A human answering "yes" at the TTY must actually perform the uninstall.

    The single riskiest path in the core migration: the pre-migration code
    confirmed and then fell straight through to the fan-out, whereas the destructive
    gate is now a returned NEEDS_CHOICE that the frontend resolves and RE-INVOKES on.
    Nothing else exercises that re-invoke - `--yes` short-circuits it by consenting
    on the first call, and the non-TTY tests refuse before reaching it - so without
    this, "user typed y" would be untested.
    """
    container = _wire_container(
        monkeypatch, FakeFrappeContainer(available_apps=["frappe", "payments"])
    )
    asked = []

    def _confirm(prompt, **kwargs):
        asked.append(prompt)  # returning normally == the user said yes

    monkeypatch.setattr(apps_mod, "confirm_or_exit", _confirm)

    apps_mod.uninstall_apps(
        "proj",
        ["payments"],
        bench=None,
        bench_path=None,
        sites=["a.localhost"],
        json_output=False,
        yes=False,
        verbose=False,
    )

    # The prompt names what is about to be destroyed, and the uninstall then ran.
    assert asked and "payments" in asked[0] and "a.localhost" in asked[0]
    assert any("uninstall-app payments --yes" in c for c in container.calls)
    assert "App(s) uninstalled." in capsys.readouterr().out


def test_uninstall_with_no_sites_notes_and_exits_zero(wired, monkeypatch, capsys):  # noqa: F811
    """Nothing to uninstall from is a clean success, not an error - and never prompts."""
    wired.sites = []
    container = _wire_container(monkeypatch, FakeFrappeContainer(available_apps=["frappe"]))
    _set_tty(monkeypatch, False)

    with pytest.raises(typer.Exit) as exc:
        apps_mod.uninstall_apps(
            "proj",
            ["payments"],
            bench=None,
            bench_path=None,
            sites=[],
            json_output=False,
            yes=False,
            verbose=False,
        )

    assert exc.value.exit_code == 0
    assert "no sites on the bench to uninstall from" in capsys.readouterr().err
    assert not any("uninstall-app" in c for c in container.calls)


# ------------------------------------------------------------------------------ list


def test_list_with_no_available_apps_says_none(wired, monkeypatch, capsys):  # noqa: F811
    """An empty apps/ renders an explicit "(none)", never a bare heading."""
    _wire_container(monkeypatch, FakeFrappeContainer(available_apps=[]))

    apps_mod.list_apps(
        "proj",
        bench=None,
        bench_path=None,
        sites=[],
        installed=False,
        json_output=False,
        yes=False,
        verbose=False,
    )

    assert "(none)" in capsys.readouterr().out


def test_list_unreadable_apps_dir_is_an_empty_listing_not_a_crash(
    wired, monkeypatch, capsys  # noqa: F811
):
    """A failed `ls -1 apps` reads as no available apps (exit 0 - list makes no claim)."""
    _wire_container(
        monkeypatch,
        FakeFrappeContainer(available_apps=["frappe"], fail_on=["ls -1 apps"]),
    )

    apps_mod.list_apps(
        "proj",
        bench=None,
        bench_path=None,
        sites=[],
        installed=False,
        json_output=True,
        yes=False,
        verbose=False,
    )

    assert json.loads(capsys.readouterr().out)["available_apps"] == []


def test_list_verbose_echoes_its_reads_to_stderr_leaving_json_pure(
    wired, monkeypatch, capsys  # noqa: F811
):
    """--verbose + --json: every echo goes to stderr, so stdout stays parseable."""
    _wire_container(
        monkeypatch,
        FakeFrappeContainer(available_apps=["frappe"], installed={"a.localhost": ["frappe"]}),
    )

    apps_mod.list_apps(
        "proj",
        bench=None,
        bench_path=None,
        sites=["a.localhost"],
        installed=True,
        json_output=True,
        yes=False,
        verbose=True,
    )

    captured = capsys.readouterr()
    assert "$ ls -1 apps" in captured.err
    assert "list-apps -> exit 0" in captured.err
    json.loads(captured.out)  # stdout is the document and nothing else
