"""Characterization tests for the ``update`` state machine, written BEFORE it moves
onto the logic core (openspec `migrate-update-core`, tasks 1.1-1.5).

**These must pass unchanged before AND after the migration.** That is what makes
"refactor under green" true here rather than aspirational: when they were written,
`tests/test_apps.py` covered 71.06% of `commands/update.py` and only 2 of the 7
aggregation-reporting branches (`failed_maintenance_enable` and
`failed_maintenance_disable`), while `--build`, `--skip-maintenance`,
`--clear-website-cache`, `--no-recache` and multi-app fan-out had none at all. The
untested branches are exactly where PR #81's reporting bug shipped.

They drive ``run_app_update`` - the documented shared entry point behind BOTH
``cwcli apps update`` and the deprecated ``cwcli update`` - deliberately:

- ``_update_project`` is an internal that MOVES to ``core/update.py``, so tests
  bound to it could not survive the migration unchanged;
- ``apps_mod.update_apps`` is a Typer command whose params must all be passed
  explicitly, so adding ``--json`` to it (this same batch) would force every call
  here to change;
- ``run_app_update`` is a plain function with real Python defaults that the
  migration preserves as the shared frontend, so a new keyword with a default
  cannot break these calls.

The behaviour pinned here is what this batch PRESERVES. The stream-loss behaviour
is deliberately NOT pinned here: `migrate-update-core` design Decision 2 changes it
(a lost stream stops aborting the fan-out and becomes a reported UNKNOWN), and its
tests live in `tests/test_apps.py`, where they change WITH it by design.
"""

import pytest
import typer

from caffeinated_whale_cli.commands import update as update_mod

from .test_apps import FakeFrappeContainer, _count_discovery, _wire_update


def _wire(monkeypatch, container, *, sites=("a.localhost",)):
    """Wire the update collaborators and return a recache-call recorder.

    ``time.sleep`` is stubbed out: the state machine sleeps 0.5s after every
    migration to let bench release its locks, which is real behaviour worth keeping
    in production and pure latency in a unit test.
    """
    _wire_update(monkeypatch, container)
    monkeypatch.setattr(update_mod.time, "sleep", lambda *_a, **_k: None)

    recache_calls = []
    monkeypatch.setattr(
        update_mod.cache,
        "recache_project",
        lambda project_name, verbose=False: (recache_calls.append(project_name), True)[1],
    )
    _count_discovery(monkeypatch, list(sites))
    return recache_calls


def _out(capsys):
    """Captured stdout with whitespace collapsed - rich hard-wraps to console width."""
    return " ".join(capsys.readouterr().out.split())


# --------------------------------------------------------- 1.1 the five untested
# ------------------------------------------------- aggregation-reporting branches


@pytest.mark.parametrize("verbose", [True, False])
def test_failed_pull_is_reported_and_exits_nonzero(monkeypatch, capsys, verbose):
    # BRANCH: failed_apps. A failed `git pull` is recorded, reported by name, and
    # forces a non-zero exit; the app is never discovered or migrated afterwards.
    container = FakeFrappeContainer(available_apps=["frappe", "payments"])
    _wire(monkeypatch, container)
    container.fail_on = ["git pull"]

    with pytest.raises(typer.Exit) as exc:
        update_mod.run_app_update("proj", ["payments"], verbose=verbose)

    assert exc.value.exit_code == 1
    out = _out(capsys)
    assert "Failed to update 1 app(s)" in out
    assert "payments: Git pull failed" in out
    # A failed pull means the app is skipped for discovery, so nothing is migrated.
    assert not any("migrate" in c for c in container.calls)


@pytest.mark.parametrize("verbose", [True, False])
def test_failed_migration_is_reported_and_exits_nonzero(monkeypatch, capsys, verbose):
    # BRANCH: failed_migrations. A migration that returns non-zero is recorded and
    # the fan-out CONTINUES to the next site (continue-and-report-all).
    container = FakeFrappeContainer(available_apps=["frappe", "payments"])
    _wire(monkeypatch, container, sites=["a.localhost", "b.localhost"])
    container.fail_on = ["bench --site a.localhost migrate"]

    with pytest.raises(typer.Exit) as exc:
        update_mod.run_app_update("proj", ["payments"], verbose=verbose)

    assert exc.value.exit_code == 1
    out = _out(capsys)
    assert "Failed to migrate 1 site(s)" in out
    assert "a.localhost: Migration failed" in out
    # The sibling still migrated: a returned non-zero never aborts the fan-out.
    assert any("bench --site b.localhost migrate" in c for c in container.calls)
    # Both sites are taken back out of maintenance regardless.
    assert any("bench --site a.localhost set-maintenance-mode off" in c for c in container.calls)
    assert any("bench --site b.localhost set-maintenance-mode off" in c for c in container.calls)


