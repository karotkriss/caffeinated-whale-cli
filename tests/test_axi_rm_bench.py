"""Agent-surface decisions for ``cwcli axi rm-bench``.

Pins what the axi frontend decides rather than inherits: ``--yes`` is REQUIRED
(a missing one is a usage error before the core is reached), a ``NEEDS_CHOICE``
becomes a non-prompting usage error, and the exit code reads ``outcome.ok`` (not
the envelope status) so an incomplete removal never exits 0.
"""

import pytest
import typer

from caffeinated_whale_cli.commands import axi as axi_mod
from caffeinated_whale_cli.core import rm_bench as core_rm_bench
from caffeinated_whale_cli.core.envelope import Choice, Message, Result, Status


def test_missing_yes_is_usage_before_the_core_is_reached(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(core_rm_bench, "remove_bench", lambda *a, **k: called.append(True))

    with pytest.raises(typer.Exit) as exc:
        axi_mod.axi_rm_bench("missing-project", bench="2", yes=False)

    assert exc.value.exit_code == 2
    assert called == []
    output = capsys.readouterr().out
    assert "error:" in output
    assert "--yes" in output


def test_confirm_remove_bench_needs_choice_is_a_usage_error(monkeypatch, capsys):
    """The core should never see a confirm returned to axi (axi passes consent), but
    if any NEEDS_CHOICE surfaces it becomes a structured usage error, never a prompt."""
    monkeypatch.setattr(
        core_rm_bench,
        "remove_bench",
        lambda *a, **k: Result(
            status=Status.NEEDS_CHOICE,
            choice=Choice(kind="confirm_remove_bench", param="consent", prompt="Remove it?"),
        ),
    )

    with pytest.raises(typer.Exit) as exc:
        axi_mod.axi_rm_bench("proj", bench="2", yes=True)

    assert exc.value.exit_code == 2
    out = capsys.readouterr().out
    assert "error:" in out
    assert "--yes" in out


def test_incomplete_removal_exits_nonzero_even_though_envelope_is_warning(monkeypatch, capsys):
    outcome = core_rm_bench.BenchRemovalOutcome(
        project="proj",
        bench_path="/workspace/frappe-bench-2",
        sites_dropped=["a.localhost"],
        sites_failed=["b.localhost"],
        archived_host_paths=["/host/x.tar"],
        dir_removed=False,
        ok=False,
        failures=["site 'b.localhost' was dropped but its backup could not be copied out"],
    )
    monkeypatch.setattr(
        core_rm_bench,
        "remove_bench",
        lambda *a, **k: Result(status=Status.WARNING, data=outcome),
    )
    monkeypatch.setattr(axi_mod.cache, "recache_project", lambda *a, **k: True)

    with pytest.raises(typer.Exit) as exc:
        axi_mod.axi_rm_bench("proj", bench="2", yes=True)

    assert exc.value.exit_code == 1  # ok=False, NOT the WARNING->0 mapping
    out = capsys.readouterr().out
    assert "bench_path: /workspace/frappe-bench-2" in out
    assert "ok: false" in out


def test_clean_removal_exits_zero_and_emits_toon(monkeypatch, capsys):
    outcome = core_rm_bench.BenchRemovalOutcome(
        project="proj",
        bench_path="/workspace/frappe-bench-2",
        sites_dropped=["a.localhost"],
        sites_failed=[],
        archived_host_paths=["/host/a.tar"],
        dir_removed=True,
        ok=True,
    )
    monkeypatch.setattr(
        core_rm_bench,
        "remove_bench",
        lambda *a, **k: Result(status=Status.OK, data=outcome, warnings=[Message("w", "note")]),
    )
    monkeypatch.setattr(axi_mod.cache, "recache_project", lambda *a, **k: True)

    with pytest.raises(typer.Exit) as exc:
        axi_mod.axi_rm_bench("proj", bench="2", yes=True)

    assert exc.value.exit_code == 0
    out = capsys.readouterr().out
    assert "ok: true" in out
    assert "dir_removed: true" in out
