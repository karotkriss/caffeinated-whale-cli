"""``core.doctor`` - the system-wide, read-only environment preflight.

Covers the registry shape (F2: a tiny in-code list, not a plugin system), each
check's pass/warn/fail classification with every external probe mocked, and the
``run_all`` aggregation contract: ``ok`` (the frontends' exit-code source) is
false iff any check FAILED - a WARN never flips it, matching the maintainer's
ruled exit contract (warn exits clean, fail exits non-zero).
"""

from __future__ import annotations

import subprocess

import pytest

from caffeinated_whale_cli.core import doctor as core_doctor
from caffeinated_whale_cli.core.doctor import CheckStatus
from caffeinated_whale_cli.core.envelope import Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from caffeinated_whale_cli.core.list import InstanceDTO


class TestRegistry:
    def test_ships_exactly_the_ruled_check_ids(self):
        """C1-C9, C11, C17, C18 - C10 and C12-C16 stay deferred (not built at all)."""
        ids = {c.id for c in core_doctor._CHECKS}
        assert ids == {
            "c1",
            "c2",
            "c3",
            "c4",
            "c5",
            "c6",
            "c7",
            "c8",
            "c9",
            "c11",
            "c17",
            "c18",
        }

    def test_is_a_plain_list_not_a_plugin_system(self):
        assert isinstance(core_doctor._CHECKS, list)
        for check in core_doctor._CHECKS:
            assert isinstance(check.id, str)
            assert isinstance(check.title, str)
            assert isinstance(check.group, str)
            assert callable(check.run)

    def test_severity_is_a_closed_three_tier_set(self):
        assert {s.value for s in CheckStatus} == {"pass", "warn", "fail"}


class TestRunAll:
    def test_ok_true_when_nothing_failed(self, monkeypatch):
        monkeypatch.setattr(
            core_doctor,
            "_CHECKS",
            [
                core_doctor.Check(
                    id="x", title="X", group="G", run=lambda: (CheckStatus.PASS, "ok", None)
                ),
                core_doctor.Check(
                    id="y", title="Y", group="G", run=lambda: (CheckStatus.WARN, "meh", "fix it")
                ),
            ],
        )
        report = core_doctor.run_all().data
        assert report.ok is True
        assert report.passed == 1
        assert report.warned == 1
        assert report.failed == 0

    def test_ok_false_when_anything_failed(self, monkeypatch):
        monkeypatch.setattr(
            core_doctor,
            "_CHECKS",
            [
                core_doctor.Check(
                    id="x", title="X", group="G", run=lambda: (CheckStatus.PASS, "ok", None)
                ),
                core_doctor.Check(
                    id="z", title="Z", group="G", run=lambda: (CheckStatus.FAIL, "broken", "fix it")
                ),
            ],
        )
        report = core_doctor.run_all().data
        assert report.ok is False
        assert report.failed == 1

    def test_result_status_is_warning_when_any_check_is_not_pass(self, monkeypatch):
        monkeypatch.setattr(
            core_doctor,
            "_CHECKS",
            [
                core_doctor.Check(
                    id="x", title="X", group="G", run=lambda: (CheckStatus.WARN, "meh", None)
                )
            ],
        )
        result = core_doctor.run_all()
        assert result.status is Status.WARNING

    def test_a_crashing_check_is_reported_as_its_own_failure_not_a_crash(self, monkeypatch):
        def _boom():
            raise RuntimeError("kaboom")

        monkeypatch.setattr(
            core_doctor, "_CHECKS", [core_doctor.Check(id="x", title="X", group="G", run=_boom)]
        )
        report = core_doctor.run_all().data
        assert report.failed == 1
        assert report.ok is False
        assert "kaboom" in report.checks[0].detail


