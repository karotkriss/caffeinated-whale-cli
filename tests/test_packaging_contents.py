"""What the published sdist and wheel actually contain.

The published 1.1.0 sdist on PyPI carried 102 test files, because the repository
had no `MANIFEST.in` and setuptools' default file list sweeps `tests/` in. Nobody
chose to ship them; it happened by omission. That is how a private hostname
committed to a test fixture ended up inside an artifact anyone can download -
a materially worse exposure than a public git repository, since a fixture reads
as throwaway scaffolding rather than published material.

`MANIFEST.in` now states the decision as a deny-all allow-list, so a newly added
test or fixture directory is out by construction. This file is the gate that
keeps it that way. It carries the `packaging` marker, off the default and
`-m unit` selections because the wheel must carry the built web UI and a fresh
clone has no Node build; CI's `Pytest` job builds the UI and runs
`-m packaging`, so a regression still blocks the merge.

The artifact checks are allow-lists too. A deny-list here would repeat the same
mistake one level up: a newly added source package such as `src/qa/` could enter
the wheel through setuptools auto-discovery without matching a known
scaffolding name, then remain invisible to the wheel-to-sdist comparison because
that comparison considers only `caffeinated_whale_cli/`.

ORDER MATTERS HERE. The negative check - "no test file is present" - passes
trivially for a package that ships nothing, and an allow-list's own failure mode
is exactly that: dropping a file the package needs. Each artifact therefore has
one test that asserts the POSITIVE first (the artifact carries the whole package,
and the sdist carries everything the wheel does) before asserting the negative.
"""

from __future__ import annotations

import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from tests.test_no_private_refs import ARTIFACT_PATTERN, find_private_refs

pytestmark = pytest.mark.packaging

REPO_ROOT = Path(__file__).resolve().parent.parent
DIST_NAME = "caffeinated_whale_cli"


def _build(tmp_path: Path, kind: str) -> Path:
    """Build one real artifact from the source tree and return its path.

    Deliberately a real `uv build` rather than a parse of `MANIFEST.in`: the
    thing under test is what setuptools PRODUCES, and the 1.1.0 leak came from
    a default nobody had read. `--wheel` builds straight from the source tree
    (not from the sdist), which is what makes the sdist-covers-wheel comparison
    below meaningful rather than circular.

    Setuptools reuses a `build/` directory left in the working tree, so a stale
    one from an earlier local `uv build` carries a since-deleted file (a
    content-hashed web UI asset from an older `npm run build`) into the wheel
    and fails the sdist-covers-wheel comparison. It is git-ignored and purely a
    setuptools scratch dir, so it is cleared here before each build rather than
    left as a local-only failure to diagnose.
    """
    if shutil.which("uv") is None:  # pragma: no cover - uv is this repo's toolchain
        pytest.fail("`uv` is not on PATH; this repo builds and tests through uv")
    shutil.rmtree(REPO_ROOT / "build", ignore_errors=True)
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
def sdist_archive(tmp_path_factory) -> Path:
    return _build(tmp_path_factory.mktemp("sdist-build"), "sdist")


@pytest.fixture(scope="module")
def wheel_archive(tmp_path_factory) -> Path:
    return _build(tmp_path_factory.mktemp("wheel-build"), "wheel")


@pytest.fixture(scope="module")
def sdist_names(sdist_archive) -> list[str]:
    """Every path inside the built sdist, relative to its root directory."""
    with tarfile.open(sdist_archive) as tar:
        members = [m.name for m in tar.getmembers() if m.isfile()]
    # Strip the `caffeinated_whale_cli-<version>/` prefix every sdist member has.
    return sorted(name.split("/", 1)[1] for name in members if "/" in name)


@pytest.fixture(scope="module")
def wheel_names(wheel_archive) -> list[str]:
    """Every path inside the built wheel."""
    with zipfile.ZipFile(wheel_archive) as zf:
        return sorted(n for n in zf.namelist() if not n.endswith("/"))


