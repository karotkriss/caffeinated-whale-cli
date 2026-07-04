"""Tests for the ``--yes`` / ``-y`` contract across the confirmation commands.

Mirrors the established restore/rm behavior:
  - ``--yes`` proceeds without prompting,
  - a non-TTY WITHOUT ``--yes`` refuses a destructive op and exits non-zero,
  - an interactive decline exits non-zero.

Covers the shared ``confirm_or_exit`` helper, ``config cache clear --all``,
``start``'s port-conflict auto-confirm, and ``ensure_containers_running``'s
auto-start path (which run/backup/update/open/unlock/inspect thread ``--yes`` into).
"""

import pytest
import typer

from caffeinated_whale_cli.commands import config as config_mod
from caffeinated_whale_cli.commands import inspect as inspect_mod
from caffeinated_whale_cli.commands import start as start_mod
from caffeinated_whale_cli.commands import utils as cmd_utils
from caffeinated_whale_cli.utils import docker_utils


class _Answer:
    def __init__(self, value):
        self.value = value

    def ask(self):
        return self.value


def _set_tty(monkeypatch, is_tty):
    class _Stdin:
        def isatty(self):
            return is_tty

    monkeypatch.setattr(cmd_utils.sys, "stdin", _Stdin())


# ------------------------------------------------------------- confirm_or_exit


class TestConfirmOrExit:
    def test_assume_yes_proceeds(self, monkeypatch):
        # Never even consults the TTY / prompt.
        def _boom(*a, **k):
            raise AssertionError("must not prompt under --yes")

        monkeypatch.setattr(cmd_utils.questionary, "confirm", _boom)
        # returns None (no exception) == proceed
        assert cmd_utils.confirm_or_exit("ok?", assume_yes=True, refuse_message="no") is None

    def test_non_tty_without_yes_refuses(self, monkeypatch):
        _set_tty(monkeypatch, False)
        monkeypatch.setattr(
            cmd_utils.questionary,
            "confirm",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not prompt")),
        )
        with pytest.raises(typer.Exit) as exc:
            cmd_utils.confirm_or_exit("ok?", assume_yes=False, refuse_message="refused")
        assert exc.value.exit_code == 1

    def test_tty_decline_exits_nonzero(self, monkeypatch):
        _set_tty(monkeypatch, True)
        monkeypatch.setattr(cmd_utils.questionary, "confirm", lambda *a, **k: _Answer(False))
        with pytest.raises(typer.Exit) as exc:
            cmd_utils.confirm_or_exit("ok?", assume_yes=False, refuse_message="refused")
        assert exc.value.exit_code == 1

    def test_tty_accept_proceeds(self, monkeypatch):
        _set_tty(monkeypatch, True)
        monkeypatch.setattr(cmd_utils.questionary, "confirm", lambda *a, **k: _Answer(True))
        assert cmd_utils.confirm_or_exit("ok?", assume_yes=False, refuse_message="refused") is None


# ---------------------------------------------------------- config cache clear


class TestConfigCacheClearYes:
    def test_yes_clears_without_prompt(self, monkeypatch):
        cleared = []
        monkeypatch.setattr(config_mod.db_utils, "clear_all_cache", lambda: cleared.append(True))
        monkeypatch.setattr(
            cmd_utils.questionary,
            "confirm",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not prompt")),
        )
        config_mod.clear_cache(project_name=None, all=True, yes=True)
        assert cleared == [True]

    def test_non_tty_without_yes_refuses_and_preserves_cache(self, monkeypatch):
        _set_tty(monkeypatch, False)
        cleared = []
        monkeypatch.setattr(config_mod.db_utils, "clear_all_cache", lambda: cleared.append(True))
        with pytest.raises(typer.Exit) as exc:
            config_mod.clear_cache(project_name=None, all=True, yes=False)
        assert exc.value.exit_code == 1
        assert cleared == []  # cache NOT wiped


# --------------------------------------------------------------- start --yes


class TestStartPortConflictYes:
    def _wire_conflict(self, monkeypatch):
        """One frappe project holds the port; freed after it is stopped."""
        state = {"checks": 0}

        monkeypatch.setattr(start_mod, "get_project_ports", lambda name: [8000])
        monkeypatch.setattr(
            start_mod,
            "find_project_using_ports",
            lambda ports, exclude_project=None: {8000: "other"},
        )

        def check_ports(ports, verbose=False):
            state["checks"] += 1
            # In use on the first check, free after the conflicting project stops.
            in_use = state["checks"] == 1
            return {p: in_use for p in ports}

        monkeypatch.setattr(start_mod, "check_ports_in_use", check_ports)
        stopped = []
        # _stop_project is imported inside the function from .stop
        from caffeinated_whale_cli.commands import stop as stop_mod

        monkeypatch.setattr(
            stop_mod, "_stop_project", lambda name, verbose=False: stopped.append(name)
        )
        return stopped

    def test_yes_auto_stops_conflicts_without_prompt(self, monkeypatch):
        stopped = self._wire_conflict(monkeypatch)
        monkeypatch.setattr(
            start_mod.questionary,
            "confirm",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not prompt under --yes")),
        )
        assert start_mod._check_port_conflicts("proj", verbose=False, assume_yes=True) is True
        assert stopped == ["other"]

    def test_without_yes_prompts(self, monkeypatch):
        stopped = self._wire_conflict(monkeypatch)
        asked = []

        def confirm(*a, **k):
            asked.append(True)
            return _Answer(True)

        monkeypatch.setattr(start_mod.questionary, "confirm", confirm)
        assert start_mod._check_port_conflicts("proj", verbose=False, assume_yes=False) is True
        assert asked == [True]  # the prompt DID fire without --yes
        assert stopped == ["other"]