class TestDockerBinary:
    def test_pass_when_resolvable(self, monkeypatch):
        monkeypatch.setattr(core_doctor.shutil, "which", lambda _n: "/usr/bin/docker")
        monkeypatch.setattr(
            core_doctor,
            "_probe_version",
            lambda _cmd: "Docker version 27.1.1, build abc",
        )
        status, detail, fix = core_doctor._check_docker_binary()
        assert status is CheckStatus.PASS
        assert "27.1.1" in detail
        assert fix is None

    def test_fail_true_absent(self, monkeypatch):
        monkeypatch.setattr(core_doctor.shutil, "which", lambda _n: None)
        monkeypatch.setattr(
            "platform.uname", lambda: type("U", (), {"release": "5.15.0-generic"})()
        )
        status, detail, fix = core_doctor._check_docker_binary()
        assert status is CheckStatus.FAIL
        assert "not found" in detail
        assert "install" in fix

    def test_fail_wsl_reads_as_desktop_stopped_not_not_installed(self, monkeypatch):
        """C1's headline defect: WSL2 + Desktop stopped must not say 'not installed'."""
        monkeypatch.setattr(core_doctor.shutil, "which", lambda _n: None)
        monkeypatch.setattr(
            "platform.uname", lambda: type("U", (), {"release": "5.15.0-microsoft-standard-WSL2"})()
        )
        status, detail, fix = core_doctor._check_docker_binary()
        assert status is CheckStatus.FAIL
        assert "WSL2" in detail
        assert "Docker Desktop" in detail
        assert "not installed" not in detail
        assert "Windows" in fix


class TestDockerDaemon:
    def test_pass_when_reachable(self, monkeypatch):
        monkeypatch.setattr(
            core_doctor.core_list,
            "list_instances",
            lambda: core_doctor.Result(
                status=Status.OK, data=[InstanceDTO(project_name="p", status="running", ports=[])]
            ),
        )
        status, detail, _fix = core_doctor._check_docker_daemon()
        assert status is CheckStatus.PASS
        assert "1 instance" in detail

    def test_fail_when_unreachable(self, monkeypatch):
        def _boom():
            raise CwcliError(ErrorKind.DOCKER, "docker.unreachable", "nope")

        monkeypatch.setattr(core_doctor.core_list, "list_instances", _boom)
        status, detail, fix = core_doctor._check_docker_daemon()
        assert status is CheckStatus.FAIL
        assert "daemon" in detail
        assert fix


class TestComposePlugin:
    def test_pass_when_version_succeeds(self, monkeypatch):
        monkeypatch.setattr(
            core_doctor.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(
                a[0], 0, stdout="Docker Compose version v2.29.1\n", stderr=""
            ),
        )
        status, detail, fix = core_doctor._check_compose_plugin()
        assert status is CheckStatus.PASS
        assert "v2.29.1" in detail
        assert fix is None

    def test_warn_when_missing(self, monkeypatch):
        monkeypatch.setattr(
            core_doctor.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(
                a[0], 1, stdout="", stderr="unknown command"
            ),
        )
        status, detail, fix = core_doctor._check_compose_plugin()
        assert status is CheckStatus.WARN
        assert "plugin" in detail
        assert fix

    def test_warn_never_fail_when_docker_binary_missing(self, monkeypatch):
        def _raise(*_a, **_k):
            raise FileNotFoundError()

        monkeypatch.setattr(core_doctor.subprocess, "run", _raise)
        status, _detail, _fix = core_doctor._check_compose_plugin()
        assert status is CheckStatus.WARN


