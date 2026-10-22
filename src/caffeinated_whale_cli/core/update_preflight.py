"""The preflight for ``cwcli apps update <project> frappe``, UI-pure.

``bench update --reset`` runs its phases in a fixed order (frappe-bench 5.31.0,
``bench/utils/bench.py:455-516``): bench's own patches, the branch and version
checks, then it writes ``maintenance_mode: 1``/``pause_scheduler: 1`` bench-wide,
backs up EVERY site, and only then resets each app's git checkout. So a git problem
in any app surfaces after every site has been backed up - on a shared bench, most of
a half-hour wait - and it leaves the bench in maintenance mode with the scheduler
paused, because bench has no ``finally`` to undo the flags.

This module predicts those failures before anything runs, by evaluating bench's
OWN rules against the same state bench reads. It never reinvents them: every rule
in :data:`_PREFLIGHT_PY` carries a comment naming the bench line it mirrors, and a
rule that disagrees with bench is a bug here, not a judgement call.

Three tiers, all read-only:

- **local + services + network** run as ONE stdlib-only Python process inside the
  frappe container (the :mod:`.bench_read` pattern). It prints raw FACTS: a code
  per problem plus the details, never a sentence. Network probes are
  ``git ls-remote`` (never a fetch), bounded by a per-probe timeout that kills the
  probe's whole process group, so a hanging remote cannot hang the preflight.
- **sites** reuse :func:`.bench_read.read_benches`: one Frappe process connects to
  every site bench will back up, which is the step that would otherwise fail first.
- **migrate locks** reuse :func:`.bench_ops.migrate_lock_held`, the same probe
  ``cwcli axi migrate`` uses.

Classification (severity, plain-language message, cwcli remedy) happens HERE, on the
host, from the facts. Severity is one of :data:`BLOCKER` (bench will certainly
fail), :data:`DATA_LOSS` (bench will succeed and destroy local work),
:data:`NOTICE` (bench will do something surprising but not fail) and
:data:`UNVERIFIED` (the question could not be answered). Fail honest: a probe that
timed out or could not run is UNVERIFIED, never "fine" and never a blocker, and
only blockers stop the update.

What no preflight can know is stated rather than implied: patch failures, schema
sync errors, fixture/hook errors, JS build errors, dependency conflicts between new
pins and the new code's own tooling requirements only show up by running the real
update.
"""

from __future__ import annotations

import json
import shlex
import time
from dataclasses import dataclass

from . import bench_ops, bench_read
from .errors import CwcliError

BLOCKER = "blocker"
DATA_LOSS = "data_loss"
NOTICE = "notice"
UNVERIFIED = "unverified"

# Prefixes the script's one record line, so nothing else on stdout can be read as it.
SENTINEL = "@@CWCLI-UPDATE-PREFLIGHT@@"

# Per-probe bounds. A remote that does not answer an ls-remote in NET_TIMEOUT is
# reported unverified rather than waited on; GitHub answered in 0.6-3 s in the recon.
NET_TIMEOUT = 15.0
LOCAL_TIMEOUT = 10.0
TCP_TIMEOUT = 3.0
WORKERS = 8

# Each entry is one tool bench needs, as alternatives: the backup dump (frappe looks
# for either), its compression, and the asset build's yarn + node.
TOOLS = [["mariadb-dump", "mysqldump"], ["gzip"], ["yarn"], ["node"], ["git"]]

