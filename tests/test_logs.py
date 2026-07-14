"""``cwcli logs``: non-follow by default and no ``-it`` without a real TTY.

Two behaviors are pinned:

- **Default is non-follow.** ``logs`` used to default ``--follow`` on, so a plain
  ``cwcli logs proj`` blocked forever tailing (``tail -F``). The default is now
  off; ``-f`` / ``--follow`` opts in.
- **``-it`` is gated on ``sys.stdin.isatty()``.** ``docker exec -it`` errors "the
  input device is not a TTY" under a pipe/agent, so the ``-i``/``-t`` flags are
  only added when actually interactive.

The default/parsing assertions go through the real Typer parser (``CliRunner``);
the ``isatty`` gating is exercised by calling the command function directly, since
``CliRunner`` redirects ``sys.stdin`` and would defeat an ``isatty`` monkeypatch.
"""

from __future__ import annotations

import types

import typer
from typer.testing import CliRunner

from caffeinated_whale_cli.commands import logs as logs_mod
from caffeinated_whale_cli.utils import docker_utils

runner = CliRunner()


class _FakeFrappe:
    labels = {"com.docker.compose.service": "frappe"}
    name = "proj-frappe-1"


def _wire(monkeypatch, *, isatty: bool):
    """Neutralize the Docker preflight/collaborators and capture every subprocess.run."""
    # @handle_docker_errors preflight (no real Docker on the unit tier).
    monkeypatch.setattr(docker_utils.shutil, "which", lambda _n: "/usr/bin/docker")
    monkeypatch.setattr(
        docker_utils.docker, "from_env", lambda: type("C", (), {"ping": lambda s: True})()
    )
    monkeypatch.setattr(logs_mod, "ensure_containers_running", lambda *a, **k: True)
    monkeypatch.setattr(logs_mod, "get_project_containers", lambda name: [_FakeFrappe()])
    monkeypatch.setattr(logs_mod, "resolve_bench_path", lambda *a, **k: "/w/bench")
    monkeypatch.setattr(
        logs_mod.supervision, "bench_start_log_path", lambda p: f"{p}/logs/bench-start.log"
    )
    monkeypatch.setattr(logs_mod.sys, "stdin", types.SimpleNamespace(isatty=lambda: isatty))

    calls: list[list[str]] = []

    def fake_run(cmd, *a, **k):
        calls.append(cmd)
        # First call is the `test -f` pre-flight check (must "succeed"); the second
        # is the tail. Return an object with a returncode either way.
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(logs_mod.subprocess, "run", fake_run)
    return calls


def _tail_cmd(calls: list[list[str]]) -> list[str]:
    # The tail is the last subprocess.run; the first is the `test -f` pre-flight.
    return calls[-1]


def _app():
    app = typer.Typer()
    app.command()(logs_mod.logs)
    return app


def _call_logs(follow: bool):
    """Invoke the command function directly with every param explicit (Typer's
    ``Option`` defaults are left as objects otherwise; see the inspect tests)."""
    logs_mod.logs(
        project_name="proj",
        follow=follow,
        lines=100,
        bench=None,
        yes=False,
        verbose=False,
    )


def test_default_is_non_follow(monkeypatch):
    # Real Typer parsing: no --follow given -> the new default (False) -> `tail` (no -F).
    calls = _wire(monkeypatch, isatty=True)
    result = runner.invoke(_app(), ["proj"])
    assert result.exit_code == 0
    tail = _tail_cmd(calls)
    assert "-F" not in tail  # not following
    assert "tail" in tail


def test_follow_flag_opts_in(monkeypatch):
    calls = _wire(monkeypatch, isatty=True)
    result = runner.invoke(_app(), ["proj", "--follow"])
    assert result.exit_code == 0
    assert "-F" in _tail_cmd(calls)  # -f opted into following


def test_no_it_flag_without_tty(monkeypatch):
    # Non-TTY (piped/agent): `docker exec` must NOT carry -it.
    calls = _wire(monkeypatch, isatty=False)
    _call_logs(follow=False)
    assert "-it" not in _tail_cmd(calls)


def test_it_flag_added_with_tty(monkeypatch):
    # Interactive TTY: -it is present so tail -F stays attached.
    calls = _wire(monkeypatch, isatty=True)
    _call_logs(follow=True)
    tail = _tail_cmd(calls)
    assert "-it" in tail
    assert "-F" in tail