class TestVersionCheck:
    def _info(self, *, current="2.1.0", latest=None, outdated=False):
        return core_doctor.core_version.VersionInfo(
            current=current,
            latest=latest,
            method="uv",
            upgrade_command=["uv", "tool", "upgrade", "caffeinated-whale-cli"],
            is_outdated=outdated,
            is_dev=False,
        )

    def _build(self, *, source="release", commit=None, dirty=None, editable=False):
        return core_doctor.core_version.BuildInfo(
            source=source, editable=editable, commit=commit, dirty=dirty
        )

    def test_pass_up_to_date(self, monkeypatch):
        monkeypatch.setattr(
            core_doctor.core_version,
            "check",
            lambda **_k: core_doctor.Result(status=Status.OK, data=self._info(latest="2.1.0")),
        )
        monkeypatch.setattr(core_doctor.core_version, "build_info", lambda: self._build())
        status, detail, fix = core_doctor._check_version()
        assert status is CheckStatus.PASS
        assert "up to date" in detail
        assert fix is None

    def test_warn_outdated(self, monkeypatch):
        monkeypatch.setattr(
            core_doctor.core_version,
            "check",
            lambda **_k: core_doctor.Result(
                status=Status.OK, data=self._info(latest="9.9.9", outdated=True)
            ),
        )
        monkeypatch.setattr(core_doctor.core_version, "build_info", lambda: self._build())
        status, detail, fix = core_doctor._check_version()
        assert status is CheckStatus.WARN
        assert "9.9.9" in detail
        assert "self-update" in fix

    def test_pass_when_pypi_unreachable_fail_open(self, monkeypatch):
        monkeypatch.setattr(
            core_doctor.core_version,
            "check",
            lambda **_k: core_doctor.Result(status=Status.WARNING, data=self._info(latest=None)),
        )
        monkeypatch.setattr(core_doctor.core_version, "build_info", lambda: self._build())
        status, detail, _fix = core_doctor._check_version()
        assert status is CheckStatus.PASS
        assert "could not check" in detail

    def test_source_build_provenance_shown(self, monkeypatch):
        monkeypatch.setattr(
            core_doctor.core_version,
            "check",
            lambda **_k: core_doctor.Result(status=Status.OK, data=self._info(latest="2.1.0")),
        )
        monkeypatch.setattr(
            core_doctor.core_version,
            "build_info",
            lambda: self._build(source="source", commit="abc1234", dirty=True),
        )
        _status, detail, _fix = core_doctor._check_version()
        assert "source build" in detail
        assert "abc1234" in detail
        assert "dirty" in detail


