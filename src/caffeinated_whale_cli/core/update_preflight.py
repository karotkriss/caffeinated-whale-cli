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

Its tiers:

- **local + services + network + content** run as ONE stdlib-only Python process
  inside the frappe container (the :mod:`.bench_read` pattern). It prints raw
  FACTS: a code per problem plus the details, never a sentence. Network probes are
  ``git ls-remote``, bounded by a per-probe timeout that ends the probe's whole
  process group, so a hanging remote cannot hang the preflight. The content tier is
  the one write: for an app whose branch its remote has, the same ``git fetch`` of
  that branch bench's reset runs (remote-tracking ref and objects, never the
  working tree), then the manifests at that ref, compared on the host
  (:func:`_content_facts`) with the bench venv's Python and the container's node.
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

The data-loss findings are the exception, because they are a guard rather than a
prediction: ``core.update`` refuses on :func:`local_work` unless ``--force``, even
under ``--no-preflight`` (which runs the local tier alone for it), and local work
that could not be checked refuses too.

What no preflight can know is stated rather than implied: patch failures, schema
sync errors, fixture/hook errors, JS build errors and dependency conflicts between
new pins only show up by running the real update.
"""

from __future__ import annotations

import dataclasses
import json
import operator
import re
import shlex
import time
from dataclasses import dataclass

import toml

from . import bench_ops, bench_read
from .app_deps import app_version
from .app_deps import parsed as _parsed
from .app_deps import semver as _semver
from .app_deps import simple_spec as _simple_spec
from .app_deps import spec_floor_major as _spec_floor_major
from .app_deps import table as _table
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
# The content tier's fetch moves real objects (a new frappe major is a whole new tree);
# bench reuses them, so a slow fetch costs the update nothing it would not spend anyway.
FETCH_TIMEOUT = 120.0
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
# No GIT_SSH_COMMAND/BatchMode: it would override each app's core.sshCommand and any env ssh
# setup that bench's own fetch honors. No-hang already holds: stdin=DEVNULL, no TTY in the
# docker exec, and the process-group kill at the probe timeout.
ENV = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", LC_ALL="C")
START = time.time()


def dec(data):
    return (data or b"").decode("utf-8", "replace").strip()


def git_error(text):
    """git's cause: its first fatal: line, else the last line (a hint often trails it)."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    fatal = [l for l in lines if l.startswith("fatal:")]
    return fatal[0] if fatal else (lines[-1] if lines else "")


def run(argv, cwd, timeout):
    """(returncode, stdout, stderr); returncode None when it timed out or never ran.

    Its own session, so a timeout kills the command AND whatever it spawned (ssh, a
    remote helper), which would otherwise hold the pipes open past the timeout. TERM
    first: a fetch killed outright would strand its ref or shallow lock, and then
    bench's own fetch fails on it."""
    try:
        proc = subprocess.Popen(
            argv, cwd=cwd, env=ENV, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )
    except OSError as error:
        return None, "", str(error)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except OSError:
                pass
            try:
                proc.communicate(timeout=5)
                break
            except subprocess.TimeoutExpired:
                pass
        return None, "", "no answer within %ss" % timeout
    return proc.returncode, dec(out), dec(err)


def git(cwd, args, timeout):
    return run(["git"] + args, cwd, timeout)


def is_frappe_app(path):
    # bench/utils/__init__.py:62-72 (paths_in_app, :34). glob's "**" without
    # recursive=True matches ONE directory level, exactly as bench calls it.
    return all(glob.glob(os.path.join(path, "**", f)) for f in ("hooks.py", "modules.txt", "patches.txt"))


def maps_branch(specs, remote, branch):
    """Whether a fetch refspec stores refs/heads/<branch> as refs/remotes/<remote>/<branch>."""
    src, dst = "refs/heads/" + branch, "refs/remotes/%s/%s" % (remote, branch)
    mapped = False
    for spec in specs:
        negative = spec.startswith("^")
        left, _, right = spec.lstrip("+^").partition(":")
        pre, star, post = left.partition("*")
        if not star:
            hit = left == src and (negative or right == dst)
        elif src.startswith(pre) and src.endswith(post) and len(src) >= len(pre) + len(post):
            hit = negative or right.replace("*", src[len(pre):len(src) - len(post)], 1) == dst
        else:
            hit = False
        if hit and negative:
            return False
        mapped = mapped or hit
    return mapped


def shallow_boundary(d):
    try:
        with open(os.path.join(d, ".git", "shallow")) as handle:
            return handle.read().split()
    except OSError:
        return []


