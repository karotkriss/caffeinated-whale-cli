"""An unrecognised trailing option is a usage error, never a project name.

``start``, ``stop``, ``restart`` and ``rm`` all take a VARIADIC project argument,
which greedily eats every token written after it - options included. Each command
recovered the flags it knows and then fell through to ``names.append(token)``, so
an option the command does NOT define became another project to act on:
``cwcli stop myproj --benhc 1`` stopped the WHOLE instance and exited 0. A typo,
or a flag that is valid on a sibling subcommand, silently retargeted a
destructive verb.

The trap in fixing it: rejecting on the leading dash alone breaks legitimate
values, because a cwcli bench label may start with one. The distinction is
POSITIONAL - the token following a value-taking option is that option's value
unless it is an option the command defines - so ``--bench -1`` still resolves the
label ``-1`` while ``--benhc`` is refused. Both halves are pinned here.

Each command asserts the POSITIVE first (a good invocation really does reach the
destructive call) so "nothing was acted on" cannot pass vacuously.
"""

from __future__ import annotations

import pytest
import typer
from typer.testing import CliRunner

from caffeinated_whale_cli.commands import restart as restart_mod
from caffeinated_whale_cli.commands import rm as rm_mod
from caffeinated_whale_cli.commands import start as start_mod
from caffeinated_whale_cli.commands import stop as stop_mod
from caffeinated_whale_cli.commands.utils import split_trailing_options
from caffeinated_whale_cli.core import rm as core_rm
from caffeinated_whale_cli.core import stop as core_stop
from caffeinated_whale_cli.utils import docker_utils


def _neutralize_docker(monkeypatch):
    """Defuse the @handle_docker_errors preflight so the command body runs."""
    monkeypatch.setattr(docker_utils.shutil, "which", lambda _n: "/usr/bin/docker")
    monkeypatch.setattr(
        docker_utils.docker, "from_env", lambda: type("C", (), {"ping": lambda s: True})()
    )


class _Stdin:
    """A stdin whose TTY-ness is explicit, and which yields piped names."""

    def __init__(self, *, tty: bool, lines: list[str] | None = None):
        self._tty = tty
        self._lines = lines or []

    def isatty(self) -> bool:
        return self._tty

    def __iter__(self):
        return iter(self._lines)


# ------------------------------------------------------------------ stop