class TestHomeLayout:
    def test_pass_when_not_yet_created(self, tmp_path, monkeypatch):
        home = tmp_path / "never-used"
        monkeypatch.setattr(core_doctor, "cwcli_home", lambda: home)
        status, detail, _fix = core_doctor._check_home_layout()
        assert status is CheckStatus.PASS
        assert "not yet created" in detail

    def test_pass_when_writable_with_correct_cache_mode(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / "cache").mkdir(parents=True)
        (home / "cache").chmod(0o700)
        monkeypatch.setattr(core_doctor, "cwcli_home", lambda: home)
        status, _detail, _fix = core_doctor._check_home_layout()
        assert status is CheckStatus.PASS

    def test_warn_on_loose_cache_mode(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / "cache").mkdir(parents=True)
        (home / "cache").chmod(0o755)
        monkeypatch.setattr(core_doctor, "cwcli_home", lambda: home)
        status, detail, fix = core_doctor._check_home_layout()
        assert status is CheckStatus.WARN
        assert "0o755" in detail or "755" in detail
        assert "chmod" in fix

    def test_fail_when_unwritable(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(core_doctor, "cwcli_home", lambda: home)
        monkeypatch.setattr(core_doctor.os, "access", lambda *_a, **_k: False)
        status, detail, fix = core_doctor._check_home_layout()
        assert status is CheckStatus.FAIL
        assert "not writable" in detail
        assert fix


class TestAutoInspect:
    def test_pass_when_disabled(self, monkeypatch):
        monkeypatch.setattr(
            "caffeinated_whale_cli.utils.config_utils.get_auto_inspect_config",
            lambda: {"enabled": False},
        )
        status, detail, _fix = core_doctor._check_auto_inspect()
        assert status is CheckStatus.PASS
        assert detail == "disabled"

    def test_pass_when_enabled_and_alive(self, monkeypatch):
        monkeypatch.setattr(
            "caffeinated_whale_cli.utils.config_utils.get_auto_inspect_config",
            lambda: {"enabled": True},
        )
        monkeypatch.setattr(
            "caffeinated_whale_cli.utils.auto_inspect._read_daemon_record",
            lambda: (4242, "start-tok"),
        )
        monkeypatch.setattr("caffeinated_whale_cli.utils.auto_inspect._pid_alive", lambda _p: True)
        monkeypatch.setattr(
            "caffeinated_whale_cli.utils.auto_inspect._process_start_time", lambda _p: "start-tok"
        )
        status, detail, _fix = core_doctor._check_auto_inspect()
        assert status is CheckStatus.PASS
        assert "4242" in detail

    def test_warn_never_prunes_via_is_running(self, monkeypatch):
        """T4: doctor must use the read-only trio, never ``is_running()``."""
        called = []
        monkeypatch.setattr(
            "caffeinated_whale_cli.utils.auto_inspect.is_running",
            lambda: called.append("is_running") or False,
        )
        monkeypatch.setattr(
            "caffeinated_whale_cli.utils.config_utils.get_auto_inspect_config",
            lambda: {"enabled": True},
        )
        monkeypatch.setattr(
            "caffeinated_whale_cli.utils.auto_inspect._read_daemon_record", lambda: None
        )
        status, _detail, fix = core_doctor._check_auto_inspect()
        assert status is CheckStatus.WARN
        assert fix
        assert called == []  # is_running() (which prunes) was never invoked

    def test_warn_when_pid_dead(self, monkeypatch):
        monkeypatch.setattr(
            "caffeinated_whale_cli.utils.config_utils.get_auto_inspect_config",
            lambda: {"enabled": True},
        )
        monkeypatch.setattr(
            "caffeinated_whale_cli.utils.auto_inspect._read_daemon_record", lambda: (1, None)
        )
        monkeypatch.setattr("caffeinated_whale_cli.utils.auto_inspect._pid_alive", lambda _p: False)
        status, detail, _fix = core_doctor._check_auto_inspect()
        assert status is CheckStatus.WARN
        assert "not running" in detail


class TestFreeSpace:
    def test_home_warn_below_floor(self, tmp_path, monkeypatch):
        monkeypatch.setattr(core_doctor, "cwcli_home", lambda: tmp_path)
        monkeypatch.setattr(
            core_doctor.shutil,
            "disk_usage",
            lambda _p: type("U", (), {"free": 1 * 1024**3, "total": 0, "used": 0})(),
        )
        status, detail, fix = core_doctor._check_home_free_space()
        assert status is CheckStatus.WARN
        assert "1.0 GiB" in detail
        assert fix

    def test_home_pass_above_floor(self, tmp_path, monkeypatch):
        monkeypatch.setattr(core_doctor, "cwcli_home", lambda: tmp_path)
        monkeypatch.setattr(
            core_doctor.shutil,
            "disk_usage",
            lambda _p: type("U", (), {"free": 100 * 1024**3, "total": 0, "used": 0})(),
        )
        status, _detail, _fix = core_doctor._check_home_free_space()
        assert status is CheckStatus.PASS

    def test_temp_warn_when_tmpfs_even_with_room(self, tmp_path, monkeypatch):
        monkeypatch.setattr(core_doctor, "cwcli_home", lambda: tmp_path)
        monkeypatch.delenv("TMPDIR", raising=False)
        monkeypatch.delenv("TEMP", raising=False)
        monkeypatch.delenv("TMP", raising=False)
        monkeypatch.setattr(
            core_doctor.shutil,
            "disk_usage",
            lambda _p: type("U", (), {"free": 100 * 1024**3, "total": 0, "used": 0})(),
        )
        monkeypatch.setattr(core_doctor, "_is_tmpfs", lambda _p: True)
        status, detail, fix = core_doctor._check_temp_free_space()
        assert status is CheckStatus.WARN
        assert "RAM-backed" in detail
        assert "TMPDIR" in fix

    def test_temp_pass_disk_backed_with_room(self, tmp_path, monkeypatch):
        monkeypatch.setattr(core_doctor, "cwcli_home", lambda: tmp_path)
        monkeypatch.setattr(
            core_doctor.shutil,
            "disk_usage",
            lambda _p: type("U", (), {"free": 100 * 1024**3, "total": 0, "used": 0})(),
        )
        monkeypatch.setattr(core_doctor, "_is_tmpfs", lambda _p: False)
        status, _detail, _fix = core_doctor._check_temp_free_space()
        assert status is CheckStatus.PASS

    def test_temp_dir_honors_tmpdir_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        resolved = core_doctor._effective_temp_dir()
        assert resolved == tmp_path / "cwcli"


class TestSendme:
    def test_warn_when_not_installed(self, monkeypatch):
        monkeypatch.setattr(
            "caffeinated_whale_cli.utils.sendme_utils.is_sendme_installed", lambda: False
        )
        status, detail, fix = core_doctor._check_sendme()
        assert status is CheckStatus.WARN
        assert "not installed" in detail
        assert "optional" in detail
        assert fix

    def test_pass_when_installed(self, monkeypatch, tmp_path):
        fake = tmp_path / "sendme"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        monkeypatch.setattr(
            "caffeinated_whale_cli.utils.sendme_utils.is_sendme_installed", lambda: True
        )
        monkeypatch.setattr(
            "caffeinated_whale_cli.utils.sendme_utils.get_sendme_path", lambda: fake
        )
        monkeypatch.setattr(core_doctor, "_probe_version", lambda _cmd: "sendme 0.30.0")
        status, detail, _fix = core_doctor._check_sendme()
        assert status is CheckStatus.PASS
        assert "0.30.0" in detail


class TestGitHostingCli:
    def test_warn_when_not_installed(self, monkeypatch):
        monkeypatch.setattr(core_doctor.shutil, "which", lambda _n: None)
        status, detail, fix = core_doctor._check_gh()
        assert status is CheckStatus.WARN
        assert "not installed" in detail
        assert fix

    def test_pass_when_authenticated(self, monkeypatch):
        monkeypatch.setattr(core_doctor.shutil, "which", lambda _n: "/usr/bin/gh")
        monkeypatch.setattr(
            core_doctor.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(
                a[0], 0, stdout="", stderr="github.com\n  Logged in to github.com account me\n"
            ),
        )
        status, detail, fix = core_doctor._check_gh()
        assert status is CheckStatus.PASS
        assert "github.com" in detail
        assert fix is None

    def test_warn_when_not_authenticated(self, monkeypatch):
        monkeypatch.setattr(core_doctor.shutil, "which", lambda _n: "/usr/bin/glab")
        monkeypatch.setattr(
            core_doctor.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a[0], 1, stdout="", stderr="not logged in"),
        )
        status, detail, fix = core_doctor._check_glab()
        assert status is CheckStatus.WARN
        assert "not authenticated" in detail
        assert "glab auth login" in fix

    def test_never_calls_login(self, monkeypatch):
        """Auth checks must be read-only status reads, never a login flow."""
        calls = []

        def _run(cmd, **_k):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

        monkeypatch.setattr(core_doctor.shutil, "which", lambda _n: "/usr/bin/gh")
        monkeypatch.setattr(core_doctor.subprocess, "run", _run)
        core_doctor._check_gh()
        assert calls == [["gh", "auth", "status"]]