def count_ahead(d, branch, extra=()):
    """Commits only this branch holds: on no remote ref, other branch or tag. The
    shallow boundary is excluded too: a shallow clone only ever receives those
    commits FROM a remote, and once a depth-1 fetch moves the remote ref past one
    (bench's own version check fetches frappe that way) its history is cut, so
    "<remote>/<branch>..HEAD" would count a commit the remote always had."""
    return git(
        d, ["rev-list", "--count", "--ignore-missing", "HEAD", "--not",
            "--exclude=" + branch, "--branches", "--tags", "--remotes"]
        + shallow_boundary(d) + list(extra),
        opts["local_timeout"],
    )


def check_app(app, excluded, shallow):
    found = []
    probes = []

    def add(code, to=None, **facts):
        facts.update(code=code, app=app)
        (found if to is None else to).append(facts)

    d = os.path.join(bench, "apps", app)
    core = app in ("frappe", "erpnext")
    pulled = False
    # bench/app.py:1022-1024: an excluded app is skipped, nothing is reset.
    if app in excluded:
        add("app.excluded")
    # bench/app.py:1026: only a dir holding .git is pulled; anything else is skipped.
    elif not os.path.exists(os.path.join(d, ".git")):
        if not core:
            add("app.not_git")
    else:
        pulled = True
    # bench/utils/bench.py:667-694 (validate_branch) and :141-166 (is_version_upgrade)
    # read frappe and erpnext from Bench('.').apps, which filters neither of the above,
    # so their branch and upstream rules still apply to a skipped one.
    if not pulled and not core:
        return found, probes, d
    rc, out, err = git(d, ["rev-parse", "--git-dir"], opts["local_timeout"])
    if rc != 0:
        if "dubious ownership" in err:
            add("git.dubious_ownership", path=os.path.realpath(d))
        elif core and "not a git repository" in err:
            add("git.not_a_checkout")
        else:
            add("git.unreadable", error=git_error(err) or "git did not answer")
        return found, probes, d

    # bench/utils/app.py:215-228 (get_remote): "upstream" whenever `git remote -v`
    # matches upstream[\s]+ (so "old-upstream" matches too), else the first remote.
    rc, remote_v, err = git(d, ["remote", "-v"], opts["local_timeout"])
    rc2, names, _ = git(d, ["remote"], opts["local_timeout"])
    if rc != 0 or rc2 != 0:
        add("git.unreadable", error=git_error(err) or "git remote did not answer")
        return found, probes, d
    remotes = names.split()
    # bench/utils/app.py:141-166: the version check reads upstream/<branch> of frappe,
    # before any app is skipped for having no remote.
    no_upstream = app == "frappe" and "upstream" not in remotes
    if no_upstream:
        add("frappe.no_upstream", remotes=remotes)

    # bench/utils/app.py:176-180 (get_current_branch): `git symbolic-ref`, which
    # fails on a detached HEAD.
    rc, branch, _ = git(d, ["symbolic-ref", "-q", "--short", "HEAD"], opts["local_timeout"])
    if rc != 0 or not branch:
        branch = None
    # bench/utils/bench.py:667-694 (validate_branch).
    if core and branch is None:
        add("git.detached_head")
    if core and branch == "master":
        add("git.master_branch")
    if not pulled:
        return found, probes, d

    remote = None
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

    # bench/app.py (pull_apps): get_current_branch for every app it goes on to reset.
    if not core and branch is None:
        add("git.detached_head")

    # bench/app.py (pull_apps) resets to <remote>/<branch>, a ref its fetch only writes
    # when the remote's fetch refspec maps refs/heads/<branch> to it; a --depth 1
    # clone's refspec covers only the branch it cloned, and `cwcli apps checkout`
    # fetches into FETCH_HEAD, so a checked-out feature branch can have no such ref.
    covered, stale, held = True, False, []
    if branch and remote and remote in remotes:
        rc, specs, _ = git(d, ["config", "--get-all", "remote.%s.fetch" % remote],
                           opts["local_timeout"])
        covered = rc not in (0, 1) or maps_branch(specs.split(), remote, branch)
        stale = git(d, ["rev-parse", "--verify", "-q", "refs/remotes/%s/%s" % (remote, branch)],
                    opts["local_timeout"])[0] == 0

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
        add("git.local_work_unverified", error=git_error(err) or "git status did not answer")
    if branch:
        rc, ahead, err = count_ahead(d, branch)
        if rc != 0 or not ahead.isdigit():
            add("git.local_work_unverified", error=git_error(err) or "git rev-list did not answer")
        elif int(ahead) > 0:
            # Without a tracking ref the remote's copy of these commits is not visible
            # locally: the probe recounts them against the remote's tip.
            add("git.unpushed", to=None if covered or stale else held,
                count=int(ahead), branch=branch, shallow_clone=shallow)

    # bench/app.py:1039-1047: with shallow_clone bench fetches ONLY <remote> <branch>
    # (`git fetch --depth=1 --no-tags`); without it, `git fetch --all` contacts EVERY
    # remote, so one dead remote nobody uses stops the update.
    if remote and remote in remotes:
        probes.append((remote, branch, covered, stale, held))
    if not shallow:
        probes.extend((r, None, True, False, []) for r in remotes if r != remote)
    return found, probes, d


