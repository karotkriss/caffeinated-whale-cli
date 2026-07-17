"""``cwcli axi logs`` - the bounded log-read verb the agent surface was missing.

``cwcli logs`` is a ``docker exec -it ... tail -F`` TTY passthrough that streams
forever; an ``axi`` verb must emit ONE terminal TOON document and exit. So this verb
runs over the NEW bounded ``core.read_logs`` (``tail -n N``, no follow), and these pin
what it must get right: one TOON document (a metadata head + one raw-line block per
process, log lines never re-parsed as key:value), the exit-code mapping (stopped ->
usage exit 2 naming ``cwcli start``; multi-bench / unknown ``--process`` -> usage exit
2 naming the flag; running-but-quiet -> empty success exit 0; no-manager -> exit 1),
and that the verb is registered (closing the ``logs`` half of the deferral guards).
"""

from __future__ import annotations

import pytest
import typer

from caffeinated_whale_cli.commands import axi as axi_mod
from caffeinated_whale_cli.core.envelope import Choice, Message, Result, Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from caffeinated_whale_cli.core.logs import LogsRead, ProcessLog

from .test_axi import assert_is_one_toon_document


def _read(logs, *, not_cwcli_supervised=False, lines=100, warnings=()):
    return Result(
        status=Status.OK,
        data=LogsRead(
            project="proj",
            container_name="proj-frappe-1",
            bench_path="/workspace/frappe-bench",
            lines_requested=lines,
            not_cwcli_supervised=not_cwcli_supervised,
            logs=list(logs),
        ),
        warnings=list(warnings),
    )


def _patch(monkeypatch, result=None, *, raises=None):
    def fake(project, *, bench=None, lines=100, process=None):
        if raises is not None:
            raise raises
        return result

    monkeypatch.setattr(axi_mod.core_logs, "read_logs", fake)


def _run(capsys, **kwargs):
    kwargs.setdefault("lines", 100)
    kwargs.setdefault("bench", None)
    kwargs.setdefault("process", None)
    with pytest.raises(typer.Exit) as exc:
        axi_mod.axi_logs("proj", **kwargs)
    return exc.value.exit_code, capsys.readouterr().out


# ------------------------------------------------------------------ OK: one document


def test_emits_metadata_head_and_per_process_blocks(monkeypatch, capsys):
    _patch(
        monkeypatch,
        _read(
            [
                ProcessLog(
                    process="web",
                    file="/workspace/frappe-bench/logs/web.supervisor.log",
                    lines=["web line 1", "web line 2"],
                ),
                ProcessLog(
                    process="worker_default",
                    file="/workspace/frappe-bench/logs/worker_default.supervisor.log",
                    lines=["worker line 1"],
                ),
            ]
        ),
    )
    code, out = _run(capsys)
    assert code == 0
    assert "project: proj" in out
    assert "bench_path: /workspace/frappe-bench" in out
    assert "container: proj-frappe-1" in out
    assert "not_cwcli_supervised: false" in out
    assert "lines_requested: 100" in out
    # one counted block per process, raw lines under it
    assert "web[2]:" in out
    assert "  web line 1" in out
    assert "  web line 2" in out
    assert "worker_default[1]:" in out
    assert "  worker line 1" in out
    assert_is_one_toon_document(out)


def test_single_process_emits_one_block(monkeypatch, capsys):
    _patch(
        monkeypatch,
        _read(
            [
                ProcessLog(
                    process="web",
                    file="/workspace/frappe-bench/logs/web.supervisor.log",
                    lines=["only web"],
                )
            ]
        ),
    )
    code, out = _run(capsys, process="web")
    assert code == 0
    assert "web[1]:" in out
    assert "worker" not in out
    assert_is_one_toon_document(out)


def test_stdout_is_pure_toon_even_with_special_chars_in_lines(monkeypatch, capsys):
    # A log line carrying `:` `,` `"` is emitted raw under the block and must not
    # corrupt the document - block rows are never re-parsed as key:value.
    line = 'ERROR: {"key": "value", "n": 42} -> boom'
    _patch(
        monkeypatch,
        _read(
            [
                ProcessLog(
                    process="web",
                    file="/workspace/frappe-bench/logs/web.supervisor.log",
                    lines=[line],
                )
            ]
        ),
    )
    code, out = _run(capsys)
    assert code == 0
    assert f"  {line}" in out  # verbatim, under the block
    assert "web[1]:" in out


