"""Community integrations (core.community) against real local git repositories: the
declaration is read before anything runs and a repository without one is refused,
the reviewed commit is the one installed, updates show old and new and turn the
integration off, remove leaves nothing, and a public repository never reaches a
sign-in. Only pip is a stand-in (it would need the network to build); the real
install is the E2E proof."""

from __future__ import annotations

import dataclasses
import io
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from caffeinated_whale_cli.core import community, integrations
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind

PYPROJECT = """\
[project]
name = "cwcli-integration-hello"
version = "{version}"

[project.entry-points."cwcli.integrations"]
hello = "cwcli_hello:INTEGRATION"

[tool.cwcli.integration]
name = "Hello webhook"
publisher = "Example Org"
description = "Says hello."
provides = ["notifications"]
"""

MODULE = """\
from caffeinated_whale_cli.core.integrations import COMMUNITY, Check, Field, Integration

INTEGRATION = Integration(
    id="hello",
    name="ignored: the declaration names it",
    publisher="official",
    description="",
    provides=({provides}),
    fields=(Field("url", "Webhook URL", required=True),),
    normalize=lambda s: {{"url": str(s.get("url") or "")}},
    test=lambda s, x: Check(bool(s["url"]), "hello from {version}"),
)
"""

# pip's stand-in: lays the package and its dist-info (with the entry points) into
# --target the way pip does, so the real loader and verifier read a real layout.
FAKE_PIP = """\
import pathlib, shutil, sys, toml
args = sys.argv[1:]
target = pathlib.Path(args[args.index("--target") + 1])
src = pathlib.Path(args[-1])
project = toml.load(src / "pyproject.toml")["project"]
if project["version"] == "broken-build":
    sys.exit("ERROR: the build failed")
target.mkdir(parents=True)
for pkg in (src / "src").iterdir():
    shutil.copytree(pkg, target / pkg.name)
info = target / (project["name"].replace("-", "_") + "-" + project["version"] + ".dist-info")
info.mkdir()
(info / "METADATA").write_text(
    "Metadata-Version: 2.1\\nName: " + project["name"] + "\\nVersion: " + project["version"] + "\\n"
)
(info / "entry_points.txt").write_text("".join(
    "[" + g + "]\\n" + "".join(k + " = " + v + "\\n" for k, v in m.items())
    for g, m in project.get("entry-points", {}).items()
))
"""


def _git(cwd: Path, *args: str) -> str:
    # The fixture repos ignore this machine's git config (commit signing, hooks).
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write(repo: Path, version: str = "1", provides: str = '"notifications",') -> str:
    """Write the hello integration and commit it; return the commit."""
    (repo / "src" / "cwcli_hello").mkdir(parents=True, exist_ok=True)
    (repo / "pyproject.toml").write_text(PYPROJECT.format(version=version))
    (repo / "src" / "cwcli_hello" / "__init__.py").write_text(
        MODULE.format(version=version, provides=provides)
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"v{version}")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CWCLI_HOME", str(tmp_path / "home"))
    fake = tmp_path / "fake_pip.py"
    fake.write_text(FAKE_PIP)
    monkeypatch.setattr(community, "_pip", lambda: [sys.executable, str(fake)])
    yield tmp_path
    for integration_id in list(community._loaded):
        community._forget(integration_id)


@pytest.fixture
def repo(home):
    path = home / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    return path


def _url(path: Path) -> str:
    return path.as_uri()