# The in-container script. Python 3.6 syntax (no walrus, no `capture_output`):
# v13 benches run old interpreters, and the system python3 is tried first.
_PREFLIGHT_PY = r'''
import glob, json, os, re, shutil, signal, socket, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor

opts = json.loads(sys.argv[1])
bench = sys.argv[2].rstrip("/")
ENV = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", LC_ALL="C")
START = time.time()


def dec(data):
    return (data or b"").decode("utf-8", "replace").strip()


def last_line(text):
    lines = [l for l in text.splitlines() if l.strip()]
    return lines[-1].strip() if lines else ""


def git(cwd, args, timeout):
    """(returncode, stdout, stderr); returncode None when it timed out or never ran.

    Its own session, so a timeout kills git AND whatever it spawned (ssh, a remote
    helper), which would otherwise hold the pipes open past the timeout."""
    try:
        proc = subprocess.Popen(
            ["git"] + args, cwd=cwd, env=ENV, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )
    except OSError as error:
        return None, "", str(error)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.communicate()
        return None, "", "no answer within %ss" % timeout
    return proc.returncode, dec(out), dec(err)


def is_frappe_app(path):
    # bench/utils/__init__.py:62-72 (paths_in_app, :34). glob's "**" without
    # recursive=True matches ONE directory level, exactly as bench calls it.
    return all(glob.glob(os.path.join(path, "**", f)) for f in ("hooks.py", "modules.txt", "patches.txt"))


def check_app(app, excluded, shallow):
    found = []
    probes = []

    def add(code, **facts):
        facts.update(code=code, app=app)
        found.append(facts)

    d = os.path.join(bench, "apps", app)
    # bench/app.py:1022-1024: an excluded app is skipped, nothing is reset.
    if app in excluded:
        add("app.excluded")
        return found, probes, d
    # bench/app.py:1026: only a dir holding .git is pulled; anything else is skipped.
    if not os.path.exists(os.path.join(d, ".git")):
        add("app.not_git")
        return found, probes, d
    rc, out, err = git(d, ["rev-parse", "--git-dir"], opts["local_timeout"])
    if rc != 0:
        if "dubious ownership" in err:
            add("git.dubious_ownership", path=os.path.realpath(d))
        else:
            add("git.unreadable", error=last_line(err) or "git did not answer")
        return found, probes, d

    # bench/utils/app.py:215-228 (get_remote): "upstream" whenever `git remote -v`
    # matches upstream[\s]+ (so "old-upstream" matches too), else the first remote.
    rc, remote_v, err = git(d, ["remote", "-v"], opts["local_timeout"])
    rc2, names, _ = git(d, ["remote"], opts["local_timeout"])
    if rc != 0 or rc2 != 0:
        add("git.unreadable", error=last_line(err) or "git remote did not answer")
        return found, probes, d
    remotes = names.split()
    # bench/utils/app.py:141-166: the version check reads upstream/<branch> of frappe,
    # before any app is skipped for having no remote.
    no_upstream = app == "frappe" and "upstream" not in remotes
    if no_upstream:
        add("frappe.no_upstream", remotes=remotes)
    if re.findall(r"upstream[\s]+", remote_v):
        remote = "upstream"
    elif not remote_v:
        # bench/app.py:1028-1035: skipped AND appended to sites/excluded_apps.txt.
        if not no_upstream:
            add("git.no_remote")
        return found, probes, d
    else:
        remote = remote_v.splitlines()[0].split()[0]

    if not no_upstream and remote not in remotes:
        add("git.remote_misdetected", remote=remote, remotes=remotes)

    # bench/utils/app.py:176-180 (get_current_branch): `git symbolic-ref`, which
    # fails on a detached HEAD.
    rc, branch, _ = git(d, ["symbolic-ref", "-q", "--short", "HEAD"], opts["local_timeout"])
    if rc != 0 or not branch:
        add("git.detached_head")
        branch = None
    # bench/utils/bench.py:667-694 (validate_branch).
    if app in ("frappe", "erpnext") and branch == "master":
        add("git.master_branch")

    for path in (d, os.path.join(d, ".git")):
        if os.path.isdir(path) and not os.access(path, os.W_OK):
            add("app.not_writable", path=os.path.realpath(path))
            break

    # What `git reset --hard <remote>/<branch>` discards: tracked changes and commits
    # not on the remote branch. Untracked files survive a reset, so they do not count.
    rc, status, err = git(
        d, ["status", "--porcelain", "--untracked-files=no"], opts["local_timeout"]
    )
    if rc == 0:
        tracked = [l for l in status.splitlines() if l]
        if tracked:
            add("git.uncommitted", count=len(tracked), shallow_clone=shallow)
    else:
        add("git.local_work_unverified", error=last_line(err) or "git status did not answer")
    # Commits only this branch holds: on no remote ref, other branch or tag. The
    # shallow boundary is excluded too: a shallow clone only ever receives those
    # commits FROM a remote, and once a depth-1 fetch moves the remote ref past one
    # (bench's own version check fetches frappe that way) its history is cut, so
    # "<remote>/<branch>..HEAD" would count a commit the remote always had.
    if branch:
        try:
            with open(os.path.join(d, ".git", "shallow")) as handle:
                boundary = handle.read().split()
        except OSError:
            boundary = []
        rc, ahead, err = git(
            d, ["rev-list", "--count", "--ignore-missing", "HEAD", "--not",
                "--exclude=" + branch, "--branches", "--tags", "--remotes"] + boundary,
            opts["local_timeout"],
        )
        if rc != 0 or not ahead.isdigit():
            add("git.local_work_unverified", error=last_line(err) or "git rev-list did not answer")
        elif int(ahead) > 0:
            add("git.unpushed", count=int(ahead), branch=branch, shallow_clone=shallow)

    # bench/app.py:1039-1047: with shallow_clone bench fetches ONLY <remote> <branch>
    # (`git fetch --depth=1 --no-tags`); without it, `git fetch --all` contacts EVERY
    # remote, so one dead remote nobody uses stops the update.
    if remote in remotes:
        probes.append((remote, branch))
    if not shallow:
        probes.extend((r, None) for r in remotes if r != remote)
    return found, probes, d


def probe(app, d, remote, branch, shallow):
    if branch:
        args = ["ls-remote", "--exit-code", "--heads", remote, "refs/heads/" + branch]
    else:
        args = ["ls-remote", remote, "HEAD"]
    rc, _, err = git(d, args, opts["net_timeout"])
    base = {"app": app, "remote": remote}
    if rc is None:
        return [dict(base, code="git.remote_unverified", error=err)]
    if rc == 2 and branch:
        # Without shallow_clone a stale local <remote>/<branch> ref survives
        # `git fetch --all` (no --prune), and bench resets to its old commit.
        stale = git(d, ["rev-parse", "--verify", "-q", "refs/remotes/%s/%s" % (remote, branch)],
                    opts["local_timeout"])[0] == 0
        code = "git.branch_deleted_upstream" if stale and not shallow else "git.branch_not_on_remote"
        return [dict(base, code=code, branch=branch)]
    if rc != 0:
        return [dict(base, code="git.remote_unreachable", error=last_line(err) or "exit %s" % rc)]
    return []


def tcp(kind, host, port):
    try:
        socket.create_connection((host, int(port)), timeout=opts["tcp_timeout"]).close()
    except socket.timeout:
        return [{"code": "service.unverified", "service": kind, "endpoint": "%s:%s" % (host, port)}]
    except (OSError, ValueError) as error:
        return [{"code": "service.unreachable", "service": kind,
                 "endpoint": "%s:%s" % (host, port), "error": type(error).__name__}]
    return []


def main():
    out = []
    sites_dir = os.path.join(bench, "sites")
    # bench/config/common_site_config.py:36-41: a missing file is {}, invalid JSON raises.
    conf = {}
    try:
        with open(os.path.join(sites_dir, "common_site_config.json")) as handle:
            conf = json.load(handle)
    except FileNotFoundError:
        pass
    except Exception as error:
        out.append({"code": "bench.config_unreadable", "error": type(error).__name__})
    if not isinstance(conf, dict):
        conf = {}
    if conf.get("release_bench"):  # bench/utils/bench.py:471-472
        out.append({"code": "bench.release_bench"})
    if conf.get("maintenance_mode"):
        out.append({"code": "bench.maintenance_mode_on"})
    if conf.get("pause_scheduler"):
        out.append({"code": "bench.scheduler_paused"})
    shallow = bool(conf.get("shallow_clone"))  # bench/app.py:1029, :1040

    for sub in ("sites", "env", "logs"):
        path = os.path.join(bench, sub)
        if not os.access(path, os.W_OK):
            out.append({"code": "bench.not_writable", "path": path})
    for alternatives in opts["tools"]:
        if not any(shutil.which(tool) for tool in alternatives):
            out.append({"code": "tool.missing", "tool": " or ".join(alternatives)})

    # bench/bench.py:96-102: the sites bench backs up are dirs holding site_config.json.
    try:
        sites = sorted(s for s in os.listdir(sites_dir)
                       if os.path.exists(os.path.join(sites_dir, s, "site_config.json")))
    except OSError:
        sites = []
    endpoints = set()
    latest = 0
    for site in sites:
        site_conf = {}
        try:
            with open(os.path.join(sites_dir, site, "site_config.json")) as handle:
                site_conf = json.load(handle) or {}
        except Exception:
            pass
        host = site_conf.get("db_host") or conf.get("db_host")
        if host:
            port = site_conf.get("db_port") or conf.get("db_port") or 3306
            endpoints.add(("database", host, str(port)))
        dumps = glob.glob(os.path.join(sites_dir, site, "private", "backups", "*-database.sql*"))
        if dumps:
            latest += os.path.getsize(max(dumps, key=os.path.getmtime))
    redis = conf.get("redis_cache")
    if isinstance(redis, str) and "://" in redis:
        hostport = redis.split("://", 1)[1].split("/", 1)[0].rsplit("@", 1)[-1]
        host, _, port = hostport.partition(":")
        if host:
            endpoints.add(("redis_cache", host, port or "6379"))
    for kind, host, port in sorted(endpoints):
        out.extend(tcp(kind, host, port))

    try:
        stat = os.statvfs(sites_dir)
        free_mb = stat.f_bavail * stat.f_frsize // (1024 * 1024)
        need_mb = (latest * 2) // (1024 * 1024) + 64
        if free_mb < opts["disk_floor_mb"]:
            out.append({"code": "disk.full", "free_mb": free_mb})
        elif free_mb < need_mb:
            out.append({"code": "disk.low", "free_mb": free_mb, "need_mb": need_mb})
    except OSError:
        pass

    try:
        with open(os.path.join(sites_dir, "excluded_apps.txt")) as handle:
            excluded = handle.read().strip().split("\n")  # bench/app.py:628-633
    except OSError:
        excluded = []
    apps_dir = os.path.join(bench, "apps")
    try:
        names = [a for a in os.listdir(apps_dir) if is_frappe_app(os.path.join(apps_dir, a))]
    except OSError:
        names = []
    # bench/bench.py:282-292: frappe first, the rest in directory order (sorted here
    # so the report is stable; the order bench meets them in is not knowable anyway).
    apps = (["frappe"] if "frappe" in names else []) + sorted(a for a in names if a != "frappe")

    with ThreadPoolExecutor(max_workers=opts["workers"]) as pool:
        checked = list(pool.map(lambda a: check_app(a, excluded, shallow), apps))
        jobs = [(app, d, r, b) for app, (_, probes, d) in zip(apps, checked) for r, b in probes]
        probed = list(pool.map(lambda job: probe(job[0], job[1], job[2], job[3], shallow), jobs))
    for app, (found, _, _) in zip(apps, checked):
        out.extend(found)
        for job, result in zip(jobs, probed):
            if job[0] == app:
                out.extend(result)

    record = {"apps": apps, "sites": sites, "shallow_clone": shallow,
              "elapsed": round(time.time() - START, 3), "findings": out}
    sys.stdout.write("\n" + SENTINEL + json.dumps(record) + "\n")


main()
'''

