"""Tests for ``--version`` / ``--frappe-branch`` resolution in ``init``.

``--version`` resolves by input shape: a bare integer ``N`` becomes the branch
``version-N``, a full semantic version ``X.Y.Z`` becomes the tag ``vX.Y.Z``, and
anything malformed is rejected with a non-zero exit. ``--frappe-branch`` keeps
working as a raw ref, and the two flags are mutually exclusive.

Re-pointed with its subjects by ``migrate-init-core``: the pure resolvers
(``resolve_frappe_ref``, ``_frappe_major_version``, ``DEFAULT_FRAPPE_BRANCH``)
moved to ``core.init``; the flag fusion (``_resolve_frappe_branch``) stays in
the frontend. Two changes BY DESIGN: the malformed-shape raise is the typed
``CwcliError(USAGE)`` (same message) instead of ``ValueError``, and the
end-to-end capture asserts the resolved ref crossing the frontend->core seam
(``init_bench(frappe_ref=...)``) - the ref-to-command construction itself is
pinned at the core by ``tests/test_core_init.py``'s exec-order and gating tests.
"""

import pytest
import typer

import caffeinated_whale_cli.commands.init as init_mod
from caffeinated_whale_cli.commands.init import _resolve_frappe_branch
from caffeinated_whale_cli.core import init as core_init
from caffeinated_whale_cli.core.envelope import Result, Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from caffeinated_whale_cli.core.init import (
    DEFAULT_FRAPPE_BRANCH,
    InitReport,
    InstanceUp,
    _frappe_major_version,
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
        """A malformed version string is a typed usage error (was ValueError;
        retyped BY DESIGN by migrate-init-core, message unchanged)."""
        with pytest.raises(CwcliError) as exc:
            resolve_frappe_ref(value)
        assert exc.value.kind is ErrorKind.USAGE


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


def _drive_init_to_bench_ref(monkeypatch, **overrides) -> str:
    """Run the real ``init`` command body far enough to capture the resolved
    ref crossing the frontend->core seam, with both core stages stubbed.

    Returns the ``frappe_ref`` the frontend handed to ``core.init_bench``.
    """
    recorded = {}

    def fake_init_instance(project, **kwargs):
        return Result(status=Status.OK, data=InstanceUp(project=project, conf_dir="/x"))

    def fake_init_bench(project, **kwargs):
        recorded.update(kwargs)
        return Result(
            status=Status.OK,
            data=InitReport(
                project=project,
                bench_name=kwargs["bench_name"],
                bench_path=f"/workspace/{kwargs['bench_name']}",
                site_name=kwargs["site_name"],
                bench_created=True,
                site_created=True,
                erpnext_installed=False,
            ),
        )

    monkeypatch.setattr(init_mod.core_init, "init_instance", fake_init_instance)
    monkeypatch.setattr(init_mod.core_init, "init_bench", fake_init_bench)
    monkeypatch.setattr(init_mod.config_utils, "get_show_tips", lambda: False)
    monkeypatch.setattr(init_mod.cache, "recache_project", lambda *a, **k: True)

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

    init_mod.init.__wrapped__(**params)
    return recorded["frappe_ref"]


class TestBenchInitRefEndToEnd:
    """The resolved ref reaches the core call that builds ``bench init``.

    (The ref-to-command-string construction is pinned at the core by
    ``tests/test_core_init.py``; this covers the frontend's half of the seam.)
    """

    def test_default_uses_version_16(self, monkeypatch):
        assert _drive_init_to_bench_ref(monkeypatch) == "version-16"

    def test_version_int_reaches_bench_init(self, monkeypatch):
        assert _drive_init_to_bench_ref(monkeypatch, version="16") == "version-16"

    def test_version_semver_tag_reaches_bench_init(self, monkeypatch):
        assert _drive_init_to_bench_ref(monkeypatch, version="16.26.3") == "v16.26.3"

    def test_frappe_branch_passthrough_reaches_bench_init(self, monkeypatch):
        assert _drive_init_to_bench_ref(monkeypatch, frappe_branch="develop") == "develop"


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
