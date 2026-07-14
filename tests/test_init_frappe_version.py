"""Tests for ``--version`` / ``--frappe-branch`` resolution in ``init``.

``--version`` resolves by input shape: a bare integer ``N`` becomes the branch
``version-N``, a full semantic version ``X.Y.Z`` becomes the tag ``vX.Y.Z``, and
anything malformed is rejected with a non-zero exit. ``--frappe-branch`` keeps
working as a raw ref, and the two flags are mutually exclusive.
"""

import pytest
import typer

import caffeinated_whale_cli.commands.init as init_mod
from caffeinated_whale_cli.commands.init import (
    DEFAULT_FRAPPE_BRANCH,
    _frappe_major_version,
    _resolve_frappe_branch,
    resolve_frappe_ref,
)


class TestResolveFrappeRef:
    """The pure shape-based resolver."""

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("16", "version-16"),
            ("15", "version-15"),
            ("14", "version-14"),
            (" 16 ", "version-16"),  # surrounding whitespace tolerated
        ],
    )
    def test_bare_integer_resolves_to_branch(self, value, expected):
        assert resolve_frappe_ref(value) == expected

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("16.26.3", "v16.26.3"),
            ("15.40.0", "v15.40.0"),
            ("16.0.0-beta.1", "v16.0.0-beta.1"),  # pre-release is valid semver
            ("16.0.0+build.7", "v16.0.0+build.7"),  # build metadata is valid semver
        ],
    )
    def test_semver_resolves_to_tag(self, value, expected):
        assert resolve_frappe_ref(value) == expected

    @pytest.mark.parametrize(
        "value",
        [
            "16.26",  # only two components is not semver
            "v16.26.3",  # already has the v prefix -> not a bare int or bare semver
            "latest",
            "version-16",
            "16.26.3.4",
            "16.x",
            "",
        ],
    )
    def test_malformed_raises(self, value):
        """`resolve_frappe_ref` raises on a malformed version string."""
        with pytest.raises(ValueError):
            resolve_frappe_ref(value)


class TestResolveFrappeBranch:
    """The command-level composition of the two mutually-exclusive flags."""

    def test_default_when_neither_given(self):
        assert _resolve_frappe_branch(None, None) == DEFAULT_FRAPPE_BRANCH
        assert DEFAULT_FRAPPE_BRANCH == "version-16"

    def test_frappe_branch_passthrough(self):
        """A raw `--frappe-branch` ref passes through unchanged."""
        assert _resolve_frappe_branch("version-15", None) == "version-15"
        assert _resolve_frappe_branch("develop", None) == "develop"

    def test_version_int_resolves(self):
        """`--version 16` resolves to the `version-16` branch."""
        assert _resolve_frappe_branch(None, "16") == "version-16"

    def test_version_semver_resolves(self):
        """`--version 16.26.3` resolves to the `v16.26.3` tag."""
        assert _resolve_frappe_branch(None, "16.26.3") == "v16.26.3"

    def test_malformed_version_exits_nonzero(self):
        with pytest.raises(typer.Exit) as exc:
            _resolve_frappe_branch(None, "not-a-version")
        assert exc.value.exit_code == 1

    def test_both_flags_exits_nonzero(self):
        with pytest.raises(typer.Exit) as exc:
            _resolve_frappe_branch("version-16", "16")
        assert exc.value.exit_code == 1


class _StopAfterBenchInitError(Exception):
    """Sentinel to stop init once the bench init command is captured."""


def _drive_init_to_bench_init(monkeypatch, tmp_path, **overrides):
    """Run the real ``init`` command body (verbose path) far enough to capture
    the ``bench init`` command string, stubbing every Docker/host boundary.

    Returns the captured command string.
    """
    captured = {}

    class _FakeContainer:
        def exec_run(self, *a, **k):  # pragma: no cover - not reached for v16
            return 0, b""

    def recording_exec(container, command, **kwargs):
        captured["command"] = command
        raise _StopAfterBenchInitError

    monkeypatch.setattr(init_mod, "check_ports_in_use", lambda ports: dict.fromkeys(ports, False))
    monkeypatch.setattr(init_mod.config_utils, "get_show_tips", lambda: False)
    monkeypatch.setattr(init_mod, "_setup_project_directory", lambda *a, **k: tmp_path)
    monkeypatch.setattr(init_mod, "_customize_compose_ports", lambda *a, **k: None)
    monkeypatch.setattr(init_mod, "_pull_compose_images", lambda *a, **k: None)
    monkeypatch.setattr(init_mod, "_start_compose_project", lambda *a, **k: None)
    monkeypatch.setattr(init_mod, "_wait_for_containers_running", lambda *a, **k: True)
    monkeypatch.setattr(init_mod, "get_frappe_container", lambda *a, **k: _FakeContainer())
    monkeypatch.setattr(init_mod, "_ensure_directory", lambda *a, **k: None)
    monkeypatch.setattr(
        init_mod,
        "_resolve_bench_target",
        lambda *a, **k: ("frappe-bench", "/workspace/frappe-bench", False),
    )
    monkeypatch.setattr(init_mod, "_exec_in_container", recording_exec)

    params = dict(
        project_name="proj",
        port=18500,
        bench_name="frappe-bench",
        site_name="development.localhost",
        bench_parent="/workspace",
        frappe_branch=None,
        version=None,
        db_root_password="123",
        admin_password="admin",
        auto_start=False,
        reuse_bench=None,
        verbose=True,
        install_erpnext=False,
        erpnext_branch="version-16",
    )
    params.update(overrides)

    with pytest.raises(_StopAfterBenchInitError):
        init_mod.init.__wrapped__(**params)
    return captured["command"]


class TestBenchInitRefEndToEnd:
    """The resolved ref reaches the actual ``bench init`` command."""

    def test_default_uses_version_16(self, monkeypatch, tmp_path):
        cmd = _drive_init_to_bench_init(monkeypatch, tmp_path)
        assert "--frappe-branch version-16" in cmd

    def test_version_int_reaches_bench_init(self, monkeypatch, tmp_path):
        cmd = _drive_init_to_bench_init(monkeypatch, tmp_path, version="16")
        assert "--frappe-branch version-16" in cmd

    def test_version_semver_tag_reaches_bench_init(self, monkeypatch, tmp_path):
        cmd = _drive_init_to_bench_init(monkeypatch, tmp_path, version="16.26.3")
        assert "--frappe-branch v16.26.3" in cmd

    def test_frappe_branch_passthrough_reaches_bench_init(self, monkeypatch, tmp_path):
        cmd = _drive_init_to_bench_init(monkeypatch, tmp_path, frappe_branch="develop")
        assert "--frappe-branch develop" in cmd


class TestFrappeMajorVersion:
    @pytest.mark.parametrize(
        "ref,expected",
        [
            ("version-16", 16),
            ("version-14", 14),
            ("v16.26.3", 16),
            ("v14.80.0", 14),
            ("develop", None),
            ("staging", None),
        ],
    )
    def test_major_extraction(self, ref, expected):
        """`_frappe_major_version` extracts the major number from a ref."""
        assert _frappe_major_version(ref) == expected
