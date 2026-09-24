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

import json
import shlex

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


def test_maintenance_is_disabled_even_when_the_migrate_raises(monkeypatch, container, _no_sleep):
    """Why this is a plain function and not a generator: an abandoned generator's
    `finally` does not run, and the cost here is a site left down.

    ``_no_sleep`` controls the interrupt-cleanup clock: the raise unwinds through
    ``end_migrate_on_interrupt``, whose 10s+5s wait must be reached by the fake
    clock rather than really waited (else this is a 15s unit test)."""

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


def test_a_held_migrate_lock_refuses_the_migrate_entirely_and_names_unlock(monkeypatch, container):
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
    assert probes == [f"flock -n -E 200 {BENCH}/sites/{SITE}/locks/bench_migrate.lock -c true"]


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


# --------------------------------------- interrupt cleanup: end the orphan, clear last
#
# The v16 race BUG-11 fix. cwcli cannot kill the in-container `bench migrate` by closing
# the exec socket (Docker has no kill-exec API), so on an interrupt the migrate keeps
# running orphaned; on v15+ it self-manages maintenance mode and re-asserts it AFTER
# cwcli's cleanup clears it, stranding the site at HTTP 503. The fix ends the process
# cwcli started FIRST (by a unique marker in its environ), waits for it to exit, then
# clears maintenance LAST with a settle + re-check. The fake below re-asserts maintenance
# after the first clear exactly like the real orphan, so the discriminating test fails on
# the pre-fix single-clear cleanup and passes with the fix.


class _InterruptAPI:
    """The migrate exec is where the interrupt lands: reading its stream raises
    KeyboardInterrupt (the SIGTERM/SIGHUP/Ctrl+C unwind), leaving the in-container
    migrate orphaned and still running (``orphan_alive``)."""

    def __init__(self, owner):
        self.owner = owner

    def exec_create(self, cid, cmd, workdir=None, tty=False, environment=None):
        self.owner.migrate_env = environment
        return {"Id": "exec-migrate"}

    def exec_start(self, exec_id, stream=True, demux=True):
        self.owner.orphan_alive = True  # the migrate is now running in-container

        def _stream():
            raise KeyboardInterrupt
            yield  # pragma: no cover - only makes this a generator

        return _stream()

    def exec_inspect(self, exec_id):
        return {"ExitCode": 0}


class InterruptedMigrateContainer:
    """A migrate that is interrupted mid-run, with an orphan modelling the v16 race."""

    labels = {"com.docker.compose.service": "frappe"}
    name = "proj-frappe-1"
    id = "cid"

    def __init__(self, *, killable=True):
        import types

        self.status = "running"
        self.maintenance = False  # the site is not in maintenance to start with
        self.orphan_alive = False
        self.killable = killable  # False models an orphan cwcli cannot stop
        self.migrate_env = None
        self.calls: list[str] = []
        self.client = types.SimpleNamespace(api=_InterruptAPI(self))

    def reload(self):
        pass

    def _run(self, cmd):
        s = cmd if isinstance(cmd, str) else " ".join(cmd)
        self.calls.append(s)
        if "set-maintenance-mode on" in s:
            self.maintenance = True
            return 0, ""
        if "set-maintenance-mode off" in s:
            self.maintenance = False
            # THE RACE: while the orphan migrate is still alive it turns maintenance
            # back ON right after cwcli clears it (v15+ self-manages it), then dies
            # without clearing it again.
            if self.orphan_alive:
                self.maintenance = True
            return 0, ""
        if s.startswith("cat sites/") and s.endswith("site_config.json"):
            import json

            return 0, json.dumps({"maintenance_mode": 1 if self.maintenance else 0})
        if "/proc/[0-9]*" in s and "kill -" in s:  # _signal_marked
            if self.killable:
                self.orphan_alive = False
            return 0, ""
        if "/proc/[0-9]*" in s:  # _marked_process_alive probe
            return (0, "") if self.orphan_alive else (1, "")
        return 0, ""

    def exec_run(self, cmd, workdir=None, **kwargs):
        code, out = self._run(cmd)
        return code, out.encode() if isinstance(out, str) else out


