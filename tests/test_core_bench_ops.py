"""``core.bench_ops`` - standalone ``bench migrate`` and ``bench run-tests``.

The two commands every Frappe proof runs, neither of which existed as a callable
operation before: ``bench migrate`` lived only inside ``core.update``'s pull-and-
fan-out and ``restore_apply``, and ``bench run-tests`` existed nowhere.

What must be right here, each pinning a decision rather than an implementation:

- **A migrate resolves EXACTLY ONE site.** This is the module's whole reason to
  exist. ``apps update`` fans out because there the APP is the subject; here the
  SITE is, and an agent must never discover it migrated four of them.
- **The maintenance gate refuses, it does not warn.** A site that cannot be put
  into maintenance is NOT migrated - no ``bench migrate`` is issued at all.
- **The disable runs in a `finally`.** A plain function was chosen over a generator
  precisely for this: the cost of a skipped cleanup here is a site left DOWN.
- **``run_tests`` takes its site and app as required parameters**, so the "no
  default target" ruling cannot be eroded by a later default sneaking into a
  keyword.
"""

from __future__ import annotations

import pytest

from caffeinated_whale_cli.core import bench_ops, resolvers
from caffeinated_whale_cli.core import docker as core_docker
from caffeinated_whale_cli.core.envelope import Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind

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


def _actions(report):
    return [r.action for r in report.results]


def _set_lock_probe_exit(monkeypatch, container, exit_code):
    original_exec_run = container.exec_run

    def exec_run(cmd, *args, **kwargs):
        cmd_str = cmd if isinstance(cmd, str) else " ".join(cmd)
        if cmd_str.startswith("flock "):
            container.calls.append(cmd_str)
            return exit_code, b"probe output"
        return original_exec_run(cmd, *args, **kwargs)

    monkeypatch.setattr(container, "exec_run", exec_run)


# ------------------------------------------------------------------ exactly one site


def test_a_migrate_runs_against_exactly_one_site_and_reports_it(container):
    result = bench_ops.migrate_site("proj", site=SITE)

    assert result.status is Status.OK
    assert result.data.ok is True
    assert result.data.site == SITE
    # endswith, not `in`: a lock-check call also names the lock file
    # `bench_migrate.lock`, and a substring match would catch it too.
    migrates = [c for c in container.calls if c.endswith("migrate")]
    assert migrates == [f"bench --site {SITE} migrate"]


def test_a_migrate_never_fans_out_across_a_multi_site_bench(monkeypatch, container):
    """The load-bearing difference from `apps update`, which discovers its targets.

    A bench holding several sites must still yield exactly ONE migrate: the site is
    the subject here, so anything else is migrating a site nobody named.
    """
    monkeypatch.setattr(
        bench_ops.resolvers,
        "require_site_dir",
        lambda _c, path, site: f"{path}/sites/{site}",
    )

    bench_ops.migrate_site("proj", site="one.localhost")

    migrates = [c for c in container.calls if " migrate" in c]
    assert len(migrates) == 1
    assert "one.localhost" in migrates[0]


def test_no_site_falls_back_to_the_default_site_and_warns(container):
    result = bench_ops.migrate_site("proj")

    assert result.data.site == SITE
    assert any(w.code == "site.default_used" for w in result.warnings)


def test_an_unknown_site_is_a_typed_error_and_migrates_nothing(monkeypatch, container):
    def _missing(_c, path, site):
        raise CwcliError(ErrorKind.NOT_FOUND, "site.not_found", f"Site '{site}' not found")

    monkeypatch.setattr(bench_ops.resolvers, "require_site_dir", _missing)

    with pytest.raises(CwcliError) as exc:
        bench_ops.migrate_site("proj", site="nope.localhost")

    assert exc.value.kind is ErrorKind.NOT_FOUND
    assert not [c for c in container.calls if "migrate" in c]


# ------------------------------------------------------------------ the maintenance gate


def test_a_failed_maintenance_enable_refuses_the_migrate_entirely(container):
    """The gate: not "migrate anyway and warn", but "do not migrate at all".

    `core/update.py` calls its equivalent THE LOAD-BEARING GATE. A standalone
    migrate that skipped it would reach the same schema mutation with strictly
    fewer protections than the path that already exists.
    """
    container.fail_on = ["set-maintenance-mode on"]

    result = bench_ops.migrate_site("proj", site=SITE)

    assert result.data.ok is False
    assert result.status is Status.WARNING
    assert not [c for c in container.calls if c.endswith("migrate")]
    assert _actions(result.data) == ["maintenance_on"]
    assert "was NOT run" in result.data.results[0].message


