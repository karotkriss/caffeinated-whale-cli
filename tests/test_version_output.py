"""``cwcli --version`` - the rendered build-identification line.

A bare release number cannot tell a published artifact from a working-tree
build, which is how a probe of a released cwcli once concluded a merged feature
did not exist. These cases pin the RENDERED shape; ``TestBuildInfo`` in
``tests/test_core_version.py`` pins the detection behind it, and
``tests/e2e/test_version_build_id_e2e.py`` proves both real install shapes.
"""

import re

from typer.testing import CliRunner

from caffeinated_whale_cli import main as main_mod
from caffeinated_whale_cli.core.version import BuildInfo

runner = CliRunner()


def _render(monkeypatch, build: BuildInfo) -> str:
    import caffeinated_whale_cli.core.version as core_version

    monkeypatch.setattr(core_version, "build_info", lambda: build)
    return main_mod._build_suffix()


class TestBuildSuffix:
    def test_release(self, monkeypatch):
        assert _render(monkeypatch, BuildInfo(source="release")) == "(release build)"

    def test_standalone(self, monkeypatch):
        assert _render(monkeypatch, BuildInfo(source="standalone")) == "(standalone build)"

    def test_source_clean(self, monkeypatch):
        build = BuildInfo(source="source", commit="618dfa5", dirty=False)
        assert _render(monkeypatch, build) == "(source build, git 618dfa5)"

    def test_source_dirty(self, monkeypatch):
        build = BuildInfo(source="source", commit="618dfa5", dirty=True)
        assert _render(monkeypatch, build) == "(source build, git 618dfa5, dirty)"

    def test_editable_without_a_readable_tree(self, monkeypatch):
        """A deleted/non-git source tree still identifies as a source build."""
        build = BuildInfo(source="source", editable=True)
        assert _render(monkeypatch, build) == "(editable source build, git unknown)"

    def test_a_failing_probe_never_breaks_version(self, monkeypatch):
        import caffeinated_whale_cli.core.version as core_version

        def boom():
            raise RuntimeError("no metadata")

        monkeypatch.setattr(core_version, "build_info", boom)
        assert main_mod._build_suffix() == "(build unknown)"


class TestVersionLine:
    def test_prefix_is_unchanged_and_parseable(self):
        """The ``Version: <pep440>`` prefix must stay stable for existing parsers."""
        result = runner.invoke(main_mod.app, ["--version"])
        assert result.exit_code == 0, result.output
        line = result.output.strip()
        match = re.match(r"^Caffeinated Whale CLI Version: (\S+) \((.+)\)$", line)
        assert match, f"unexpected --version shape: {line!r}"
        version, build = match.groups()
        assert version == main_mod.__version__
        assert build, "the build identification must never render empty"