@pytest.mark.parametrize("verbose", [True, False])
def test_failed_build_is_reported_and_exits_nonzero(monkeypatch, capsys, verbose):
    # BRANCH: failed_builds. --build's failure path had zero coverage.
    container = FakeFrappeContainer(available_apps=["frappe", "payments"])
    _wire(monkeypatch, container)
    container.fail_on = ["bench build"]

    with pytest.raises(typer.Exit) as exc:
        update_mod.run_app_update("proj", ["payments"], verbose=verbose, build=True)

    assert exc.value.exit_code == 1
    out = _out(capsys)
    assert "Failed to build assets for 1 app(s)" in out
    assert "payments: Build failed" in out


@pytest.mark.parametrize("verbose", [True, False])
def test_failed_cache_clear_is_reported_and_exits_nonzero(monkeypatch, capsys, verbose):
    # BRANCH: failed_cache_clears.
    container = FakeFrappeContainer(available_apps=["frappe", "payments"])
    _wire(monkeypatch, container)
    # "clear-cache" is not a substring of "clear-website-cache", so this fails only
    # the plain cache clear.
    container.fail_on = ["clear-cache"]

    with pytest.raises(typer.Exit) as exc:
        update_mod.run_app_update("proj", ["payments"], verbose=verbose, clear_cache=True)

    assert exc.value.exit_code == 1
    out = _out(capsys)
    assert "Failed to clear cache for 1 site(s)" in out
    assert "a.localhost: Cache clearing failed" in out


@pytest.mark.parametrize("verbose", [True, False])
def test_failed_website_cache_clear_is_reported_and_exits_nonzero(monkeypatch, capsys, verbose):
    # BRANCH: failed_website_cache_clears.
    container = FakeFrappeContainer(available_apps=["frappe", "payments"])
    _wire(monkeypatch, container)
    container.fail_on = ["clear-website-cache"]

    with pytest.raises(typer.Exit) as exc:
        update_mod.run_app_update("proj", ["payments"], verbose=verbose, clear_website_cache=True)

    assert exc.value.exit_code == 1
    out = _out(capsys)
    assert "Failed to clear website cache for 1 site(s)" in out
    assert "a.localhost: Website cache clearing failed" in out


def test_every_phase_failing_reports_all_of_them_not_just_the_first(monkeypatch, capsys):
    # The aggregation is continue-and-report-ALL: several phases failing in one run
    # must each appear. This is the property the seven-way summary exists for, and
    # the one a migration is most likely to quietly drop.
    container = FakeFrappeContainer(available_apps=["frappe", "payments", "hrms"])
    _wire(monkeypatch, container)
    container.fail_on = [
        "apps/hrms",  # its git pull fails (the app path is the workdir)
        "bench --site a.localhost migrate",
        "bench build",
        "clear-cache",
        "clear-website-cache",
    ]

    with pytest.raises(typer.Exit) as exc:
        update_mod.run_app_update(
            "proj",
            ["payments", "hrms"],
            verbose=True,
            build=True,
            clear_cache=True,
            clear_website_cache=True,
        )

    assert exc.value.exit_code == 1
    out = _out(capsys)
    assert "Update completed with errors" in out
    assert "hrms: Git pull failed" in out
    assert "a.localhost: Migration failed" in out
    assert "Failed to build assets" in out
    assert "a.localhost: Cache clearing failed" in out
    assert "a.localhost: Website cache clearing failed" in out


# --------------------------------------------------------------- 1.2 the flags
# ------------------------------------------------------- with zero coverage today


@pytest.mark.parametrize("verbose", [True, False])
def test_build_flag_builds_each_successfully_pulled_app(monkeypatch, verbose):
    # --build's happy path: `bench build --app <app>` per successfully-pulled app.
    container = FakeFrappeContainer(available_apps=["frappe", "payments"])
    _wire(monkeypatch, container)

    update_mod.run_app_update("proj", ["payments"], verbose=verbose, build=True)

    assert any("bench build --app payments" in c for c in container.calls)


def test_build_skips_an_app_whose_pull_failed(monkeypatch):
    # _build_apps builds only the apps that pulled cleanly: building an app whose
    # source never updated would be work over a lie.
    container = FakeFrappeContainer(available_apps=["frappe", "payments", "hrms"])
    _wire(monkeypatch, container)
    container.fail_on = ["apps/hrms"]  # hrms' git pull fails (its path is the workdir)

    with pytest.raises(typer.Exit):
        update_mod.run_app_update("proj", ["payments", "hrms"], verbose=True, build=True)

    assert any("bench build --app payments" in c for c in container.calls)
    assert not any("bench build --app hrms" in c for c in container.calls)