def test_maintenance_mode_is_enabled_before_the_migrate_and_disabled_after(container):
    bench_ops.migrate_site("proj", site=SITE)

    ordered = [c for c in container.calls if "maintenance" in c or c.endswith("migrate")]
    assert ordered == [
        f"bench --site {SITE} set-maintenance-mode on",
        f"bench --site {SITE} migrate",
        f"bench --site {SITE} set-maintenance-mode off",
    ]


def test_a_site_left_in_maintenance_is_reported_and_fails_the_operation(container):
    """A site that cannot be taken back out is DOWN, even though the migrate worked."""
    container.fail_on = ["set-maintenance-mode off"]

    result = bench_ops.migrate_site("proj", site=SITE)

    assert result.data.maintenance_left_on is True
    assert result.data.ok is False
    off = [r for r in result.data.results if r.action == "maintenance_off"][0]
    assert "STILL in maintenance mode" in off.message
    # The remedy is a runnable cwcli command, never a raw in-container one.
    assert "cwcli run" in off.message


def test_maintenance_is_disabled_even_when_the_migrate_raises(monkeypatch, container):
    """Why this is a plain function and not a generator: an abandoned generator's
    `finally` does not run, and the cost here is a site left down."""

    def _boom(*_a, **_k):
        raise RuntimeError("stream exploded")

    monkeypatch.setattr(bench_ops, "_run_step", _boom)

    with pytest.raises(RuntimeError):
        bench_ops.migrate_site("proj", site=SITE)

    assert f"bench --site {SITE} set-maintenance-mode off" in container.calls


def test_there_is_no_skip_maintenance_escape_hatch(container):
    """Captain ruling M1. A flag that removes the gate has no named beneficiary,
    and its existence would invite its use."""
    with pytest.raises(TypeError):
        bench_ops.migrate_site("proj", site=SITE, skip_maintenance=True)


# ------------------------------------------------------------------ the stranded-lock gate


def test_a_held_migrate_lock_refuses_the_migrate_entirely_and_names_unlock(
    monkeypatch, container
):
    """The gate this task exists for: a genuinely-held migrate lock is refused
    BEFORE maintenance mode is even touched, and the refusal names the exact
    remedy rather than surfacing a generic failure later.

    Verified against a real bench (see `_migrate_lock_held`'s docstring): `flock
    -n` probes the SAME kernel primitive frappe's own `filelock()` acquires, so
    this can never be a false positive on a harmless leftover file.
    """
    _set_lock_probe_exit(monkeypatch, container, 200)

    result = bench_ops.migrate_site("proj", site=SITE)

    assert result.data.ok is False
    assert result.status is Status.WARNING
    assert not [c for c in container.calls if c.endswith("migrate")]
    assert not [c for c in container.calls if "maintenance" in c]
    assert _actions(result.data) == ["lock_check"]
    message = result.data.results[0].message
    assert f"cwcli unlock proj --site {SITE}" in message
    assert "locks/bench_migrate.lock" in message


def test_a_failed_migrate_lock_probe_raises_a_typed_precondition(monkeypatch, container):
    _set_lock_probe_exit(monkeypatch, container, 127)

    with pytest.raises(CwcliError) as exc:
        bench_ops.migrate_site("proj", site=SITE)

    assert exc.value.kind is ErrorKind.PRECONDITION
    assert exc.value.code == "migrate.lock_probe_failed"
    assert "flock exited 127" in exc.value.message
    assert not [c for c in container.calls if c.endswith("migrate")]
    assert not [c for c in container.calls if "maintenance" in c]


def test_a_lock_file_with_no_live_holder_never_blocks_a_migrate(container):
    """The false-positive guard, proven on a real bench: an empty leftover
    `bench_migrate.lock` that nothing holds is harmless, and `bench migrate`
    succeeds straight through it. Gating on file presence would refuse a
    perfectly runnable migrate; gating on `flock -n` does not."""
    result = bench_ops.migrate_site("proj", site=SITE)

    assert result.data.ok is True
    probes = [c for c in container.calls if c.startswith("flock -n")]
    assert probes == [
        f"flock -n -E 200 {BENCH}/sites/{SITE}/locks/bench_migrate.lock -c true"
    ]


