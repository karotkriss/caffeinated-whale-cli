"""Honest exit codes and non-TTY safety across the commands.

Pins the contract hardened in the command sweep:
  - ``ensure_containers_running`` refuses (Exit 1) on a non-TTY without auto_start
    instead of hanging on questionary or crashing on EOF, and an interactive
    decline exits 1 - while the ``prompt=False`` and ``auto_start=True`` paths keep
    their load-bearing silent behavior.
  - ``start``/``stop``/``restart`` exit 1 on a nonexistent project (and never print
    "started"/"stopped" for it), processing the rest of a multi-project run first.
  - ``config`` error paths exit 1, with the deliberate idempotent-success carve-out
    for "stop when not running".
  - ``update`` folds a maintenance-mode-disable failure into its error exit and
    names the stuck site.
"""

import pytest
import typer

from caffeinated_whale_cli.commands import config as config_mod
from caffeinated_whale_cli.commands import restart as restart_mod
from caffeinated_whale_cli.commands import start as start_mod
from caffeinated_whale_cli.commands import stop as stop_mod
from caffeinated_whale_cli.commands import update as update_mod
from caffeinated_whale_cli.commands import utils as cmd_utils
from caffeinated_whale_cli.core import stop as core_stop
from caffeinated_whale_cli.core import update as core_update
from caffeinated_whale_cli.utils import docker_utils


def _neutralize_docker(monkeypatch):
    """Defuse the @handle_docker_errors preflight so the command body runs."""
    monkeypatch.setattr(docker_utils.shutil, "which", lambda _n: "/usr/bin/docker")
    monkeypatch.setattr(
        docker_utils.docker, "from_env", lambda: type("C", (), {"ping": lambda s: True})()
    )


class _StoppedFrappe:
    status = "exited"
    labels = {"com.docker.compose.service": "frappe"}

    def reload(self):
        pass


# ------------------------------------------------- ensure_containers_running


class TestEnsureContainersRunning:
    def _wire_stopped(self, monkeypatch):
        monkeypatch.setattr(cmd_utils, "get_frappe_container", lambda name: _StoppedFrappe())

    def test_non_tty_refuses_without_prompting(self, monkeypatch):
        self._wire_stopped(monkeypatch)

        class _Stdin:
            def isatty(self):
                return False

        monkeypatch.setattr(cmd_utils.sys, "stdin", _Stdin())
        # questionary must NEVER be reached on a non-TTY.
        monkeypatch.setattr(
            cmd_utils.questionary,
            "confirm",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not prompt on non-TTY")),
        )
        with pytest.raises(typer.Exit) as exc:
            cmd_utils.ensure_containers_running("proj", require_running=True)
        assert exc.value.exit_code == 1

    def test_interactive_decline_exits_one(self, monkeypatch):
        self._wire_stopped(monkeypatch)

        class _Stdin:
            def isatty(self):
                return True

        monkeypatch.setattr(cmd_utils.sys, "stdin", _Stdin())

        class _Answer:
            def ask(self):
                return False

        monkeypatch.setattr(cmd_utils.questionary, "confirm", lambda *a, **k: _Answer())
        with pytest.raises(typer.Exit) as exc:
            cmd_utils.ensure_containers_running("proj", require_running=True)
        assert exc.value.exit_code == 1

    def test_prompt_false_returns_false_silently(self, monkeypatch):
        self._wire_stopped(monkeypatch)
        monkeypatch.setattr(
            cmd_utils.questionary,
            "confirm",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not prompt")),
        )
        # No TTY check, no prompt, no Exit - just a quiet False (inspect T2 / rm recache).
        assert (
            cmd_utils.ensure_containers_running("proj", require_running=True, prompt=False) is False
        )

    def test_auto_start_starts_without_prompt(self, monkeypatch):
        self._wire_stopped(monkeypatch)
        started = []
        monkeypatch.setattr(
            cmd_utils,
            "_start_containers_for_command",
            lambda name, verbose=False: started.append(name),
        )
        monkeypatch.setattr(
            cmd_utils.questionary,
            "confirm",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not prompt")),
        )
        assert (
            cmd_utils.ensure_containers_running("proj", require_running=True, auto_start=True)
            is True
        )
        assert started == ["proj"]