class TestPortCollisions:
    def test_pass_with_fewer_than_two_instances(self, monkeypatch):
        monkeypatch.setattr(
            core_doctor.core_list,
            "list_instances",
            lambda: core_doctor.Result(status=Status.OK, data=[]),
        )
        status, _detail, _fix = core_doctor._check_port_collisions()
        assert status is CheckStatus.PASS

    def test_pass_when_no_overlap(self, monkeypatch):
        monkeypatch.setattr(
            core_doctor.core_list,
            "list_instances",
            lambda: core_doctor.Result(
                status=Status.OK,
                data=[
                    InstanceDTO(project_name="a", status="running", ports=["8000"]),
                    InstanceDTO(project_name="b", status="running", ports=["8100"]),
                ],
            ),
        )
        status, _detail, _fix = core_doctor._check_port_collisions()
        assert status is CheckStatus.PASS

    def test_warn_on_overlap_covers_stopped_instances_too(self, monkeypatch):
        """The report's headline gap: two STOPPED instances still collide because
        Docker's PortBindings persist regardless of run state."""
        monkeypatch.setattr(
            core_doctor.core_list,
            "list_instances",
            lambda: core_doctor.Result(
                status=Status.OK,
                data=[
                    InstanceDTO(project_name="frappe15", status="exited", ports=["8000", "8001"]),
                    InstanceDTO(project_name="frappe16", status="exited", ports=["8000", "8002"]),
                ],
            ),
        )
        status, detail, fix = core_doctor._check_port_collisions()
        assert status is CheckStatus.WARN
        assert "frappe15" in detail and "frappe16" in detail
        assert "8000" in detail
        assert fix

    def test_warn_not_fail_when_daemon_unreachable(self, monkeypatch):
        def _boom():
            raise CwcliError(ErrorKind.DOCKER, "docker.unreachable", "nope")

        monkeypatch.setattr(core_doctor.core_list, "list_instances", _boom)
        status, detail, _fix = core_doctor._check_port_collisions()
        assert status is CheckStatus.WARN
        assert "unreachable" in detail