class TestDeclaration:
    def test_a_repository_without_a_declaration_is_refused_before_anything_installs(
        self, home, repo
    ):
        (repo / "README.md").write_text("no integration here")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "readme")
        with pytest.raises(CwcliError) as e:
            community.preview(_url(repo))
        assert e.value.code == "community.no_declaration"
        assert "no pyproject.toml" in e.value.message
        assert not community.root().exists()
        assert community.records() == {}

    @pytest.mark.parametrize(
        ("edit", "reason"),
        [
            (lambda t: t.replace('[project.entry-points."cwcli.integrations"]', "[x]"), "has no"),
            (lambda t: t.replace("hello = ", 'other = "m:I"\nhello = '), "registers 2"),
            (lambda t: t.replace("hello = ", '"Hello!" = '), "is not 2-40"),
            (lambda t: t.replace("[tool.cwcli.integration]", "[tool.other]"), "[tool.cwcli"),
            (lambda t: t.replace('publisher = "Example Org"', ""), "needs a publisher"),
            (lambda t: t.replace('["notifications"]', '["shell"]'), "unknown hook points"),
            (lambda t: t.replace("hello = ", "ldap = "), "official integration"),
            (lambda t: t + "[[[", "cannot be read"),
        ],
    )
    def test_an_invalid_declaration_is_refused(self, home, repo, edit, reason):
        _write(repo)
        (repo / "pyproject.toml").write_text(edit(PYPROJECT.format(version="1")))
        _git(repo, "commit", "-qam", "edit")
        with pytest.raises(CwcliError) as e:
            community.preview(_url(repo))
        assert reason in e.value.message
        assert not community.root().exists()

    def test_the_preview_is_the_trust_screen_and_runs_nothing(self, home, repo):
        _write(repo)
        # Code that would stop this process if anything imported it.
        (repo / "src" / "cwcli_hello" / "__init__.py").write_text("raise SystemExit('ran')")
        _git(repo, "commit", "-qam", "would run")
        commit = _git(repo, "rev-parse", "HEAD")
        found = community.preview(_url(repo))
        assert found.commit == commit
        assert found.signed_in_with is None
        assert found.declaration == community.Declaration(
            "hello",
            "Hello webhook",
            "Example Org",
            "Says hello.",
            ("notifications",),
            "cwcli-integration-hello",
        )
        assert not community.root().exists()

    def test_a_branch_or_tag_is_resolved_to_its_commit(self, home, repo):
        first = _write(repo, version="1")
        _git(repo, "tag", "v1")
        _write(repo, version="2")
        assert community.preview(_url(repo), "v1").commit == first


class TestUrl:
    @pytest.mark.parametrize(
        "url",
        ["git@github.com:o/r.git", "ssh://git@github.com/o/r", "github.com/o/r", "-oProxy=x"],
    )
    def test_only_http_and_file_urls_are_fetched(self, url):
        with pytest.raises(CwcliError) as e:
            community.check_url(url)
        assert e.value.code == "community.url"

    def test_a_url_carrying_a_token_is_refused(self):
        with pytest.raises(CwcliError) as e:
            community.check_url("https://oauth2:glpat-secret@gitlab.com/o/r")
        assert e.value.code == "community.url_credentials"
        assert "glpat-secret" not in e.value.message

    def test_a_ref_cannot_be_an_option(self, home, repo):
        with pytest.raises(CwcliError) as e:
            community.preview(_url(repo), "--upload-pack=x")
        assert e.value.code == "community.ref"