# ------------------------------------------------ ensure_containers_running(auto_start)


class _StoppedContainer:
    status = "exited"

    def reload(self):
        pass


class TestEnsureContainersAutoStart:
    def test_auto_start_starts_without_prompt(self, monkeypatch):
        monkeypatch.setattr(cmd_utils, "get_frappe_container", lambda name: _StoppedContainer())
        started = []
        monkeypatch.setattr(
            cmd_utils,
            "_start_containers_for_command",
            lambda name, verbose=False: started.append(name),
        )
        monkeypatch.setattr(
            cmd_utils.questionary,
            "confirm",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("must not prompt under auto_start")
            ),
        )
        result = cmd_utils.ensure_containers_running("proj", require_running=True, auto_start=True)
        assert result is True
        assert started == ["proj"]


# ------------------------------------------------ start's inspect-fallback exit


class _RunningFrappe:
    status = "running"
    labels = {"com.docker.compose.service": "frappe"}
    name = "proj-frappe-1"

    def start(self):  # pragma: no cover - already running
        pass


class TestStartInspectExitPropagates:
    """`_start_project`'s inspect fallback must re-raise ``typer.Exit`` (e.g. a
    multi-bench ambiguity) instead of swallowing it in the broad ``except`` -
    mirroring the guard already in ``open``/``update``."""

    def _wire(self, monkeypatch):
        # Neutralize the @handle_docker_errors preflight so the body runs.
        monkeypatch.setattr(docker_utils.shutil, "which", lambda _n: "/usr/bin/docker")
        monkeypatch.setattr(
            docker_utils.docker, "from_env", lambda: type("C", (), {"ping": lambda s: True})()
        )
        monkeypatch.setattr(start_mod, "get_project_containers", lambda name: [_RunningFrappe()])
        # No cached bench -> the inspect fallback branch runs.
        monkeypatch.setattr(cmd_utils, "resolve_bench_path", lambda *a, **k: None)

    def test_inspect_exit_propagates(self, monkeypatch):
        self._wire(monkeypatch)

        def _boom(**kwargs):
            raise typer.Exit(code=1)

        monkeypatch.setattr(inspect_mod, "inspect", _boom)
        with pytest.raises(typer.Exit) as exc:
            start_mod._start_project("proj", verbose=False, status=None, bench_selector=None)
        assert exc.value.exit_code == 1

    def test_generic_inspect_error_is_still_swallowed(self, monkeypatch):
        # Control: a NON-Exit failure from inspect stays swallowed (degrade to the
        # "could not detect bench path" warning + return), so the guard is scoped.
        self._wire(monkeypatch)

        def _oops(**kwargs):
            raise ValueError("inspect blew up")

        monkeypatch.setattr(inspect_mod, "inspect", _oops)
        # Must not raise; returns None after warning that bench start was skipped.
        assert (
            start_mod._start_project("proj", verbose=False, status=None, bench_selector=None)
            is None
        )


# ------------------------------------------------ start multi-project loop


class TestStartMultiProjectLoop:
    """A ``typer.Exit`` from ``_start_project`` must not abort sibling projects in
    a ``cwcli start a b c`` run: a non-zero exit (e.g. a multi-bench ambiguity or
    a no-bench inspect failure) skips just that project, while an exit code 0
    (user cancel / Ctrl-C) aborts the whole run."""

    def _wire(self, monkeypatch):
        # Non-interactive stdin so the loop does not try to read piped names.
        class _Stdin:
            def isatty(self):
                return True

        monkeypatch.setattr(start_mod.sys, "stdin", _Stdin())
        # No port conflicts for any project.
        monkeypatch.setattr(start_mod, "_check_port_conflicts", lambda *a, **k: True)

    def test_failing_project_is_skipped_others_continue(self, monkeypatch, capsys):
        self._wire(monkeypatch)
        processed = []

        def fake_start(name, verbose=False, status=None, bench_selector=None):
            processed.append(name)
            if name == "b":
                raise typer.Exit(code=1)
            return None

        monkeypatch.setattr(start_mod, "_start_project", fake_start)
        # Must NOT raise: 'b' is skipped, but 'a' and 'c' are still processed.
        start_mod.start(verbose=False, bench=None, yes=False, project_name=["a", "b", "c"])
        assert processed == ["a", "b", "c"]
        out = capsys.readouterr().out
        assert "Instance 'a' started." in out
        assert "Instance 'c' started." in out
        # The skipped project does NOT get the "started" line.
        assert "Instance 'b' started." not in out
        assert "Skipping project 'b'" in out

    def test_exit_zero_aborts_whole_run(self, monkeypatch):
        self._wire(monkeypatch)
        processed = []

        def fake_start(name, verbose=False, status=None, bench_selector=None):
            processed.append(name)
            if name == "b":
                raise typer.Exit(code=0)
            return None

        monkeypatch.setattr(start_mod, "_start_project", fake_start)
        with pytest.raises(typer.Exit) as exc:
            start_mod.start(verbose=False, bench=None, yes=False, project_name=["a", "b", "c"])
        assert exc.value.exit_code == 0
        # Aborted at 'b'; 'c' is never reached.
        assert processed == ["a", "b"]
