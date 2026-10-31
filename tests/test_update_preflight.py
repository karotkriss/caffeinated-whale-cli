"""``core.update_preflight`` - the checks before ``bench update --reset``.

Most of this file runs the REAL in-container script, through the real ``sh``
launcher, against synthetic benches whose apps are real git repositories cloned
from local bare remotes with ``--origin upstream`` (the way ``bench get-app``
clones). That tests the actual git logic - remote choice, branch existence, the
shallow-clone fork, ownership, data loss - with no container and no Docker.

Each planted problem mirrors a failure real ``bench update --reset`` (frappe-bench
5.31.0) hits after it has backed up every site; the rule each one mirrors is cited
in the script itself. The host-side tiers (sites, migrate locks) and the
classification are tested with a fake exec at the bottom.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import socket
import subprocess
import time

import pytest

from caffeinated_whale_cli.core import bench_read
from caffeinated_whale_cli.core import update_preflight as up
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind

GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(GIT is None, reason="needs git")


# ------------------------------------------------------------------ the fixture


class Bench:
    """A synthetic bench under ``tmp_path``, plus the means to plant each problem."""

    def __init__(self, tmp_path, *, shallow_clone=True):
        self.root = tmp_path
        self.path = tmp_path / "frappe-bench"
        self.remotes = tmp_path / "remotes"
        self.home = tmp_path / "home"
        self.bin = tmp_path / "bin"
        for d in (self.remotes, self.home, self.bin):
            d.mkdir()
        for sub in ("apps", "sites", "env", "logs"):
            (self.path / sub).mkdir(parents=True)
        # The tools bench needs, present unless a test takes one away; node and the
        # bench venv's python answer with a version, as the content tier asks them.
        for tool in ("mariadb-dump", "gzip", "yarn", "node"):
            self.stub(self.bin / tool)
        self.stub(self.bin / "node", "v22.23.4")
        (self.path / "env" / "bin").mkdir()
        self.stub(self.path / "env" / "bin" / "python", "3.12.13")
        (self.bin / "git").symlink_to(GIT)
        self.env = {
            "HOME": str(self.home),
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        }
        self.git("config", "--global", "advice.detachedHead", "false")
        self.git("config", "--global", "protocol.file.allow", "always")
        self.conf = {"maintenance_mode": 0, "pause_scheduler": 0, "shallow_clone": shallow_clone}
        self.write_conf()

    @staticmethod
    def stub(path, version=None):
        path.write_text("#!/bin/sh\n" + (f"echo {version}\n" if version else "exit 0\n"))
        path.chmod(0o755)

    def git(self, *args, cwd=None):
        subprocess.run(
            [GIT, *args], cwd=cwd, env=self.env, check=True, capture_output=True, text=True
        )

    def write_conf(self, **extra):
        self.conf.update(extra)
        (self.path / "sites" / "common_site_config.json").write_text(json.dumps(self.conf))

    def app(self, name, branch="main", *, version="1.0.0", depth=None, files=None):
        """A bare remote plus a clone of it at apps/<name>, remote named upstream."""
        src = self.remotes / f"{name}.src"
        (src / name).mkdir(parents=True)
        (src / name / "__init__.py").write_text(f'__version__ = "{version}"\n')
        (src / name / "hooks.py").write_text(f'app_name = "{name}"\n')
        (src / name / "modules.txt").write_text("")
        (src / name / "patches.txt").write_text("")
        for path, text in (files or {}).items():
            (src / path).write_text(text)
        self.git("init", "-q", "-b", branch, cwd=src)
        self.git("add", "-A", cwd=src)
        self.git("commit", "-qm", "init", cwd=src)
        bare = self.remotes / f"{name}.git"
        self.git("clone", "-q", "--bare", str(src), str(bare))
        url = f"file://{bare}" if depth else str(bare)
        extra = ["--depth", str(depth)] if depth else []
        self.git(
            "clone",
            "-q",
            "--origin",
            "upstream",
            "--branch",
            branch,
            *extra,
            url,
            str(self.path / "apps" / name),
        )
        return self.path / "apps" / name

    def publish(self, name, files, branch="main"):
        """A new tip on the app's remote: the code ``bench update --reset`` installs."""
        src = self.remotes / f"{name}.src"
        for path, text in files.items():
            (src / path).write_text(text)
        self.git("add", "-A", cwd=src)
        self.git("commit", "-qm", "publish", cwd=src)
        self.git("push", "-q", str(self.remotes / f"{name}.git"), branch, cwd=src)

    def site(self, name, **config):
        (self.path / "sites" / name).mkdir()
        (self.path / "sites" / name / "site_config.json").write_text(json.dumps(config))

    def run(self, **opts):
        """Run the real script through the real launcher; return its parsed record."""
        argv = up.script_argv(str(self.path))
        merged = json.loads(argv[5])
        merged.update(opts)
        argv[5] = json.dumps(merged)
        argv[0] = "/bin/sh"
        proc = subprocess.run(argv, env=self.env, capture_output=True, text=True, timeout=60)
        record = up.parse(proc.stdout)
        assert record is not None, proc.stdout + proc.stderr
        return record

    def findings(self, **opts):
        """The script's facts plus the content tier's, classified as run_preflight does."""
        record = self.run(**opts)
        return up.classify(record["findings"] + up._content_facts(record), project="proj")


def codes(findings):
    return {(f.code, f.app) for f in findings}


def by_code(findings, code):
    found = [f for f in findings if f.code == code]
    assert found, f"{code} not in {codes(findings)}"
    return found[0]


@pytest.fixture()
def bench(tmp_path):
    b = Bench(tmp_path)
    b.app("frappe", "version-16", version="16.0.0")
    b.app("goodapp")
    return b


# ------------------------------------------------------------------ the baseline


def test_a_healthy_bench_has_no_findings(bench):
    record = bench.run()

    assert record["findings"] == []
    assert record["apps"] == ["frappe", "goodapp"]  # frappe first, as bench orders them
    assert record["shallow_clone"] is True


def test_the_script_keeps_python_36_syntax():
    # v13 benches run old interpreters; the launcher may land on any of them.
    ast.parse(up._SCRIPT, feature_version=(3, 6))


def test_app_discovery_mirrors_bench_and_ignores_a_non_app_dir(bench):
    (bench.path / "apps" / "notes").mkdir()  # no hooks.py/modules.txt/patches.txt

    assert "notes" not in bench.run()["apps"]


# ------------------------------------------------------------------ git: blockers


def test_a_branch_missing_on_its_remote_is_a_blocker(bench):
    app = bench.app("localbranch")
    bench.git("checkout", "-q", "-b", "feature-x", cwd=app)

    finding = by_code(bench.findings(), "git.branch_not_on_remote")
    assert finding.severity == up.BLOCKER
    assert finding.app == "localbranch"
    assert "feature-x" in finding.message
    assert finding.remedy == "cwcli apps checkout proj localbranch <published-branch>"