def manifest(app, read):
    """What bench's requirements step installs an app from (bench/bench.py:494-507,
    bench/app.py:891-969): pyproject.toml, package.json (yarn) and <app>/__init__.py's
    version, read as bench/utils/app.py get_version_from_string reads it."""
    init = read(app + "/__init__.py") or ""
    match = re.search(r"^(\s*__version__\s*=\s*['\"])(.+?)(['\"])", init, re.S | re.M)
    return {"pyproject": read("pyproject.toml"), "package_json": read("package.json"),
            "version": match.group(2) if match else None}


def local_manifest(app):
    def read(path):
        try:
            with open(os.path.join(bench, "apps", app, path), encoding="utf-8",
                      errors="replace") as handle:
                return handle.read()
        except (OSError, ValueError):
            return None
    return manifest(app, read)


def fetched_manifest(app, d, remote, branch, shallow):
    """(manifest, error): the files bench's reset installs, from <remote>/<branch>.

    bench/app.py (pull_apps) fetches <remote> <branch> (--depth=1 with shallow_clone,
    else --all) and resets to <remote>/<branch>. This is that fetch, for that one
    branch: it moves the same remote-tracking ref and never the working tree. It is
    --depth=1 only when bench's is and HEAD is itself a shallow commit: a depth-1 fetch
    cuts the ref's history, and a blocked run keeps HEAD, so any HEAD commit below the
    cut would read as unpushed work on the next run."""
    head = git(d, ["rev-parse", "HEAD"], opts["local_timeout"])[1].strip()
    depth = ["--depth=1"] if shallow and head in shallow_boundary(d) else []
    rc, _, err = git(d, ["fetch", "--quiet", "--no-tags"] + depth + [remote, branch],
                     opts["fetch_timeout"])
    if rc != 0:
        return None, git_error(err) or "git fetch exited %s" % rc
    ref = "refs/remotes/%s/%s:" % (remote, branch)

    def read(path):
        code, out, _ = git(d, ["show", ref + path], opts["local_timeout"])
        return out if code == 0 else None
    return manifest(app, read), None


def probe(app, d, remote, branch, covered, stale, held, shallow):
    """(facts, manifest): the remote's answer, plus the tracked branch's new manifest
    when the remote has the branch and bench's fetch writes its tracking ref."""
    if branch:
        args = ["ls-remote", "--exit-code", "--heads", remote, "refs/heads/" + branch]
    else:
        args = ["ls-remote", remote, "HEAD"]
    rc, out, err = git(d, args, opts["net_timeout"])
    on_remote = rc == 0 and bool(branch)
    base = {"app": app, "remote": remote}
    if rc == 2 and branch:
        # Without shallow_clone a stale local <remote>/<branch> ref survives
        # `git fetch --all` (no --prune), and bench resets to its old commit.
        code = "git.branch_deleted_upstream" if stale and not shallow else "git.branch_not_on_remote"
        return [dict(base, code=code, branch=branch)] + held, None
    found = []
    if rc is None:
        found.append(dict(base, code="git.remote_unverified", error=err))
    elif rc != 0:
        found.append(dict(base, code="git.remote_unreachable", error=git_error(err) or "exit %s" % rc))
    # The fetch leaves <remote>/<branch> as it was: absent, so the reset fails whatever
    # the remote answers; present, so bench resets to its old commit.
    if not covered and (rc == 0 or not stale):
        code = "git.branch_tracking_stale" if stale else "git.branch_not_tracked"
        found.append(dict(base, code=code, branch=branch, shallow_repo=bool(shallow_boundary(d))))
    if held:
        tip = (out.split() or [""])[0] if rc == 0 else ""
        if tip and git(d, ["cat-file", "-e", tip + "^{commit}"], opts["local_timeout"])[0] == 0:
            rc, ahead, err = count_ahead(d, branch, [tip])
        else:
            rc, ahead, err = None, "", "the tip of %s on %s could not be compared" % (branch, remote)
        if rc == 0 and ahead.isdigit():
            if int(ahead) > 0:
                found.append(dict(held[0], count=int(ahead)))
        else:
            found.append({"code": "git.local_work_unverified", "app": app,
                          "error": git_error(err) or "git rev-list did not answer"})
    fetched = None
    if on_remote and covered:
        fetched, error = fetched_manifest(app, d, remote, branch, shallow)
        if error:
            found.append(dict(base, code="content.unverified", error=error))
    return found, fetched