_SCRIPT = f"SENTINEL = {SENTINEL!r}\n" + _PREFLIGHT_PY

# Prefer the system python3: the bench venv's interpreter is the one thing a
# recreated v13/v14 container can lose, and the script needs only the stdlib.
_RUN_SH = (
    'for py in /usr/bin/python3 python3 "$3/env/bin/python"; do '
    'if command -v "$py" >/dev/null 2>&1; then exec "$py" -c "$1" "$2" "$3"; fi; '
    'done; echo "no python3 in the container" >&2; exit 127'
)


@dataclass(frozen=True, slots=True, kw_only=True)
class PreflightFinding:
    """One thing the preflight found. ``code`` is stable; branch on it, not on prose."""

    severity: str  # BLOCKER | DATA_LOSS | NOTICE | UNVERIFIED
    code: str
    app: str | None
    site: str | None
    message: str
    remedy: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class PreflightOutcome:
    findings: list[PreflightFinding]
    apps: int
    sites: int
    elapsed: float
    bench_path: str

    @property
    def blocked(self) -> bool:
        return any(f.severity == BLOCKER for f in self.findings)

    @property
    def summary(self) -> str:
        return (
            f"{self.apps} app(s), {self.sites} site(s) on {self.bench_path} "
            f"({self.elapsed:.1f}s)"
        )