def test_an_unreachable_remote_is_a_blocker(bench):
    app = bench.app("deadremote")
    bench.git("remote", "set-url", "upstream", str(bench.root / "gone.git"), cwd=app)

    finding = by_code(bench.findings(), "git.remote_unreachable")
    assert finding.severity == up.BLOCKER
    assert finding.app == "deadremote"
    # git's cause, not the last line of its multi-line access-rights hint.
    assert "does not appear to be a git repository" in finding.message


def test_a_dead_extra_remote_blocks_only_without_shallow_clone(bench):
    # bench/app.py:1039-1047: `git fetch --all` (no shallow_clone) contacts every
    # remote; with shallow_clone it fetches only <remote> <branch>.
    app = bench.app("extraremote")
    bench.git("remote", "add", "fork", str(bench.root / "gone.git"), cwd=app)

    assert ("git.remote_unreachable", "extraremote") not in codes(bench.findings())

    bench.write_conf(shallow_clone=False)
    finding = by_code(bench.findings(), "git.remote_unreachable")
    assert "'fork'" in finding.message


def test_a_detached_head_is_a_blocker(bench):
    app = bench.app("detached")
    bench.git("checkout", "-q", "--detach", cwd=app)

    finding = by_code(bench.findings(), "git.detached_head")
    assert finding.severity == up.BLOCKER
    assert finding.remedy == "cwcli apps checkout proj detached <branch>"


def test_a_remote_bench_would_misdetect_is_a_blocker(bench):
    # bench's rule picks "upstream" whenever `git remote -v` matches upstream[\s]+,
    # so a lone remote named old-upstream makes it reset to upstream/<branch>.
    app = bench.app("suffixremote")
    bench.git("remote", "rename", "upstream", "old-upstream", cwd=app)

    finding = by_code(bench.findings(), "git.remote_misdetected")
    assert finding.severity == up.BLOCKER
    assert "'upstream'" in finding.message and "old-upstream" in finding.message


def test_frappe_without_an_upstream_remote_is_a_blocker(bench):
    bench.git("remote", "rename", "upstream", "origin", cwd=bench.path / "apps" / "frappe")

    finding = by_code(bench.findings(), "frappe.no_upstream")
    assert finding.severity == up.BLOCKER
    assert finding.app == "frappe"
    assert finding.remedy == (
        "in `cwcli open proj`: git -C apps/frappe remote rename origin upstream"
    )


def test_frappe_on_master_is_a_blocker(tmp_path):
    b = Bench(tmp_path)
    b.app("frappe", "master")

    assert by_code(b.findings(), "git.master_branch").severity == up.BLOCKER


def test_master_is_fine_for_any_other_app(bench):
    bench.app("mainline", "master")

    assert ("git.master_branch", "mainline") not in codes(bench.findings())


def test_a_dubious_ownership_refusal_is_a_blocker(bench, monkeypatch):
    # git's own verdict, so safe.directory is honoured exactly as bench's git sees it.
    # GIT_TEST_ASSUME_DIFFERENT_OWNER is git's own test knob for the uid comparison.
    bench.env["GIT_TEST_ASSUME_DIFFERENT_OWNER"] = "1"

    findings = bench.findings()

    refused = {f.app: f for f in findings if f.code == "git.dubious_ownership"}
    assert {f.severity for f in refused.values()} == {up.BLOCKER}
    # The preflight never checked a refused repo for local work, so the remedy is the
    # non-destructive re-own for every app, never the destructive --force.
    for app in ("goodapp", "frappe"):
        app_path = os.path.realpath(bench.path / "apps" / app)
        assert refused[app].remedy == (
            f"in `cwcli open proj`: sudo chown -R frappe:frappe {app_path}"
        )


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores write permission")
def test_an_app_the_container_user_cannot_write_is_a_blocker(bench):
    app = bench.path / "apps" / "goodapp"
    app.chmod(0o555)
    try:
        finding = by_code(bench.findings(), "app.not_writable")
    finally:
        app.chmod(0o755)
    assert finding.app == "goodapp"


def test_a_missing_bench_env_is_a_blocker(bench):
    shutil.rmtree(bench.path / "env")

    finding = by_code(bench.findings(), "bench.not_writable")
    assert finding.message.endswith("env is missing or not writable by the container user")


def test_release_bench_is_a_blocker(bench):
    bench.write_conf(release_bench=True)

    assert by_code(bench.findings(), "bench.release_bench").severity == up.BLOCKER


def test_an_invalid_common_site_config_is_a_blocker(bench):
    (bench.path / "sites" / "common_site_config.json").write_text("{not json")

    assert by_code(bench.findings(), "bench.config_unreadable").severity == up.BLOCKER


def test_a_missing_tool_is_a_blocker(bench):
    (bench.bin / "yarn").unlink()

    finding = by_code(bench.findings(), "tool.missing")
    assert finding.message == "yarn is not on the container's PATH"


def test_either_dump_tool_satisfies_the_backup(bench):
    (bench.bin / "mariadb-dump").rename(bench.bin / "mysqldump")

    assert "tool.missing" not in {f.code for f in bench.findings()}


def test_an_unreachable_database_or_redis_cache_is_a_blocker(bench):
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()  # nothing listens there now: connection refused
    bench.write_conf(db_host="127.0.0.1", db_port=port, redis_cache=f"redis://127.0.0.1:{port}")
    bench.site("a.localhost")

    found = [f for f in bench.findings() if f.code == "service.unreachable"]
    assert {f.message.split(" at ")[0] for f in found} == {"database", "redis_cache"}
    assert all(f.severity == up.BLOCKER for f in found)


def test_a_listening_database_is_not_a_finding(bench):
    with socket.socket() as listening:
        listening.bind(("127.0.0.1", 0))
        listening.listen()
        bench.write_conf(db_host="127.0.0.1", db_port=listening.getsockname()[1])
        bench.site("a.localhost")

        assert "service.unreachable" not in {f.code for f in bench.findings()}


def test_a_nearly_full_disk_is_a_blocker_and_a_low_one_a_notice(bench):
    assert by_code(bench.findings(disk_floor_mb=10**12), "disk.full").severity == up.BLOCKER

    # A sparse "latest backup" larger than the free space: the next backups need ~2x.
    bench.site("a.localhost")
    backups = bench.path / "sites" / "a.localhost" / "private" / "backups"
    backups.mkdir(parents=True)
    stat = os.statvfs(bench.path / "sites")
    with open(backups / "20260101_000000-a_localhost-database.sql.gz", "wb") as handle:
        handle.truncate(stat.f_bavail * stat.f_frsize)
    assert by_code(bench.findings(), "disk.low").severity == up.NOTICE


# ------------------------------------------------------------------ data loss