class TestStop:
    def _wire(self, monkeypatch, *, tty=True, piped=None):
        _neutralize_docker(monkeypatch)
        monkeypatch.setattr(stop_mod.sys, "stdin", _Stdin(tty=tty, lines=piped))
        stopped: list[str] = []
        monkeypatch.setattr(
            core_stop,
            "stop",
            lambda name: stopped.append(name)
            or type("R", (), {"data": type("O", (), {"stopped": 1, "already_stopped": False})()})(),
        )
        return stopped

    def test_a_good_invocation_really_does_stop_the_instance(self, monkeypatch):
        stopped = self._wire(monkeypatch)

        stop_mod.stop(ctx=None, verbose=False, bench=None, project_name=["proj"])

        assert stopped == ["proj"]

    def test_an_unrecognised_option_is_refused_and_stops_nothing(self, monkeypatch):
        stopped = self._wire(monkeypatch)

        with pytest.raises(typer.Exit) as exc:
            stop_mod.stop(
                ctx=None, verbose=False, bench=None, project_name=["proj", "--benhc", "1"]
            )

        assert exc.value.exit_code == 2
        assert stopped == []

    @pytest.mark.parametrize("help_option", ["-h", "--help"])
    def test_trailing_help_shows_stop_help_and_stops_nothing(
        self, monkeypatch, help_option
    ):
        stopped = self._wire(monkeypatch)

        result = CliRunner().invoke(stop_mod.app, ["proj", help_option])

        assert result.exit_code == 0
        assert "Stop a Frappe project's containers." in result.output
        assert stopped == []

    def test_the_refusal_names_the_option_and_says_nothing_changed(self, monkeypatch, capsys):
        self._wire(monkeypatch)

        with pytest.raises(typer.Exit):
            stop_mod.stop(ctx=None, verbose=False, bench=None, project_name=["proj", "--benhc"])

        err = capsys.readouterr().err
        assert "--benhc" in err
        assert "Nothing was changed" in err

    def test_the_refusal_holds_non_interactively_before_stdin_is_read(self, monkeypatch):
        # An agent piping names must not have the unknown flag rescued by them.
        stopped = self._wire(monkeypatch, tty=False, piped=["other\n"])

        with pytest.raises(typer.Exit) as exc:
            stop_mod.stop(ctx=None, verbose=False, bench=None, project_name=["proj", "--force"])

        assert exc.value.exit_code == 2
        assert stopped == []

    def test_a_dash_leading_bench_label_still_selects_that_bench(self, monkeypatch):
        self._wire(monkeypatch)
        seen: list[tuple] = []
        monkeypatch.setattr(
            stop_mod, "_stop_benches", lambda names, bench, verbose: seen.append((names, bench))
        )

        stop_mod.stop(
            ctx=None, verbose=False, bench=None, project_name=["proj", "--bench", "-staging"]
        )

        assert seen == [(["proj"], "-staging")]

    def test_a_dash_leading_bench_label_survives_the_inline_form(self, monkeypatch):
        self._wire(monkeypatch)
        seen: list[tuple] = []
        monkeypatch.setattr(
            stop_mod, "_stop_benches", lambda names, bench, verbose: seen.append((names, bench))
        )

        stop_mod.stop(ctx=None, verbose=False, bench=None, project_name=["proj", "--bench=-1"])

        assert seen == [(["proj"], "-1")]

    def test_a_recognised_trailing_flag_is_recovered_not_treated_as_a_name(self, monkeypatch):
        stopped = self._wire(monkeypatch)

        stop_mod.stop(ctx=None, verbose=False, bench=None, project_name=["proj", "-v"])

        assert stopped == ["proj"]


# ------------------------------------------------------------------ restart


class TestRestart:
    def _wire(self, monkeypatch, *, tty=True):
        _neutralize_docker(monkeypatch)
        monkeypatch.setattr(restart_mod.sys, "stdin", _Stdin(tty=tty))
        restarted: list[str] = []
        monkeypatch.setattr(
            restart_mod, "_restart_project", lambda name, **kw: restarted.append(name) or (None, 1)
        )
        return restarted

    def test_a_good_invocation_really_does_restart_the_instance(self, monkeypatch):
        restarted = self._wire(monkeypatch)

        restart_mod.restart(
            ctx=None, verbose=False, process=None, bench=None, project_name=["proj"]
        )

        assert restarted == ["proj"]

    def test_an_unrecognised_option_is_refused_and_restarts_nothing(self, monkeypatch):
        restarted = self._wire(monkeypatch)

        with pytest.raises(typer.Exit) as exc:
            restart_mod.restart(
                ctx=None, verbose=False, process=None, bench=None, project_name=["proj", "--typo"]
            )

        assert exc.value.exit_code == 2
        assert restarted == []

    @pytest.mark.parametrize("help_option", ["-h", "--help"])
    def test_trailing_help_shows_restart_help_and_restarts_nothing(
        self, monkeypatch, help_option
    ):
        restarted = self._wire(monkeypatch)

        result = CliRunner().invoke(restart_mod.app, ["proj", help_option])

        assert result.exit_code == 0
        assert "Restart a Frappe project's containers." in result.output
        assert restarted == []

    def test_a_dash_leading_process_label_is_still_a_value(self, monkeypatch):
        self._wire(monkeypatch)
        seen: list[tuple] = []
        monkeypatch.setattr(
            restart_mod,
            "_restart_processes",
            lambda names, process, bench, verbose: seen.append((names, process, bench)),
        )

        restart_mod.restart(
            ctx=None,
            verbose=False,
            process=None,
            bench=None,
            project_name=["proj", "-p", "-web", "--bench", "-staging"],
        )

        assert seen == [(["proj"], "-web", "-staging")]


