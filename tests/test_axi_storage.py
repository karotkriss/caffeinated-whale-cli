"""``cwcli axi storage`` - the log-storage group (``logs`` read, and the three trims).

Pins: the group and its four verbs are registered; ``logs`` emits ONE TOON document; a
removal without ``--yes`` is a usage error naming the flag (exit 2), never a prompt;
with ``--yes`` the core's consent is passed; ``prune-bench-logs`` renders a
``select_bench`` fork as the shared usage error; typed errors keep the exit mapping.
"""

from __future__ import annotations

import pytest
import typer

from caffeinated_whale_cli.commands import axi as axi_mod
from caffeinated_whale_cli.core import log_storage as ls
from caffeinated_whale_cli.core.envelope import Choice, Message, Result, Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind

from .test_axi import assert_is_one_toon_document


def _group():
    return next(g for g in axi_mod.app.registered_groups if g.name == "storage").typer_instance


def test_the_storage_group_carries_the_four_verbs():
    names = {c.name for c in _group().registered_commands}
    assert names == {"logs", "truncate-docker-logs", "set-docker-log-rotation", "prune-bench-logs"}


def _report():
    return Result(
        status=Status.OK,
        data=ls.LogStorageReport(
            instances=[
                ls.InstanceLogStorage(
                    project="proj",
                    running=True,
                    docker=ls.DockerLogs(
                        project="proj",
                        containers=[
                            ls.DockerLog(
                                "proj-frappe-1",
                                "frappe",
                                "json-file",
                                "10m",
                                "3",
                                True,
                                150,
                                2,
                                "/x",
                            )
                        ],
                        total=150,
                        method="host",
                        compose_rotation={"max_size": "10m", "max_file": "3"},
                        note=None,
                    ),
                    benches=[
                        ls.BenchLogs(
                            "proj",
                            "/workspace/frappe-bench",
                            0,
                            100,
                            40,
                            ["/workspace/frappe-bench/logs/web.log.1"],
                            3,
                        )
                    ],
                    note=None,
                )
            ],
            journal=ls.JournalReport(False, "macOS has no systemd journal", None, None, False),
            docker_total=150,
            bench_total=140,
        ),
    )


def test_logs_emits_one_toon_document(monkeypatch, capsys):
    monkeypatch.setattr(ls, "report", lambda project=None: _report())
    with pytest.raises(typer.Exit) as exc:
        axi_mod.axi_storage_logs(None)
    out = capsys.readouterr().out
    assert exc.value.exit_code == 0
    assert_is_one_toon_document(out)
    assert "docker_total: 150" in out and "proj-frappe-1" in out and "web.log.1" in out


def test_logs_maps_a_typed_error_to_its_exit_code(monkeypatch, capsys):
    def boom(project=None):
        raise CwcliError(ErrorKind.NOT_FOUND, "project.not_found", "Project 'x' not found.")

    monkeypatch.setattr(ls, "report", boom)
    with pytest.raises(typer.Exit) as exc:
        axi_mod.axi_storage_logs("x")
    assert exc.value.exit_code == 1
    assert "Project 'x' not found." in capsys.readouterr().out


def test_truncate_without_yes_is_a_usage_error_naming_the_flag(monkeypatch, capsys):
    seen = {}

    def fake(project, *, consent=False):
        seen["consent"] = consent
        return Result(
            status=Status.NEEDS_CHOICE,
            choice=Choice(kind="confirm_truncate_logs", param="consent", prompt="Empty the logs?"),
        )

    monkeypatch.setattr(ls, "truncate_docker_logs", fake)
    with pytest.raises(typer.Exit) as exc:
        axi_mod.axi_storage_truncate_docker_logs("proj", yes=False)
    out = capsys.readouterr().out
    assert exc.value.exit_code == 2 and seen["consent"] is False
    assert "error: Empty the logs?" in out and "--yes" in out


def test_truncate_with_yes_passes_consent_and_emits_the_outcome(monkeypatch, capsys):
    monkeypatch.setattr(
        ls,
        "truncate_docker_logs",
        lambda project, *, consent=False: Result(
            status=Status.OK,
            data={"project": project, "truncated": ["a"], "freed_bytes": 9, "consent": consent},
        ),
    )
    with pytest.raises(typer.Exit) as exc:
        axi_mod.axi_storage_truncate_docker_logs("proj", yes=True)
    out = capsys.readouterr().out
    assert exc.value.exit_code == 0 and "consent: true" in out and "freed_bytes: 9" in out
    assert_is_one_toon_document(out)


def test_set_rotation_emits_the_recreate_warning(monkeypatch, capsys):
    monkeypatch.setattr(
        ls,
        "set_docker_log_rotation",
        lambda project, *, max_size, max_file: Result(
            status=Status.OK,
            data={"project": project, "max_size": max_size, "max_file": max_file, "changed": True},
            warnings=[Message("logs.rotation_on_recreate", "applies on recreate")],
        ),
    )
    with pytest.raises(typer.Exit) as exc:
        axi_mod.axi_storage_set_docker_log_rotation("proj", max_size="20m", max_file=2)
    out = capsys.readouterr().out
    assert exc.value.exit_code == 0 and "max_size: 20m" in out and "applies on recreate" in out


def test_prune_renders_select_bench_as_the_shared_usage_error(monkeypatch, capsys):
    monkeypatch.setattr(
        ls,
        "prune_bench_logs",
        lambda project, *, bench=None, consent=False: Result(
            status=Status.NEEDS_CHOICE,
            choice=Choice(
                kind="select_bench",
                param="bench",
                prompt="Which bench?",
                options=[
                    {"value": "0", "label": "/workspace/a"},
                    {"value": "1", "label": "/workspace/b"},
                ],
            ),
        ),
    )
    with pytest.raises(typer.Exit) as exc:
        axi_mod.axi_storage_prune_bench_logs("proj", bench=None, yes=True)
    out = capsys.readouterr().out
    assert exc.value.exit_code == 2 and "--bench" in out and "/workspace/b" in out


def test_prune_without_yes_is_a_usage_error_and_with_yes_removes(monkeypatch, capsys):
    calls = []

    def fake(project, *, bench=None, consent=False):
        calls.append((bench, consent))
        if not consent:
            return Result(
                status=Status.NEEDS_CHOICE,
                choice=Choice(
                    kind="confirm_prune_logs", param="consent", prompt="Remove 2 rotated?"
                ),
            )
        return Result(
            status=Status.OK,
            data={"project": project, "removed": ["x.log.1"], "freed_bytes": 3, "benches": ["/b"]},
        )

    monkeypatch.setattr(ls, "prune_bench_logs", fake)
    with pytest.raises(typer.Exit) as exc:
        axi_mod.axi_storage_prune_bench_logs("proj", bench="0", yes=False)
    assert exc.value.exit_code == 2 and "--yes" in capsys.readouterr().out
    with pytest.raises(typer.Exit) as exc:
        axi_mod.axi_storage_prune_bench_logs("proj", bench="0", yes=True)
    out = capsys.readouterr().out
    assert exc.value.exit_code == 0 and "x.log.1" in out
    assert calls == [("0", False), ("0", True)]