def test_uncommitted_and_unpushed_work_is_data_loss_not_a_blocker(bench):
    app = bench.path / "apps" / "goodapp"
    (app / "goodapp" / "__init__.py").write_text("x = 1\n")
    (app / "goodapp" / "committed.py").write_text("z = 3\n")
    bench.git("add", "goodapp/committed.py", cwd=app)
    bench.git("commit", "-qm", "unpushed work", cwd=app)
    (app / "goodapp" / "__init__.py").write_text("x = 2\n")

    findings = bench.findings()

    uncommitted = by_code(findings, "git.uncommitted")
    unpushed = by_code(findings, "git.unpushed")
    assert {uncommitted.severity, unpushed.severity} == {up.DATA_LOSS}
    assert unpushed.message.startswith("1 commit(s) on 'main' exist nowhere else")
    assert "cannot be recovered" in unpushed.message  # shallow_clone prunes the reflog
    assert not [f for f in findings if f.severity == up.BLOCKER]


def test_a_shallow_clone_behind_its_remote_is_not_unpushed_work(bench):
    # The bench-init shape: a depth-1 clone, then a depth-1 fetch that moves the
    # remote ref past HEAD (bench's own version check does exactly this to frappe).
    # HEAD's history is cut there, so a naive <remote>/<branch>..HEAD counts the
    # commit the remote has always had.
    app = bench.app("shallowapp", depth=1)
    src = bench.remotes / "shallowapp.src"
    (src / "shallowapp" / "later.py").write_text("x = 1\n")
    bench.git("add", "-A", cwd=src)
    bench.git("commit", "-qm", "upstream moved on", cwd=src)
    bench.git("push", "-q", str(bench.remotes / "shallowapp.git"), "main", cwd=src)
    bench.git("fetch", "-q", "--depth=1", "--no-tags", "upstream", "main", cwd=app)

    assert ("git.unpushed", "shallowapp") not in codes(bench.findings())

    (app / "shallowapp" / "mine.py").write_text("y = 2\n")
    bench.git("add", "-A", cwd=app)
    bench.git("commit", "-qm", "local work", cwd=app)
    assert by_code(bench.findings(), "git.unpushed").message.startswith("1 commit(s)")


def test_a_commit_another_branch_holds_is_not_data_loss(bench):
    app = bench.path / "apps" / "goodapp"
    (app / "goodapp" / "kept.py").write_text("z = 3\n")
    bench.git("add", "-A", cwd=app)
    bench.git("commit", "-qm", "kept elsewhere", cwd=app)
    bench.git("branch", "keep", cwd=app)  # the reset moves main, never this branch

    assert bench.findings() == []


def test_untracked_files_alone_are_not_data_loss(bench):
    # `git reset --hard` leaves untracked files in place.
    (bench.path / "apps" / "goodapp" / "goodapp" / "new_module.py").write_text("y = 2\n")

    assert bench.findings() == []


# ------------------------------------------------------------------ the data-loss guard


class HostContainer:
    """Answers ``run_preflight``'s exec by running it on the host, against the bench."""

    def __init__(self, bench):
        self.bench = bench

    def exec_run(self, cmd, **_kwargs):
        proc = subprocess.run(cmd, env=self.bench.env, capture_output=True, timeout=60)
        return proc.returncode, proc.stdout


def guard(bench, *, local_only):
    """What ``core.update``'s data-loss guard acts on, through the real ``run_preflight``."""
    outcome = up.run_preflight(
        HostContainer(bench), str(bench.path), project="proj", local_only=local_only
    )
    return up.local_work(outcome.findings), outcome


def plant_local_work(bench, app="goodapp"):
    """An unpushed commit plus an uncommitted edit to a tracked file."""
    path = bench.path / "apps" / app
    (path / app / "committed.py").write_text("z = 3\n")
    bench.git("add", f"{app}/committed.py", cwd=path)
    bench.git("commit", "-qm", "unpushed work", cwd=path)
    (path / app / "__init__.py").write_text("x = 2\n")


@pytest.mark.parametrize("local_only", [False, True])
def test_the_guard_sees_uncommitted_and_unpushed_work_on_either_tier(bench, local_only):
    plant_local_work(bench)

    at_risk, _ = guard(bench, local_only=local_only)

    assert codes(at_risk) == {("git.uncommitted", "goodapp"), ("git.unpushed", "goodapp")}


@pytest.mark.parametrize("local_only", [False, True])
def test_untracked_files_alone_never_trigger_the_guard(bench, local_only):
    # bench keeps them: `git reset --hard` never touches an untracked file.
    (bench.path / "apps" / "goodapp" / "goodapp" / "new_module.py").write_text("y = 2\n")

    assert guard(bench, local_only=local_only)[0] == []


@pytest.mark.parametrize("shallow", [True, False])
def test_shallow_clone_says_the_loss_cannot_be_recovered(tmp_path, shallow):
    # With shallow_clone bench also expires the reflog and prunes (bench/app.py:1043),
    # so the reflog that would otherwise still hold the work is gone too.
    b = Bench(tmp_path, shallow_clone=shallow)
    b.app("frappe", "version-16", version="16.0.0")
    b.app("goodapp")
    plant_local_work(b)

    at_risk, _ = guard(b, local_only=True)

    assert len(at_risk) == 2
    assert all(("cannot be recovered" in f.message) is shallow for f in at_risk)


def test_the_local_tier_probes_no_remote_and_no_service(bench):
    # --no-preflight exists for a false positive in exactly these probes.
    app = bench.app("deadremote")
    bench.git("remote", "set-url", "upstream", str(bench.root / "gone.git"), cwd=app)
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()  # nothing listens there now: connection refused
    bench.write_conf(redis_cache=f"redis://127.0.0.1:{port}")

    _, local = guard(bench, local_only=True)
    _, full = guard(bench, local_only=False)

    probed = ("git.remote_unreachable", "service.unreachable")
    assert not [f for f in local.findings if f.code in probed]
    assert {f.code for f in full.findings} >= set(probed)  # the twin: they are real


def test_under_no_preflight_an_app_bench_cannot_reset_holds_no_data_loss(bench):
    # No refspec writes upstream/<branch>, so bench's reset of this app fails before
    # it discards anything; only the network tier can tell its commits apart.
    app = bench.app("localonly", depth=1)
    bench.git("checkout", "-q", "-b", "feature-z", cwd=app)
    (app / "localonly" / "new.py").write_text("x = 1\n")
    bench.git("add", "-A", cwd=app)
    bench.git("commit", "-qm", "local", cwd=app)

    assert guard(bench, local_only=True)[0] == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_local_work_that_cannot_be_read_fails_the_guard_closed(bench):
    index = bench.path / "apps" / "goodapp" / ".git" / "index"
    index.chmod(0)
    try:
        at_risk, _ = guard(bench, local_only=True)
    finally:
        index.chmod(0o644)

    assert codes(at_risk) == {("git.local_work_unverified", "goodapp")}


def test_a_repo_git_refuses_for_dubious_ownership_fails_the_guard_closed(bench):
    # Under --no-preflight too: the guard never read that repo's work, so it may not pass.
    bench.env["GIT_TEST_ASSUME_DIFFERENT_OWNER"] = "1"

    at_risk, _ = guard(bench, local_only=True)

    assert codes(at_risk) == {
        ("git.dubious_ownership", "goodapp"),
        ("git.dubious_ownership", "frappe"),
    }


# ------------------------------------------------------------------ notices