# ------------------------------------------------------------------ start


class TestStart:
    def _wire(self, monkeypatch, *, tty=True):
        _neutralize_docker(monkeypatch)
        monkeypatch.setattr(start_mod.sys, "stdin", _Stdin(tty=tty))
        monkeypatch.setattr(start_mod, "_frappe_running", lambda name: True)
        started: list[tuple] = []

        def fake_run_start(name, bench, verbose, autorestart):
            started.append((name, bench, autorestart))
            return None

        monkeypatch.setattr(start_mod, "_run_start", fake_run_start)
        return started

    def test_a_good_invocation_really_does_start_the_instance(self, monkeypatch):
        started = self._wire(monkeypatch)

        start_mod.start(
            verbose=False, bench=None, yes=False, autorestart=True, project_name=["proj"]
        )

        assert started == [("proj", None, True)]

    def test_an_unrecognised_option_is_refused_and_starts_nothing(self, monkeypatch):
        started = self._wire(monkeypatch)

        with pytest.raises(typer.Exit) as exc:
            start_mod.start(
                verbose=False,
                bench=None,
                yes=False,
                autorestart=True,
                project_name=["proj", "--benhc", "1"],
            )

        assert exc.value.exit_code == 2
        assert started == []

    @pytest.mark.parametrize("help_option", ["-h", "--help"])
    def test_trailing_help_shows_start_help_and_starts_nothing(
        self, monkeypatch, help_option
    ):
        started = self._wire(monkeypatch)

        result = CliRunner().invoke(start_mod.app, ["proj", help_option])

        assert result.exit_code == 0
        assert "Start a Frappe project's containers." in result.output
        assert started == []

    def test_recognised_trailing_flags_and_a_dash_leading_label_still_work(self, monkeypatch):
        started = self._wire(monkeypatch)

        start_mod.start(
            verbose=False,
            bench=None,
            yes=False,
            autorestart=True,
            project_name=["proj", "--no-autorestart", "--bench", "-staging"],
        )

        assert started == [("proj", "-staging", False)]

    def test_a_bench_selector_with_no_value_is_a_usage_error(self, monkeypatch):
        # Previously silently dropped, leaving start to pick a bench of its own.
        started = self._wire(monkeypatch)

        with pytest.raises(typer.Exit) as exc:
            start_mod.start(
                verbose=False,
                bench=None,
                yes=False,
                autorestart=True,
                project_name=["proj", "--bench"],
            )

        assert exc.value.exit_code == 2
        assert started == []


# ------------------------------------------------------------------ rm