# code -> (severity, message, remedy). Messages and remedies are str.format templates
# over the finding's facts plus `project`, `bench_flag` and shell-quoted `q_*` names.
# Remedies follow the "Suggested commands" convention: a cwcli verb where one exists,
# else a `cwcli open` shell step, with the live names substituted.
_OPEN = "in `cwcli open {q_project}{bench_flag}`: "
_RULES: dict[str, tuple[str, str, str | None]] = {
    "bench.config_unreadable": (
        BLOCKER,
        "sites/common_site_config.json could not be read ({error}); bench reads it first",
        "repair the file " + _OPEN + "edit sites/common_site_config.json",
    ),
    "bench.release_bench": (
        BLOCKER,
        "release_bench is set in sites/common_site_config.json, so bench refuses to update",
        None,
    ),
    "bench.maintenance_mode_on": (
        NOTICE,
        "maintenance_mode is already 1 bench-wide (a previous update may have failed); "
        "bench clears it only when this update succeeds",
        "cwcli run {q_project}{bench_flag} set-config --global --parse maintenance_mode 0",
    ),
    "bench.scheduler_paused": (
        NOTICE,
        "pause_scheduler is already 1 bench-wide (a previous update may have failed); "
        "bench clears it only when this update succeeds",
        "cwcli run {q_project}{bench_flag} set-config --global --parse pause_scheduler 0",
    ),
    "bench.not_writable": (
        BLOCKER,
        "{path} is missing or not writable by the container user",
        None,
    ),
    "tool.missing": (BLOCKER, "{tool} is not on the container's PATH", None),
    "disk.full": (
        BLOCKER,
        "only {free_mb} MiB free on the bench filesystem; the site backups would fail",
        None,
    ),
    "disk.low": (
        NOTICE,
        "{free_mb} MiB free on the bench filesystem; the site backups alone may need "
        "about {need_mb} MiB",
        None,
    ),
    "service.unreachable": (
        BLOCKER,
        "{service} at {endpoint} is unreachable ({error})",
        "check the instance's services: cwcli status {q_project}",
    ),
    "service.unverified": (
        UNVERIFIED,
        "{service} at {endpoint} did not answer within the timeout",
        None,
    ),
    "app.excluded": (
        NOTICE,
        "listed in sites/excluded_apps.txt, so bench will not update it",
        None,
    ),
    "app.not_git": (NOTICE, "not a git checkout, so bench will not update it", None),
    "git.dubious_ownership": (
        BLOCKER,
        "git refuses the repository at {path}: another user owns it (dubious ownership)",
        "cwcli apps update {q_project} {q_app}{bench_flag} --force re-owns it to the "
        "container user (and pulls that app)",
    ),
    "git.unreadable": (UNVERIFIED, "git could not read the repository ({error})", None),
    "git.no_remote": (
        NOTICE,
        "no git remote: bench skips it and adds it to sites/excluded_apps.txt, so later "
        "updates skip it too",
        None,
    ),
    "frappe.no_upstream": (
        BLOCKER,
        "frappe has no remote named 'upstream' (remotes: {remotes}); bench's version "
        "check reads upstream/<branch>",
        _OPEN + "git -C apps/frappe remote add upstream https://github.com/frappe/frappe "
        "&& git -C apps/frappe fetch upstream",
    ),
    "git.remote_misdetected": (
        BLOCKER,
        "bench would reset to remote '{remote}', which does not exist (remotes: "
        "{remotes}); bench picks 'upstream' whenever a remote's name or URL ends in it",
        _OPEN + "git -C apps/{q_app} remote rename <remote> <name>",
    ),
    "git.detached_head": (
        BLOCKER,
        "HEAD is detached; bench needs a branch to reset to",
        "cwcli apps checkout {q_project} {q_app} <branch>{bench_flag}",
    ),
    "git.master_branch": (
        BLOCKER,
        "on branch 'master', which bench refuses for {app}",
        "cwcli apps checkout {q_project} {q_app} <version-branch>{bench_flag}",
    ),
    "app.not_writable": (BLOCKER, "{path} is not writable by the container user", None),
    "git.uncommitted": (
        DATA_LOSS,
        "{count} uncommitted change(s) to tracked files would be DISCARDED by the reset",
        None,
    ),
    "git.unpushed": (
        DATA_LOSS,
        "{count} commit(s) on '{branch}' exist nowhere else (no remote, branch or tag) "
        "and would be DISCARDED by the reset",
        None,
    ),
    "git.local_work_unverified": (
        UNVERIFIED,
        "could not tell whether the reset would discard local work ({error})",
        None,
    ),
    "git.remote_unreachable": (
        BLOCKER,
        "remote '{remote}' is unreachable ({error})",
        _OPEN + "repair it, or remove it with git -C apps/{q_app} remote remove {q_remote}",
    ),
    "git.remote_unverified": (
        UNVERIFIED,
        "remote '{remote}' could not be checked ({error})",
        None,
    ),
    "git.branch_not_on_remote": (
        BLOCKER,
        "branch '{branch}' does not exist on remote '{remote}'",
        "cwcli apps checkout {q_project} {q_app} <published-branch>{bench_flag}",
    ),
    "git.branch_deleted_upstream": (
        NOTICE,
        "branch '{branch}' no longer exists on '{remote}'; bench would reset to the "
        "stale local {remote}/{branch}",
        "cwcli apps checkout {q_project} {q_app} <published-branch>{bench_flag}",
    ),
    "site.unreadable": (
        BLOCKER,
        "site '{site}' could not be opened ({error}); bench backs up every site first, "
        "and this backup would fail",
        "cwcli status {q_project}",
    ),
    "site.unverified": (UNVERIFIED, "site '{site}' could not be checked ({error})", None),
    "sites.unverified": (UNVERIFIED, "the bench's sites could not be checked ({error})", None),
    "site.migrate_locked": (
        BLOCKER,
        "site '{site}' holds an active migrate lock; bench's migrate would fail on it",
        "wait for the running migrate, or clear a stranded lock: "
        "cwcli unlock {q_project} --site {q_site}{bench_flag}",
    ),
    "site.lock_unverified": (
        UNVERIFIED,
        "the migrate lock of site '{site}' could not be checked ({error})",
        None,
    ),
    "preflight.unverified": (
        UNVERIFIED,
        "the preflight checks could not run ({error}), so apps, remotes and services "
        "were not checked",
        None,
    ),
}

