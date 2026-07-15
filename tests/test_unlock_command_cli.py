"""``cwcli unlock <project> --bench <selector>`` must resolve and succeed.

Regression test: the CLI wrapper used to pre-resolve ``--bench``/``--path`` into a
concrete ``bench_path`` (via ``resolve_bench_path``) but then forwarded BOTH the
raw ``bench`` selector and the resolved ``bench_path`` to ``core.unlock``. Since
``core.resolvers.resolve_bench`` raises ``USAGE bench.selector_conflict`` whenever
both are truthy, any use of ``--bench`` on ``cwcli unlock`` failed even though
``--path`` was never passed. ``commands/backup.py`` (the reference bench-op
frontend) never re-forwards the raw selector - only the resolved path - and this
pins ``unlock`` to the same contract.
"""

from caffeinated_whale_cli.commands import unlock as unlock_mod
from caffeinated_whale_cli.core.envelope import Result, Status
from caffeinated_whale_cli.core.unlock import UnlockOutcome


def _run(*, bench):
    monkeypatch_calls = {}

    def fake_unlock(project_name, **kwargs):
        monkeypatch_calls["project_name"] = project_name
        monkeypatch_calls["kwargs"] = kwargs
        return Result(
            status=Status.OK,
            data=UnlockOutcome(
                site="s.localhost",
                bench_path="/workspace/frappe-bench-1",
                locks_path="/workspace/frappe-bench-1/sites/s.localhost/locks",
                removed=[],
                already_unlocked=True,
            ),
        )

    return fake_unlock, monkeypatch_calls


def test_bench_selector_resolves_and_unlocks_successfully(monkeypatch):
    monkeypatch.setattr(unlock_mod, "ensure_containers_running", lambda *a, **k: True)
    monkeypatch.setattr(
        unlock_mod, "resolve_bench_path", lambda *a, **k: "/workspace/frappe-bench-1"
    )
    fake_unlock, calls = _run(bench="1")
    monkeypatch.setattr(unlock_mod.core_unlock, "unlock", fake_unlock)

    # __wrapped__ skips @handle_docker_errors' live Docker ping. Params are
    # positional per the Typer signature: project_name, site, bench, bench_path,
    # yes, verbose.
    unlock_mod.unlock.__wrapped__(
        "proj",  # project_name
        "s.localhost",  # site
        "1",  # bench (the selector under test)
        None,  # bench_path
        False,  # yes
        False,  # verbose
    )

    assert calls["project_name"] == "proj"
    # The core only ever receives the pre-resolved path - never the raw selector -
    # so it can't hit resolve_bench's selector_conflict guard.
    assert calls["kwargs"] == {"site": "s.localhost", "bench_path": "/workspace/frappe-bench-1"}