def version_of(argv):
    """(version, error): the last line a version command printed."""
    rc, out, err = run(argv, bench, opts["local_timeout"])
    if rc == 0 and out:
        return out.splitlines()[-1].strip(), None
    return None, git_error(err) or "exit %s" % rc


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
    if opts["network"]:
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
        # Without the network tier an app's held unpushed findings are dropped with its
        # probe: they exist only when <remote>/<branch> is absent and its fetch cannot
        # create it, so bench's reset of that app fails before it discards anything.
        jobs = [(app, d) + p for app, (_, probes, d) in zip(apps, checked) for p in probes]
        if not opts["network"]:
            jobs = []
        probed = list(pool.map(lambda job: probe(*(job + (shallow,))), jobs))
    fetched = {}
    for app, (found, _, _) in zip(apps, checked):
        out.extend(found)
        for job, (result, new) in zip(jobs, probed):
            if job[0] == app:
                out.extend(result)
                if new is not None:
                    fetched[app] = new

    record = {"apps": apps, "sites": sites, "shallow_clone": shallow, "findings": out}
    if opts["network"]:
        # The content tier: every app as bench's requirements step reinstalls it (its
        # fetched tip where the reset moves it), and what that runs under: the bench
        # venv's Python (bench.python, bench/bench.py:74-75) and the node on PATH, which
        # yarn runs. A node not on PATH is already tool.missing.
        record["manifests"] = [
            {"app": a, "local": local_manifest(a), "remote": fetched.get(a)} for a in apps
        ]
        python = version_of([os.path.join(bench, "env", "bin", "python"), "-c",
                             "import platform; print(platform.python_version())"])
        node = version_of(["node", "-v"]) if shutil.which("node") else (None, None)
        record["tooling"] = {"python": python[0], "python_error": python[1],
                             "node": node[0], "node_error": node[1]}
    record["elapsed"] = round(time.time() - START, 3)
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
    facts: list[dict] = dataclasses.field(default_factory=list)  # the raw facts, for the fixes

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
# over the finding's facts plus `project`, `selector_flag` and shell-quoted `q_*` names.
# Remedies follow the "Suggested commands" convention: a cwcli verb where one exists,
# else a `cwcli open` shell step, with the live names substituted.
_OPEN = "in `cwcli open {q_project}{selector_flag}`: "
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
        "cwcli run {q_project}{selector_flag} set-config --global --parse maintenance_mode 0",
    ),
    "bench.scheduler_paused": (
        NOTICE,
        "pause_scheduler is already 1 bench-wide (a previous update may have failed); "
        "bench clears it only when this update succeeds",
        "cwcli run {q_project}{selector_flag} set-config --global --parse pause_scheduler 0",
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
        _OPEN + "sudo chown -R frappe:frappe {q_path}",
    ),
    "git.not_a_checkout": (
        BLOCKER,
        "not a git checkout; bench reads {app}'s branch before updating anything and fails",
        _OPEN + "replace apps/{q_app} with a git clone of it",
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
        "cwcli apps checkout {q_project} {q_app} <branch>{selector_flag}",
    ),
    "git.master_branch": (
        BLOCKER,
        "on branch 'master', which bench refuses for {app}",
        "cwcli apps checkout {q_project} {q_app} <version-branch>{selector_flag}",
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
        "cwcli apps checkout {q_project} {q_app} <published-branch>{selector_flag}",
    ),
    "git.branch_deleted_upstream": (
        NOTICE,
        "branch '{branch}' no longer exists on '{remote}'; bench would reset to the "
        "stale local {remote}/{branch}",
        "cwcli apps checkout {q_project} {q_app} <published-branch>{selector_flag}",
    ),
    "git.branch_not_tracked": (
        BLOCKER,
        "remote '{remote}' is not fetched into {remote}/{branch} (its fetch refspec does "
        "not cover '{branch}'), so bench's reset to {remote}/{branch} would fail",
        _OPEN + "git -C apps/{q_app} config --add remote.{q_remote}.fetch {q_refspec} "
        "&& git -C apps/{q_app} fetch {fetch_depth}{q_remote} {q_branch}",
    ),
    "git.branch_tracking_stale": (
        NOTICE,
        "remote '{remote}' is not fetched into {remote}/{branch} (its fetch refspec does "
        "not cover '{branch}'); bench would reset to the stale local {remote}/{branch}",
        _OPEN + "git -C apps/{q_app} config --add remote.{q_remote}.fetch {q_refspec} "
        "&& git -C apps/{q_app} fetch {fetch_depth}{q_remote} {q_branch}",
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
        "cwcli unlock {q_project} --site {q_site}{selector_flag}",
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
    "frappe.major_upgrade": (
        BLOCKER,
        "the update moves frappe from version {current} to {new}; bench stops to ask "
        "before a major upgrade, and cwcli's run cannot answer it",
        "to upgrade on purpose, first commit and push (or back up) any local app work: "
        "running bench directly skips cwcli's data-loss guard and its report of the "
        "bench-wide maintenance/scheduler flags. Then answer bench's prompt yourself: "
        "cwcli run {q_project}{selector_flag} -i update --reset",
    ),
    "tooling.python_too_old": (
        BLOCKER,
        "needs Python {spec} (requires-python) but the bench's environment runs Python "
        "{python}; bench would fail reinstalling it, after backing up every site and "
        "resetting every app",
        None,
    ),
    "tooling.node_too_old": (
        BLOCKER,
        "needs Node {spec} (engines.node in package.json) but the container runs Node "
        "{node}; yarn would refuse it, after bench has backed up every site and reset "
        "every app",
        None,
    ),
    "app.frappe_dependency_unsatisfied": (
        BLOCKER,
        "requires {dep} {spec} ([tool.bench.frappe-dependencies]) but {dep} will be at "
        "{version}; bench only warns, and the site migrate fails later (staging16, "
        "2026-10-28). Fix: switch {fix_app} to {fix_branch}",
        "cwcli apps checkout {q_project} {q_fix_app} {q_fix_branch}{selector_flag}, or re-run "
        "with --fix-branches to have cwcli switch it (a confirmed step)",
    ),
    "app.required_app_missing": (
        BLOCKER,
        "the new code at {ref} requires {dep} (required_apps), which is not on this bench; "
        "neither bench nor the migrate would fetch it",
        "re-run with --with-deps to fetch {dep} from the apps catalog and install it on the "
        "sites that have {app} first, or fetch it yourself: cwcli apps install {q_project} "
        "<{dep} repository URL>{selector_flag} --fetch-only",
    ),
    "app.required_app_source_unknown": (
        BLOCKER,
        "the new code at {ref} requires {dep} (required_apps), which is not on this bench "
        "and was not found in your app sources",
        "add {dep}'s source to the apps catalog and run the update again, or fetch it "
        "yourself: cwcli apps install {q_project} <{dep} repository URL>{selector_flag} "
        "--fetch-only",
    ),
    "app.required_app_not_installed": (
        BLOCKER,
        "site '{site}' has {app} installed but not {dep}, which the new code at {ref} "
        "requires; the migrate would run {app}'s new code without it",
        "re-run with --with-deps, or install it first: cwcli apps install {q_project} "
        "{q_dep} --site {q_site}{selector_flag}",
    ),
    "app.range_unparseable": (
        BLOCKER,
        'declares {dep} = "{spec}" in [tool.bench.frappe-dependencies], which bench cannot '
        'parse (it stops on it); ranges are comma-separated, like ">=15.0.0,<16.0.0"',
        None,
    ),
    "app.dependencies_unverified": (
        UNVERIFIED,
        "its required apps and Frappe range at the new tip could not be checked ({error})",
        None,
    ),
    "content.unverified": (
        UNVERIFIED,
        "the Python, Node and frappe versions its code needs could not be checked ({error})",
        None,
    ),
}