class TestInstall:
    def test_the_reviewed_commit_installs_off_and_is_audited(self, home, repo):
        _write(repo)
        found = community.preview(_url(repo))
        rec = community.install(found.url, None, commit=found.commit, by="ana")
        assert rec["commit"] == found.commit
        assert (community.root() / rec["dir"]).is_dir()
        (card,) = [c for c in integrations.view() if c["id"] == "hello"]
        assert card["publisher"] == integrations.COMMUNITY
        assert card["name"] == "Hello webhook"
        assert card["state"] == "off" and not card["enabled"]
        assert card["community"]["commit"] == found.commit
        assert card["community"]["publisher"] == "Example Org"
        assert card["community"]["load_error"] is None
        (line,) = community.recent_audit()
        assert f"add id=hello by=ana url={found.url} ref=- commit={found.commit}" in line

    def test_a_ref_that_moved_after_review_is_refused(self, home, repo):
        _write(repo)
        reviewed = community.preview(_url(repo)).commit
        _write(repo, version="2")
        with pytest.raises(CwcliError) as e:
            community.install(_url(repo), commit=reviewed)
        assert e.value.code == "community.moved"
        assert not community.root().exists()
        assert community.records() == {}

    def test_installing_needs_the_commit_reviewed(self, home, repo):
        _write(repo)
        with pytest.raises(CwcliError) as e:
            community.install(_url(repo), commit="")
        assert e.value.code == "community.commit"

    def test_the_same_id_cannot_be_installed_twice(self, home, repo):
        commit = _write(repo)
        community.install(_url(repo), commit=commit)
        with pytest.raises(CwcliError) as e:
            community.preview(_url(repo))
        assert e.value.code == "community.installed"

    def test_a_failed_build_leaves_nothing(self, home, repo):
        commit = _write(repo, version="broken-build")
        with pytest.raises(CwcliError) as e:
            community.install(_url(repo), commit=commit)
        assert e.value.code == "community.install_failed"
        assert "the build failed" in e.value.message
        assert [p.name for p in community.root().iterdir()] == []
        assert community.records() == {}

    def test_a_package_that_does_not_provide_what_it_declares_is_removed_again(self, home, repo):
        commit = _write(repo, provides='"sign_in",')
        with pytest.raises(CwcliError) as e:
            community.install(_url(repo), commit=commit)
        assert e.value.code == "community.invalid_package"
        assert "not the declared ['notifications']" in e.value.message
        assert [p.name for p in community.root().iterdir()] == []

    def test_it_turns_on_through_its_own_test(self, home, repo):
        community.install(_url(repo), commit=_write(repo))
        check, view = integrations.configure("hello", {"url": "https://x"}, enable=True)
        assert check is not None and check.ok and check.message == "hello from 1"
        assert view["state"] == "on"


class TestUpdate:
    def test_check_then_update_shows_old_and_new_and_turns_it_off(self, home, repo):
        old = _write(repo)
        community.install(_url(repo), commit=old)
        integrations.configure("hello", {"url": "https://x"}, enable=True)
        assert integrations.test("hello").message == "hello from 1"
        assert community.check_update("hello")["update_available"] is False

        new = _write(repo, version="2")
        found = community.check_update("hello")
        assert (found["current"], found["latest"], found["update_available"]) == (old, new, True)
        assert found["declaration"]["name"] == "Hello webhook"
        done = community.update("hello", commit=new, by="ana")
        assert (done["old"], done["new"], done["was_on"]) == (old, new, True)

        (card,) = [c for c in integrations.view() if c["id"] == "hello"]
        assert card["state"] == "off"
        assert card["settings"] == {"url": "https://x"}
        # The new code, in this same process.
        assert integrations.test("hello").message == "hello from 2"
        assert [p.name for p in community.root().iterdir() if p.name != ".pip"] == [
            done["record"]["dir"]
        ]
        assert f"old_commit={old}" in community.recent_audit()[-1]

    def test_only_the_reviewed_update_installs(self, home, repo):
        community.install(_url(repo), commit=_write(repo))
        reviewed = _write(repo, version="2")
        _write(repo, version="3")
        with pytest.raises(CwcliError) as e:
            community.update("hello", commit=reviewed)
        assert e.value.code == "community.moved"
        assert integrations.test("hello").message == "hello from 1"


class TestRemove:
    def test_remove_leaves_nothing_behind(self, home, repo):
        community.install(_url(repo), commit=_write(repo))
        integrations.configure("hello", {"url": "https://x"}, enable=True)
        assert integrations.test("hello").ok
        community.remove("hello", by="ana")
        data = json.loads(integrations.store_path().read_text())
        assert data["community"] == {} and data["integrations"] == {}
        assert not community.root().parent.exists()
        assert [c["id"] for c in integrations.view()] == ["ldap", "s3"]
        assert not any("cwcli_hello" in m for m in sys.modules)
        assert "remove id=hello by=ana" in community.recent_audit()[-1]

    def test_removing_an_unknown_id_says_so(self, home):
        with pytest.raises(CwcliError) as e:
            community.remove("nope")
        assert e.value.kind is ErrorKind.NOT_FOUND