def test_a_bench_never_migrated_has_no_locks_dir_and_skips_the_probe(container):
    """A fresh bench has no locks directory at all - nothing to probe, and the
    probe command (which would otherwise try to create the lock file) never runs."""
    container.fail_on = ["/locks"]  # simulate `test -d .../locks` failing (absent)

    result = bench_ops.migrate_site("proj", site=SITE)

    assert result.data.ok is True
    assert not [c for c in container.calls if c.startswith("flock")]


def test_the_bench_op_command_naming_the_bench_selector_is_carried_in_the_hint(
    monkeypatch, container
):
    """When the caller passed an explicit --bench selector, the unlock hint must
    carry the SAME selector - unlock has to resolve to the identical bench, not
    whichever one a bare re-run would default to."""
    _set_lock_probe_exit(monkeypatch, container, 200)

    result = bench_ops.migrate_site("proj", site=SITE, bench="0")

    assert "--bench 0" in result.data.results[0].message


# ------------------------------------------------------------------------- run-tests


def test_run_tests_requires_its_site_and_app_as_parameters():
    """Captain ruling S1, pinned at the CORE so no frontend can default them."""
    with pytest.raises(TypeError):
        bench_ops.run_tests("proj")
    with pytest.raises(TypeError):
        bench_ops.run_tests("proj", site=SITE)
    with pytest.raises(TypeError):
        bench_ops.run_tests("proj", app="payments")


def test_a_passing_suite_reports_ok_with_its_resolved_target(container):
    result = bench_ops.run_tests("proj", site=SITE, app="payments")

    assert result.status is Status.OK
    assert result.data.ok is True
    assert result.data.site == SITE
    assert result.data.app == "payments"
    assert f"bench --site {SITE} run-tests --app payments" in container.calls


def test_a_failing_suite_reports_not_ok(container):
    container.fail_on = ["run-tests"]

    result = bench_ops.run_tests("proj", site=SITE, app="payments")

    assert result.data.ok is False
    assert result.status is Status.WARNING


def test_run_tests_does_not_touch_maintenance_mode(container):
    """A test run is not a schema mutation, and maintenance mode would change the
    conditions the suite runs under."""
    bench_ops.run_tests("proj", site=SITE, app="payments")

    assert not [c for c in container.calls if "maintenance" in c]


def test_an_empty_app_is_a_usage_error(container):
    with pytest.raises(CwcliError) as exc:
        bench_ops.run_tests("proj", site=SITE, app="   ")

    assert exc.value.kind is ErrorKind.USAGE


# ----------------------------------------------------------------- the shared forks


def test_a_stopped_container_is_a_returned_choice_never_a_start(container):
    """The core REPORTS the fork; it never starts anything."""
    container.status = "exited"

    result = bench_ops.migrate_site("proj", site=SITE)

    assert result.status is Status.NEEDS_CHOICE
    assert result.choice.kind == "confirm_start"
    assert not container.calls


def test_a_multi_bench_project_with_no_selector_is_a_returned_choice(monkeypatch, container):
    monkeypatch.setattr(
        resolvers, "cached_benches", lambda _p: [{"path": BENCH}, {"path": "/workspace/other"}]
    )

    result = bench_ops.migrate_site("proj", site=SITE)

    assert result.status is Status.NEEDS_CHOICE
    assert result.choice.kind == "select_bench"


# ------------------------------------------------------- the promoted shared helper


def test_set_maintenance_is_shared_with_core_update_not_copied(monkeypatch):
    """Captain's binding engineering item: PROMOTE, do not copy.

    Two copies of a maintenance-mode lifecycle drift until one of them stops
    disabling, and a site stuck in maintenance is a site that is down. This proves
    `core.update` routes through THIS implementation - a re-inlined copy in either
    module makes it fail.
    """
    from caffeinated_whale_cli.core import update as core_update

    seen: list[tuple] = []
    monkeypatch.setattr(
        bench_ops, "set_maintenance", lambda c, p, s, *, enable: seen.append((s, enable)) or True
    )

    core_update._set_maintenance(object(), BENCH, SITE, enable=True)

    assert seen == [(SITE, True)]