_ORDER = {BLOCKER: 0, DATA_LOSS: 1, UNVERIFIED: 2, NOTICE: 3}


def classify(
    facts: list[dict], *, project: str, bench: str | None = None, path: str | None = None
) -> list[PreflightFinding]:
    """Turn raw facts into typed findings, blockers first, then by app/site."""
    selector_flag = (
        f" --path {shlex.quote(path)}"
        if path
        else f" --bench {shlex.quote(bench)}" if bench else ""
    )
    findings = []
    for fact in facts:
        code = str(fact.get("code"))
        severity, message, remedy = _RULES.get(code, (UNVERIFIED, code, None))
        fields = {
            k: (", ".join(v) or "none") if isinstance(v, list) else v for k, v in fact.items()
        }
        fields.update(
            project=project,
            selector_flag=selector_flag,
            q_project=shlex.quote(project),
            q_app=shlex.quote(str(fact.get("app") or "")),
            q_site=shlex.quote(str(fact.get("site") or "")),
            q_dep=shlex.quote(str(fact.get("dep") or "")),
            q_remote=shlex.quote(str(fact.get("remote") or "")),
            q_branch=shlex.quote(str(fact.get("branch") or "")),
            fetch_depth="--depth=1 " if fact.get("shallow_repo") else "",
            q_path=shlex.quote(str(fact.get("path") or "")),
            q_fix_app=shlex.quote(str(fact.get("fix_app") or "")),
            q_fix_branch=shlex.quote(str(fact.get("fix_branch") or "")),
        )
        fields["q_refspec"] = shlex.quote(
            "+refs/heads/{0}:refs/remotes/{1}/{0}".format(fact.get("branch"), fact.get("remote"))
        )
        if code == "frappe.no_upstream" and fact.get("remotes"):
            remedy = _OPEN + "git -C apps/frappe remote rename {q_tracked} upstream"
            fields["q_tracked"] = shlex.quote(str(fact["remotes"][0]))
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
    return sort_findings(findings)


