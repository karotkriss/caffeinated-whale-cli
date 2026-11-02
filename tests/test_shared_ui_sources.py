"""The app catalog's sources on a shared (multi-user) install - five defects seen there.

1. A source is listed WHILE its first scan runs (``CatalogStore.sources`` carries the
   in-flight row and ``_scan`` publishes a ``source`` frame), not only once it lands.
4. cwcli's own ``gh``/``glab`` calls never depend on the caller's working directory:
   glab reads ``.git/glab-cli/config.yml`` from the repository at its cwd, so a daemon
   started inside a repository its account cannot read failed every call.
5. The catalog asks the machine's shared credential first (the machine-wide bridge
   socket the service-account daemon serves), then the running account's own sign-in.
Items 2 and 3 are page-side and live in test_webui_catalog_sources.py.
"""

from __future__ import annotations

import os
import socket
import stat
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from caffeinated_whale_cli.core import app_catalog as cat
from caffeinated_whale_cli.core import credbridge, doctor, git_refs
from caffeinated_whale_cli.utils import config_utils, cred_daemon, shared_home

unix_only = pytest.mark.skipif(
    credbridge._prefer_tcp() or not hasattr(socket, "AF_UNIX"), reason="AF_UNIX host socket"
)


# ------------------------------------------------------------ item 4: the working dir