_ORDER = {BLOCKER: 0, DATA_LOSS: 1, UNVERIFIED: 2, NOTICE: 3}


def classify(
    facts: list[dict], *, project: str, bench: str | None = None
) -> list[PreflightFinding]:
    """Turn raw facts into typed findings, blockers first, then by app/site."""
    bench_flag = f" --bench {shlex.quote(bench)}" if bench else ""
    findings = []
    for fact in facts:
        code = str(fact.get("code"))
        severity, message, remedy = _RULES.get(code, (UNVERIFIED, code, None))
        fields = {
            k: (", ".join(v) or "none") if isinstance(v, list) else v for k, v in fact.items()
        }
        fields.update(
            project=project,
            bench_flag=bench_flag,
            q_project=shlex.quote(project),
            q_app=shlex.quote(str(fact.get("app") or "")),
            q_site=shlex.quote(str(fact.get("site") or "")),
            q_remote=shlex.quote(str(fact.get("remote") or "")),
            q_path=shlex.quote(str(fact.get("path") or "")),
        )
        if code == "git.dubious_ownership" and fact.get("app") == "frappe":
            remedy = _OPEN + "sudo chown -R frappe:frappe {q_path}"
        if code == "git.uncommitted" or code == "git.unpushed":
            if fact.get("shallow_clone"):
                # bench/app.py:1043-1044: reflog expire + gc --prune=all after the reset.
                message += "; shallow_clone also prunes the reflog, so it cannot be recovered"
        try:
            text = message.format(**fields)
            fix = remedy.format(**fields) if remedy else None
        except (KeyError, IndexError, ValueError):
            text, fix = code, None
        findings.append(
            PreflightFinding(
                severity=severity,
                code=code,
                app=fact.get("app"),
                site=fact.get("site"),
                message=text,
                remedy=fix,
            )
        )
    # Stable: within a severity the script's own order (bench-level, then per app in
    # bench order) is kept.
    return sorted(findings, key=lambda f: _ORDER.get(f.severity, 9))