class TestTheWheelShipsThePackage:
    def test_the_wheel_contains_only_the_package_and_metadata(self, wheel_names):
        """Assert the useful contents before enforcing the artifact allow-list."""
        modules = [n for n in wheel_names if n.startswith(f"{DIST_NAME}/")]
        assert len(modules) > 50, f"wheel looks empty: {modules}"
        assert f"{DIST_NAME}/main.py" in modules
        assert f"{DIST_NAME}/core/rm.py" in modules
        # The package-data tree: the built web UI `cwcli ui` serves. A wheel
        # without it installs cleanly and then serves the "not built" notice, so
        # the build step (`cd webui && npm run build`) must have run before this
        # test; CI's test job does so, and so must a local release build.
        assert f"{DIST_NAME}/webui/dist/index.html" in modules, (
            "webui/dist is missing from the wheel: run `cd webui && npm ci && "
            "npm run build` before building"
        )
        assert any(n.startswith(f"{DIST_NAME}/webui/dist/assets/") for n in modules)
        assert f"{DIST_NAME}/webui/dist/csp.json" in modules

        unexpected = []
        for name in wheel_names:
            root = name.split("/", 1)[0]
            package_file = name.startswith(f"{DIST_NAME}/")
            metadata_file = (
                root.startswith(f"{DIST_NAME}-") and root.endswith(".dist-info") and "/" in name
            )
            if not package_file and not metadata_file:
                unexpected.append(name)
        assert unexpected == [], f"wheel contains paths outside its allow-list: {unexpected}"


class TestTheSdistShipsWhatABuildNeeds:
    def test_the_sdist_contains_only_build_inputs_package_and_metadata(
        self, sdist_names, wheel_names
    ):
        """Assert complete build contents before enforcing the artifact allow-list.

        `MANIFEST.in` denies everything and then re-includes by extension, so a
        new package-data type added to `[tool.setuptools.package-data]` but not
        to `MANIFEST.in` would be in the wheel and absent from the sdist. The
        wheel built from that sdist during a release would silently lack it.
        The wheel here is built from the SOURCE TREE, so this comparison is a
        real check rather than a tautology.
        """
        for required in ("pyproject.toml", "README.md", "LICENSE", "PKG-INFO"):
            assert required in sdist_names, f"sdist is missing {required}"

        in_wheel = {n[len(DIST_NAME) + 1 :] for n in wheel_names if n.startswith(f"{DIST_NAME}/")}
        in_sdist = {
            n[len(f"src/{DIST_NAME}/") :] for n in sdist_names if n.startswith(f"src/{DIST_NAME}/")
        }
        assert in_wheel, "no package files in the wheel; the comparison would be vacuous"
        assert in_wheel - in_sdist == set(), (
            "these package files are in the wheel but NOT in the sdist - add the "
            "pattern to MANIFEST.in: " + str(sorted(in_wheel - in_sdist))
        )

        permitted_files = {"pyproject.toml", "README.md", "LICENSE", "PKG-INFO", "setup.cfg"}
        unexpected = [
            name
            for name in sdist_names
            if name not in permitted_files
            and not name.startswith(f"src/{DIST_NAME}/")
            and not name.startswith(f"src/{DIST_NAME}.egg-info/")
        ]
        assert unexpected == [], f"sdist contains paths outside its allow-list: {unexpected}"


def test_no_artifact_names_private_infrastructure(sdist_archive, wheel_archive):
    """The published files, README-as-package-page and built web UI included."""
    hits: list[str] = []
    with tarfile.open(sdist_archive) as tar:
        members = [m for m in tar.getmembers() if m.isfile()]
        assert members, "sdist is empty; the scan would be vacuous"
        for m in members:
            data = tar.extractfile(m).read()  # type: ignore[union-attr]
            hits += find_private_refs(f"sdist/{m.name}", data, ARTIFACT_PATTERN)
    with zipfile.ZipFile(wheel_archive) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        assert names, "wheel is empty; the scan would be vacuous"
        for n in names:
            hits += find_private_refs(f"wheel/{n}", zf.read(n), ARTIFACT_PATTERN)
    assert not hits, "replace with a neutral placeholder:\n" + "\n".join(hits)