class TestLoading:
    def test_a_package_that_no_longer_loads_is_a_card_that_cannot_turn_on(self, home, repo):
        rec = community.install(_url(repo), commit=_write(repo))
        community._forget("hello")
        init = community.root() / rec["dir"] / "cwcli_hello" / "__init__.py"
        init.write_text("raise RuntimeError('broken on purpose')")
        (card,) = [c for c in integrations.view() if c["id"] == "hello"]
        assert card["community"]["load_error"] == "broken on purpose"
        check, view = integrations.configure("hello", enable=True)
        assert check is not None and not check.ok and "does not load" in check.message
        assert view["state"] != "on"
        community.remove("hello")

    def test_cwclis_own_packages_win_over_a_bundled_copy(self, home, repo):
        rec = community.install(_url(repo), commit=_write(repo))
        integrations.view()
        site = str(community.root() / rec["dir"])
        own = Path(integrations.__file__).resolve().parents[2]
        mine = [i for i, entry in enumerate(sys.path) if entry and Path(entry).resolve() == own]
        assert mine and sys.path.index(site) > mine[0]

    def test_official_hook_points_never_load_community_code(self, home, repo, monkeypatch):
        community.install(_url(repo), commit=_write(repo))
        monkeypatch.setattr(community, "load_all", lambda: pytest.fail("community code loaded"))
        assert integrations.active("ldap") is None
        assert integrations.get("s3").id == "s3"

    def test_community_callbacks_never_raise_into_cwcli(self):
        def boom(*args):
            raise ZeroDivisionError("boom")

        guarded = community._guarded(
            dataclasses.replace(
                integrations.S3_INTEGRATION, test=boom, normalize=boom, on_change=boom, notices=boom
            )
        )
        assert guarded.test({}, {}).ok is False
        with pytest.raises(CwcliError) as e:
            guarded.normalize({})
        assert e.value.kind is ErrorKind.USAGE
        assert guarded.on_change is not None and guarded.on_change() is None
        assert guarded.notices is not None and "boom" in guarded.notices()[0]