def _fake_glab(bin_dir: Path) -> None:
    """A ``glab`` that behaves like the real one on the reported install: it reads the
    per-repository config at ITS working directory and dies when that file is there
    but unreadable; otherwise it answers the credential."""
    script = bin_dir / "glab"
    script.write_text(
        "#!/bin/sh\n"
        "if [ -e .git/glab-cli/config.yml ] && ! cat .git/glab-cli/config.yml >/dev/null 2>&1; then\n"
        '  echo "failed to read configuration: open .git/glab-cli/config.yml: permission denied" >&2\n'
        "  exit 1\n"
        "fi\n"
        "printf 'protocol=https\\nhost=gitlab.example.com\\nusername=svc\\npassword=tok-from-glab\\n'\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def unreadable_repo_cwd(tmp_path, monkeypatch):
    """The process sits inside a git repository whose glab config it cannot read, with
    the fake glab first on PATH and the cwcli home somewhere readable."""
    if os.name == "nt" or os.getuid() == 0:
        pytest.skip("needs a non-root POSIX account for an unreadable file")
    repo = tmp_path / "someone-elses-repo"
    (repo / ".git" / "glab-cli").mkdir(parents=True)
    cfg = repo / ".git" / "glab-cli" / "config.yml"
    cfg.write_text("hosts: {}\n")
    cfg.chmod(0)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_glab(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    home = tmp_path / "cwcli-home"
    home.mkdir()
    monkeypatch.setenv("CWCLI_HOME", str(home))
    monkeypatch.chdir(repo)
    yield repo
    cfg.chmod(0o644)


def test_the_fake_glab_really_fails_from_the_unreadable_repo(unreadable_repo_cwd):
    proc = subprocess.run(["glab", "auth", "git-credential", "get"], capture_output=True, text=True)
    assert proc.returncode == 1 and "permission denied" in proc.stderr


def test_catalog_credential_does_not_depend_on_the_callers_cwd(unreadable_repo_cwd, monkeypatch):
    monkeypatch.setattr(cat, "gitlab_hosts", lambda: ["gitlab.example.com"])
    monkeypatch.setattr(cred_daemon, "shared_credential", lambda host, **_: b"")
    assert cat.credential("gitlab.example.com") == "tok-from-glab"


def test_bridge_host_credential_does_not_depend_on_the_callers_cwd(unreadable_repo_cwd):
    out = credbridge.host_credential(b"protocol=https\nhost=gitlab.example.com\n\n")
    assert b"password=tok-from-glab" in out


def test_neutral_cwd_is_the_cwcli_home_else_the_temp_dir(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CWCLI_HOME", str(home))
    assert credbridge.neutral_cwd() == str(home)
    monkeypatch.setenv("CWCLI_HOME", str(tmp_path / "not-yet-created"))
    assert Path(credbridge.neutral_cwd()).is_dir()
    assert credbridge.neutral_cwd() != str(tmp_path / "not-yet-created")


def test_every_host_tool_caller_runs_from_the_neutral_cwd(monkeypatch, tmp_path):
    """doctor's auth/version probes, git_refs' ls-remote and community's git all pass the
    neutral cwd, so git-run gh/glab helpers never inherit the daemon's directory."""
    seen: list[tuple[list[str], str | None]] = []

    def fake_run(argv, **kw):
        seen.append((list(argv), kw.get("cwd")))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(credbridge.subprocess, "run", fake_run)
    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    monkeypatch.setattr(git_refs.subprocess, "run", fake_run)
    neutral = credbridge.neutral_cwd()
    assert doctor._host_signed_in("glab", "gitlab.example.com") is True
    assert doctor._probe_version(["glab", "--version"]) is None
    git_refs._memo.clear()
    git_refs.list_remote_refs("https://gitlab.example.com/acme/app.git")
    assert seen and all(cwd == neutral for _argv, cwd in seen), seen
    assert seen[0][0][:3] == ["glab", "auth", "status"]
    assert seen[-1][0][:2] == ["git", "ls-remote"]


# ------------------------------------------------- item 5: the shared credential first


@pytest.fixture
def shared_mode(tmp_path, monkeypatch):
    """Shared mode on, with the daemon's run dir and the config under a temp tree."""
    marker = tmp_path / "shared.toml"
    marker.write_text(f'enabled = true\nstate_dir = "{tmp_path / "state"}"\n')
    monkeypatch.setenv("CWCLI_SHARED_MARKER", str(marker))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setattr(cred_daemon, "PID_DIR", run_dir)
    monkeypatch.setattr(cred_daemon, "LOG_FILE", run_dir / "credbridge.log")
    monkeypatch.setattr(cred_daemon, "AUDIT_FILE", run_dir / "credbridge-audit.log")
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config_utils, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config_utils, "CONFIG_FILE", cfg_dir / "config.toml")
    # No real chgrp in a test: the group policy is pinned by test_shared_home.py.
    monkeypatch.setattr(shared_home, "apply_group", lambda path: None)
    assert shared_home.shared_mode()
    return run_dir


@pytest.fixture
def machine_bridge(shared_mode, monkeypatch):
    """The machine-wide socket, served the way the daemon serves it. One fake tool
    stands in for both accounts' gh/glab (they are one function in one process): the
    listener thread is the service account, the test's main thread is the member,
    whose calls are recorded on the returned list."""
    bridge = SimpleNamespace(asked_by_member=[], allowed={"gitlab.example.com", "github.com"})

    def tool(argv, **kw):
        if threading.current_thread() is threading.main_thread():
            bridge.asked_by_member.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout=b"password=tok-from-me\n")
        return subprocess.CompletedProcess(argv, 0, stdout=b"password=tok-from-shared\n")

    monkeypatch.setattr(credbridge.subprocess, "run", tool)
    # The listener reads the allowlist per request (the live-config contract).
    monkeypatch.setattr(cred_daemon, "allowed_hosts", lambda: set(bridge.allowed))
    stop = threading.Event()
    srv = cred_daemon._start_machine_listener(stop, credbridge)
    assert srv is not None
    yield bridge
    stop.set()
    srv.close()


@unix_only
def test_the_machine_socket_is_group_gated_and_answers_the_service_credential(machine_bridge):
    path = cred_daemon.machine_socket_path()
    assert path.is_socket()
    assert stat.S_IMODE(path.stat().st_mode) == shared_home.socket_mode()
    assert cred_daemon.shared_credential("gitlab.example.com") == b"password=tok-from-shared\n"
    # The daemon's allowlist still applies: a host outside it is answered with nothing.
    assert cred_daemon.shared_credential("git.other.example") == b""


@unix_only
def test_catalog_asks_the_shared_credential_before_the_accounts_own(machine_bridge, monkeypatch):
    monkeypatch.setattr(cat, "gitlab_hosts", lambda: ["gitlab.example.com"])
    assert cat.credential("gitlab.example.com") == "tok-from-shared"
    assert cat.credential("github.com") == "tok-from-shared"
    assert machine_bridge.asked_by_member == [], "the member's own tool was asked needlessly"
    # A host the shared bridge does not answer for falls through to the member's own.
    machine_bridge.allowed.clear()
    assert cat.credential("gitlab.example.com") == "tok-from-me"
    assert machine_bridge.asked_by_member[-1][:2] == ["glab", "auth"]


@unix_only
def test_catalog_falls_back_to_the_accounts_own_sign_in(shared_mode, monkeypatch):
    """Shared mode with the daemon not serving (no socket): the member's own sign-in."""
    monkeypatch.setattr(cat, "gitlab_hosts", lambda: ["gitlab.example.com"])
    monkeypatch.setattr(
        credbridge.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout=b"password=tok-from-me\n"),
    )
    assert not cred_daemon.machine_socket_path().exists()
    assert cat.credential("gitlab.example.com") == "tok-from-me"