class TestRm:
    def _wire(self, monkeypatch, *, tty=True):
        _neutralize_docker(monkeypatch)
        monkeypatch.setattr(rm_mod.sys, "stdin", _Stdin(tty=tty))
        monkeypatch.setattr(rm_mod, "_frappe_container_running", lambda name: False)
        monkeypatch.setattr(core_rm, "is_valid_project_name", lambda name: True)
        removed: list[dict] = []

        def fake_remove(name, **kwargs):
            removed.append({"name": name, **kwargs})
            return type("R", (), {"data": _rm_outcome()})()

        monkeypatch.setattr(core_rm, "remove", fake_remove)
        return removed

    def test_a_good_invocation_really_does_remove_the_instance(self, monkeypatch):
        removed = self._wire(monkeypatch)

        rm_mod.rm(
            ctx=None,
            verbose=False,
            volumes=True,
            no_backup=True,
            yes=True,
            project_name=["proj"],
        )

        assert [r["name"] for r in removed] == ["proj"]

    def test_an_unrecognised_option_is_refused_and_removes_nothing(self, monkeypatch):
        removed = self._wire(monkeypatch)

        with pytest.raises(typer.Exit) as exc:
            rm_mod.rm(
                ctx=None,
                verbose=False,
                volumes=True,
                no_backup=True,
                yes=True,
                project_name=["proj", "--benhc", "1"],
            )

        assert exc.value.exit_code == 2
        assert removed == []

    @pytest.mark.parametrize("help_option", ["-h", "--help"])
    def test_trailing_help_shows_rm_help_and_removes_nothing(
        self, monkeypatch, help_option
    ):
        removed = self._wire(monkeypatch)

        result = CliRunner().invoke(rm_mod.app, ["proj", help_option])

        assert result.exit_code == 0
        assert "Remove a Frappe project and its containers." in result.output
        assert removed == []

    def test_the_refusal_holds_non_interactively(self, monkeypatch):
        removed = self._wire(monkeypatch, tty=False)

        with pytest.raises(typer.Exit) as exc:
            rm_mod.rm(
                ctx=None,
                verbose=False,
                volumes=True,
                no_backup=True,
                yes=True,
                project_name=["proj", "--force"],
            )

        assert exc.value.exit_code == 2
        assert removed == []

    def test_recognised_trailing_flags_are_still_recovered(self):
        names, verbose, yes, no_backup, volumes = rm_mod._recover_trailing_flags(
            ["proj", "--yes", "--no-volumes", "-v"], False, False, False, True
        )

        assert (names, verbose, yes, no_backup, volumes) == (["proj"], True, True, False, False)

    def test_those_recovered_flags_still_reach_the_core(self, monkeypatch):
        removed = self._wire(monkeypatch)

        rm_mod.rm(
            ctx=None,
            verbose=False,
            volumes=True,
            no_backup=False,
            yes=False,
            project_name=["proj", "--no-volumes", "--no-backup", "--yes"],
        )

        assert removed[0]["remove_volumes"] is False
        assert removed[0]["no_backup"] is True


def _rm_outcome():
    class _Outcome:
        found = True
        orphan = False
        containers_removed = 1
        volumes_removed = ()
        volumes_failed = ()
        dir_removed = True
        dir_archive = None
        cache_cleared = True
        network_removed = True
        backup_ok = True
        backup_dir = None
        failures: tuple = ()

    return _Outcome()


# ------------------------------------------------------- the splitter itself


class TestSplitTrailingOptions:
    FLAGS = {"-v": ("verbose", True), "--no-x": ("x", False), "--x": ("x", True)}
    VALUES = {"--bench": "bench", "-p": "process"}

    def _split(self, tokens):
        return split_trailing_options(tokens, command="stop", flags=self.FLAGS, values=self.VALUES)

    def test_names_and_options_are_separated_in_any_order(self):
        names, flags, values = self._split(["a", "-v", "b", "--bench", "1", "c"])

        assert names == ["a", "b", "c"]
        assert flags == {"verbose": True}
        assert values == {"bench": "1"}

    def test_two_spellings_of_one_destination_are_last_one_wins(self):
        _names, flags, _values = self._split(["--x", "--no-x"])

        assert flags == {"x": False}

    def test_nothing_recovered_leaves_the_dict_empty_so_a_parsed_value_survives(self):
        names, flags, values = self._split(["a"])

        assert (names, flags, values) == (["a"], {}, {})

    @pytest.mark.parametrize("value", ["-1", "-staging", "--weird-label"])
    def test_any_dash_leading_value_is_taken_as_the_value(self, value):
        _names, _flags, values = self._split(["--bench", value])

        assert values == {"bench": value}

    @pytest.mark.parametrize("tokens", [["--bench"], ["--bench", "-v"], ["--bench="]])
    def test_a_missing_value_is_a_usage_error(self, tokens):
        with pytest.raises(typer.Exit) as exc:
            self._split(tokens)

        assert exc.value.exit_code == 2

    @pytest.mark.parametrize("token", ["--benhc", "--force", "-q", "--bench-name"])
    def test_an_unrecognised_option_is_a_usage_error(self, token):
        with pytest.raises(typer.Exit) as exc:
            self._split(["proj", token])

        assert exc.value.exit_code == 2

    def test_a_bare_dash_stays_a_name_because_it_is_not_option_syntax(self):
        names, _flags, _values = self._split(["-"])

        assert names == ["-"]