def test_no_build_flag_builds_nothing(monkeypatch):
    container = FakeFrappeContainer(available_apps=["frappe", "payments"])
    _wire(monkeypatch, container)

    update_mod.run_app_update("proj", ["payments"], verbose=True, build=False)

    assert not any("bench build" in c for c in container.calls)


@pytest.mark.parametrize("verbose", [True, False])
def test_skip_maintenance_migrates_every_affected_site_without_maintenance_mode(
    monkeypatch, verbose
):
    # --skip-maintenance: no site is put into maintenance mode at all, and the
    # migration set falls back to every affected site rather than the (empty) set
    # of sites we put into maintenance. Getting that fallback wrong would silently
    # migrate NOTHING.
    container = FakeFrappeContainer(available_apps=["frappe", "payments"])
    _wire(monkeypatch, container, sites=["a.localhost", "b.localhost"])

    update_mod.run_app_update("proj", ["payments"], verbose=verbose, skip_maintenance=True)

    assert not any("set-maintenance-mode" in c for c in container.calls)
    assert any("bench --site a.localhost migrate" in c for c in container.calls)
    assert any("bench --site b.localhost migrate" in c for c in container.calls)


def test_skip_maintenance_still_clears_cache_for_every_affected_site(monkeypatch):
    # The clears key off the same migrate set, so --skip-maintenance must not lose
    # them (they are gated on sites_to_migrate, not on maintenance_sites).
    container = FakeFrappeContainer(available_apps=["frappe", "payments"])
    _wire(monkeypatch, container)

    update_mod.run_app_update(
        "proj", ["payments"], verbose=True, skip_maintenance=True, clear_cache=True
    )

    assert any("bench --site a.localhost clear-cache" in c for c in container.calls)
    assert any("a.localhost/locks" in c for c in container.calls if c.startswith("rm -rf"))


@pytest.mark.parametrize("verbose", [True, False])
def test_clear_website_cache_flag_clears_it_for_each_migrated_site(monkeypatch, verbose):
    container = FakeFrappeContainer(available_apps=["frappe", "payments"])
    _wire(monkeypatch, container, sites=["a.localhost", "b.localhost"])

    update_mod.run_app_update("proj", ["payments"], verbose=verbose, clear_website_cache=True)

    assert any("bench --site a.localhost clear-website-cache" in c for c in container.calls)
    assert any("bench --site b.localhost clear-website-cache" in c for c in container.calls)
    # --clear-website-cache alone must not also clear the plain cache.
    assert not any(c.endswith("clear-cache") for c in container.calls)


def test_both_cache_flags_clear_both(monkeypatch):
    container = FakeFrappeContainer(available_apps=["frappe", "payments"])
    _wire(monkeypatch, container)

    update_mod.run_app_update(
        "proj", ["payments"], verbose=True, clear_cache=True, clear_website_cache=True
    )

    assert any("bench --site a.localhost clear-cache" in c for c in container.calls)
    assert any("bench --site a.localhost clear-website-cache" in c for c in container.calls)


def test_no_flags_clears_neither_cache_but_still_clears_locks(monkeypatch):
    # Locks are cleared unconditionally for the migrated set; the caches are opt-in.
    container = FakeFrappeContainer(available_apps=["frappe", "payments"])
    _wire(monkeypatch, container)

    update_mod.run_app_update("proj", ["payments"], verbose=True)

    assert not any("clear-cache" in c for c in container.calls)
    assert not any("clear-website-cache" in c for c in container.calls)
    assert any("a.localhost/locks" in c for c in container.calls if c.startswith("rm -rf"))


@pytest.mark.parametrize("verbose", [True, False])
def test_no_recache_skips_the_post_pull_recache(monkeypatch, verbose):
    container = FakeFrappeContainer(available_apps=["frappe", "payments"])
    recache_calls = _wire(monkeypatch, container)

    update_mod.run_app_update("proj", ["payments"], verbose=verbose, no_recache=True)

    assert recache_calls == []
    # The update itself still ran.
    assert any("bench --site a.localhost migrate" in c for c in container.calls)


def test_recache_runs_by_default_after_a_successful_pull(monkeypatch):
    container = FakeFrappeContainer(available_apps=["frappe", "payments"])
    recache_calls = _wire(monkeypatch, container)

    update_mod.run_app_update("proj", ["payments"], verbose=True)

    assert recache_calls == ["proj"]


