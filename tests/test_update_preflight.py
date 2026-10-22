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

from caffeinated_whale_cli.core import bench_ops, bench_read
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
        # The tools bench needs, present unless a test takes one away.
        for tool in ("mariadb-dump", "gzip", "yarn", "node"):
            stub = self.bin / tool
            stub.write_text("#!/bin/sh\nexit 0\n")
            stub.chmod(0o755)
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

    def git(self, *args, cwd=None):
        subprocess.run(
            [GIT, *args], cwd=cwd, env=self.env, check=True, capture_output=True, text=True
        )

    def write_conf(self, **extra):
        self.conf.update(extra)
        (self.path / "sites" / "common_site_config.json").write_text(json.dumps(self.conf))

    def app(self, name, branch="main", *, version="1.0.0", depth=None):
        """A bare remote plus a clone of it at apps/<name>, remote named upstream."""
        src = self.remotes / f"{name}.src"
        (src / name).mkdir(parents=True)
        (src / name / "__init__.py").write_text(f'__version__ = "{version}"\n')
        (src / name / "hooks.py").write_text(f'app_name = "{name}"\n')
        (src / name / "modules.txt").write_text("")
        (src / name / "patches.txt").write_text("")
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
        return up.classify(self.run(**opts)["findings"], project="proj")


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

    finding = by_code(findings, "git.dubious_ownership")
    assert finding.severity == up.BLOCKER
    assert "--force" in finding.remedy


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
    (bench.path / "env").rmdir()

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


# ------------------------------------------------------------------ notices


def test_an_app_without_a_remote_is_a_notice(bench):
    app = bench.app("noremote")
    bench.git("remote", "remove", "upstream", cwd=app)

    finding = by_code(bench.findings(), "git.no_remote")
    assert finding.severity == up.NOTICE
    assert "excluded_apps.txt" in finding.message


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


def test_a_shallow_repo_is_unshallowed_first_without_shallow_clone(tmp_path):
    b = Bench(tmp_path, shallow_clone=False)
    b.app("frappe", "version-16", depth=1)

    assert by_code(b.findings(), "git.unshallow").severity == up.NOTICE


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
        _record(sites=["down.localhost", "odd.localhost", "ok.localhost"]),
        site_errors={
            "down.localhost": "frappe.connect failed (OperationalError)",
            "odd.localhost": "list-apps failed (DoesNotExistError)",
        },
    )

    findings = up.run_preflight(container, "/w/b", project="proj").findings

    down = by_code(findings, "site.unreadable")
    assert (down.site, down.severity) == ("down.localhost", up.BLOCKER)
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


def test_an_unreadable_lock_is_unverified(wired):
    container = FakeContainer(_record(), lock_error=True)

    finding = by_code(
        up.run_preflight(container, "/w/b", project="proj").findings, "site.lock_unverified"
    )
    assert finding.severity == up.UNVERIFIED


def test_the_lock_probe_is_bench_ops_own_not_a_copy():
    # Promoted, not copied: the preflight and `axi migrate` share one probe.
    assert up.bench_ops.migrate_lock_held is bench_ops.migrate_lock_held


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
