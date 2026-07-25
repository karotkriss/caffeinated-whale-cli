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

import click
import pytest
import typer
from click.testing import CliRunner as ClickRunner
from typer.testing import CliRunner

from caffeinated_whale_cli.commands import restart as restart_mod
from caffeinated_whale_cli.commands import rm as rm_mod
from caffeinated_whale_cli.commands import start as start_mod
from caffeinated_whale_cli.commands import stop as stop_mod
from caffeinated_whale_cli.commands import utils as utils_mod
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


# --------------------------------------------- attached / clustered shorts


class TestShortOptionClusters:
    """``-pweb`` and ``-vy`` must work, and ``-yq`` must never become ``-y``.

    The splitter used to compare whole tokens against the option table, so every
    attached (``-pweb``) or clustered (``-vy``) short was refused as "No such
    option" - forms Click itself accepts, and forms these four commands are the
    only ones in cwcli that have to parse for themselves (they are Typer
    sub-apps, hence Click Groups, whose ``allow_interspersed_args=False`` dumps
    every token after the first project name into the variadic argument).

    The grammar has to know which shorts consume a value, and it has to refuse a
    cluster WHOLE. Resolving a cluster character by character and keeping what
    matched would let ``cwcli rm proj -yq`` - a typo, or ``-q`` borrowed from a
    sibling verb - synthesise the ``-y`` that skips rm's destructive
    confirmation. Refusal must land before a project is selected, so nothing is
    started, stopped, prompted for, backed up or deleted.
    """

    FLAGS = {
        "-v": ("verbose", True),
        "--verbose": ("verbose", True),
        "-y": ("yes", True),
        "--yes": ("yes", True),
    }
    VALUES = {"-p": "process", "--process": "process", "--bench": "bench"}

    def _split(self, tokens):
        return split_trailing_options(tokens, command="stop", flags=self.FLAGS, values=self.VALUES)

    # -- the positive half: the forms that must now work -------------------

    def test_an_attached_value_is_the_rest_of_the_token(self):
        _names, _flags, values = self._split(["proj", "-pweb"])

        assert values == {"process": "web"}

    def test_a_cluster_of_flags_sets_every_one_of_them(self):
        _names, flags, _values = self._split(["proj", "-vy"])

        assert flags == {"verbose": True, "yes": True}

    def test_a_cluster_may_end_in_a_value_option_carrying_its_value(self):
        names, flags, values = self._split(["proj", "-vpweb"])

        assert (names, flags, values) == (["proj"], {"verbose": True}, {"process": "web"})

    def test_a_cluster_ending_in_a_bare_value_option_takes_the_next_token(self):
        names, flags, values = self._split(["proj", "-vp", "web"])

        assert (names, flags, values) == (["proj"], {"verbose": True}, {"process": "web"})

    def test_a_value_option_stops_the_cluster_so_the_rest_is_its_value(self):
        # Click's rule: -pv is --process v, and "web" stays a project name. The
        # grammar must know -p consumes a value, not just that -v is a flag.
        names, _flags, values = self._split(["-pv", "web"])

        assert (names, values) == (["web"], {"process": "v"})

    def test_cluster_order_does_not_matter(self):
        assert self._split(["-vy"])[1] == self._split(["-yv"])[1]

    # -- the safety half: no -y may be synthesised -------------------------

    @pytest.mark.parametrize(
        "token",
        [
            "-yq",  # a typo after the consent flag
            "-qy",  # a typo before it
            "-y-",  # a malformed dash inside the cluster
            "-yy!",  # a stray character
            "-vyq",  # a longer cluster with one unknown
            "-yb",  # -b is a sibling verb's short, not defined here
            "-y1",  # a digit that is not an option
        ],
    )
    def test_a_cluster_holding_an_unknown_character_is_refused_whole(self, token):
        with pytest.raises(typer.Exit) as exc:
            self._split(["proj", token])

        assert exc.value.exit_code == 2

    def test_the_refused_cluster_yields_no_flags_at_all(self):
        # The load-bearing assertion: not merely "it exited 2", but that no
        # value ever comes back for a caller to apply. split_trailing_options
        # raises instead of returning, so there is no partial result carrying
        # the -y that the refused token happened to contain.
        recorded: list = []

        with pytest.raises(typer.Exit):
            recorded.append(self._split(["proj", "-yq"]))

        assert recorded == []

    def test_an_incomplete_cluster_missing_its_value_is_a_usage_error(self):
        with pytest.raises(typer.Exit) as exc:
            self._split(["proj", "-vp"])

        assert exc.value.exit_code == 2

    def test_a_cluster_may_not_borrow_the_next_option_as_its_value(self):
        with pytest.raises(typer.Exit) as exc:
            self._split(["proj", "-vp", "-y"])

        assert exc.value.exit_code == 2

    # -- what must NOT change ---------------------------------------------

    @pytest.mark.parametrize("value", ["-1", "-staging", "--weird-label"])
    def test_a_dash_leading_label_is_still_a_value_not_a_cluster(self, value):
        # None of these resolve as clusters, so the positional rule still wins
        # and the label survives. This is why an unknown cluster RETURNS rather
        # than raising: raising here would break every dash-leading bench label.
        _names, _flags, values = self._split(["proj", "--bench", value])

        assert values == {"bench": value}

    def test_a_valid_cluster_in_value_position_is_still_a_missing_value(self):
        # -vy became parseable, so it must now also be refused as a --bench value.
        with pytest.raises(typer.Exit) as exc:
            self._split(["proj", "--bench", "-vy"])

        assert exc.value.exit_code == 2

    def test_long_options_are_untouched_by_the_short_grammar(self):
        names, flags, values = self._split(["proj", "--verbose", "--bench=1", "--process", "web"])

        assert (names, flags, values) == (
            ["proj"],
            {"verbose": True},
            {"bench": "1", "process": "web"},
        )

    @pytest.mark.parametrize("token", ["--benhc", "--force", "-q", "--"])
    def test_unknown_options_are_still_refused(self, token):
        with pytest.raises(typer.Exit) as exc:
            self._split(["proj", token])

        assert exc.value.exit_code == 2

    def test_help_still_wins_inside_a_cluster(self, monkeypatch):
        # -h is eager standalone, so it stays eager clustered, as it is in Click.
        shown: list[bool] = []

        def _fake_help():
            shown.append(True)
            raise typer.Exit()

        monkeypatch.setattr(utils_mod, "_show_command_help", _fake_help)

        with pytest.raises(typer.Exit) as exc:
            self._split(["proj", "-vh"])

        assert shown == [True]
        assert exc.value.exit_code in (0, None)