# ----------------------------------------------------------- start honesty


class TestStartNotFound:
    def test_nonexistent_project_exits_one_no_started(self, monkeypatch, capsys):
        _neutralize_docker(monkeypatch)
        monkeypatch.setattr(start_mod.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(start_mod, "_check_port_conflicts", lambda *a, **k: True)
        monkeypatch.setattr(start_mod, "get_project_containers", lambda name: [])
        # core.start resolves the project through its OWN imported accessor.
        monkeypatch.setattr(start_mod.core_start, "get_project_containers", lambda name: [])

        with pytest.raises(typer.Exit) as exc:
            start_mod.start(verbose=False, bench=None, yes=False, project_name=["no-such"])
        assert exc.value.exit_code == 1
        out = capsys.readouterr().out
        assert "started" not in out.lower()
        assert "not found" in out.lower()


# ----------------------------------------------------------- stop honesty


class _RunningFrappe:
    labels = {"com.docker.compose.service": "frappe"}
    status = "running"
    name = "good-frappe-1"

    def stop(self):
        pass


class TestStopHonesty:
    @pytest.mark.parametrize(
        "tokens",
        [
            ["good", "--bench"],
            ["good", "--bench="],
            ["good", "--bench", "--verbose"],
        ],
    )
    def test_a_missing_bench_value_cannot_expand_to_project_stop(self, tokens, monkeypatch):
        monkeypatch.setattr(stop_mod.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(
            core_stop,
            "stop",
            lambda name: pytest.fail("malformed --bench must not stop the project"),
        )

        with pytest.raises(typer.Exit) as exc:
            stop_mod.stop(ctx=None, verbose=False, bench=None, project_name=tokens)

        assert exc.value.exit_code == 2

    def test_a_dash_prefixed_bench_label_remains_addressable(self, monkeypatch):
        monkeypatch.setattr(stop_mod.sys.stdin, "isatty", lambda: True)
        seen = []
        monkeypatch.setattr(
            stop_mod,
            "_stop_benches",
            lambda names, bench, verbose: seen.append((names, bench, verbose)),
        )

        stop_mod.stop(
            ctx=None,
            verbose=False,
            bench=None,
            project_name=["good", "--bench", "-staging"],
        )

        assert seen == [(["good"], "-staging", False)]

    def test_nonexistent_project_exits_one(self, monkeypatch, capsys):
        _neutralize_docker(monkeypatch)
        monkeypatch.setattr(stop_mod.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(core_stop, "get_project_containers", lambda name: [])
        with pytest.raises(typer.Exit) as exc:
            stop_mod.stop(ctx=None, verbose=False, project_name=["no-such"])
        assert exc.value.exit_code == 1
        assert "not found" in capsys.readouterr().out.lower()

    def test_good_then_bad_stops_good_and_exits_one(self, monkeypatch, capsys):
        _neutralize_docker(monkeypatch)
        monkeypatch.setattr(stop_mod.sys.stdin, "isatty", lambda: True)

        def containers(name):
            return [_RunningFrappe()] if name == "good" else []

        monkeypatch.setattr(core_stop, "get_project_containers", containers)
        with pytest.raises(typer.Exit) as exc:
            stop_mod.stop(ctx=None, verbose=False, bench=None, project_name=["good", "no-such"])
        assert exc.value.exit_code == 1
        out = capsys.readouterr().out
        assert "Instance 'good' stopped." in out  # the good one still stopped


class TestRestartHonesty:
    def test_nonexistent_project_exits_one_no_started(self, monkeypatch, capsys):
        _neutralize_docker(monkeypatch)
        monkeypatch.setattr(restart_mod.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(restart_mod, "get_project_containers", lambda name: [])
        with pytest.raises(typer.Exit) as exc:
            restart_mod.restart(
                ctx=None, verbose=False, process=None, bench=None, project_name=["no-such"]
            )
        assert exc.value.exit_code == 1
        out = capsys.readouterr().out
        assert "not found" in out.lower()
        assert "Instance 'no-such' started." not in out


# ----------------------------------------------------------- config honesty


class TestConfigExitCodes:
    def test_invalid_interval_exits_one(self, monkeypatch):
        with pytest.raises(typer.Exit) as exc:
            config_mod.set_interval(30)
        assert exc.value.exit_code == 1

    def test_enable_invalid_interval_exits_one(self, monkeypatch):
        # Enabling with a sub-minimum interval still exits 1. Since the
        # rework-config-dx migration the validation runs in core.auto_inspect
        # BEFORE anything persists (the F3 fix), so no write needs neutralizing;
        # the signature changed by design (--startup/--no-startup tri-state).
        with pytest.raises(typer.Exit) as exc:
            config_mod.enable_auto_inspect(interval=30, startup=None)
        assert exc.value.exit_code == 1

    def test_handler_exception_exits_one(self, monkeypatch):
        def _boom(_v):
            raise RuntimeError("disk full")

        monkeypatch.setattr(config_mod.config_utils, "set_show_tips", _boom)
        with pytest.raises(typer.Exit) as exc:
            config_mod.enable_tips()
        assert exc.value.exit_code == 1

    def test_clear_cache_no_target_exits_two(self):
        # Changed BY DESIGN by rework-config-dx (F10): a missing target is a
        # usage error, and usage errors exit 2 everywhere else in the CLI.
        with pytest.raises(typer.Exit) as exc:
            config_mod.clear_cache(project_name=None, all=False, yes=False)
        assert exc.value.exit_code == 2

    def test_stop_when_not_running_is_idempotent_success(self, monkeypatch):
        # The deliberate carve-out: asking to stop something already stopped is a
        # no-op success (exit 0), not an error.
        monkeypatch.setattr(config_mod.auto_inspect, "is_running", lambda: False)
        assert config_mod.stop_auto_inspect() is None  # returns, does not raise


# ----------------------------------------------------------- update honesty


class _MaintFailContainer:
    """A frappe container whose ``set-maintenance-mode off`` exec fails (non-zero),
    while every other exec succeeds - the exact stuck-in-maintenance scenario."""

    labels = {"com.docker.compose.service": "frappe"}
    status = "running"

    def reload(self):
        pass

    def exec_run(self, cmd, workdir=None, **kwargs):
        if "set-maintenance-mode off" in cmd:
            return (1, b"could not disable")
        return (0, b"")


class TestUpdateMaintenanceDisableFailure:
    def test_disable_failure_alone_exits_one_and_names_site(self, monkeypatch, capsys):
        # The state machine moved into core.update (openspec `migrate-update-core`),
        # so the collaborators are patched there; the property - a stuck site is named
        # with its recovery command and forces exit 1 - is unchanged.
        container = _MaintFailContainer()
        monkeypatch.setattr(cmd_utils, "ensure_containers_running", lambda *a, **k: True)
        monkeypatch.setattr(
            cmd_utils, "resolve_bench_path", lambda *a, **k: "/workspace/frappe-bench"
        )
        monkeypatch.setattr(core_update.core_docker, "get_frappe_container", lambda name: container)
        # Everything except the maintenance-off exec succeeds.
        monkeypatch.setattr(core_update, "_stream_step", lambda *a, **k: (0, None))
        monkeypatch.setattr(core_update, "_sites_with_app", lambda *a, **k: ["site1"])
        monkeypatch.setattr(core_update.time, "sleep", lambda *a, **k: None)

        with pytest.raises(typer.Exit) as exc:
            update_mod.run_app_update(
                "proj",
                ["myapp"],
                verbose=True,
                no_recache=True,
            )
        assert exc.value.exit_code == 1
        out = capsys.readouterr().out
        # The stuck site is named with the recovery command.
        assert "site1" in out
        assert "set-maintenance-mode off" in out
