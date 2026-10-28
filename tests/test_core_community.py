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
name = "cwcli-integration-{iid}"
version = "{version}"

[project.entry-points."cwcli.integrations"]
{iid} = "cwcli_{iid}:INTEGRATION"

[tool.cwcli.integration]
name = "Hello {iid}"
publisher = "Example Org"
description = "Says hello."
provides = ["{hook}"]
"""

# A sign-in check: ``users`` names who may sign in and their role; one shared password.
SIGN_IN = """\
from caffeinated_whale_cli.core.integrations import Check, Field, Integration


def _users(settings):
    return dict(p.split("=", 1) for p in settings["users"].split(",") if "=" in p)


def _sign_in(settings, secrets, username, password):
    role = _users(settings).get(username)
    if role and password == secrets.get("password"):
        return {{"username": username, "role": role, "name": username.title()}}
    return None
"""

# A backup target in a local directory.
BACKUP_TARGET = """\
import shutil
from pathlib import Path

from caffeinated_whale_cli.core.integrations import Check, Field, Integration


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.location = str(self.root)

    def key(self, *parts):
        return "/".join(p.strip("/") for p in parts if p and p.strip("/"))

    def ensure(self):
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key):
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def put_file(self, key, path):
        shutil.copyfile(path, self._path(key))

    def put_bytes(self, key, data):
        self._path(key).write_bytes(data)

    def get_to_file(self, key, path):
        shutil.copyfile(self.root / key, path)
        return Path(path).stat().st_size

    def get_bytes(self, key):
        return (self.root / key).read_bytes()

    def delete(self, key):
        (self.root / key).unlink(missing_ok=True)

    def list(self, prefix=""):
        files = sorted(p for p in self.root.rglob("*") if p.is_file())
        keys = [p.relative_to(self.root).as_posix() for p in files]
        return [{{"key": k}} for k in keys if k.startswith(prefix)]
"""

HOOKS = {
    "sign_in": (
        SIGN_IN,
        '(Field("users", "Users", required=True), '
        'Field("password", "Password", kind="secret", required=True))',
        'lambda s: {{"users": str(s.get("users") or "")}}',
        "bool(_users(s))",
        '{{"sign_in": _sign_in}}',
    ),
    "backup_target": (
        BACKUP_TARGET,
        '(Field("path", "Directory", required=True),)',
        'lambda s: {{"path": str(s.get("path") or "")}}',
        's["path"].startswith("/")',
        '{{"backup_target": lambda s, x: Store(s["path"])}}',
    ),
}

TAIL = """
INTEGRATION = Integration(
    id="{iid}",
    name="ignored: the declaration names it",
    publisher="official",
    description="",
    provides=("{obj_hook}",),
    fields={fields},
    normalize={normalize},
    test=lambda s, x: Check({ok}, "hello from {version}"),
    hooks={hooks},
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
(info / "RECORD").write_text("".join(
    p.relative_to(target).as_posix() + ",,\\n" for p in target.rglob("*") if p.is_file()
))
for dep in project.get("dependencies", []):
    (target / dep).mkdir()
    (target / dep / "__init__.py").write_text("")
    dep_info = target / (dep + "-1.dist-info")
    dep_info.mkdir()
    (dep_info / "METADATA").write_text("Metadata-Version: 2.1\\nName: " + dep + "\\nVersion: 1\\n")
    (dep_info / "RECORD").write_text(dep + "/__init__.py,,\\n")
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


def _write(
    repo: Path,
    version: str = "1",
    *,
    hook: str = "sign_in",
    iid: str = "hello",
    obj_hook: str | None = None,
) -> str:
    """Write a throwaway integration providing ``hook`` and commit it; return the commit.
    ``obj_hook`` makes the code provide a different hook than the declaration."""
    module, fields, normalize, ok, hooks = HOOKS[hook]
    code = module.format() + TAIL.format(
        iid=iid,
        obj_hook=obj_hook or hook,
        fields=fields.format(),
        normalize=normalize.format(),
        ok=ok,
        version=version,
        hooks=hooks.format(),
    )
    (repo / "src" / f"cwcli_{iid}").mkdir(parents=True, exist_ok=True)
    (repo / "pyproject.toml").write_text(PYPROJECT.format(version=version, iid=iid, hook=hook))
    (repo / "src" / f"cwcli_{iid}" / "__init__.py").write_text(code)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"v{version}")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CWCLI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(community._TEST_ONLY_FILE_URLS, "1")
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