def test_an_app_without_a_remote_is_a_notice(bench):
    app = bench.app("noremote")
    bench.git("remote", "remove", "upstream", cwd=app)

    finding = by_code(bench.findings(), "git.no_remote")
    assert finding.severity == up.NOTICE
    assert "excluded_apps.txt" in finding.message


def test_frappe_without_any_remote_is_the_no_upstream_blocker(bench):
    bench.git("remote", "remove", "upstream", cwd=bench.path / "apps" / "frappe")

    findings = bench.findings()

    finding = by_code(findings, "frappe.no_upstream")
    assert finding.severity == up.BLOCKER
    assert "remotes: none" in finding.message
    assert ("git.no_remote", "frappe") not in codes(findings)
    assert finding.remedy == (
        "in `cwcli open proj`: git -C apps/frappe remote add upstream "
        "https://github.com/frappe/frappe && git -C apps/frappe fetch upstream"
    )


def test_frappe_without_any_remote_still_reports_every_local_problem(bench):
    frappe = bench.path / "apps" / "frappe"
    bench.git("remote", "remove", "upstream", cwd=frappe)
    bench.git("branch", "-m", "master", cwd=frappe)
    (frappe / "frappe" / "committed.py").write_text("committed = True\n")
    bench.git("add", "-A", cwd=frappe)
    bench.git("commit", "-qm", "local commit", cwd=frappe)
    (frappe / "frappe" / "hooks.py").write_text("dirty = True\n")

    found = codes(bench.findings())

    assert ("frappe.no_upstream", "frappe") in found
    assert ("git.master_branch", "frappe") in found
    assert ("git.uncommitted", "frappe") in found
    assert ("git.unpushed", "frappe") in found


def test_frappe_without_any_remote_still_reports_a_detached_head(bench):
    frappe = bench.path / "apps" / "frappe"
    bench.git("remote", "remove", "upstream", cwd=frappe)
    bench.git("checkout", "-q", "--detach", cwd=frappe)

    found = codes(bench.findings())

    assert ("frappe.no_upstream", "frappe") in found
    assert ("git.detached_head", "frappe") in found


def test_following_the_no_upstream_remedy_with_its_fetch_leaves_nothing_to_report(bench):
    frappe = bench.path / "apps" / "frappe"
    bench.git("remote", "remove", "upstream", cwd=frappe)
    bench.git("remote", "add", "upstream", str(bench.remotes / "frappe.git"), cwd=frappe)
    # Without the fetch no upstream ref exists yet, so all local history reads unpushed.
    assert ("git.unpushed", "frappe") in codes(bench.findings())

    bench.git("fetch", "-q", "upstream", cwd=frappe)

    assert bench.findings() == []


def test_excluded_and_non_git_apps_are_notices(bench):
    bench.app("parked")
    (bench.path / "sites" / "excluded_apps.txt").write_text("parked\n")
    plain = bench.path / "apps" / "plain" / "plain"
    plain.mkdir(parents=True)
    for name in ("hooks.py", "modules.txt", "patches.txt"):
        (plain / name).write_text("")

    found = codes(bench.findings())
    assert ("app.excluded", "parked") in found
    assert ("app.not_git", "plain") in found


def test_an_excluded_or_non_git_frappe_or_erpnext_still_gets_the_branch_rules(bench):
    # bench's validate_branch and version check read frappe and erpnext whether or
    # not pull_apps would skip them, so they still fail on these before any backup.
    frappe = bench.path / "apps" / "frappe"
    bench.git("remote", "remove", "upstream", cwd=frappe)
    bench.git("checkout", "-q", "--detach", cwd=frappe)
    erpnext = bench.app("erpnext", "master")
    bench.git("remote", "remove", "upstream", cwd=erpnext)
    parked = bench.app("parked")
    bench.git("checkout", "-q", "--detach", cwd=parked)
    (bench.path / "sites" / "excluded_apps.txt").write_text("frappe\nerpnext\nparked\n")

    findings = bench.findings()
    found = codes(findings)

    assert ("frappe.no_upstream", "frappe") in found
    assert ("git.detached_head", "frappe") in found
    assert ("git.master_branch", "erpnext") in found
    assert ("git.no_remote", "erpnext") not in found
    assert ("git.detached_head", "parked") not in found
    assert {f.severity for f in findings if f.app == "parked"} == {up.NOTICE}


def test_a_frappe_or_erpnext_that_is_not_a_git_checkout_is_a_blocker(bench):
    frappe = bench.path / "apps" / "frappe"
    shutil.rmtree(frappe / ".git")
    erpnext = bench.app("erpnext")
    shutil.rmtree(erpnext / ".git")

    findings = bench.findings()

    for app in ("frappe", "erpnext"):
        assert by_code([f for f in findings if f.app == app], "git.not_a_checkout").severity == (
            up.BLOCKER
        )
        assert ("app.not_git", app) not in codes(findings)


def test_leftover_bench_wide_flags_are_notices(bench):
    bench.write_conf(maintenance_mode=1, pause_scheduler=1)

    findings = bench.findings()
    assert by_code(findings, "bench.maintenance_mode_on").remedy == (
        "cwcli run proj set-config --global --parse maintenance_mode 0"
    )
    assert by_code(findings, "bench.scheduler_paused").severity == up.NOTICE


def test_a_stale_tracking_ref_is_a_notice_without_shallow_clone_and_a_blocker_with_it(tmp_path):
    b = Bench(tmp_path, shallow_clone=False)
    b.app("frappe", "version-16")
    app = b.app("stale")
    b.git("checkout", "-q", "-b", "feature-y", cwd=app)
    b.git("push", "-q", "upstream", "feature-y", cwd=app)
    b.git("fetch", "-q", "upstream", cwd=app)  # refs/remotes/upstream/feature-y exists
    b.git("branch", "-D", "feature-y", cwd=b.remotes / "stale.git")  # deleted upstream

    # `git fetch --all` keeps the stale ref and bench resets to its old commit ...
    assert by_code(b.findings(), "git.branch_deleted_upstream").severity == up.NOTICE
    # ... while `git fetch --depth=1 upstream feature-y` fails outright.
    b.write_conf(shallow_clone=True)
    assert by_code(b.findings(), "git.branch_not_on_remote").severity == up.BLOCKER