def test_not_cwcli_supervised_is_reported_in_the_document(monkeypatch, capsys):
    _patch(
        monkeypatch,
        _read(
            [ProcessLog(process="web", file="/w/logs/web.log", lines=["x"])],
            not_cwcli_supervised=True,
        ),
    )
    code, out = _run(capsys)
    assert code == 0
    assert "not_cwcli_supervised: true" in out


# ------------------------------------------------------------- running-but-quiet


def test_running_but_quiet_is_empty_success(monkeypatch, capsys):
    _patch(
        monkeypatch,
        _read([], warnings=[Message("logs.none_yet", "No logs written yet under '/w/logs'.")]),
    )
    code, out = _run(capsys)
    assert code == 0  # a successful empty read, not an error
    assert "logs: 0 log lines" in out
    assert "warnings[1]:" in out
    assert_is_one_toon_document(out)


# --------------------------------------------------------------- usage errors (2)


def test_stopped_container_is_usage_error_naming_start(monkeypatch, capsys):
    _patch(
        monkeypatch,
        Result(
            status=Status.NEEDS_CHOICE,
            choice=Choice(
                kind="confirm_start",
                param="project",
                prompt="the project's Frappe container is not running",
                options=[],
            ),
        ),
    )
    code, out = _run(capsys)
    assert code == 2
    assert "error:" in out
    assert "cwcli start" in out


def test_multi_bench_without_selector_is_usage_error_naming_bench(monkeypatch, capsys):
    _patch(
        monkeypatch,
        Result(
            status=Status.NEEDS_CHOICE,
            choice=Choice(
                kind="select_bench",
                param="bench",
                prompt="multiple benches",
                options=[{"value": "0", "label": "bench-a"}, {"value": "1", "label": "bench-b"}],
            ),
        ),
    )
    code, out = _run(capsys)
    assert code == 2
    assert "--bench" in out


def test_unknown_process_is_usage_error_naming_process(monkeypatch, capsys):
    _patch(
        monkeypatch,
        Result(
            status=Status.NEEDS_CHOICE,
            choice=Choice(
                kind="select_process",
                param="process",
                prompt="No process 'nope'",
                options=[{"value": "web", "label": "web"}],
            ),
        ),
    )
    code, out = _run(capsys, process="nope")
    assert code == 2
    assert "--process" in out


# ------------------------------------------------------------- operational errors


def test_no_manager_is_operational_error_exit_1(monkeypatch, capsys):
    _patch(
        monkeypatch,
        raises=CwcliError(
            ErrorKind.NOT_RUNNING,
            "logs.no_manager",
            "No process logs found under '/w/logs'.",
            hint="The bench may not be running. Start it with: cwcli start proj",
        ),
    )
    code, out = _run(capsys)
    assert code == 1
    assert "error:" in out
    assert "cwcli start proj" in out


def test_docker_error_exit_1(monkeypatch, capsys):
    _patch(
        monkeypatch,
        raises=CwcliError(ErrorKind.DOCKER, "docker.down", "Docker daemon is not running."),
    )
    code, out = _run(capsys)
    assert code == 1
    assert "error:" in out


# ---------------------------------------------------------------------- the guard


def test_the_logs_verb_is_registered():
    """The presence assertion that closes the ``logs`` half of the deferral guards.

    ``axi logs`` was deferred by prose with no test; building it flips that to a
    shipped verb, so adding/removing it is a deliberate act that updates this test.
    """
    registered = {c.name for c in axi_mod.app.registered_commands}
    assert "logs" in registered


def test_the_verb_takes_no_follow_or_yes():
    """No ``--follow`` (a follow cannot terminate into one document) and no ``--yes``
    (no auto-start on the agent surface) - the two flags this verb deliberately omits."""
    import inspect

    params = inspect.signature(axi_mod.axi_logs).parameters
    assert "follow" not in params
    assert "yes" not in params
    assert {"project", "lines", "bench", "process"} <= set(params)
