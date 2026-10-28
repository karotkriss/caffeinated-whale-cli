"""Community integrations through the real binary and a real pip build.

The binary is ``harness.CWCLI``: in CI's ``e2e_pkg`` job that is the runtime-only
``uv tool install .`` build, which has no pip of its own, exactly like a user's uv
tool install - so this proves cwcli makes its private pip venv, builds the package
from the repository with the real build backend, loads it, signs a person in to the
real web UI daemon through its ``sign_in`` hook once an Admin turns it on (and stops
once it is off), updates it and removes it. The repository is a local git repo
(``file://``); fetching from GitHub and GitLab with a sign-in is unit-tested in
``tests/test_core_community.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.e2e import harness

pytestmark = pytest.mark.e2e_pkg

PYPROJECT = """\
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "cwcli-integration-hello"
version = "0.1.0"
requires-python = ">=3.10"

[project.entry-points."cwcli.integrations"]
hello = "cwcli_hello:INTEGRATION"

[tool.cwcli.integration]
name = "Hello sign-in"
publisher = "cwcli E2E"
description = "Lets the listed users sign in as Viewers."
provides = ["sign_in"]
"""

MODULE = """\
from caffeinated_whale_cli.core.integrations import COMMUNITY, Check, Field, Integration


def _sign_in(settings, secrets, username, password):
    if username in settings["users"].split(",") and password == secrets.get("password"):
        return {{"username": username, "role": "viewer"}}
    return None


INTEGRATION = Integration(
    id="hello",
    name="Hello sign-in",
    publisher=COMMUNITY,
    description="Lets the listed users sign in as Viewers.",
    provides=("sign_in",),
    fields=(
        Field("users", "Users {version}", required=True),
        Field("password", "Password", kind="secret", required=True),
    ),
    normalize=lambda s: {{"users": str(s.get("users") or "")}},
    test=lambda s, x: Check(bool(s["users"]), "hello"),
    hooks={{"sign_in": _sign_in}},
)
"""


# No Docker, no instance and no ~/.cwcli: CWCLI_HOME is pinned to tmp_path. The
# e2e conftest's autouse Docker gate, isolated HOME and cwe2e- sweep are shadowed
# so a Docker-less runner still runs this (the test_version_build_id_e2e pattern).
@pytest.fixture(scope="module", autouse=True)
def _docker_gate():
    yield


@pytest.fixture(scope="module", autouse=True)
def isolated_home():
    yield


@pytest.fixture(scope="module", autouse=True)
def _teardown_backstop():
    yield


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repo: Path, version: str) -> str:
    pkg = repo / "src" / "cwcli_hello"
    pkg.mkdir(parents=True, exist_ok=True)
    (repo / "pyproject.toml").write_text(PYPROJECT)
    (pkg / "__init__.py").write_text(MODULE.format(version=version))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"v{version}")
    return _git(repo, "rev-parse", "HEAD")


TOKEN = "cwe2e-community-token"


def _signs_in_through_the_hook(env: dict) -> None:
    """The real daemon: the community sign-in signs a person in only while it is on."""
    with harness.ui_daemon(TOKEN, env) as base:
        vic = {"username": "vic", "password": "vic-pw"}
        assert harness.api_call(base, "/api/v1/session", vic)[0] == 401  # installed, off
        configure = {
            "op": "configure",
            "id": "hello",
            "settings": {"users": "vic"},
            "secrets": {"password": "vic-pw"},
            "enable": True,
        }
        status, body = harness.api_call(base, "/api/v1/integrations", configure, token=TOKEN)
        assert status == 200 and body["data"]["integration"]["state"] == "on", body
        assert harness.api_call(base, "/api/v1/meta")[1]["directory_sign_in"] is True
        status, body = harness.api_call(base, "/api/v1/session", vic)
        assert status == 200, body
        assert (body["data"]["username"], body["data"]["role"], body["data"]["source"]) == (
            "vic", "viewer", "hello",
        )  # fmt: skip
        off = {"op": "disable", "id": "hello"}
        assert harness.api_call(base, "/api/v1/integrations", off, token=TOKEN)[0] == 200
        assert harness.api_call(base, "/api/v1/session", vic)[0] == 401


def test_add_update_and_remove_a_community_integration(tmp_path):
    home = tmp_path / "home"
    env = {
        **os.environ,
        "CWCLI_HOME": str(home),
        "CWCLI_NO_UPDATE_CHECK": "1",
        "CWCLI_TEST_ONLY_ALLOW_FILE_REPOSITORY_URLS": "1",
    }

    def cwcli(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [harness.CWCLI, "integrations", *args],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=900,
        )

    def card() -> dict:
        listing = cwcli("list", "--json")
        assert listing.returncode == 0, listing.stderr
        (found,) = [c for c in json.loads(listing.stdout) if c["id"] == "hello"]
        return found

    plain = tmp_path / "plain"
    plain.mkdir()
    _git(plain, "init", "-q", "-b", "main")
    (plain / "README.md").write_text("not an integration")
    _git(plain, "add", "-A")
    _git(plain, "commit", "-qm", "readme")
    refused = cwcli("add", plain.as_uri(), "--yes")
    assert refused.returncode == 1
    assert "not a cwcli community integration" in refused.stderr
    assert not (home / "integrations").exists()

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    first = _commit(repo, "1")

    unconfirmed = cwcli("add", repo.as_uri())
    assert unconfirmed.returncode == 1, unconfirmed.stdout
    assert "Pass --yes" in unconfirmed.stderr
    assert not (home / "integrations").exists()

    added = cwcli("add", repo.as_uri(), "--yes")
    assert added.returncode == 0, added.stderr
    assert first in added.stdout and "read anonymously" in added.stdout
    installed = card()
    assert installed["publisher"] == "community" and installed["state"] == "off"
    assert installed["community"]["commit"] == first
    assert installed["community"]["load_error"] is None
    # The package's own code loaded: its settings field came from the built package.
    assert [f["label"] for f in installed["fields"]] == ["Users 1", "Password"]
    _signs_in_through_the_hook(env)

    second = _commit(repo, "2")
    check = cwcli("update", "hello", "--check")
    assert check.returncode == 0, check.stderr
    assert first in check.stdout and second in check.stdout
    updated = cwcli("update", "hello", "--yes")
    assert updated.returncode == 0, updated.stderr
    assert f"{first[:12]} -> {second[:12]}" in updated.stdout
    assert card()["community"]["commit"] == second
    assert [f["label"] for f in card()["fields"]] == ["Users 2", "Password"]

    removed = cwcli("remove", "hello", "--yes")
    assert removed.returncode == 0, removed.stderr
    assert not (home / "integrations").exists()
    store = json.loads((home / "integrations.json").read_text())
    assert store["community"] == {} and store["integrations"] == {}
    audit = (home / "integrations-audit.log").read_text().splitlines()
    assert [line.split()[2] for line in audit] == ["add", "update", "remove"]