ANA = ({"users": "ana=admin"}, {"password": "pw"})


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
            (
                lambda t: 'tool = "x"\n' + t.replace("[tool.cwcli.integration]", "[x]"),
                "[tool.cwcli",
            ),
            (
                lambda t: t.replace("[tool.cwcli.integration]", '[tool]\ncwcli = "x"\n[x]'),
                "[tool.cwcli",
            ),
            (lambda t: t.replace('publisher = "Example Org"', ""), "needs a publisher"),
            (lambda t: t.replace('["sign_in"]', '["shell"]'), "unknown hook points"),
            (lambda t: t.replace('["sign_in"]', "[]"), "provides no hook point"),
            (lambda t: t.replace("hello = ", "ldap = "), "official integration"),
            (lambda t: t + "[[[", "cannot be read"),
        ],
    )
    def test_an_invalid_declaration_is_refused(self, home, repo, edit, reason):
        _write(repo)
        (repo / "pyproject.toml").write_text(
            edit(PYPROJECT.format(version="1", iid="hello", hook="sign_in"))
        )
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
            "Hello hello",
            "Example Org",
            "Says hello.",
            ("sign_in",),
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
        [
            "git@github.com:o/r.git",
            "ssh://git@github.com/o/r",
            "github.com/o/r",
            "-oProxy=x",
            "http://github.com/o/r",
            "file:///srv/repo",
        ],
    )
    def test_only_https_urls_are_fetched(self, url, monkeypatch):
        monkeypatch.delenv(community._TEST_ONLY_FILE_URLS, raising=False)
        with pytest.raises(CwcliError) as e:
            community.check_url(url)
        assert e.value.code == "community.url"
        assert community.check_url("https://github.com/o/r") == "https://github.com/o/r"

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
        assert card["name"] == "Hello hello"
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

    @pytest.mark.parametrize("iid", ["local", "host", "api"])
    def test_an_id_cwcli_uses_as_a_session_source_is_refused(self, home, repo, iid):
        _write(repo, iid=iid)
        with pytest.raises(CwcliError) as e:
            community.preview(_url(repo))
        assert e.value.code == "community.id_taken"

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
        commit = _write(repo, obj_hook="backup_target")
        with pytest.raises(CwcliError) as e:
            community.install(_url(repo), commit=commit)
        assert e.value.code == "community.invalid_package"
        assert "not the declared ['sign_in']" in e.value.message
        assert [p.name for p in community.root().iterdir()] == []

    def test_it_turns_on_through_its_own_test(self, home, repo):
        community.install(_url(repo), commit=_write(repo))
        check, view = integrations.configure("hello", *ANA, enable=True)
        assert check is not None and check.ok and check.message == "hello from 1"
        assert view["state"] == "on"


    def test_two_packages_that_ship_the_same_module_cannot_both_install(self, home, repo):
        community.install(_url(repo), commit=_write(repo))
        other = _second_repo(home, "other")
        _write(other, iid="other")
        (other / "src" / "cwcli_hello").mkdir()
        (other / "src" / "cwcli_hello" / "__init__.py").write_text("")
        _git(other, "add", "-A")
        _git(other, "commit", "-qm", "ships cwcli_hello too")
        found = community.preview(_url(other))
        with pytest.raises(CwcliError) as e:
            community.install(found.url, commit=found.commit)
        assert e.value.code == "community.module_taken"
        assert "cwcli_hello" in e.value.message and "'hello'" in e.value.message
        assert sorted(community.records()) == ["hello"]
        assert community.load_error("hello") is None
        assert [p.name for p in community.root().iterdir() if p.name != ".pip"] == [
            community.record("hello")["dir"]
        ]

    def test_a_dependency_both_packages_bring_is_not_a_conflict(self, home, repo):
        for path, iid in ((repo, "hello"), (_second_repo(home, "other"), "other")):
            _write(path, iid=iid)
            text = (path / "pyproject.toml").read_text()
            (path / "pyproject.toml").write_text(
                text.replace("[project.entry-points", 'dependencies = ["shared"]\n\n[project.entry-points')
            )
            _git(path, "commit", "-qam", "depends on shared")
            community.install(_url(path), commit=_git(path, "rev-parse", "HEAD"))
        assert sorted(community.records()) == ["hello", "other"]


