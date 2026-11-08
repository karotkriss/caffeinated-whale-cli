"""Unusable-uid handling for ``cwcli init`` / ``cwcli axi init`` (issue #229).

On a host running as uid 0 (a common CI-runner shape), remapping the container
``frappe`` user to 0 collides with the container's own root row in
``/etc/passwd`` and ``bench init`` fails with exit 127; a uid a system account in
the image already holds (a shared install's service account, a runner's system
user) fails the same way. The fix gives ``frappe`` the image default for any such
uid, warns once, and makes the workspace dir writable by that user. ``--uid <n>``
and ``CWCLI_UID`` are the explicit overrides.

The uid DECISION TABLE and the helpers live in ``tests/test_core_docker.py``
(``resolve_frappe_alignment_ids``, ``container_account_uids``,
``align_container_user_to_host``, ``align_workspace_dir_owner``). The axi surface
lives in ``tests/test_axi_init.py::TestUidOverride``. This module covers the core
validator, the human CLI surface, and the init seams that wire it together.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from caffeinated_whale_cli.commands import init as init_mod
from caffeinated_whale_cli.core import init as core_init
from caffeinated_whale_cli.core.envelope import Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from caffeinated_whale_cli.main import app
from caffeinated_whale_cli.utils import config_utils, db_utils, docker_utils

from .test_core_init import (
    PROJECT,
    FakeContainer,
    bench_kwargs,
    use_container,
)

runner = CliRunner()


def _patch_common(monkeypatch):
    """The seams init needs to run without touching real state (mirrors the
    ``patched`` fixture in ``test_core_init``, inlined so no fixture is imported)."""
    monkeypatch.setattr(db_utils, "clear_cache_for_project", lambda name: None)
    monkeypatch.setattr(config_utils, "add_custom_path", lambda path: True)
    monkeypatch.setattr(core_init.time, "sleep", lambda _s: None)


# ------------------------------------------------------------------ core validator


class TestCheckUidOverride:
    def test_none_is_accepted(self):
        core_init.check_uid_override(None)  # no raise

    def test_a_positive_uid_is_accepted(self):
        core_init.check_uid_override(1001)  # no raise

    def test_zero_is_a_usage_error(self):
        with pytest.raises(CwcliError) as exc:
            core_init.check_uid_override(0)
        assert exc.value.kind is ErrorKind.USAGE
        assert exc.value.code == "uid.invalid"

    def test_a_negative_uid_is_a_usage_error(self):
        with pytest.raises(CwcliError) as exc:
            core_init.check_uid_override(-5)
        assert exc.value.kind is ErrorKind.USAGE
        assert exc.value.code == "uid.invalid"

    @pytest.mark.parametrize("value", ["0", "-1", "abc", "1000x"])
    def test_a_bad_cwcli_uid_is_a_usage_error(self, monkeypatch, value):
        monkeypatch.setenv("CWCLI_UID", value)
        with pytest.raises(CwcliError) as exc:
            core_init.check_uid_override(None)
        assert exc.value.kind is ErrorKind.USAGE
        assert exc.value.code == "uid.invalid"
        assert "CWCLI_UID" in exc.value.message

    def test_a_valid_cwcli_uid_is_accepted(self, monkeypatch):
        monkeypatch.setenv("CWCLI_UID", "1005")
        core_init.check_uid_override(None)  # no raise


# ------------------------------------------------------------------ human CLI surface


class TestHumanCliRejectsBadUid:
    @staticmethod
    def _bypass_docker_precheck(monkeypatch):
        """Neutralize @handle_docker_errors so the uid validation is reached even on
        a runner with no Docker daemon (the CI unit tier), where the decorator would
        otherwise exit 1 before the command body runs."""
        monkeypatch.setattr(docker_utils.shutil, "which", lambda _name: "/usr/bin/docker")
        monkeypatch.setattr(
            docker_utils.docker, "from_env", lambda: SimpleNamespace(ping=lambda: True)
        )

    def test_uid_zero_is_a_usage_error_before_any_work(self, monkeypatch):
        """--uid 0 exits 2 (a usage error) before init_instance runs, so no project
        dir or containers are left behind."""
        self._bypass_docker_precheck(monkeypatch)

        def fail_if_called(*a, **k):
            raise AssertionError("init_instance ran despite an invalid --uid")

        monkeypatch.setattr(core_init, "init_instance", fail_if_called)
        result = runner.invoke(app, ["init", "proj", "--uid", "0"])
        assert result.exit_code == 2

    def test_negative_uid_is_a_usage_error(self, monkeypatch):
        self._bypass_docker_precheck(monkeypatch)
        monkeypatch.setattr(
            core_init, "init_instance", lambda *a, **k: (_ for _ in ()).throw(AssertionError())
        )
        result = runner.invoke(app, ["init", "proj", "--uid", "-3"])
        assert result.exit_code == 2

    def test_non_numeric_uid_is_a_parse_time_usage_error(self, monkeypatch):
        monkeypatch.setattr(
            core_init, "init_instance", lambda *a, **k: (_ for _ in ()).throw(AssertionError())
        )
        result = runner.invoke(app, ["init", "proj", "--uid", "abc"])
        assert result.exit_code == 2

    def test_a_bad_cwcli_uid_is_a_usage_error_before_any_work(self, monkeypatch):
        """A mistyped CWCLI_UID is refused up front, naming the variable, rather
        than silently aligning frappe to the host uid."""
        self._bypass_docker_precheck(monkeypatch)
        monkeypatch.setenv("CWCLI_UID", "zero")
        monkeypatch.setattr(
            core_init, "init_instance", lambda *a, **k: (_ for _ in ()).throw(AssertionError())
        )
        result = runner.invoke(app, ["init", "proj"])
        assert result.exit_code == 2
        assert "CWCLI_UID" in result.output


# ------------------------------------------------------------------ init_bench: warn once


class TestInitBenchSurfacesTheFallbackNote:
    def test_the_unusable_uid_fallback_is_reported_as_a_warning(self, monkeypatch):
        """When the requested uid is unusable (root, or a system account in the
        image), init_bench surfaces the fallback once as a non-silent
        ``init.uid_align_root_host`` warning (not a silent remap)."""
        _patch_common(monkeypatch)
        container = FakeContainer()
        use_container(monkeypatch, container)
        monkeypatch.setattr(
            core_init.core_docker, "align_container_user_to_host", lambda *a, **k: (False, None)
        )
        monkeypatch.setattr(
            core_init.core_docker,
            "frappe_alignment_target",
            lambda c, uid=None: (1000, 1000, "Using uid 1000 ... Set CWCLI_UID=<n>."),
        )

        result = core_init.init_bench(PROJECT, **bench_kwargs())

        assert result.status is Status.WARNING
        codes = [w.code for w in result.warnings]
        assert codes.count("init.uid_align_root_host") == 1

    def test_no_note_on_a_normal_host(self, monkeypatch):
        """A non-root host resolves its own ids with no note, so init succeeds clean
        (no fallback warning) - the unchanged common path."""
        _patch_common(monkeypatch)
        container = FakeContainer()
        # use_container wires a non-root host resolver (no note), the common path;
        # assert init_bench leaks no fallback warning when resolve returns none.
        use_container(monkeypatch, container)
        result = core_init.init_bench(PROJECT, **bench_kwargs())
        codes = [w.code for w in result.warnings]
        assert "init.uid_align_root_host" not in codes


# ------------------------------------------------------------------ init_bench: workspace dir


class TestInitBenchOwnsTheWorkspaceDir:
    def test_the_workspace_dir_follows_the_alignment_target(self, monkeypatch):
        """bench init creates the bench inside the workspace dir as frappe, so
        init_bench hands that dir to the SAME target frappe was aligned to (on a
        root host the dir was created by root), before creating anything in it."""
        _patch_common(monkeypatch)
        container = FakeContainer()
        use_container(monkeypatch, container)
        monkeypatch.setattr(
            core_init.core_docker, "align_container_user_to_host", lambda *a, **k: (True, None)
        )
        monkeypatch.setattr(
            core_init.core_docker,
            "frappe_alignment_target",
            lambda c, uid=None: (uid or 1000, uid or 1000, None),
        )
        spy: list[tuple] = []

        def fake_owner(c, path, uid, gid):
            spy.append((path, uid, gid))
            container.exec_run_calls.append("REOWN-WORKSPACE")
            return True

        monkeypatch.setattr(core_init.core_docker, "align_workspace_dir_owner", fake_owner)

        events: list = []
        core_init.init_bench(PROJECT, **bench_kwargs(), uid=1005, on_event=events.append)

        assert spy == [("/workspace", 1005, 1005)]
        traces = [e.text for e in events if isinstance(e, core_init.InitTrace)]
        assert any("/workspace owner to uid 1005" in t for t in traces)
        # ...before init makes anything inside it.
        calls = container.exec_run_calls
        first_mkdir = next(i for i, c in enumerate(calls) if c.startswith("mkdir -p"))
        assert calls.index("REOWN-WORKSPACE") < first_mkdir


class TestInitThreadsTheUidIntoTheAutoStart:
    def test_default_start_threads_uid_into_the_auto_start(self, monkeypatch, tmp_path):
        """The human init's end-of-init auto-start reuses the SAME --uid so it aligns
        to the id the bench was built under, not the host uid."""
        calls: list[dict] = []
        monkeypatch.setattr(
            init_mod.core_start,
            "start",
            lambda project, **kw: calls.append({"project": project, **kw}) or _fake_start_result(),
        )
        init_mod._start_services("proj", "/workspace/frappe-bench", uid_override=1005)
        assert calls[0]["uid_override"] == 1005


def _fake_start_result():
    from types import SimpleNamespace

    return SimpleNamespace(status=Status.OK, warnings=[], data=SimpleNamespace(web_ready=True))


class TestAnInvalidCwcliUidChangesNothing:
    """A bad CWCLI_UID is refused at every aligning entry point before any container
    is stopped, started, or recreated (README: "refused with a usage error before
    cwcli changes anything"). Each test records the first mutating step and asserts it
    never ran."""

    @pytest.fixture(autouse=True)
    def bad_env(self, monkeypatch):
        monkeypatch.setenv("CWCLI_UID", "root")

    def test_core_start(self, monkeypatch):
        from caffeinated_whale_cli.core import start as core_start

        touched: list[str] = []
        monkeypatch.setattr(
            core_start, "resolve_project_containers", lambda p: touched.append(p) or []
        )
        with pytest.raises(CwcliError) as raised:
            core_start.start("proj")
        assert raised.value.code == "uid.invalid"
        assert touched == []

    def test_core_scale(self, monkeypatch):
        from caffeinated_whale_cli.core import docker as core_docker
        from caffeinated_whale_cli.core import scale as core_scale

        touched: list[str] = []
        monkeypatch.setattr(
            core_docker, "ensure_compose_available", lambda: touched.append("compose")
        )
        with pytest.raises(CwcliError) as raised:
            core_scale.scale("proj", consent=True)
        assert raised.value.code == "uid.invalid"
        assert touched == []

    def test_cli_restart(self, monkeypatch, capsys):
        import typer

        from caffeinated_whale_cli.commands import restart as restart_mod

        touched: list[str] = []
        monkeypatch.setattr(restart_mod.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(
            restart_mod, "stop_project_best_effort", lambda p, **kw: touched.append(p) or 0
        )
        with pytest.raises(typer.Exit) as exc:
            restart_mod.restart(
                ctx=None, verbose=False, process=None, bench=None, project_name=["proj"]
            )
        assert exc.value.exit_code == 1
        assert "CWCLI_UID" in capsys.readouterr().err
        assert touched == []

    def test_cli_start(self, monkeypatch, capsys):
        import typer

        from caffeinated_whale_cli.commands import start as start_mod

        touched: list[str] = []
        monkeypatch.setattr(start_mod.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(start_mod, "_frappe_running", lambda p: touched.append(p) or False)
        with pytest.raises(typer.Exit) as exc:
            start_mod.start(verbose=False, bench=None, yes=False, project_name=["proj"])
        assert exc.value.exit_code == 1
        assert "CWCLI_UID" in capsys.readouterr().err
        assert touched == []

    def test_axi_start(self, monkeypatch):
        from caffeinated_whale_cli.commands import axi as axi_mod

        touched: list[str] = []
        monkeypatch.setattr(
            axi_mod, "_axi_resolve_port_conflicts", lambda p, y: touched.append(p) or []
        )
        result = runner.invoke(axi_mod.app, ["start", "proj"])
        assert result.exit_code == 2
        assert "CWCLI_UID must be a positive" in result.output
        assert touched == []

    def test_web_ui_restart_job(self, monkeypatch):
        from caffeinated_whale_cli import jobs

        touched: list[str] = []
        monkeypatch.setattr(jobs.core_stop, "stop", lambda p, **kw: touched.append(p))
        monkeypatch.setattr(jobs, "_serving_bench_paths", lambda p: touched.append(p) or [])
        with pytest.raises(CwcliError) as raised:
            jobs._restart_instance({"project": "proj"}, lambda e: None)
        assert raised.value.code == "uid.invalid"
        assert touched == []
