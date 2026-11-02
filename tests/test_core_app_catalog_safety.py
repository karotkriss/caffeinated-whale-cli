"""Credential scoping, redirect, scheme and path guards of the app catalog and its
siblings (advisories, git_refs, updates) - each pins a review finding."""

from __future__ import annotations

import io
import subprocess
import urllib.request
from email.message import Message

from caffeinated_whale_cli.core import advisories as adv
from caffeinated_whale_cli.core import app_catalog as cat
from caffeinated_whale_cli.core import credbridge, git_refs


def _fake_tools(monkeypatch, gitlab_hosts):
    asked = []

    def run(argv, input=None, **_kw):
        asked.append((argv[0], input.decode()))
        return subprocess.CompletedProcess(argv, 0, stdout=b"password=tok\n")

    # The host tools run through credbridge.run_host_tool (the neutral-cwd seam) after
    # the shared credential, which is off outside shared mode.
    monkeypatch.setattr(credbridge.subprocess, "run", run)
    monkeypatch.setattr(cat, "gitlab_hosts", lambda: gitlab_hosts)
    return asked


def test_glab_is_asked_only_for_hosts_it_is_configured_for(monkeypatch):
    asked = _fake_tools(monkeypatch, ["gitlab.com", "git.example.org"])
    assert cat.credential("codeberg.org") is None
    assert cat.credential("evilgithub.com") is None
    assert cat.credential("git.example.org") == "tok"
    assert cat.credential("github.com") == "tok"
    assert [tool for tool, _ in asked] == ["glab", "gh"]


def test_git_refs_scopes_the_glab_helper_to_configured_hosts(monkeypatch):
    monkeypatch.setattr(git_refs.shutil, "which", lambda t: f"/usr/bin/{t}")
    monkeypatch.setattr(git_refs, "gitlab_hosts", lambda: ["gitlab.com"])
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    env = git_refs._git_env()
    keys = {env[f"GIT_CONFIG_KEY_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))}
    assert keys == {"credential.https://github.com.helper", "credential.https://gitlab.com.helper"}


def _redirect(src: str, dst: str) -> urllib.request.Request | None:
    req = urllib.request.Request(src, headers={"Authorization": "Bearer t", "PRIVATE-TOKEN": "t"})
    return cat._SameHostAuthRedirect().redirect_request(
        req, io.BytesIO(), 302, "Found", Message(), dst  # type: ignore[arg-type]
    )


def test_auth_headers_never_follow_a_redirect_to_another_host():
    other = _redirect("https://gitlab.com/api/v4/x", "https://storage.example.net/blob")
    assert other is not None
    assert not other.has_header("Authorization") and not other.has_header("Private-token")
    same = _redirect("https://gitlab.com/api/v4/x", "https://gitlab.com/api/v4/y")
    assert same is not None and same.has_header("Authorization")


def test_a_graphql_answer_without_data_is_an_error_not_an_empty_listing(monkeypatch):
    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        cat._OPENER, "open", lambda *a, **k: _Resp(b'{"data": null, "errors": [{"m": "x"}]}')
    )
    monkeypatch.setattr(cat, "credential", lambda host: "tok")
    monkeypatch.setattr(cat, "fetch_host_icon", lambda host: None)
    source, apps = cat.scan_github({})
    assert source.error and apps == {}


def test_public_fetch_refuses_non_http_schemes(tmp_path):
    secret = tmp_path / "x.png"
    secret.write_bytes(b"\x89PNG secret")
    assert cat._fetch_public(secret.as_uri()) is None


def test_host_bench_dir_refuses_an_escaping_project_name(monkeypatch, tmp_path):
    monkeypatch.setenv("CWCLI_HOME", str(tmp_path / "home"))
    (tmp_path / "home" / "apps").mkdir(parents=True)
    assert cat._host_bench_dir("..", "/workspace/home") is None
    assert cat._host_bench_dir("p", "/workspace/..") is None


def test_an_installed_only_official_app_carries_the_badge():
    installed = {
        "github.com/frappe/frappe": {"name": "frappe", "installs": [], "logo": {"file": "a"}},
        "github.com/acme/thing": {"name": "thing", "installs": [], "logo": {"file": "b"}},
    }
    apps = {a.key: a for a in cat._merge({}, {}, installed)}
    assert apps["github.com/frappe/frappe"].official
    assert not apps["github.com/acme/thing"].official


def test_stored_remotes_carry_no_credentials_and_ssh_upstream_is_not_a_fork():
    assert cat._safe_remote_url("https://u:tok@github.com/frappe/erpnext.git") == (
        "https://github.com/frappe/erpnext"
    )
    assert adv._without_credentials("https://u:tok@gitlab.example.com:8443/grp/app.git") == (
        "https://gitlab.example.com:8443/grp/app.git"
    )
    for ssh in ("git@gitlab.example.com:grp/app.git", "ssh://git@host:2222/grp/app.git"):
        assert adv._without_credentials(ssh) == ssh
    assert not adv._is_fork("erpnext", "git@github.com:frappe/erpnext.git")
    assert adv._is_fork("erpnext", "https://github.com/acme/erpnext")


def test_pins_unreadable_on_both_files_are_unknown_not_empty():
    class _F:
        def get(self, url, **_kw):
            return 404, None, False

    assert adv._pins(_F(), {}, "https://github.com/acme/private", "version-15") is None  # type: ignore[arg-type]


def test_an_unscanned_key_on_any_signed_in_gitlab_host_is_gitlab(monkeypatch):
    """Self-hosted GitLab is recognised by glab's configured hosts, never by a name."""
    monkeypatch.setattr(cat, "gitlab_hosts", lambda: ["git.example.org"])
    assert cat._source_from_key("git.example.org/group/app")["kind"] == "gitlab"
    assert cat._source_from_key("gitlab.example.com/group/app")["kind"] == "gitlab"
    assert cat._source_from_key("github.com/owner/app")["kind"] == "github"
    assert cat._source_from_key("code.example.net/group/app")["kind"] == "git"