class TestUpdate:
    def test_check_then_update_shows_old_and_new_and_turns_it_off(self, home, repo):
        old = _write(repo)
        community.install(_url(repo), commit=old)
        integrations.configure("hello", *ANA, enable=True)
        assert integrations.test("hello").message == "hello from 1"
        assert community.check_update("hello")["update_available"] is False

        new = _write(repo, version="2")
        found = community.check_update("hello")
        assert (found["current"], found["latest"], found["update_available"]) == (old, new, True)
        assert found["declaration"]["name"] == "Hello hello"
        done = community.update("hello", commit=new, by="ana")
        assert (done["old"], done["new"], done["was_on"]) == (old, new, True)

        (card,) = [c for c in integrations.view() if c["id"] == "hello"]
        assert card["state"] == "off"
        assert card["settings"] == {"users": "ana=admin"}
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
        integrations.configure("hello", *ANA, enable=True)
        assert integrations.test("hello").ok
        community.remove("hello", by="ana")
        data = json.loads(integrations.store_path().read_text())
        assert data["community"] == {} and data["integrations"] == {}
        assert not community.root().parent.exists()
        assert [c["id"] for c in integrations.view()] == ["ldap", "s3"]
        assert not any("cwcli_hello" in m for m in sys.modules)
        assert "remove id=hello by=ana" in community.recent_audit()[-1]

    def test_removing_or_updating_an_enabled_sign_in_signs_its_users_out(self, home, repo):
        from caffeinated_whale_cli.core import accounts

        community.install(_url(repo), commit=_write(repo))
        integrations.configure("hello", *ANA, enable=True)
        session = accounts.open_session("ana", remember=False, source="hello", role="admin")
        community.update("hello", commit=_write(repo, version="2"))
        assert accounts.session_account(session) is None

        integrations.configure("hello", *ANA, enable=True)
        session = accounts.open_session("ana", remember=False, source="hello", role="admin")
        community.remove("hello")
        assert accounts.session_account(session) is None

    def test_a_package_built_while_the_last_one_is_removed_keeps_its_directory(self, home, repo):
        community.install(_url(repo), commit=_write(repo))
        building = community.root() / "other-0123456789ab-abcdef"
        building.mkdir()
        community.remove("hello")
        assert building.is_dir()
        assert not (community.root() / ".pip").exists()

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

    def test_turning_off_a_sign_in_that_no_longer_loads_signs_its_users_out(self, home, repo):
        from caffeinated_whale_cli.core import accounts

        rec = community.install(_url(repo), commit=_write(repo))
        integrations.configure("hello", *ANA, enable=True)
        session = accounts.open_session("ana", remember=False, source="hello", role="admin")
        community._forget("hello")
        init = community.root() / rec["dir"] / "cwcli_hello" / "__init__.py"
        init.write_text("raise RuntimeError('broken on purpose')")
        integrations.disable("hello")
        assert accounts.session_account(session) is None

    @pytest.mark.parametrize(
        "bad",
        ['fields=({"key": "users", "label": "Users"},)', 'actions={"check": "not a function"}'],
    )
    def test_a_malformed_integration_is_a_card_and_never_breaks_the_page(self, home, repo, bad):
        rec = community.install(_url(repo), commit=_write(repo))
        community._forget("hello")
        init = community.root() / rec["dir"] / "cwcli_hello" / "__init__.py"
        init.write_text(
            init.read_text() + f"\nimport dataclasses\n"
            f"INTEGRATION = dataclasses.replace(INTEGRATION, {bad})\n"
        )
        cards = {c["id"]: c for c in integrations.view()}
        assert {"ldap", "s3", "hello"} <= set(cards)
        assert cards["hello"]["community"]["load_error"].startswith("its ")

        repo2 = _second_repo(home, "hola")
        _write(repo2, iid="hola")
        init2 = repo2 / "src" / "cwcli_hola" / "__init__.py"
        init2.write_text(
            init2.read_text() + f"\nimport dataclasses\n"
            f"INTEGRATION = dataclasses.replace(INTEGRATION, {bad})\n"
        )
        _git(repo2, "commit", "-qam", "bad")
        with pytest.raises(CwcliError) as e:
            community.install(_url(repo2), commit=_git(repo2, "rev-parse", "HEAD"))
        assert e.value.code == "community.invalid_package"

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
        monkeypatch.setattr(community, "load", lambda _id: pytest.fail("community code loaded"))
        assert integrations.active("ldap") is None
        assert integrations.get("s3").id == "s3"
        assert [c["id"] for c in integrations.view("s3")] == ["s3"]
        integrations.disable("ldap")

    def test_one_community_id_loads_only_its_own_package(self, home, repo):
        community.install(_url(repo), commit=_write(repo))
        hola = _second_repo(home, "hola")
        community.install(_url(hola), commit=_write(hola, iid="hola"))
        integrations.view()
        for integration_id in list(community._loaded):
            community._forget(integration_id)
        assert integrations.get("hello").id == "hello"
        assert [c["id"] for c in integrations.view("hello")] == ["hello"]
        assert integrations.view("nope") == []
        assert any("cwcli_hello" in m for m in sys.modules)
        assert not any("cwcli_hola" in m for m in sys.modules)
        with pytest.raises(CwcliError) as e:
            integrations.get("nope")
        assert e.value.code == "integration.unknown"

    def test_community_callbacks_never_raise_into_cwcli(self):
        def boom(*args):
            raise ZeroDivisionError("boom")

        guarded = community._guarded(
            dataclasses.replace(
                integrations.S3_INTEGRATION,
                test=boom,
                normalize=boom,
                on_change=boom,
                notices=boom,
                actions={"check": boom},
            )
        )
        with pytest.raises(CwcliError) as action:
            guarded.actions["check"]({}, {}, {})
        assert action.value.kind is ErrorKind.USAGE and "boom" in action.value.message
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