class TestSignIn:
    """The fetch is anonymous first; only an https host that asks for a sign-in
    reaches this machine's gh/glab, and a missing one is named."""

    answers: list[str] = []  # git fetch's stderr, one per attempt; "" succeeds

    @pytest.fixture
    def calls(self, monkeypatch):
        seen: list[str | None] = []

        def _fake(args, *, helper=None):
            if "fetch" not in args:
                return subprocess.CompletedProcess(args, 0, "c0ffee\n", "")
            seen.append(helper)
            err = self.answers[len(seen) - 1]
            return subprocess.CompletedProcess(args, 128 if err else 0, "", err)

        monkeypatch.setattr(community, "_git", _fake)
        return seen

    def _fetch(self, url, tmp_path):
        return community._fetch(url, None, tmp_path / "src")

    def test_a_public_repository_never_asks_for_a_sign_in(self, calls, tmp_path):
        self.answers = [""]
        assert self._fetch("https://github.com/o/r", tmp_path) == ("c0ffee", None)
        assert calls == [None]

    def test_a_private_repository_reads_with_the_hosts_sign_in(self, calls, tmp_path):
        self.answers = [
            "fatal: could not read Username for 'https://h': terminal prompts disabled",
            "",
        ]
        assert self._fetch("https://dev.example.org/g/r", tmp_path) == ("c0ffee", "glab")
        assert calls[0] is None and "core.community credential" in str(calls[1])

    @pytest.mark.parametrize(
        ("url", "fix"),
        [
            ("https://github.com/o/private", "Run: gh auth login"),
            ("https://gitlab.com/o/private", "Run: glab auth login --hostname gitlab.com"),
            ("https://dev.example.org/g/r", "Run: glab auth login --hostname dev.example.org"),
        ],
    )
    def test_a_missing_sign_in_names_the_fix(self, calls, tmp_path, url, fix):
        host = url.split("/")[2]
        self.answers = [
            f"fatal: could not read Username for 'https://{host}': terminal prompts disabled",
            f"{community._SIGN_IN_MISSING}{host}\nfatal: could not read Username",
        ]
        with pytest.raises(CwcliError) as e:
            self._fetch(url, tmp_path)
        assert e.value.code == "community.sign_in_missing"
        assert e.value.hint is not None and e.value.hint.startswith(fix)
        assert e.value.detail is not None and f"Run: {e.value.detail['command']}" == fix
        assert calls[0] is None and "community credential" in calls[1]

    def test_a_sign_in_that_cannot_read_the_repository_says_so(self, calls, tmp_path):
        self.answers = [
            "fatal: could not read Username for 'https://github.com': terminal prompts disabled",
            "remote: Repository not found.\nfatal: repository 'x' not found",
        ]
        with pytest.raises(CwcliError) as e:
            self._fetch("https://github.com/o/r", tmp_path)
        assert e.value.code == "community.unreadable"
        assert "The gh sign-in for github.com cannot read" in e.value.message

    def test_plain_http_never_reaches_a_sign_in(self, calls, tmp_path):
        self.answers = ["fatal: Authentication failed for 'http://h/r'"]
        with pytest.raises(CwcliError) as e:
            self._fetch("http://h/r", tmp_path)
        assert e.value.code == "community.fetch_failed"
        assert calls == [None]

    def test_a_missing_ref_is_not_a_sign_in_problem(self, calls, tmp_path):
        self.answers = ["fatal: couldn't find remote ref nope"]
        with pytest.raises(CwcliError) as e:
            self._fetch("https://github.com/o/r", tmp_path)
        assert "couldn't find remote ref nope" in e.value.message
        assert calls == [None]

    def test_the_helper_answers_from_the_host_tools_and_flags_a_miss(self, monkeypatch, capfd):
        request = b"protocol=https\nhost=dev.example.org\n\n"
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(request)))
        monkeypatch.setattr(community.credbridge, "host_credential", lambda req: b"")
        assert community._credential("get") == 0
        assert f"{community._SIGN_IN_MISSING}dev.example.org" in capfd.readouterr().err
        assert community._credential("store") == 0

    def test_this_machines_own_credential_helpers_are_switched_off(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            community.subprocess,
            "run",
            lambda cmd, **kw: seen.update(cmd=cmd, env=kw["env"])
            or subprocess.CompletedProcess(cmd, 0, "", ""),
        )
        community._git(["ls-remote", "https://github.com/o/r"])
        assert seen["cmd"][:5] == ["git", "-c", "credential.helper=", "-c", "core.askPass="]
        assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert seen["env"]["GIT_ASKPASS"] == ""


def test_the_verifier_runs_in_its_own_process(home, repo):
    rec = community.install(_url(repo), commit=_write(repo))
    payload = {"dir": str(community.root() / rec["dir"]), **{k: rec[k] for k in ("id", "name")}}
    payload.update(publisher="x", description="x", project=rec["project"], provides=["sign_in"])
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "caffeinated_whale_cli.core.community",
            "verify",
            json.dumps(payload),
        ],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 1
    assert "not the declared ['sign_in']" in done.stderr


def test_no_axi_verb_installs_community_code():
    from caffeinated_whale_cli.commands import axi

    names = {c.name or c.callback.__name__ for c in axi.app.registered_commands}
    names |= {g.name for g in axi.app.registered_groups}
    assert not {"integrations", "community", "integration"} & names
    source = textwrap.dedent(Path(axi.__file__).read_text())
    assert "core.community" not in source and "core import community" not in source