def parse(output: str) -> dict | None:
    """The script's record, or None when no readable record was printed."""
    for line in reversed(output.splitlines()):
        if line.startswith(SENTINEL):
            try:
                record = json.loads(line[len(SENTINEL) :])
            except ValueError:
                return None
            return record if isinstance(record, dict) else None
    return None


def script_argv(bench_path: str, *, net_timeout: float = NET_TIMEOUT) -> list[str]:
    """The exec argv. Paths and options ride as argv, never interpolated."""
    opts = json.dumps(
        {
            "net_timeout": net_timeout,
            "local_timeout": LOCAL_TIMEOUT,
            "tcp_timeout": TCP_TIMEOUT,
            "workers": WORKERS,
            "tools": TOOLS,
            "disk_floor_mb": 100,
        }
    )
    return ["sh", "-c", _RUN_SH, "sh", _SCRIPT, opts, bench_path]


def _site_facts(container, bench_path: str, sites: list[str]) -> list[dict]:
    """The sites tier: one Frappe process opens every site bench will back up."""
    if not sites:
        return []
    batch = bench_read.read_benches(container, [bench_path], sites=sites, list_apps=True)
    read = batch.benches.get(bench_path)
    if read is None or read.sites is None:
        error = batch.errors.get(bench_path, "the read left no record")
        return [{"code": "sites.unverified", "error": error}]
    facts = []
    for site in read.sites:
        if site.list_apps is not None:
            continue
        error = site.error or "no answer"
        # Only a failed init/connect predicts the backup's failure for certain (it
        # opens the same site_config.json and database); anything else is unverified.
        certain = error.startswith(("frappe.init failed", "frappe.connect failed"))
        facts.append(
            {
                "code": "site.unreadable" if certain else "site.unverified",
                "site": site.name,
                "error": error,
            }
        )
    return facts