def _second_repo(home: Path, name: str) -> Path:
    path = home / name
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    return path


class TestHookPoints:
    """An enabled community integration is called through the same hook points the
    official LDAP sign-in and S3 backup target use, one provider per hook point."""

    def test_a_community_sign_in_signs_people_in_with_their_role(self, home, repo):
        from caffeinated_whale_cli.core import accounts, directory

        accounts.ensure_default()
        community.install(_url(repo), commit=_write(repo))
        with pytest.raises(CwcliError) as off:
            directory.authenticate("ana", "pw")
        assert off.value.code == "auth.invalid"  # installed but off: it signs nobody in

        integrations.configure("hello", {"users": "ana=admin,otto=operator,admin=admin"},
                               {"password": "pw"}, enable=True)  # fmt: skip
        user = directory.authenticate("otto", "pw")
        assert (user.username, user.role, user.name, user.source) == (
            "otto", "operator", "Otto", "hello",
        )  # fmt: skip
        for name, password, code in (
            ("otto", "wrong", "auth.invalid"),
            ("zed", "pw", "auth.invalid"),
            ("admin", "pw", "auth.directory_reserved"),
        ):
            with pytest.raises(CwcliError) as e:
                directory.authenticate(name, password)
            assert e.value.code == code, name

        integrations.disable("hello")
        with pytest.raises(CwcliError) as e:
            directory.authenticate("otto", "pw")
        assert e.value.code == "auth.invalid"

    def test_a_failing_sign_in_reads_as_unreachable_and_shows_on_the_card(
        self, home, repo, monkeypatch
    ):
        from caffeinated_whale_cli.core import directory

        community.install(_url(repo), commit=_write(repo))
        integrations.configure("hello", *ANA, enable=True)
        integ, settings, secrets = integrations.provider("sign_in")

        def boom(*args):
            raise OSError("the user list is gone")

        broken = dataclasses.replace(integ, hooks={"sign_in": boom})
        monkeypatch.setattr(integrations, "provider", lambda hook: (broken, settings, secrets))
        with pytest.raises(CwcliError) as e:
            directory.authenticate("ana", "pw")
        assert e.value.code == "auth.directory_unreachable"
        (card,) = [c for c in integrations.view() if c["id"] == "hello"]
        assert card["state"] == "error" and "the user list is gone" in card["last_error"]["message"]

    def test_a_role_it_cannot_confirm_without_a_password_is_said_so(self, home, repo):
        from caffeinated_whale_cli.core import directory

        community.install(_url(repo), commit=_write(repo))
        integrations.configure("hello", *ANA, enable=True)
        with pytest.raises(CwcliError) as e:
            directory.current_role("ana", "hello")
        assert e.value.code == "auth.role_unconfirmable"
        assert directory.current_role("ana", "ldap") is None  # not the sign-in that is on

    def test_one_provider_per_hook_point_and_switching_is_the_admins_choice(self, home, repo):
        community.install(_url(repo), commit=_write(repo))
        hola = _second_repo(home, "hola")
        community.install(_url(hola), commit=_write(hola, iid="hola"))
        integrations.configure("hello", *ANA, enable=True)

        with pytest.raises(CwcliError) as e:
            integrations.configure("hola", *ANA, enable=True)
        assert e.value.code == "integration.hook_taken"
        assert e.value.detail == {
            "hook": "sign_in", "current": "hello", "current_name": "Hello hello",
        }  # fmt: skip
        ldap = {
            "server_url": "ldaps://dc.example.org",
            "bind_dn": "cn=svc",
            "base_dn": "dc=example,dc=org",
            "group_roles": [{"group": "staff", "role": "viewer"}],
        }
        with pytest.raises(CwcliError) as e:  # nor does LDAP silently take it over
            integrations.configure("ldap", ldap, {"bind_password": "p"}, enable=True)
        assert e.value.code == "integration.hook_taken"
        states = {c["id"]: c["state"] for c in integrations.view()}
        assert states["hello"] == "on" and states["hola"] == "off"

        check, view = integrations.configure("hola", *ANA, enable=True, take_over=True)
        assert check is not None and check.ok and view["state"] == "on"
        states = {c["id"]: c["state"] for c in integrations.view()}
        assert states["hello"] == "off" and states["hola"] == "on"
        assert integrations.provider("sign_in")[0].id == "hola"

    def test_switching_runs_the_new_ones_test_first(self, home, repo):
        community.install(_url(repo), commit=_write(repo))
        hola = _second_repo(home, "hola")
        community.install(_url(hola), commit=_write(hola, iid="hola"))
        integrations.configure("hello", *ANA, enable=True)
        check, view = integrations.configure(
            "hola", {"users": "nobody"}, {"password": "pw"}, enable=True, take_over=True
        )
        assert check is not None and not check.ok and view["state"] != "on"
        assert integrations.provider("sign_in")[0].id == "hello"

    def test_a_community_backup_target_carries_a_set_off_site_and_back(self, home, repo):
        import shutil

        from caffeinated_whale_cli.core import backup_store as store

        archive = home / "archive"
        archive.mkdir()
        ts, site = "20261028_120000", "shop_localhost"
        import gzip

        with gzip.open(archive / f"{ts}-{site}-database.sql.gz", "wb") as f:
            f.write(b"CREATE TABLE `tabUser` (id int);\n-- Dump completed on 2026-10-28\n")
        (archive / f"{ts}-{site}-site_config_backup.json").write_text('{"db_name": "x"}')
        bset = store.adopt_dir("erp", "shop.localhost", archive)
        assert bset is not None and bset.verified

        community.install(_url(repo), commit=_write(repo, hook="backup_target"))
        assert store.offsite_target("erp", require=False) is None  # installed, still off
        offsite = home / "offsite"
        integrations.configure("hello", {"path": str(offsite)}, enable=True)
        target = store.offsite_target("erp")
        assert (target.integration_id, target.location) == ("hello", str(offsite))

        copied = store.offsite_copy(bset)
        assert copied.offsite["target"] == "hello"
        stored = sorted(p.name for p in offsite.rglob("*") if p.is_file())
        assert "set.json" in stored and all(n.endswith(".cwenc") or n == "set.json" for n in stored)
        assert [d["key"] for d in store.offsite_list("erp")] == [bset.key]

        shutil.rmtree(bset.path)
        fetched = store.offsite_fetch("erp", bset.key)
        assert fetched.verified and fetched.key == bset.key

        integrations.disable("hello")
        assert store.offsite_target("erp", require=False) is None

    def test_a_failure_inside_a_community_target_is_a_cwcli_error(self, home, repo):
        from caffeinated_whale_cli.core import backup_store as store

        community.install(_url(repo), commit=_write(repo, hook="backup_target"))
        integrations.configure("hello", {"path": str(home / "offsite")}, enable=True)
        with pytest.raises(CwcliError) as e:
            store.offsite_target("erp").get_bytes("missing/set.json")
        assert e.value.code == "offsite.target_failed" and "Hello hello" in e.value.message

    def test_a_target_whose_attributes_raise_never_breaks_a_caller(self, monkeypatch):
        from caffeinated_whale_cli.commands import serve
        from caffeinated_whale_cli.core import backup_store as store

        class Store:
            @property
            def location(self):
                raise KeyError("path")

            @property
            def bucket(self):
                raise KeyError("bucket")

        integ = dataclasses.replace(
            integrations.S3_INTEGRATION,
            id="hello",
            name="Hello",
            hooks={"backup_target": lambda settings, secrets: Store()},
        )
        monkeypatch.setattr(integrations, "provider", lambda hook: (integ, {}, {}))
        target = store.offsite_target("erp")
        assert target.location == "Hello"
        with pytest.raises(CwcliError) as e:
            target.bucket
        assert e.value.code == "offsite.target_failed"
        assert serve._offsite_summary() == {"id": "hello", "name": "Hello", "location": "Hello"}

    def test_a_target_that_is_on_but_fails_to_open_is_not_reported_as_none(self, monkeypatch):
        from caffeinated_whale_cli.commands import serve

        def boom(settings, secrets):
            raise KeyError("token")

        integ = dataclasses.replace(
            integrations.S3_INTEGRATION, id="hello", name="Hello", hooks={"backup_target": boom}
        )
        monkeypatch.setattr(integrations, "provider", lambda hook: (integ, {}, {}))
        summary = serve._offsite_summary()
        assert summary is not None
        assert (summary["id"], summary["name"]) == ("hello", "Hello")
        assert "could not open its backup target" in summary["error"]
        monkeypatch.setattr(integrations, "provider", lambda hook: None)
        assert serve._offsite_summary() is None

    def test_a_target_whose_secret_cannot_be_read_is_reported_not_raised(self, home):
        from caffeinated_whale_cli.commands import serve

        integrations.configure(
            "s3",
            {"endpoint": "https://s3.example.org", "bucket": "b", "access_key": "k"},
            {"secret_key": "s"},
        )
        data = json.loads(integrations.store_path().read_text())
        data["integrations"]["s3"]["enabled"] = True
        integrations.store_path().write_text(json.dumps(data))
        integrations.key_path().write_bytes(os.urandom(32))
        summary = serve._offsite_summary()
        assert summary is not None and summary["error"]
        assert summary["name"] == "The backup target"


