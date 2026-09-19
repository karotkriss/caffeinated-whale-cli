"""Root-uid host handling for ``cwcli init`` / ``cwcli axi init`` (issue #229).

On a host running as uid 0 (a common CI-runner shape), remapping the container
``frappe`` user to 0 collides with the container's own root row in
``/etc/passwd`` and ``bench init`` fails with exit 127. The fix keeps ``frappe``
at the image default on a root host, warns once, and keeps the bind-mount data
dir writable by that user. ``--uid <n>`` is the explicit escape hatch.

The uid DECISION TABLE and the two host-side helpers live in
``tests/test_core_docker.py`` (``resolve_frappe_alignment_ids``,
``align_container_user_to_host``, ``align_bind_mount_source_owner``). The axi
surface lives in ``tests/test_axi_init.py::TestUidOverride``. This module covers
the core validator, the human CLI surface, and the two init seams that wire it
together.
"""

from __future__ import annotations

import urllib.request
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
    COMPOSE_TEMPLATE,
    PROJECT,
    FakeContainer,
    bench_kwargs,
    instance_setup,
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


# ------------------------------------------------------------------ init_bench: warn once


class TestInitBenchSurfacesTheRootHostNote:
    def test_the_root_host_fallback_is_reported_as_a_warning(self, monkeypatch):
        """On a root host with no --uid, init_bench surfaces the fallback once as a
        non-silent ``init.uid_align_root_host`` warning (not a silent remap)."""
        _patch_common(monkeypatch)
        container = FakeContainer()
        use_container(monkeypatch, container)
        monkeypatch.setattr(
            core_init.core_docker, "align_container_user_to_host", lambda *a, **k: (False, None)
        )
        monkeypatch.setattr(
            core_init.core_docker,
            "resolve_frappe_alignment_ids",
            lambda uid=None: (1000, 1000, "Host is running as uid 0 (root): ... Pass --uid <n>."),
        )

        result = core_init.init_bench(PROJECT, **bench_kwargs())

        assert result.status is Status.WARNING
        codes = [w.code for w in result.warnings]
        assert codes.count("init.uid_align_root_host") == 1

    def test_no_note_on_a_normal_host(self, monkeypatch):
        """A non-root host resolves its own ids with no note, so init succeeds clean
        (no root-host warning) - the unchanged common path."""
        _patch_common(monkeypatch)
        container = FakeContainer()
        # use_container wires a non-root host resolver (no note), the common path;
        # assert init_bench leaks no root-host warning when resolve returns none.
        use_container(monkeypatch, container)
        result = core_init.init_bench(PROJECT, **bench_kwargs())
        codes = [w.code for w in result.warnings]
        assert "init.uid_align_root_host" not in codes


# ------------------------------------------------------------------ init_instance: data dir


class TestInitInstanceAlignsTheDataDir:
    def test_the_created_data_dir_owner_follows_the_alignment_target(self, monkeypatch, tmp_path):
        """init_instance chowns the freshly created bind-mount source dir to the same
        target frappe is aligned to, threading --uid through."""
        _patch_common(monkeypatch)
        instance_setup(monkeypatch, tmp_path, seed_compose=False)
        monkeypatch.setattr(
            urllib.request, "urlretrieve", lambda url, dest: dest.write_text(COMPOSE_TEMPLATE)
        )
        monkeypatch.setattr(core_init, "_ports_still_in_use", lambda ports, emit: [])

        spy: list[tuple] = []
        monkeypatch.setattr(
            core_init.core_docker,
            "align_bind_mount_source_owner",
            lambda path, uid_override=None: (spy.append((str(path), uid_override)) or 1005),
        )

        core_init.init_instance(PROJECT, port=18000, uid=1005)

        data_dir = str(tmp_path / PROJECT / "data")
        assert spy == [(data_dir, 1005)]

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