def _lock_facts(container, bench_path: str, sites: list[str]) -> list[dict]:
    facts = []
    for site in sites:
        try:
            if bench_ops.migrate_lock_held(container, bench_path, site):
                facts.append({"code": "site.migrate_locked", "site": site})
        except CwcliError as error:
            facts.append({"code": "site.lock_unverified", "site": site, "error": error.message})
    return facts


def run_preflight(
    container, bench_path: str, *, project: str, bench: str | None = None
) -> PreflightOutcome:
    """Run every tier against ``bench_path`` and return the classified outcome.

    Never raises for a check that could not run: that is an UNVERIFIED finding. A
    lost Docker connection raises what ``exec_run`` raises, as every read does.
    """
    started = time.monotonic()
    exit_code, output = container.exec_run(script_argv(bench_path))
    raw = output[0] if isinstance(output, tuple) else output
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw or "")
    record = parse(text)
    facts: list[dict]
    if record is None:
        tail = [line for line in text.strip().splitlines() if line.strip()][-1:]
        error = tail[0] if tail else f"exit code {exit_code}, no output"
        facts, apps, sites = [{"code": "preflight.unverified", "error": error}], [], []
    else:
        facts = [f for f in record.get("findings", []) if isinstance(f, dict)]
        apps = [a for a in record.get("apps", []) if isinstance(a, str)]
        sites = [s for s in record.get("sites", []) if isinstance(s, str)]
    facts += _site_facts(container, bench_path, sites)
    facts += _lock_facts(container, bench_path, sites)
    return PreflightOutcome(
        findings=classify(facts, project=project, bench=bench),
        apps=len(apps),
        sites=len(sites),
        elapsed=time.monotonic() - started,
        bench_path=bench_path,
    )
