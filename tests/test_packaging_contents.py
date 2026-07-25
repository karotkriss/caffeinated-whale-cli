"""What the published sdist and wheel actually contain.

The published 1.1.0 sdist on PyPI carried 102 test files, because the repository
had no `MANIFEST.in` and setuptools' default file list sweeps `tests/` in. Nobody
chose to ship them; it happened by omission. That is how a private hostname
committed to a test fixture ended up inside an artifact anyone can download -
a materially worse exposure than a public git repository, since a fixture reads
as throwaway scaffolding rather than published material.

`MANIFEST.in` now states the decision as a deny-all allow-list, so a newly added
test or fixture directory is out by construction. This file is the gate that
keeps it that way, and it runs in the fast `unit` tier so the existing `Pytest`
check blocks a regression with no extra CI workflow step.

ORDER MATTERS HERE. The negative check - "no test file is present" - passes
trivially for a package that ships nothing, and an allow-list's own failure mode
is exactly that: dropping a file the package needs. So every test below asserts
the POSITIVE first (the artifacts carry the whole package, and the sdist carries
everything the wheel does) before asserting the negative.
"""

from __future__ import annotations

import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DIST_NAME = "caffeinated_whale_cli"

# Path components that mean "this is test or development scaffolding, not
# something a user installing the tool needs". Matched against every path
# component, so a future `tests/`, `e2e/fixtures/` or `src/tests/` is caught
# wherever it is added.
SCAFFOLDING_DIRS = {
    ".claude",
    ".github",
    "docs",
    "e2e",
    "fixtures",
    "openspec",
    "scripts",
    "skills",
    "test",
    "testing",
    "tests",
}
SCAFFOLDING_FILES = {
    "AGENTS.md",
    "CLAUDE.md",
    "CONTRIBUTING.md",
    ".no-mistakes.yaml",
    "uv.lock",
}


def _build(tmp_path: Path, kind: str) -> Path:
    """Build one real artifact from the source tree and return its path.

    Deliberately a real `uv build` rather than a parse of `MANIFEST.in`: the
    thing under test is what setuptools PRODUCES, and the 1.1.0 leak came from
    a default nobody had read. `--wheel` builds straight from the source tree
    (not from the sdist), which is what makes the sdist-covers-wheel comparison
    below meaningful rather than circular.

    Setuptools reuses a `build/` directory left in the working tree, so a stale
    one from an earlier local `uv build` can carry a since-deleted file into the
    wheel and fail the sdist-covers-wheel comparison. `rm -rf build src/*.egg-info`
    clears it. CI checks out fresh, so that is a local-only nuisance, and it
    fails loudly rather than passing quietly.
    """
    if shutil.which("uv") is None:  # pragma: no cover - uv is this repo's toolchain
        pytest.fail("`uv` is not on PATH; this repo builds and tests through uv")
    out = tmp_path / kind
    subprocess.run(
        ["uv", "build", f"--{kind}", "--out-dir", str(out), str(REPO_ROOT)],
        check=True,
        capture_output=True,
    )
    # `uv build` also drops a `.gitignore` into its out-dir, so glob the suffix.
    suffix = "*.tar.gz" if kind == "sdist" else "*.whl"
    built = sorted(out.glob(suffix))
    assert len(built) == 1, f"expected one {kind}, got {built}"
    return built[0]


@pytest.fixture(scope="module")
def sdist_names(tmp_path_factory) -> list[str]:
    """Every path inside the built sdist, relative to its root directory."""
    archive = _build(tmp_path_factory.mktemp("sdist-build"), "sdist")
    with tarfile.open(archive) as tar:
        members = [m.name for m in tar.getmembers() if m.isfile()]
    # Strip the `caffeinated_whale_cli-<version>/` prefix every sdist member has.
    return sorted(name.split("/", 1)[1] for name in members if "/" in name)


@pytest.fixture(scope="module")
def wheel_names(tmp_path_factory) -> list[str]:
    """Every path inside the built wheel."""
    archive = _build(tmp_path_factory.mktemp("wheel-build"), "wheel")
    with zipfile.ZipFile(archive) as zf:
        return sorted(n for n in zf.namelist() if not n.endswith("/"))


def _scaffolding_in(names: list[str]) -> list[str]:
    hits = []
    for name in names:
        parts = Path(name).parts
        if SCAFFOLDING_DIRS.intersection(parts):
            hits.append(name)
        elif parts[-1] in SCAFFOLDING_FILES:
            hits.append(name)
        elif parts[-1].startswith("test_") and parts[-1].endswith(".py"):
            hits.append(name)
        elif parts[-1] == "conftest.py":
            hits.append(name)
    return hits


class TestTheWheelShipsThePackage:
    def test_the_wheel_carries_the_package_and_its_data(self, wheel_names):
        """POSITIVE FIRST: the wheel is genuinely the tool, not an empty box."""
        modules = [n for n in wheel_names if n.startswith(f"{DIST_NAME}/")]
        assert len(modules) > 50, f"wheel looks empty: {modules}"
        assert f"{DIST_NAME}/main.py" in modules
        assert f"{DIST_NAME}/core/rm.py" in modules
        # The one package-data file. It is served by `cwcli serve`, so a wheel
        # without it installs cleanly and then fails at runtime.
        assert f"{DIST_NAME}/commands/console.html" in modules

    def test_the_wheel_ships_no_tests_or_scaffolding(self, wheel_names):
        assert _scaffolding_in(wheel_names) == []


class TestTheSdistShipsWhatABuildNeeds:
    def test_the_sdist_carries_the_build_inputs(self, sdist_names):
        """POSITIVE FIRST: an sdist a consumer cannot build from is worthless."""
        for required in ("pyproject.toml", "README.md", "LICENSE", "PKG-INFO"):
            assert required in sdist_names, f"sdist is missing {required}"

    def test_the_sdist_carries_every_file_the_wheel_does(self, sdist_names, wheel_names):
        """The allow-list's own failure mode, guarded.

        `MANIFEST.in` denies everything and then re-includes by extension, so a
        new package-data type added to `[tool.setuptools.package-data]` but not
        to `MANIFEST.in` would be in the wheel and absent from the sdist - and
        the wheel built from that sdist during a release would silently lack it.
        The wheel here is built from the SOURCE TREE, so this comparison is a
        real check rather than a tautology.
        """
        in_wheel = {n[len(DIST_NAME) + 1 :] for n in wheel_names if n.startswith(f"{DIST_NAME}/")}
        in_sdist = {
            n[len(f"src/{DIST_NAME}/") :] for n in sdist_names if n.startswith(f"src/{DIST_NAME}/")
        }
        assert in_wheel, "no package files in the wheel; the comparison would be vacuous"
        assert in_wheel - in_sdist == set(), (
            "these package files are in the wheel but NOT in the sdist - add the "
            "pattern to MANIFEST.in: " + str(sorted(in_wheel - in_sdist))
        )

    def test_the_sdist_ships_no_tests_or_scaffolding(self, sdist_names):
        """The 1.1.0 regression itself. 102 test files used to land here."""
        assert _scaffolding_in(sdist_names) == []