@pytest.fixture()
def _no_sleep(monkeypatch):
    """Control time in the interrupt cleanup: sleeps are instant AND they advance a
    fake ``time.monotonic`` clock, so ``_wait_marked_gone``'s real 10s+5s deadline is
    reached by the clock, never by really waiting. Without this a killable=False
    orphan spins the loop flat-out for 15s while ``container.calls`` grows to
    gigabytes - the class the tests/conftest.py memory/slow guard fails on."""
    clock = [0.0]

    def _sleep(seconds=0.0, *_a, **_k):
        clock[0] += seconds or bench_ops._INTERRUPT_POLL_INTERVAL

    monkeypatch.setattr(bench_ops.time, "sleep", _sleep)
    monkeypatch.setattr(bench_ops.time, "monotonic", lambda: clock[0])


def _drive_interrupted_migrate(monkeypatch, container):
    monkeypatch.setattr(
        bench_ops.resolvers,
        "resolve_container_and_bench",
        lambda *a, **k: (container, BENCH, []),
    )
    monkeypatch.setattr(bench_ops, "_resolve_site", lambda *a, **k: SITE)
    monkeypatch.setattr(bench_ops, "_migrate_lock_held", lambda *a, **k: False)
    events: list = []
    with pytest.raises(KeyboardInterrupt):
        bench_ops.migrate_site("proj", site=SITE, on_event=events.append)
    return events


def test_an_interrupted_migrate_ends_the_orphan_then_leaves_the_site_out_of_maintenance(
    monkeypatch, _no_sleep
):
    """DISCRIMINATING: fails on the pre-fix single-clear cleanup (the orphan re-asserts
    and wins), passes with the fix (the orphan is ended first, so the clear is last)."""
    container = InterruptedMigrateContainer(killable=True)
    _drive_interrupted_migrate(monkeypatch, container)

    assert container.orphan_alive is False  # the orphan was terminated
    assert container.maintenance is False  # and the site is OUT of maintenance


def test_the_migrate_exec_carries_a_unique_marker_and_cleanup_targets_only_it(
    monkeypatch, _no_sleep
):
    """The orphan is found by a unique per-invocation marker in its environ, never a
    name pattern that could match another site's or another user's migrate."""
    container = InterruptedMigrateContainer(killable=True)
    _drive_interrupted_migrate(monkeypatch, container)

    assert container.migrate_env is not None
    assert list(container.migrate_env) == [bench_ops._MIGRATE_MARKER_ENV]
    token = container.migrate_env[bench_ops._MIGRATE_MARKER_ENV]
    assert token  # a non-empty per-invocation token

    kill_scans = [c for c in container.calls if "/proc/[0-9]*" in c and "kill -" in c]
    assert kill_scans, "the orphan must be signalled"
    assert all(token in c for c in kill_scans), "cleanup must target exactly this token"
    assert not any("pkill" in c for c in container.calls)  # never a name pattern


def test_cleanup_order_is_terminate_then_clear_maintenance_last(monkeypatch, _no_sleep):
    """terminate -> wait -> clear. The first clear must come AFTER the first kill, or
    the orphan re-asserts maintenance behind the clear."""
    container = InterruptedMigrateContainer(killable=True)
    _drive_interrupted_migrate(monkeypatch, container)

    first_kill = next(i for i, c in enumerate(container.calls) if "kill -" in c)
    first_clear = next(i for i, c in enumerate(container.calls) if "set-maintenance-mode off" in c)
    assert first_kill < first_clear