def test_a_checked_out_branch_a_single_branch_clone_does_not_track_is_a_blocker(bench):
    # bench resets to upstream/<branch>, which neither its fetch nor `cwcli apps
    # checkout`'s `fetch upstream <ref>` writes when the refspec covers only the
    # branch a --depth 1 clone cloned.
    app = bench.app("feature", depth=1)
    src = bench.remotes / "feature.src"
    bench.git("checkout", "-q", "-b", "feature-x", cwd=src)
    (src / "feature" / "new.py").write_text("x = 1\n")
    bench.git("add", "-A", cwd=src)
    bench.git("commit", "-qm", "feature", cwd=src)
    bench.git("push", "-q", str(bench.remotes / "feature.git"), "feature-x", cwd=src)
    bench.git("fetch", "-q", "upstream", "feature-x", cwd=app)
    bench.git("checkout", "-q", "-B", "feature-x", "FETCH_HEAD", cwd=app)

    findings = bench.findings()
    finding = by_code(findings, "git.branch_not_tracked")
    assert finding.severity == up.BLOCKER
    assert finding.app == "feature"
    # The commits are on the remote, so they are not reported as existing nowhere else.
    assert ("git.unpushed", "feature") not in codes(findings)

    # The remedy makes bench's own fetch write the ref, so nothing is left to report.
    remedy = finding.remedy.split(": ", 1)[1]
    assert "+refs/heads/feature-x:refs/remotes/upstream/feature-x" in remedy
    assert remedy.endswith("fetch --depth=1 upstream feature-x")  # keeps the clone shallow
    subprocess.run(remedy, shell=True, cwd=bench.path, env=bench.env, check=True)
    assert not [f for f in bench.findings() if f.app == "feature"]

    # A refspec that stops covering it leaves the ref behind: bench resets to it.
    bench.git("config", "--unset", "remote.upstream.fetch", "feature-x", cwd=app)
    assert by_code(bench.findings(), "git.branch_tracking_stale").severity == up.NOTICE


def test_local_commits_on_an_untracked_branch_its_remote_has_are_still_data_loss(bench):
    app = bench.app("feature", depth=1)
    src = bench.remotes / "feature.src"
    bench.git("checkout", "-q", "-b", "feature-x", cwd=src)
    (src / "feature" / "new.py").write_text("x = 1\n")
    bench.git("add", "-A", cwd=src)
    bench.git("commit", "-qm", "feature", cwd=src)
    bench.git("push", "-q", str(bench.remotes / "feature.git"), "feature-x", cwd=src)
    bench.git("fetch", "-q", "upstream", "feature-x", cwd=app)
    bench.git("checkout", "-q", "-B", "feature-x", "FETCH_HEAD", cwd=app)
    (app / "feature" / "local.py").write_text("y = 1\n")
    bench.git("add", "-A", cwd=app)
    bench.git("commit", "-qm", "local", cwd=app)

    findings = bench.findings()
    assert by_code(findings, "git.branch_not_tracked").app == "feature"
    unpushed = by_code(findings, "git.unpushed")
    assert unpushed.app == "feature"
    assert unpushed.message.startswith("1 commit(s)")

    # Once the remote moves to a tip this clone has never fetched, the count is unknown.
    (src / "feature" / "more.py").write_text("z = 1\n")
    bench.git("add", "-A", cwd=src)
    bench.git("commit", "-qm", "more", cwd=src)
    bench.git("push", "-q", str(bench.remotes / "feature.git"), "feature-x", cwd=src)
    found = codes(bench.findings())
    assert ("git.local_work_unverified", "feature") in found
    assert ("git.unpushed", "feature") not in found


@pytest.mark.parametrize("unanswered", ["gone", "hangs"])
def test_an_untracked_branch_blocks_even_when_its_remote_does_not_answer(bench, unanswered):
    # No refspec writes upstream/<branch>, so bench's reset fails whatever the remote
    # says; and an unanswered remote cannot show the feature commits exist nowhere else.
    app = bench.app("feature", depth=1)
    src = bench.remotes / "feature.src"
    bench.git("checkout", "-q", "-b", "feature-x", cwd=src)
    (src / "feature" / "new.py").write_text("x = 1\n")
    bench.git("add", "-A", cwd=src)
    bench.git("commit", "-qm", "feature", cwd=src)
    bench.git("push", "-q", str(bench.remotes / "feature.git"), "feature-x", cwd=src)
    bench.git("fetch", "-q", "upstream", "feature-x", cwd=app)
    bench.git("checkout", "-q", "-B", "feature-x", "FETCH_HEAD", cwd=app)
    if unanswered == "gone":
        bench.git("remote", "set-url", "upstream", str(bench.root / "gone.git"), cwd=app)
    else:
        bench.git("config", "remote.upstream.uploadpack", "sleep 30; git-upload-pack", cwd=app)

    found = codes(bench.findings(net_timeout=0.5))

    assert ("git.branch_not_tracked", "feature") in found
    assert ("git.local_work_unverified", "feature") in found
    assert ("git.unpushed", "feature") not in found


def test_local_commits_on_an_untracked_branch_missing_on_its_remote_stay_data_loss(bench):
    app = bench.app("localonly", depth=1)
    bench.git("checkout", "-q", "-b", "feature-z", cwd=app)
    (app / "localonly" / "new.py").write_text("x = 1\n")
    bench.git("add", "-A", cwd=app)
    bench.git("commit", "-qm", "local", cwd=app)

    found = codes(bench.findings())
    assert ("git.branch_not_on_remote", "localonly") in found
    assert ("git.unpushed", "localonly") in found
    assert ("git.branch_not_tracked", "localonly") not in found


# ------------------------------------------------------------------ the content tier
# bench resets every app, then reinstalls every app: `uv pip install -e` refuses a
# requires-python the venv does not meet and `yarn install` an engines.node the node
# does not, both after every site was backed up and every app reset. Each problem
# here is planted on the REMOTE's tip, which is what the reset would install.


def pyproject(name, version="1.0.0", python=">=3.10", deps=None):
    text = f'[project]\nname = "{name}"\nversion = "{version}"\nrequires-python = "{python}"\n'
    if deps:
        text += "\n[tool.bench.frappe-dependencies]\n"
        text += "".join(f'{dep} = "{spec}"\n' for dep, spec in deps.items())
    return text


def package_json(name, node):
    return json.dumps({"name": name, "engines": {"node": node}})


@pytest.mark.parametrize("venv, refused", [("3.12.13", True), ("3.14.2", False)])
def test_new_code_that_needs_a_newer_python_is_a_blocker(bench, venv, refused):
    bench.stub(bench.path / "env" / "bin" / "python", venv)
    bench.publish(
        "frappe", {"pyproject.toml": pyproject("frappe", "16.1.0", ">=3.14,<3.15")}, "version-16"
    )
    head = bench.path / "apps" / "frappe" / ".git" / "HEAD"
    before = head.read_text()

    findings = bench.findings()

    if refused:
        finding = by_code(findings, "tooling.python_too_old")
        assert (finding.severity, finding.app) == (up.BLOCKER, "frappe")
        assert "needs Python >=3.14,<3.15" in finding.message
        assert "runs Python 3.12.13" in finding.message
    else:
        assert findings == []
    # Read from the fetched ref, never checked out: the working tree is as it was.
    assert head.read_text() == before
    assert not (bench.path / "apps" / "frappe" / "pyproject.toml").exists()


@pytest.mark.parametrize("node, refused", [("v22.23.4", True), ("v24.1.0", False)])
def test_new_code_that_needs_a_newer_node_is_a_blocker(bench, node, refused):
    bench.stub(bench.bin / "node", node)
    bench.publish("goodapp", {"package.json": package_json("goodapp", "^18 || >=24")})

    findings = bench.findings()

    if refused:
        finding = by_code(findings, "tooling.node_too_old")
        assert (finding.severity, finding.app) == (up.BLOCKER, "goodapp")
        assert "needs Node ^18 || >=24" in finding.message
        assert "runs Node v22.23.4" in finding.message
    else:
        assert findings == []