class TestEveryCommandInBothModes:
    """One grammar, four commands, TTY and non-TTY alike.

    Each command owns its own flag/value table, so the accept-and-refuse pair is
    asserted per command rather than once on the splitter: a table that forgets a
    short would accept the cluster nowhere, and a command wired to a stale copy
    of the splitter would accept it everywhere. Both modes, because the refusal
    is what an agent driving cwcli non-interactively hits.
    """

    @pytest.mark.parametrize("tty", [True, False])
    def test_stop_takes_a_cluster_and_refuses_a_malformed_one(self, monkeypatch, tty):
        stopped = TestStop()._wire(monkeypatch, tty=tty)
        seen: list[tuple] = []
        monkeypatch.setattr(
            stop_mod, "_stop_benches", lambda names, bench, verbose: seen.append((names, bench))
        )

        stop_mod.stop(ctx=None, verbose=False, bench=None, project_name=["proj", "-v"])
        assert stopped == ["proj"]

        with pytest.raises(typer.Exit) as exc:
            stop_mod.stop(ctx=None, verbose=False, bench=None, project_name=["proj", "-vq"])

        assert exc.value.exit_code == 2
        assert stopped == ["proj"] and seen == []

    @pytest.mark.parametrize("tty", [True, False])
    def test_restart_takes_an_attached_process_and_refuses_a_malformed_cluster(
        self, monkeypatch, tty
    ):
        TestRestart()._wire(monkeypatch, tty=tty)
        seen: list[tuple] = []
        monkeypatch.setattr(
            restart_mod,
            "_restart_processes",
            lambda names, process, bench, verbose: seen.append((names, process)),
        )

        restart_mod.restart(
            ctx=None, verbose=False, process=None, bench=None, project_name=["proj", "-pweb"]
        )
        assert seen == [(["proj"], "web")]

        with pytest.raises(typer.Exit) as exc:
            restart_mod.restart(
                ctx=None, verbose=False, process=None, bench=None, project_name=["proj", "-vq"]
            )

        assert exc.value.exit_code == 2
        assert seen == [(["proj"], "web")]

    @pytest.mark.parametrize("tty", [True, False])
    def test_start_takes_a_cluster_and_refuses_a_malformed_one(self, monkeypatch, tty):
        started = TestStart()._wire(monkeypatch, tty=tty)

        start_mod.start(
            verbose=False, bench=None, yes=False, autorestart=True, project_name=["proj", "-vy"]
        )
        assert started == [("proj", None, True)]

        with pytest.raises(typer.Exit) as exc:
            start_mod.start(
                verbose=False,
                bench=None,
                yes=False,
                autorestart=True,
                project_name=["proj", "-vyq"],
            )

        assert exc.value.exit_code == 2
        assert started == [("proj", None, True)]

    @pytest.mark.parametrize("tty", [True, False])
    def test_rm_takes_a_cluster_and_refuses_a_malformed_one(self, monkeypatch, tty):
        removed = TestRm()._wire(monkeypatch, tty=tty)

        # -vy is a valid cluster, so it really does carry rm's consent through.
        rm_mod.rm(
            ctx=None,
            verbose=False,
            volumes=True,
            no_backup=True,
            yes=False,
            project_name=["proj", "-vy"],
        )
        assert [r["name"] for r in removed] == ["proj"]

        with pytest.raises(typer.Exit) as exc:
            rm_mod.rm(
                ctx=None,
                verbose=False,
                volumes=True,
                no_backup=True,
                yes=False,
                project_name=["proj", "-vyq"],
            )

        assert exc.value.exit_code == 2
        assert [r["name"] for r in removed] == ["proj"]