def sort_findings(findings: list[PreflightFinding]) -> list[PreflightFinding]:
    """Blockers first. Stable: within a severity the script's own order (bench-level,
    then per app in bench order) is kept."""
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


def script_argv(
    bench_path: str, *, net_timeout: float = NET_TIMEOUT, network: bool = True
) -> list[str]:
    """The exec argv. Paths and options ride as argv, never interpolated."""
    opts = json.dumps(
        {
            "network": network,
            "net_timeout": net_timeout,
            "fetch_timeout": FETCH_TIMEOUT,
            "local_timeout": LOCAL_TIMEOUT,
            "tcp_timeout": TCP_TIMEOUT,
            "workers": WORKERS,
            "tools": TOOLS,
            "disk_floor_mb": 100,
        }
    )
    return ["sh", "-c", _RUN_SH, "sh", _SCRIPT, opts, bench_path]


# ------------------------------------------------------------------ the content tier
# Each version check in the dialect of the tool that makes it: requires-python is PEP
# 440 (uv, and pip), engines.node is node-semver (yarn v1), and
# [tool.bench.frappe-dependencies] is semantic_version's SimpleSpec (bench itself). A
# spec or version these do not read is UNVERIFIED, never a guess either way.

_CMP = {"=": operator.eq, "<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge}
_NPM_OP = re.compile(r"<=|>=|<|>|=|\^|~>?")
_NPM_PART = re.compile(
    r"^[v=]*(\d+|[xX*])(?:\.(\d+|[xX*])(?:\.(\d+|[xX*]))?)?(?:\+[0-9A-Za-z.-]+)?$"
)


def _pep440_contains(spec: str, version: str) -> bool | None:
    from packaging.specifiers import InvalidSpecifier, SpecifierSet
    from packaging.version import InvalidVersion, Version

    try:
        return SpecifierSet(spec).contains(Version(version), prereleases=True)
    except (InvalidSpecifier, InvalidVersion):
        return None


def _npm_bounds(op: str, text: str) -> list[tuple[str, tuple[int, ...]]] | None:
    """One node-semver comparator as plain bounds on X.Y.Z (None: not read here)."""
    match = _NPM_PART.match(text or "*")
    if not match:
        return None
    parts: list[int | None] = []
    for part in match.groups():  # after the first wildcard, everything is one
        parts.append(int(part) if part and part.isdigit() and None not in parts else None)
    a, b, c = parts
    if a is None:  # "*": anything, but as an exclusive bound nothing satisfies it
        return [("<", (0, 0, 0))] if op in ("<", ">") else []
    low = (a, b or 0, c or 0)
    above = (a + 1, 0, 0) if b is None else (a, b + 1, 0)  # the first version past a partial
    if op in ("", "="):
        return [("=", low)] if c is not None else [(">=", low), ("<", above)]
    if op == "^":
        if a or b is None:
            high = (a + 1, 0, 0)
        elif b or c is None:
            high = (0, b + 1, 0)
        else:
            high = (0, 0, c + 1)
        return [(">=", low), ("<", high)]
    if op.startswith("~"):
        return [(">=", low), ("<", above)]
    if op in (">=", "<"):
        return [(op, low)]
    if op == ">":
        return [(">", low)] if c is not None else [(">=", above)]
    return [("<=", low)] if c is not None else [("<", above)]  # "<="


def _npm_split(token: str) -> tuple[str, str]:
    op = _NPM_OP.match(token)
    return (op.group(0), token[op.end() :]) if op else ("", token)


def _npm_satisfies(spec: str, version: str) -> bool | None:
    """node-semver's ``satisfies`` for a release version: yarn v1's engines check."""
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", version.strip())
    if not match:
        return None
    have = tuple(int(n) for n in match.groups())
    # node-semver reads ">= 18" as ">=18", and "A - B" as an inclusive range.
    for alternative in re.sub(r"(<=|>=|<|>|=|\^|~>?)\s+", r"\1", spec).split("||"):
        tokens = alternative.split()
        if len(tokens) == 3 and tokens[1] == "-":
            pairs = [(">=", tokens[0]), ("<=", tokens[2])]
        else:
            pairs = [_npm_split(token) for token in tokens] or [("", "*")]
        bounds = []
        for op, text in pairs:
            found = _npm_bounds(op or "", text)
            if found is None:
                return None
            bounds += found
        if all(_CMP[op](have, bound) for op, bound in bounds):
            return True
    return False


def _content_facts(record: dict) -> list[dict]:
    """What each app needs from the bench once ``bench update --reset`` has moved it.

    bench's requirements step reinstalls EVERY app after the reset (bench/bench.py:
    494-507): ``uv pip install -e`` checks requires-python against the bench venv's
    Python, ``yarn install`` checks engines.node against the container's node, and bench
    checks [tool.bench.frappe-dependencies], only warning. An app the reset moves is
    read at its fetched tip; any other app as it is on disk. Before all of that,
    bench's version check stops to ask about a frappe major upgrade.
    """
    tooling = record.get("tooling")
    if not isinstance(tooling, dict):
        return []  # the local tier alone, or a record from before the content tier
    manifests = {
        m["app"]: m
        for m in record.get("manifests") or []
        if isinstance(m, dict) and isinstance(m.get("app"), str)
    }
    facts: list[dict] = []

    def unverified(app: str, error: str) -> None:
        facts.append({"code": "content.unverified", "app": app, "error": error})

    def installed(app: str) -> dict:
        found = manifests[app].get("remote") or manifests[app].get("local")
        return found if isinstance(found, dict) else {}

    def version(manifest: dict) -> object:
        # bench/utils/app.py:306-333 (get_current_version), bench/app.py:520-540
        # (get_dep_version): pyproject's project.version, else <app>/__init__.py's,
        # through the one reader app_deps shares with the install check.
        return app_version(manifest.get("pyproject"), None) or manifest.get("version")

    # bench/utils/app.py:24-38 (is_version_upgrade): the installed frappe's major against
    # upstream/<branch>:frappe/__init__.py's, which bench reads straight from that file.
    frappe = manifests.get("frappe", {})
    if isinstance(frappe.get("remote"), dict) and isinstance(frappe.get("local"), dict):
        was, now = version(frappe["local"]), frappe["remote"].get("version")
        current, new = _semver(was), _semver(now)
        if current is None or new is None:
            unverified("frappe", f"frappe's version could not be read ({was!r} -> {now!r})")
        elif new[0] > current[0]:
            facts.append(
                {
                    "code": "frappe.major_upgrade",
                    "app": "frappe",
                    "current": str(current[0]),
                    "new": str(new[0]),
                }
            )

    python, node = tooling.get("python"), tooling.get("node")
    for app in manifests:
        manifest = installed(app)
        pyproject = _parsed(manifest.get("pyproject"), toml.loads)
        package = _parsed(manifest.get("package_json"), json.loads)
        if pyproject is None or package is None:
            name = "pyproject.toml" if pyproject is None else "package.json"
            unverified(app, f"its {name} could not be parsed")
        needs = [
            (
                _table(pyproject or {}, "project").get("requires-python"),
                python,
                tooling.get("python_error"),
                _pep440_contains,
                "tooling.python_too_old",
                "python",
                "requires-python",
            ),
            (
                _table(package or {}, "engines").get("node"),
                node,
                tooling.get("node_error"),
                _npm_satisfies,
                "tooling.node_too_old",
                "node",
                "engines.node",
            ),
        ]
        for spec, have, error, satisfies, code, tool, field in needs:
            if not isinstance(spec, str) or not spec.strip():
                continue
            if not isinstance(have, str):
                if error:  # an absent node is already tool.missing
                    unverified(app, f"the bench's {tool} could not be run ({error})")
                continue
            ok = satisfies(spec, have)
            if ok is None:
                unverified(app, f"{field} {spec!r} could not be compared with {have}")
            elif not ok:
                facts.append({"code": code, "app": app, "spec": spec, tool: have})
        deps = _table(pyproject or {}, "tool", "bench").get("frappe-dependencies")
        for dep, spec in deps.items() if isinstance(deps, dict) else []:
            # bench/app.py:497-518: a dependency not installed, or without a version, is
            # skipped (bench prints the first and moves on).
            dep_version = version(installed(dep)) if dep in manifests else None
            if not isinstance(spec, str) or not dep_version:
                continue
            ok = _simple_spec(spec, str(dep_version))
            if ok is None:
                unverified(
                    app,
                    f"frappe-dependency {dep} {spec!r} could not be compared with {dep_version!r}",
                )
            elif not ok:
                facts.append(
                    dependency_unsatisfied(
                        app,
                        dep,
                        spec,
                        str(dep_version),
                        version(installed("frappe")) if "frappe" in manifests else None,
                    )
                )
    return facts


def dependency_unsatisfied(
    app: str, dep: str, spec: str, dep_version: str, frappe_version: object
) -> dict:
    """The ``app.frappe_dependency_unsatisfied`` fact, with the branch switch that fixes it.

    The fix moves whichever side is off the bench's frappe line onto it: the APP when
    it wants another frappe, or a sibling already on that line; the SIBLING when it is
    the one off the line. The spec's lower bound only answers when frappe's own major
    cannot be read.
    """
    frappe_major = _semver(frappe_version)
    dep_major = _semver(dep_version)
    wanted = _spec_floor_major(spec)
    on_line = bool(frappe_major and dep_major and dep_major[0] == frappe_major[0])
    fix_app = app if dep == "frappe" or on_line else dep
    fix_major = frappe_major[0] if frappe_major else wanted
    return {
        "code": "app.frappe_dependency_unsatisfied",
        "app": app,
        "dep": dep,
        "spec": spec,
        "version": dep_version,
        "fix_app": fix_app,
        "fix_branch": f"version-{fix_major}" if fix_major is not None else None,
    }


def fixable_branches(facts: list[dict]) -> list[tuple[str, str]]:
    """``(app, branch)`` pairs the dependency mismatches say to switch, deduplicated."""
    out: list[tuple[str, str]] = []
    for f in facts:
        if (
            f.get("code") == "app.frappe_dependency_unsatisfied"
            and f.get("fix_app")
            and f.get("fix_branch")
        ):
            pair = (str(f["fix_app"]), str(f["fix_branch"]))
            if pair not in out:
                out.append(pair)
    return out


def untracked_branches(facts: list[dict]) -> list[tuple[str, str, str, bool]]:
    """``(app, remote, branch, shallow)`` for every branch the remote's fetch refspec does
    not cover (``git.branch_not_tracked``)."""
    return [
        (str(f["app"]), str(f["remote"]), str(f["branch"]), bool(f.get("shallow_repo")))
        for f in facts
        if f.get("code") == "git.branch_not_tracked"
        and f.get("app")
        and f.get("remote")
        and f.get("branch")
    ]


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
        # A DB-level OperationalError counts wherever it surfaced: v16's connect is
        # lazy, so a rejected login first fails at list-apps' query.
        certain = error.startswith(
            ("frappe.init failed", "frappe.connect failed")
        ) or error.endswith("(OperationalError)")
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
    container,
    bench_path: str,
    *,
    project: str,
    bench: str | None = None,
    path: str | None = None,
    local_only: bool = False,
) -> PreflightOutcome:
    """Run every tier against ``bench_path`` and return the classified outcome.

    ``local_only`` runs only the in-container local checks (no remote, service, site
    or lock probe): the data-loss guard's input under ``--no-preflight``, where only
    :func:`local_work` of the outcome is acted on.

    Never raises for a check that could not run: that is an UNVERIFIED finding. A
    lost Docker connection raises what ``exec_run`` raises, as every read does.
    """
    started = time.monotonic()
    exit_code, output = container.exec_run(script_argv(bench_path, network=not local_only))
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
        facts += _content_facts(record)
        apps = [a for a in record.get("apps", []) if isinstance(a, str)]
        sites = [s for s in record.get("sites", []) if isinstance(s, str)]
    if not local_only:
        facts += _site_facts(container, bench_path, sites)
        facts += _lock_facts(container, bench_path, sites)
    return PreflightOutcome(
        findings=classify(facts, project=project, bench=bench, path=path),
        apps=len(apps),
        sites=len(sites),
        elapsed=time.monotonic() - started,
        bench_path=bench_path,
        facts=facts,
    )


# What the data-loss guard cannot rule out: the app's local work could not be read
# (git refused the repo or failed to answer), or the checks did not run at all.
_LOCAL_WORK_UNKNOWN = frozenset(
    {
        "git.dubious_ownership",
        "git.local_work_unverified",
        "git.unreadable",
        "preflight.unverified",
    }
)


def local_work(findings: list[PreflightFinding]) -> list[PreflightFinding]:
    """The findings the data-loss guard refuses on unless ``--force``.

    ``bench update --reset`` hard-resets every app it pulls, discarding uncommitted
    changes to tracked files and commits on no remote, branch or tag. A guard, not a
    prediction, so it FAILS CLOSED: local work that could not be checked counts too,
    the ``apps checkout`` dirty-tree rule. Untracked files survive the reset and
    never appear here.
    """
    return [f for f in findings if f.severity == DATA_LOSS or f.code in _LOCAL_WORK_UNKNOWN]