@pytest.mark.parametrize("upstream, refused", [("17.0.0", True), ("16.3.0", False)])
def test_a_frappe_major_upgrade_upstream_is_a_blocker(bench, upstream, refused):
    # bench's version check prompts before a major upgrade, and cwcli's exec has no TTY
    # to answer it: "Do you want to continue? [y/N]: Aborted!".
    bench.publish("frappe", {"frappe/__init__.py": f'__version__ = "{upstream}"\n'}, "version-16")

    findings = bench.findings()

    if refused:
        finding = by_code(findings, "frappe.major_upgrade")
        assert (finding.severity, finding.app) == (up.BLOCKER, "frappe")
        assert "from version 16 to 17" in finding.message
        assert finding.remedy.endswith("cwcli run proj -i update --reset")
        assert "skips cwcli's data-loss guard" in finding.remedy
        assert "maintenance/scheduler flags" in finding.remedy
        assert "commit and push (or back up) any local app work" in finding.remedy
    else:
        assert findings == []


def test_an_unsatisfied_frappe_dependency_is_a_notice_against_the_updated_version(bench):
    bench.publish(
        "goodapp",
        {"pyproject.toml": pyproject("goodapp", deps={"frappe": ">=16.5.0-dev,<17.0.0"})},
    )

    finding = by_code(bench.findings(), "app.frappe_dependency_unsatisfied")
    # 040 (staging server): a refusal, naming the switch, not a notice bench would only warn on.
    assert (finding.severity, finding.app) == (up.BLOCKER, "goodapp")
    assert "switch goodapp to version-" in finding.message
    assert "--fix-branches" in finding.remedy
    assert "requires frappe >=16.5.0-dev,<17.0.0" in finding.message
    assert "frappe will be at 16.0.0" in finding.message

    # bench checks after resetting every app, so frappe's NEW version is the one compared.
    bench.publish("frappe", {"frappe/__init__.py": '__version__ = "16.5.0"\n'}, "version-16")
    assert bench.findings() == []


@pytest.mark.parametrize(
    "erpnext_branch, erpnext_version, spec, expected",
    [
        ("version-16", "16.0.0", ">=15.0.0,<16.0.0", ("goodapp", "version-16")),
        ("version-15", "15.0.0", ">=16.0.0,<17.0.0", ("erpnext", "version-16")),
    ],
)
def test_fix_branches_moves_whichever_side_is_off_the_frappe_line(
    bench, erpnext_branch, erpnext_version, spec, expected
):
    bench.app("erpnext", erpnext_branch, version=erpnext_version)
    bench.publish(
        "erpnext",
        {"pyproject.toml": pyproject("erpnext", version=erpnext_version)},
        erpnext_branch,
    )
    bench.publish("goodapp", {"pyproject.toml": pyproject("goodapp", deps={"erpnext": spec})})

    record = bench.run()
    facts = record["findings"] + up._content_facts(record)

    assert up.fixable_branches(facts) == [expected]


def test_an_app_the_reset_does_not_move_is_checked_as_it_is_on_disk(bench):
    # bench reinstalls every app, excluded or not, so its own requirements still apply.
    bench.app("custom", files={"package.json": package_json("custom", ">=24")})
    (bench.path / "sites" / "excluded_apps.txt").write_text("custom\n")

    finding = by_code(bench.findings(), "tooling.node_too_old")
    assert finding.app == "custom"