class TestGrammarMatchesClick:
    """The grammar is Click's, not an approximation of it.

    These four commands parse their own trailing options only because a Click
    Group stops parsing at the first positional. What they accept there should
    be what Click would have accepted had it kept going, so this compares the
    splitter against a real Click command carrying the same option set.
    """

    FLAGS = {"-v": ("verbose", True), "-y": ("yes", True)}
    VALUES = {"-p": "process"}

    @staticmethod
    def _click_reference(argv):
        """Click's own answer, in split_trailing_options' return shape."""
        captured: dict = {}

        @click.command()
        @click.option("-v", "--verbose", is_flag=True)
        @click.option("-y", "--yes", is_flag=True)
        @click.option("-p", "--process", default=None)
        @click.argument("names", nargs=-1)
        def cmd(verbose, yes, process, names):
            captured["names"] = list(names)
            captured["flags"] = {
                key: True for key, on in (("verbose", verbose), ("yes", yes)) if on
            }
            captured["values"] = {} if process is None else {"process": process}

        if ClickRunner().invoke(cmd, argv).exit_code != 0:
            return None
        return captured["names"], captured["flags"], captured["values"]

    @pytest.mark.parametrize(
        "argv",
        [
            ["-pweb"],
            ["-p=web"],
            ["-vy"],
            ["-yv"],
            ["-vp", "web"],
            ["-pv", "web"],
            ["-vpweb"],
            ["proj", "-pweb"],
        ],
    )
    def test_accepted_forms_parse_the_same_way_click_would(self, argv):
        names, flags, values = split_trailing_options(
            argv, command="stop", flags=self.FLAGS, values=self.VALUES
        )
        expected = self._click_reference(argv)

        assert (names, flags, values) == expected

    @pytest.mark.parametrize("argv", [["-yq"], ["-y-"], ["-q"], ["-p"], ["-vp"]])
    def test_forms_click_rejects_are_rejected_here_too(self, argv):
        assert self._click_reference(argv) is None

        with pytest.raises(typer.Exit) as exc:
            split_trailing_options(argv, command="stop", flags=self.FLAGS, values=self.VALUES)

        assert exc.value.exit_code == 2