def test_recache_is_skipped_when_every_app_failed_to_pull(monkeypatch):
    # Nothing was updated, so there is nothing to re-cache: the recache is gated on
    # at least one app having pulled cleanly.
    container = FakeFrappeContainer(available_apps=["frappe", "payments"])
    recache_calls = _wire(monkeypatch, container)
    container.fail_on = ["git pull"]

    with pytest.raises(typer.Exit):
        update_mod.run_app_update("proj", ["payments"], verbose=True)

    assert recache_calls == []


# --------------------------------------------------------- 1.3 multi-app fan-out
# ------------------- (every existing _update_project test passes exactly ONE app)


def test_multi_app_pulls_every_app_once(monkeypatch):
    container = FakeFrappeContainer(available_apps=["frappe", "payments", "hrms"])
    _wire(monkeypatch, container)

    update_mod.run_app_update("proj", ["payments", "hrms"], verbose=True)

    # Each app's pull runs in ITS OWN directory, which is how the fan-out addresses
    # them: `git pull` is the same string every time, so the workdir is the only
    # thing distinguishing them.
    assert sum(1 for c in container.calls if c == "git pull") == 2
    assert any("test -d /workspace/frappe-bench/apps/payments" in c for c in container.calls)
    assert any("test -d /workspace/frappe-bench/apps/hrms" in c for c in container.calls)


def test_multi_app_one_failure_still_updates_the_others(monkeypatch, capsys):
    # Continue-and-report-all across the APP fan-out, mirroring the site fan-out.
    container = FakeFrappeContainer(available_apps=["frappe", "payments", "hrms"])
    _wire(monkeypatch, container)
    container.fail_on = ["apps/hrms"]  # only hrms' pull fails

    with pytest.raises(typer.Exit) as exc:
        update_mod.run_app_update("proj", ["payments", "hrms"], verbose=True)

    assert exc.value.exit_code == 1
    out = _out(capsys)
    assert "hrms: Git pull failed" in out
    # The healthy app still drove a migration, and the summary still credits it.
    assert any("bench --site a.localhost migrate" in c for c in container.calls)
    assert "Successfully updated 1 app(s)" in out


def test_multi_app_missing_app_directory_is_reported_not_pulled(monkeypatch, capsys):
    # An app with no apps/<name> directory is recorded as failed without attempting
    # a pull inside a directory that is not there.
    container = FakeFrappeContainer(available_apps=["frappe", "payments"])
    _wire(monkeypatch, container)
    container.fail_on = ["test -d /workspace/frappe-bench/apps/ghost"]

    with pytest.raises(typer.Exit) as exc:
        update_mod.run_app_update("proj", ["payments", "ghost"], verbose=True)

    assert exc.value.exit_code == 1
    err = " ".join(capsys.readouterr().err.split())
    assert "App 'ghost' not found" in err
    assert sum(1 for c in container.calls if c == "git pull") == 1


def test_multi_app_affected_sites_are_the_union_across_apps(monkeypatch):
    # Discovery unions each app's sites: a site with either app installed must be
    # migrated exactly once, never twice.
    container = FakeFrappeContainer(available_apps=["frappe", "payments", "hrms"])
    _wire_update(monkeypatch, container)
    monkeypatch.setattr(update_mod.time, "sleep", lambda *_a, **_k: None)

    per_app = {"payments": ["a.localhost"], "hrms": ["a.localhost", "b.localhost"]}
    monkeypatch.setattr(
        update_mod,
        "_get_sites_with_app",
        lambda project, bench_path, app, container=None, verbose=False: list(per_app[app]),
    )

    update_mod.run_app_update("proj", ["payments", "hrms"], verbose=True)

    assert sum(1 for c in container.calls if c == "bench --site a.localhost migrate") == 1
    assert sum(1 for c in container.calls if c == "bench --site b.localhost migrate") == 1


def test_multi_app_success_banner_counts_only_the_apps_that_updated(monkeypatch, capsys):
    container = FakeFrappeContainer(available_apps=["frappe", "payments", "hrms"])
    _wire(monkeypatch, container)

    update_mod.run_app_update("proj", ["payments", "hrms"], verbose=True)

    assert "Successfully updated 2 app(s)" in _out(capsys)


def test_no_apps_refuses_before_touching_the_project(monkeypatch, capsys):
    # run_app_update's own guard: at least one app is required.
    container = FakeFrappeContainer(available_apps=["frappe"])
    _wire(monkeypatch, container)

    with pytest.raises(typer.Exit) as exc:
        update_mod.run_app_update("proj", [], verbose=True)

    assert exc.value.exit_code == 1
    assert "At least one app must be specified" in capsys.readouterr().err
    assert container.calls == []