def test_per_user_mode_never_opens_a_machine_socket(tmp_path, monkeypatch):
    monkeypatch.setenv("CWCLI_SHARED_MARKER", str(tmp_path / "absent.toml"))
    monkeypatch.setattr(cred_daemon, "PID_DIR", tmp_path)
    assert cred_daemon._start_machine_listener(threading.Event(), credbridge) is None
    assert cred_daemon.shared_credential("github.com") == b""
    assert not (tmp_path / cred_daemon.MACHINE_SOCK_NAME).exists()


def test_shared_mode_scans_the_hosts_the_bridge_may_answer(shared_mode, monkeypatch, tmp_path):
    """The service account's glab config is not this account's to read, so the
    allowlist names the GitLab hosts the shared credential can answer for."""
    monkeypatch.setenv("GLAB_CONFIG_DIR", str(tmp_path / "no-glab"))
    monkeypatch.setattr(
        cred_daemon, "allowed_hosts", lambda: {"github.com", "gitlab.example.com", "gitlab.com"}
    )
    assert cat.gitlab_hosts() == ["gitlab.com", "gitlab.example.com"]
    # Per-user: byte-identical to before - only the account's own glab config.
    monkeypatch.setenv("CWCLI_SHARED_MARKER", str(tmp_path / "absent.toml"))
    assert cat.gitlab_hosts() == []


# ------------------------------------------------- item 1: listed while scanning


def _store(tmp_path, monkeypatch) -> cat.CatalogStore:
    monkeypatch.setenv("CWCLI_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cat, "gitlab_hosts", lambda: ["gitlab.example.com"])
    monkeypatch.setattr(cat, "installed_apps", lambda: ({}, []))
    monkeypatch.setattr(cat, "fetch_host_icon", lambda host: None)
    return cat.CatalogStore()


def test_a_source_is_listed_with_its_progress_from_the_moment_it_signs_in(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    mid_scan = threading.Event()
    release = threading.Event()
    frames: list[dict] = []

    def gh(previous, extra_orgs=None, *, started=None):
        src = cat.SourceState(kind="github", host="github.com")
        src.error = "gh is not signed in"
        return src, {}

    def gl(host, previous, extra_groups=None, progress=None, *, started=None):
        src = cat.SourceState(kind="gitlab", host=host, account="svc", signed_in=True)
        started(src)
        progress(100, 442)
        mid_scan.set()
        assert release.wait(5)
        src.repos, src.apps, src.scanned_at, src.state = 442, 3, time.time(), "verified"
        return src, {}

    def mk(previous, *, started=None):
        return cat.SourceState(kind="marketplace", host="marketplace", signed_in=True), {}

    monkeypatch.setattr(cat, "scan_github", gh)
    monkeypatch.setattr(cat, "scan_gitlab", gl)
    monkeypatch.setattr(cat, "scan_marketplace", mk)
    assert store.refresh(frames.append)
    assert mid_scan.wait(5)
    try:
        rows = {s.host: s for s in store.sources()}
        live = rows["gitlab.example.com"]
        assert live.signed_in and live.account == "svc", "a first scan's host must be listed now"
        assert live.progress == {"done": 100, "total": 442}
        listed = store.get().sources
        assert any(s.host == "gitlab.example.com" and s.signed_in for s in listed)
        assert {"source": {**cat.asdict(live), "progress": None}} in frames or any(
            f.get("source", {}).get("host") == "gitlab.example.com" for f in frames
        )
        assert {"progress": {"host": "gitlab.example.com", "done": 100, "total": 442}} in frames
    finally:
        release.set()
    for _ in range(100):
        if not store.scanning:
            break
        time.sleep(0.05)
    assert not store.scanning
    landed = {s.host: s for s in store.sources()}["gitlab.example.com"]
    assert landed.signed_in and landed.repos == 442 and landed.progress is None


def test_a_rescan_keeps_the_last_counts_on_a_known_host(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    store._disk = {
        "version": cat.CATALOG_VERSION,
        "scanned_at": 1.0,
        "remote": {},
        "sources": [
            cat.asdict(
                cat.SourceState(
                    kind="gitlab",
                    host="gitlab.example.com",
                    signed_in=True,
                    repos=442,
                    apps=3,
                    scanned_at=1.0,
                )
            )
        ],
    }
    store._scanning = True
    store._inflight["gitlab.example.com"] = cat.SourceState(
        kind="gitlab", host="gitlab.example.com", signed_in=True
    )
    store._progress["gitlab.example.com"] = {"done": 10, "total": 442}
    row = {s.host: s for s in store.sources()}["gitlab.example.com"]
    assert row.repos == 442 and row.apps == 3 and row.progress == {"done": 10, "total": 442}