def test_an_orphan_cwcli_cannot_kill_still_clears_maintenance_and_says_so(monkeypatch, _no_sleep):
    """The 'cannot stop the process' path: cwcli must STILL clear maintenance, escalate
    SIGTERM -> SIGKILL, and say plainly the migrate may still be running, naming the
    command to re-check and clear the site."""
    container = InterruptedMigrateContainer(killable=False)
    events = _drive_interrupted_migrate(monkeypatch, container)

    signals = [c for c in container.calls if "kill -" in c]
    assert any("kill -TERM" in c for c in signals)
    assert any("kill -KILL" in c for c in signals)  # escalated
    # It still ATTEMPTED to clear maintenance (the last write it can make).
    assert any("set-maintenance-mode off" in c for c in container.calls)
    # And it said so plainly, naming how to re-check + clear.
    notices = [e for e in events if isinstance(e, bench_ops.BenchOpNotice)]
    assert notices, "an unconfirmed orphan must be surfaced"
    msg = notices[0].message
    assert "may still be running" in msg
    assert "cwcli status proj" in msg
    assert "set-maintenance-mode off" in msg


def test_a_normal_migrate_does_not_touch_the_interrupt_cleanup(container, monkeypatch):
    """No interrupt: no /proc scan, no kill, no settle re-check - the normal path is
    byte-for-byte the old single disable, so nothing here slows a clean migrate."""
    no_sleep_calls: list = []
    monkeypatch.setattr(bench_ops.time, "sleep", lambda *a, **k: no_sleep_calls.append(a))

    result = bench_ops.migrate_site("proj", site=SITE)

    assert result.status is Status.OK
    assert not any("/proc/" in c for c in container.calls)
    assert not any("kill -" in c for c in container.calls)
    assert no_sleep_calls == []  # no settle sleep on the clean path


# ------------------------------------------------------------------- setup wizard


class SetupWizardContainer(FakeContainer):
    """Models the `setup_complete` System Settings probe and
    `setup_wizard.setup_complete` RPC over `bench execute`, mirroring the real
    function's own idempotency: the flag only flips True once the RPC actually
    runs, and a `fail` container reports the RPC itself failing (as bench does
    when the called function raises)."""

    def __init__(self, *, already_complete=False, fail=False, **kwargs):
        super().__init__(**kwargs)
        self.is_setup_complete = already_complete
        self.fail = fail

    def _run(self, cmd):
        cmd_str = cmd if isinstance(cmd, str) else " ".join(cmd)
        if "execute frappe.db.get_single_value" in cmd_str:
            self.calls.append(cmd_str)
            # bench execute prints the return only when truthy, so an incomplete
            # site is an empty read (the real cross-version behavior).
            return 0, (json.dumps(1) + "\n" if self.is_setup_complete else "")
        if "setup_wizard.setup_wizard.setup_complete" in cmd_str:
            self.calls.append(cmd_str)
            if self.fail:
                return 1, "Traceback (most recent call last):\nRuntimeError: boom\n"
            self.is_setup_complete = True
            return 0, json.dumps({"status": "ok"}) + "\n"
        return super()._run(cmd)


@pytest.fixture()
def setup_container(monkeypatch):
    c = SetupWizardContainer()
    monkeypatch.setattr(core_docker, "get_frappe_container", lambda _p: c)
    monkeypatch.setattr(resolvers, "cached_benches", lambda _p: [{"path": BENCH}])
    monkeypatch.setattr(resolvers, "resolve_default_site", lambda *a, **k: SITE)
    return c


def test_completes_a_fresh_sites_setup_wizard(setup_container):
    result = bench_ops.complete_setup_wizard("proj", site=SITE)

    assert result.status is Status.OK
    assert result.data.ok is True
    assert result.data.already_complete is False
    assert result.data.setup_complete is True
    rpc_calls = [c for c in setup_container.calls if "setup_wizard.setup_complete" in c]
    assert len(rpc_calls) == 1
    assert f"--site {SITE}" in rpc_calls[0]