def test_the_content_fetch_is_bench_s_own_and_never_touches_the_working_tree(bench):
    app = bench.app("shallow", depth=1)
    for n in range(3):
        bench.publish("shallow", {"shallow/new.py": f"x = {n}\n"})
    tip = subprocess.run(
        [GIT, "-C", str(bench.remotes / "shallow.git"), "rev-parse", "main"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    head_before = (app / ".git" / "HEAD").read_text()

    assert bench.findings() == []

    def git_out(*args):
        return subprocess.run(
            [GIT, *args], cwd=app, env=bench.env, capture_output=True, text=True
        ).stdout.strip()

    # The same ref bench's fetch writes, at the tip its reset would move to ...
    assert git_out("rev-parse", "refs/remotes/upstream/main") == tip
    # ... fetched --depth=1, so a shallow repo never receives the full history ...
    assert git_out("rev-list", "--count", "refs/remotes/upstream/main") == "1"
    # ... and nothing checked out.
    assert (app / ".git" / "HEAD").read_text() == head_before
    assert git_out("status", "--porcelain") == ""
    assert not (app / "shallow" / "new.py").exists()


@pytest.mark.parametrize("shallow_clone", [False, True])
def test_the_content_fetch_keeps_the_history_below_a_head_past_its_shallow_commit(
    tmp_path, shallow_clone
):
    # A depth-1 clone moved on by full fetches (bench's `git fetch --all`, or
    # `apps checkout`): a depth-1 content fetch would cut the tracking ref's history,
    # and the next run would count every commit since the clone as unpushed work.
    b = Bench(tmp_path, shallow_clone=shallow_clone)
    app = b.app("deep", depth=1)
    for n in range(2):
        b.publish("deep", {"deep/new.py": f"x = {n}\n"})
    b.git("fetch", "-q", "upstream", cwd=app)
    b.git("reset", "-q", "--hard", "upstream/main", cwd=app)
    b.publish("deep", {"deep/new.py": "x = 2\n"})

    assert b.findings() == []
    assert b.findings() == []


def test_the_local_tier_fetches_nothing_and_checks_no_content(bench):
    # --no-preflight runs only the data-loss guard's local checks.
    bench.publish("goodapp", {"package.json": package_json("goodapp", ">=24")})

    def tracking():
        return subprocess.run(
            [GIT, "rev-parse", "refs/remotes/upstream/main"],
            cwd=bench.path / "apps" / "goodapp",
            env=bench.env,
            capture_output=True,
            text=True,
        ).stdout

    before = tracking()
    _, outcome = guard(bench, local_only=True)

    assert outcome.findings == []
    assert tracking() == before


def test_a_fetch_that_fails_is_unverified_not_a_blocker(bench):
    bench.publish("goodapp", {"package.json": package_json("goodapp", ">=24")})
    lock = bench.path / "apps" / "goodapp" / ".git" / "refs" / "remotes" / "upstream"
    lock.mkdir(parents=True, exist_ok=True)
    (lock / "main.lock").write_text("")  # another git holds the ref

    findings = bench.findings()

    finding = by_code(findings, "content.unverified")
    assert (finding.severity, finding.app) == (up.UNVERIFIED, "goodapp")
    assert "could not be checked" in finding.message
    assert not [f for f in findings if f.severity == up.BLOCKER]


def test_a_fetch_that_does_not_answer_is_killed_and_unverified(bench):
    # ls-remote answers; the fetch after it hangs. A counter tells the two apart.
    app = bench.path / "apps" / "goodapp"
    bench.publish("goodapp", {"package.json": package_json("goodapp", ">=24")})
    calls = bench.root / "uploadpack-calls"
    script = bench.root / "uploadpack.sh"
    script.write_text(
        f'#!/bin/sh\necho x >> {calls}\n[ "$(wc -l < {calls})" -gt 1 ] && sleep 30\n'
        'exec git-upload-pack "$@"\n'
    )
    script.chmod(0o755)
    bench.git("config", "remote.upstream.uploadpack", str(script), cwd=app)

    started = time.monotonic()
    findings = bench.findings(fetch_timeout=0.5)

    assert time.monotonic() - started < 15
    assert by_code(findings, "content.unverified").app == "goodapp"
    assert not [f for f in findings if f.severity == up.BLOCKER]
    assert not list((app / ".git").rglob("*.lock"))  # nothing stranded for bench's fetch


def test_a_python_that_cannot_run_is_unverified(bench):
    (bench.path / "env" / "bin" / "python").unlink()
    bench.publish("goodapp", {"pyproject.toml": pyproject("goodapp", python=">=3.14")})

    finding = by_code(bench.findings(), "content.unverified")
    assert finding.app == "goodapp"
    assert "the bench's python could not be run" in finding.message


def test_a_manifest_that_does_not_parse_is_unverified(bench):
    bench.publish("goodapp", {"pyproject.toml": "[project\nname = "})

    finding = by_code(bench.findings(), "content.unverified")
    assert "pyproject.toml could not be parsed" in finding.message


def test_every_tooling_problem_is_reported_in_one_run(bench):
    bench.publish(
        "frappe",
        {
            "frappe/__init__.py": '__version__ = "17.0.0"\n',
            "pyproject.toml": pyproject("frappe", "17.0.0", ">=3.14,<3.15"),
            "package.json": package_json("frappe", ">=24"),
        },
        "version-16",
    )
    bench.publish("goodapp", {"pyproject.toml": pyproject("goodapp", deps={"frappe": "^16.0.0"})})

    assert codes(bench.findings()) == {
        ("frappe.major_upgrade", "frappe"),
        ("tooling.python_too_old", "frappe"),
        ("tooling.node_too_old", "frappe"),
        ("app.frappe_dependency_unsatisfied", "goodapp"),
    }


@pytest.mark.parametrize(
    "spec, version, expected",
    [
        (">=3.14,<3.15", "3.12.13", False),
        (">=3.14,<3.15", "3.14.2", True),
        (">=3.14,<3.15", "3.15.0", False),
        ("~=3.10", "3.12.1", True),
        ("==3.12.*", "3.12.13", True),
        ("!=3.11.*", "3.11.4", False),
        ("not a specifier", "3.12.0", None),
    ],
)
def test_requires_python_is_read_as_pep_440(spec, version, expected):
    assert up._pep440_contains(spec, version) is expected


@pytest.mark.parametrize(
    "spec, version, expected",
    [
        ("^18 || >=20", "18.19.0", True),
        ("^18 || >=20", "19.9.0", False),
        ("^18 || >=20", "v22.23.4", True),
        (">=24", "v22.23.4", False),
        (">= 18.0.0 <21", "20.1.0", True),
        (">= 18.0.0 <21", "21.0.0", False),
        ("18.x", "18.4.0", True),
        ("18.x", "19.0.0", False),
        ("~18.2", "18.2.9", True),
        ("~18.2", "18.3.0", False),
        ("18 - 20", "20.9.9", True),
        ("18 - 20", "21.0.0", False),
        ("18.1.2 - 20.3.4", "20.3.5", False),
        (">18", "18.9.0", False),
        (">18", "19.0.0", True),
        ("<=20", "20.5.0", True),
        ("^0.2.3", "0.2.9", True),
        ("^0.2.3", "0.3.0", False),
        ("^0.0.3", "0.0.4", False),
        ("*", "1.0.0", True),
        ("", "1.0.0", True),
        (">=18.0.0-rc.1", "20.0.0", None),  # a prerelease bound: not read, so unverified
        (">=18", "nightly", None),
    ],
)
def test_engines_node_is_read_as_node_semver(spec, version, expected):
    assert up._npm_satisfies(spec, version) is expected


@pytest.mark.parametrize(
    "spec, version, expected",
    [
        (">=16.0.0-dev,<17.0.0", "16.26.3", True),
        (">=16.0.0-dev,<17.0.0", "16.0.0-dev", True),
        (">=16.0.0-dev,<17.0.0", "17.0.0-dev", False),  # "<17.0.0" excludes 17's prereleases
        (">=15.0.0,<16.0.0", "16.0.0-dev", False),
        ("^15.2", "15.9.0", True),
        ("^15.2", "16.0.0", False),
        ("~=15.2", "15.9.1", True),
        ("==15.*", "15.40.0", True),
        ("==15", "16.0.0", False),
        (">15", "15.5.0", False),  # a partial: ">15" is ">=16.0.0"
        ("<=15", "15.5.0", True),
        ("!=15.1.0", "15.1.0", False),
        ("15.*.1", "15.40.4", True),  # a wildcard ends the version
        (">=15.0.0, <16.0.0", "15.1.0", None),  # a space: semantic_version raises on it
        ("!=15,~*.1", "16.0.0", None),  # every block parses before any matches
        (">=*.1.2-dev", "16.0.0", None),  # a wildcard major cannot carry a prerelease
        (">=15.0.0", "15.40", None),
    ],
)
def test_frappe_dependencies_are_read_as_semantic_version_simple_spec(spec, version, expected):
    assert up._simple_spec(spec, version) is expected


# ------------------------------------------------------------------ fail honest


def test_a_remote_that_does_not_answer_is_unverified_not_a_blocker(bench):
    app = bench.app("slowremote")
    # Local transport runs upload-pack through the shell: make it hang.
    bench.git("config", "remote.upstream.uploadpack", "sleep 30; git-upload-pack", cwd=app)

    started = time.monotonic()
    findings = bench.findings(net_timeout=0.5)

    assert time.monotonic() - started < 10  # the hung probe was killed, not waited on
    finding = by_code(findings, "git.remote_unverified")
    assert finding.severity == up.UNVERIFIED
    assert not [f for f in findings if f.severity == up.BLOCKER]


# ------------------------------------------------------------------ all at once


def test_every_problem_on_one_bench_is_reported_in_one_run(tmp_path):
    # bench stops at the FIRST of these it meets, after backing up every site; the
    # preflight reports them all, before anything runs.
    b = Bench(tmp_path)
    b.app("frappe", "version-16")
    b.git("remote", "set-url", "upstream", str(b.root / "gone.git"), cwd=b.app("deadremote"))
    b.git("checkout", "-q", "--detach", cwd=b.app("detached"))
    b.git("checkout", "-q", "-b", "feature-x", cwd=b.app("localbranch"))
    b.git("remote", "remove", "upstream", cwd=b.app("noremote"))
    b.git("remote", "rename", "upstream", "old-upstream", cwd=b.app("suffixremote"))
    dirty = b.app("dirty")
    (dirty / "dirty" / "__init__.py").write_text("x = 1\n")
    b.write_conf(maintenance_mode=1)
    (b.bin / "node").unlink()

    found = codes(b.findings())

    assert {
        ("git.remote_unreachable", "deadremote"),
        ("git.detached_head", "detached"),
        ("git.branch_not_on_remote", "localbranch"),
        ("git.no_remote", "noremote"),
        ("git.remote_misdetected", "suffixremote"),
        ("git.uncommitted", "dirty"),
        ("bench.maintenance_mode_on", None),
        ("tool.missing", None),
    } <= found


# ------------------------------------------------------------------ host side


class FakeContainer:
    """Answers the preflight exec, the batched site read and the lock probe."""

    def __init__(self, record=None, *, raw=None, site_errors=None, held=(), lock_error=None):
        self.record = record
        self.raw = raw
        self.site_errors = site_errors or {}
        self.held = set(held)
        self.lock_error = lock_error
        self.calls = []

    def exec_run(self, cmd, **_kwargs):
        self.calls.append(cmd)
        if cmd[4] == up._SCRIPT:
            if self.raw is not None:
                return 0, self.raw
            return 0, f"noise\n{up.SENTINEL}{json.dumps(self.record)}\n".encode()
        raise AssertionError(f"unexpected exec {cmd!r}")


@pytest.fixture()
def wired(monkeypatch):
    def fake_read(container, paths, *, sites=None, list_apps=False, **_):
        reads = [
            bench_read.SiteRead(
                name=s,
                site_config={},
                list_apps=None if s in container.site_errors else ["frappe"],
                installed=None,
                error=container.site_errors.get(s),
            )
            for s in sites
        ]
        bench = bench_read.BenchRead(
            path=paths[0],
            available_apps=[],
            app_imports={},
            common_site_config={},
            sites=reads,
            current_site=None,
            label=None,
        )
        return bench_read.BatchRead(exit_code=0, output="", benches={paths[0]: bench}, errors={})

    def fake_lock(container, bench_path, site):
        if container.lock_error:
            raise CwcliError(ErrorKind.PRECONDITION, "migrate.lock_probe_failed", "flock exited 1")
        return site in container.held

    monkeypatch.setattr(up.bench_read, "read_benches", fake_read)
    monkeypatch.setattr(up.bench_ops, "migrate_lock_held", fake_lock)


def _record(findings=(), sites=("a.localhost",)):
    return {
        "apps": ["frappe"],
        "sites": list(sites),
        "shallow_clone": True,
        "elapsed": 0.01,
        "findings": list(findings),
    }


def test_run_preflight_classifies_the_record_and_counts_apps_and_sites(wired):
    container = FakeContainer(
        _record([{"code": "git.detached_head", "app": "frappe"}], sites=["a.localhost", "b"])
    )

    outcome = up.run_preflight(container, "/w/frappe-bench", project="proj")

    assert outcome.blocked is True
    assert outcome.apps == 1 and outcome.sites == 2
    assert outcome.summary.startswith("1 app(s), 2 site(s) on /w/frappe-bench (")
    assert [f.code for f in outcome.findings] == ["git.detached_head"]


def test_unparseable_output_is_unverified_and_never_blocks(wired):
    outcome = up.run_preflight(
        FakeContainer(raw=b"no python3 in the container\n"), "/w/b", project="proj"
    )

    assert outcome.blocked is False
    finding = by_code(outcome.findings, "preflight.unverified")
    assert "no python3 in the container" in finding.message


def test_a_site_that_cannot_be_opened_blocks_and_any_other_read_failure_is_unverified(wired):
    container = FakeContainer(
        _record(sites=["down.localhost", "lazy.localhost", "odd.localhost", "ok.localhost"]),
        site_errors={
            "down.localhost": "frappe.connect failed (OperationalError)",
            # v16's connect is lazy: a rejected DB login surfaces at the first query.
            "lazy.localhost": "list-apps failed (OperationalError)",
            "odd.localhost": "list-apps failed (DoesNotExistError)",
        },
    )

    findings = up.run_preflight(container, "/w/b", project="proj").findings

    unreadable = [f for f in findings if f.code == "site.unreadable"]
    assert [(f.site, f.severity) for f in unreadable] == [
        ("down.localhost", up.BLOCKER),
        ("lazy.localhost", up.BLOCKER),
    ]
    odd = by_code(findings, "site.unverified")
    assert (odd.site, odd.severity) == ("odd.localhost", up.UNVERIFIED)


def test_a_held_migrate_lock_blocks_and_names_unlock(wired):
    container = FakeContainer(_record(), held={"a.localhost"})

    finding = by_code(
        up.run_preflight(container, "/w/b", project="proj", bench="1").findings,
        "site.migrate_locked",
    )

    assert finding.severity == up.BLOCKER
    assert finding.remedy.endswith("cwcli unlock proj --site a.localhost --bench 1")


def test_an_explicit_path_is_preserved_in_every_scoped_remedy(wired):
    path = "/workspace/bench with spaces"
    findings = up.classify(
        [
            {"code": "bench.maintenance_mode_on"},
            {"code": "git.dubious_ownership", "app": "other", "path": "/apps/other"},
            {"code": "git.detached_head", "app": "other"},
            {"code": "git.remote_unreachable", "app": "other", "remote": "origin", "error": "x"},
            {"code": "site.migrate_locked", "site": "a.localhost"},
        ],
        project="proj",
        path=path,
    )

    scoped = [finding.remedy for finding in findings if finding.remedy]
    assert len(scoped) == 5
    assert all("--path '/workspace/bench with spaces'" in remedy for remedy in scoped)


def test_an_unreadable_lock_is_unverified(wired):
    container = FakeContainer(_record(), lock_error=True)

    finding = by_code(
        up.run_preflight(container, "/w/b", project="proj").findings, "site.lock_unverified"
    )
    assert finding.severity == up.UNVERIFIED


def test_classify_orders_by_severity_and_quotes_names_in_remedies():
    findings = up.classify(
        [
            {"code": "git.no_remote", "app": "a"},
            {"code": "git.uncommitted", "app": "b", "count": 2, "shallow_clone": False},
            {"code": "git.detached_head", "app": "it's"},
        ],
        project="proj",
    )

    assert [f.severity for f in findings] == [up.BLOCKER, up.DATA_LOSS, up.NOTICE]
    assert findings[0].remedy == "cwcli apps checkout proj 'it'\"'\"'s' <branch>"
    assert (
        findings[1].message
        == "2 uncommitted change(s) to tracked files would be DISCARDED by the reset"
    )


def test_an_unknown_code_is_unverified_rather_than_dropped():
    (finding,) = up.classify([{"code": "git.something_new", "app": "a"}], project="proj")

    assert (finding.severity, finding.message) == (up.UNVERIFIED, "git.something_new")


def test_parse_reads_only_the_sentinel_line():
    assert up.parse("bench noise\n" + up.SENTINEL + '{"apps": []}\n') == {"apps": []}
    assert up.parse("no record here") is None
    assert up.parse(up.SENTINEL + "{truncated") is None
