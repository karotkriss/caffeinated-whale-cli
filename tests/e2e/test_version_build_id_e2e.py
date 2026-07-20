"""Packaging E2E: ``cwcli --version`` must identify the BUILD, not just the release.

The defect this guards is a packaging/install-shape problem, so a test that runs
only in the dev environment proves nothing: the dev checkout is editable, and the
whole question is what a NON-editable, index-resolved artifact prints. On
2026-07-19 someone probed a published 1.0.0 for ``apps checkout`` and the
credential bridge, saw neither, and concluded the features did not exist - they
were merged and sitting at the tip of ``develop``. That wrong conclusion was
written into a downstream consumer's standing instructions as a permanent
workaround for a problem that was not real.

So this builds BOTH install shapes for real and compares them:

* **release** - ``uv tool install --find-links <dist> caffeinated-whale-cli``.
  Resolving by NAME (not by path) is an index-style resolution, so pip/uv record
  NO PEP 610 ``direct_url.json`` - byte-identical provenance to a real PyPI
  install, but offline and carrying the code under test. Asserted, not assumed:
  the test fails if a ``direct_url.json`` shows up, because then the shape is
  wrong and the comparison would be meaningless.
* **source** - ``uv tool install .``, a real non-editable wheel built from this
  working tree, which DOES record a direct URL.

Both are ``uv tool install`` (the shape ``README.md`` prescribes) resolving
runtime dependencies only - never ``uv run``, never ``uv sync --all-extras``,
never an editable install. The runtime-only property is asserted too (``click``
present, ``pytest`` absent), matching the ``Clean install smoke`` job's guard.

Every assertion states the POSITIVE first - each binary ran, exited 0, and
printed real build identification - before the two are compared, so the test
cannot pass vacuously on two empty outputs.

Marked ``e2e_pkg`` (with ``test_pkg_lifecycle_e2e.py``) because it is a
packaging-realism leg, not a per-Frappe-version one. It needs no Docker and no
running instance. Run locally with::

    uv run pytest tests/e2e/test_version_build_id_e2e.py -m e2e_pkg -o addopts=""
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e_pkg

REPO_ROOT = Path(__file__).resolve().parents[2]
VERSION_LINE = re.compile(r"^Caffeinated Whale CLI Version: (\S+) \((.+)\)$")


# This leg is packaging-realism, not Docker-realism: it builds a wheel and does
# two `uv tool install`s, and touches no daemon, no instance, and no ~/.cwcli
# (both installs are pinned into tmp_path via UV_TOOL_DIR/UV_TOOL_BIN_DIR). The
# e2e conftest's session-autouse Docker gate, isolated HOME, and cwe2e- sweep
# would otherwise skip it wherever no daemon is reachable - hiding a packaging
# regression behind an unrelated infrastructure condition. Same-named fixtures
# here shadow the conftest's, which is how a test opts out of an autouse gate.
@pytest.fixture(scope="module", autouse=True)
def _docker_gate():
    yield


@pytest.fixture(scope="module", autouse=True)
def isolated_home():
    yield


@pytest.fixture(scope="module", autouse=True)
def _teardown_backstop():
    yield


def _uv_tool_install(*args: str, root: Path, name: str) -> tuple[Path, Path]:
    """``uv tool install`` into a throwaway tool dir; returns (cwcli path, tool dir)."""
    tool_dir, bin_dir = root / name, root / f"{name}-bin"
    env = {
        **os.environ,
        "UV_TOOL_DIR": str(tool_dir),
        "UV_TOOL_BIN_DIR": str(bin_dir),
    }
    done = subprocess.run(
        ["uv", "tool", "install", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert done.returncode == 0, f"uv tool install {args} failed:\n{done.stdout}{done.stderr}"
    cwcli = bin_dir / "cwcli"
    assert cwcli.exists(), f"no cwcli console script at {cwcli}"
    return cwcli, tool_dir


def _version_line(cwcli: Path) -> tuple[str, str]:
    """Run ``cwcli --version``, asserting it genuinely ran; returns (version, build)."""
    done = subprocess.run([str(cwcli), "--version"], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, f"cwcli --version exited {done.returncode}:\n{done.stderr}"
    line = done.stdout.strip()
    match = VERSION_LINE.match(line)
    assert match, f"cwcli --version printed no build identification: {line!r}"
    version, build = match.group(1), match.group(2)
    assert version, f"empty version in {line!r}"
    assert build, f"empty build identification in {line!r}"
    return version, build


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is required to build both shapes")
def test_release_and_source_builds_are_distinguishable(tmp_path):
    """The two install shapes must each self-identify, and must not read alike."""
    dist = tmp_path / "dist"
    built = subprocess.run(
        ["uv", "build", "--wheel", "-o", str(dist)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert built.returncode == 0, f"uv build failed:\n{built.stdout}{built.stderr}"
    assert list(dist.glob("*.whl")), "uv build produced no wheel"

    # --- shape 1: the published-artifact shape -----------------------------
    # By NAME + --find-links: an index-style resolution, so no direct_url.json,
    # exactly as a `uv tool install caffeinated-whale-cli` from PyPI. The local
    # wheel wins over PyPI's same-version release, so this carries our code.
    release_cli, release_dir = _uv_tool_install(
        "--find-links", str(dist), "caffeinated-whale-cli", root=tmp_path, name="release"
    )
    assert not list(release_dir.rglob("direct_url.json")), (
        "the 'release' shape recorded a PEP 610 direct_url.json, so it is NOT an "
        "index-resolved install and this comparison would prove nothing"
    )

    # Runtime deps only - the same property `Clean install smoke` guards.
    site = next(release_dir.glob("caffeinated-whale-cli/lib/python*/site-packages"))
    assert (site / "click").exists(), "runtime dep `click` missing from the release install"
    assert not list(site.glob("pytest*")), "dev dep `pytest` leaked into the release install"

    # --- shape 2: a real build from this working tree ----------------------
    source_cli, source_dir = _uv_tool_install(".", root=tmp_path, name="source")
    direct_urls = list(source_dir.rglob("direct_url.json"))
    assert direct_urls, "the working-tree install recorded no direct_url.json"
    assert (
        not json.loads(direct_urls[0].read_text()).get("dir_info", {}).get("editable")
    ), "the working-tree install is editable; this leg must build a real wheel"

    # --- assert the positive for each, THEN that they differ ---------------
    release_version, release_build = _version_line(release_cli)
    source_version, source_build = _version_line(source_cli)

    assert (
        release_build == "release build"
    ), f"an index-resolved install must identify as a release build, got {release_build!r}"
    assert source_build.startswith(
        "source build"
    ), f"a working-tree install must identify as a source build, got {source_build!r}"
    # A source build must name the commit, which is the whole point: the release
    # number alone is what misled the probe.
    assert re.search(
        r"git [0-9a-f]{7,}", source_build
    ), f"a source build must carry its commit sha, got {source_build!r}"

    assert release_version == source_version, (
        "both shapes were built from the same tree, so the release NUMBER should "
        f"match - that it does is the defect: {release_version} vs {source_version}"
    )
    assert release_build != source_build, (
        "the two install shapes are indistinguishable:\n"
        f"  release: {release_version} ({release_build})\n"
        f"  source:  {source_version} ({source_build})"
    )