def test_defaults_are_used_when_no_flags_are_given(setup_container):
    result = bench_ops.complete_setup_wizard("proj", site=SITE)

    assert result.data.country == bench_ops.DEFAULT_SETUP_COUNTRY
    assert result.data.currency == bench_ops.DEFAULT_SETUP_CURRENCY
    assert result.data.timezone == bench_ops.DEFAULT_SETUP_TIMEZONE
    assert result.data.language == bench_ops.DEFAULT_SETUP_LANGUAGE
    rpc_call = next(c for c in setup_container.calls if "setup_wizard.setup_complete" in c)
    assert bench_ops.DEFAULT_SETUP_COUNTRY in rpc_call
    assert bench_ops.DEFAULT_SETUP_CURRENCY in rpc_call


def test_explicit_country_currency_timezone_override_the_defaults(setup_container):
    result = bench_ops.complete_setup_wizard(
        "proj", site=SITE, country="Germany", currency="EUR", timezone="Europe/Berlin"
    )

    assert result.data.country == "Germany"
    assert result.data.currency == "EUR"
    assert result.data.timezone == "Europe/Berlin"
    rpc_call = next(c for c in setup_container.calls if "setup_wizard.setup_complete" in c)
    assert "Germany" in rpc_call
    assert "EUR" in rpc_call
    assert "Europe/Berlin" in rpc_call


def test_no_user_is_created_by_the_args_dict(setup_container):
    """email/full_name/password are deliberately absent: Administrator is already
    provisioned by `cwcli init`, and this verb must not invent a second user."""
    bench_ops.complete_setup_wizard("proj", site=SITE)

    rpc_call = next(c for c in setup_container.calls if "setup_wizard.setup_complete" in c)
    kwargs_json = shlex.split(rpc_call)[shlex.split(rpc_call).index("--kwargs") + 1]
    args = json.loads(kwargs_json)["args"]
    assert "email" not in args
    assert "full_name" not in args
    assert "password" not in args


def test_an_already_complete_site_is_reported_as_such_and_stays_ok(monkeypatch):
    """Idempotency is Frappe's own (setup_complete no-ops under its lock when the
    site is already set up); this proves the report reads that state honestly."""
    c = SetupWizardContainer(already_complete=True)
    monkeypatch.setattr(core_docker, "get_frappe_container", lambda _p: c)
    monkeypatch.setattr(resolvers, "cached_benches", lambda _p: [{"path": BENCH}])
    monkeypatch.setattr(resolvers, "resolve_default_site", lambda *a, **k: SITE)

    result = bench_ops.complete_setup_wizard("proj", site=SITE)

    assert result.status is Status.OK
    assert result.data.ok is True
    assert result.data.already_complete is True
    assert result.data.setup_complete is True


def test_a_failed_rpc_is_reported_not_raised(monkeypatch):
    c = SetupWizardContainer(fail=True)
    monkeypatch.setattr(core_docker, "get_frappe_container", lambda _p: c)
    monkeypatch.setattr(resolvers, "cached_benches", lambda _p: [{"path": BENCH}])
    monkeypatch.setattr(resolvers, "resolve_default_site", lambda *a, **k: SITE)

    result = bench_ops.complete_setup_wizard("proj", site=SITE)

    assert result.status is Status.WARNING
    assert result.data.ok is False
    assert result.data.setup_complete is False


def test_no_site_falls_back_to_the_default_site(setup_container):
    result = bench_ops.complete_setup_wizard("proj")

    assert result.data.site == SITE
    assert any(w.code == "site.default_used" for w in result.warnings)


def test_setup_wizard_on_a_stopped_container_is_a_returned_choice_never_a_start(monkeypatch):
    c = SetupWizardContainer(status="exited")
    monkeypatch.setattr(core_docker, "get_frappe_container", lambda _p: c)

    result = bench_ops.complete_setup_wizard("proj", site=SITE)

    assert result.status is Status.NEEDS_CHOICE
    assert result.choice.kind == "confirm_start"
    assert c.calls == []