class TestCliShowsWhatIsDeclared:
    """``cwcli integrations`` prints community text and cwcli's own messages verbatim:
    a declaration is never read as Rich markup."""

    def _run(self, *args):
        from typer.testing import CliRunner

        from caffeinated_whale_cli.commands import integrations as cmd

        return CliRunner().invoke(cmd.app, list(args))

    def test_markup_in_a_declaration_is_shown_as_written(self, home, repo):
        _write(repo)
        text = (repo / "pyproject.toml").read_text()
        text = text.replace('name = "Hello hello"', 'name = "Hello [/x] [bold green]hi"')
        text = text.replace(
            'publisher = "Example Org"', 'publisher = "[link=https://evil.example]Frappe[/link]"'
        )
        (repo / "pyproject.toml").write_text(text)
        _git(repo, "commit", "-qam", "markup")
        done = self._run("add", _url(repo), "--yes")
        assert done.exit_code == 0, done.output
        assert "Hello [/x] [bold green]hi" in done.output
        assert "[link=https://evil.example]Frappe[/link]" in done.output
        listed = self._run("list")
        assert "Hello [/x] [bold green]hi" in listed.output
        assert "[link=https://evil.example]Frappe[/link]" in listed.output

    def test_an_error_naming_a_toml_table_keeps_it(self, home, repo):
        _write(repo)
        text = (repo / "pyproject.toml").read_text()
        (repo / "pyproject.toml").write_text(
            text.replace('[project.entry-points."cwcli.integrations"]', "[x]")
        )
        _git(repo, "commit", "-qam", "no entry points")
        done = self._run("add", _url(repo), "--yes")
        assert done.exit_code == 1
        assert '[project.entry-points."cwcli.integrations"]' in done.output


def test_the_verifier_runs_in_its_own_process(home, repo):
    rec = community.install(_url(repo), commit=_write(repo))
    payload = {"dir": str(community.root() / rec["dir"]), **{k: rec[k] for k in ("id", "name")}}
    payload.update(
        publisher="x", description="x", project=rec["project"], provides=["backup_target"]
    )
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
    assert "not the declared ['backup_target']" in done.stderr


def test_no_axi_verb_installs_community_code():
    from caffeinated_whale_cli.commands import axi

    names = {c.name or c.callback.__name__ for c in axi.app.registered_commands}
    names |= {g.name for g in axi.app.registered_groups}
    assert not {"integrations", "community", "integration"} & names
