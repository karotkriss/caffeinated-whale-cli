"""``core.app_deps`` - an app's Frappe range and required apps, checked before it lands.

A Frappe app declares two things about what it needs:

- ``[tool.bench.frappe-dependencies]`` in ``pyproject.toml``: a version range per app
  key (``frappe = ">=15.0.0,<17.0.0"``, ``toolkit = ">=0.22.0"``). bench only
  prints a warning about it after the clone, and Frappe never reads it, so a v16 app
  lands on a v15 bench and fails at migrate.
- ``required_apps`` in ``<module>/hooks.py``: apps that must be installed first.
  Frappe installs them before the app at ``install-app`` time, but only when they are
  already on the bench; a missing one fails the install, and ``bench update`` and
  ``migrate`` never look at the list at all.

This module is the ONE rule every surface shares: :func:`build_plan` reads both
declarations at the exact ref that will land (the bench's own copy for an app already
there, else the remote ref read on the host), compares the ranges with the bench in
bench's own dialect (semantic_version 2.10's ``SimpleSpec``), finds each missing
required app in the apps catalog, picks its ref, and orders everything
dependencies-first. Nothing here fetches into the bench, installs or prompts: a
caller turns the plan into a refusal (:func:`enforce`), a consent question, or a
fetch-and-install order.

Fail honest, as the update preflight is: a declaration that could not be read is a
stated ``notice``, never a pass and never a blocker. An app that declares nothing is
never given a constraint it did not state.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace

import toml

from . import app_catalog, bench_read, git_refs, resolvers
from .envelope import Choice, Message, Result, Status
from .errors import CwcliError, ErrorKind

# ------------------------------------------------------------------- the range dialect
# semantic_version 2.10's SimpleSpec, the parser bench 5.31 pins (bench/app.py:497-518).
# Promoted here from core.update_preflight, which imports them back: one port of the
# dialect, never two that can drift.

_SIMPLE = re.compile(
    r"^(<|<=||=|==|>=|>|!=|\^|~|~=)(\*|0|[1-9][0-9]*)(?:\.(\*|0|[1-9][0-9]*)"
    r"(?:\.(\*|0|[1-9][0-9]*))?)?(?:-([a-z0-9A-Z.-]*))?(?:\+([a-z0-9A-Z.-]*))?$"
)
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9a-zA-Z.-]+))?(?:\+[0-9a-zA-Z.-]+)?$")


def semver(text: object) -> tuple | None:
    """A semantic_version Version as a precedence key: (X, Y, Z, released, prerelease)."""
    match = _SEMVER.match(str(text or ""))
    if not match:
        return None
    pre = tuple(
        (0, int(p), "") if p.isdigit() else (1, 0, p)
        for p in (match.group(4) or "").split(".")
        if p
    )
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)), not pre, pre)


def _next(target: tuple, level: int) -> tuple:
    """semantic_version's next_major/next_minor/next_patch (level 0/1/2)."""
    major, minor, patch, _, pre = target
    if level == 0:
        return (
            (major, 0, 0, True, ())
            if pre and not minor and not patch
            else (major + 1, 0, 0, True, ())
        )
    if level == 1:
        return (major, minor, 0, True, ()) if pre and not patch else (major, minor + 1, 0, True, ())
    return (major, minor, patch, True, ()) if pre else (major, minor, patch + 1, True, ())


def _below(have: tuple, bound: tuple, natural: bool = True) -> bool:
    # semantic_version Range OP_LT: a prerelease of the bound's own X.Y.Z is not "below" it.
    if natural and have[4] and have[:3] == bound[:3] and not bound[4]:
        return False
    return have < bound


def simple_spec(spec: str, version: str) -> bool | None:
    """``SimpleSpec(spec).match(Version(version))``, bench's frappe-dependency check
    (bench/app.py:497-518), mirroring semantic_version 2.10's ``parse_block``. Every
    block parses before any is matched, as there: one bad block is bench's error.
    None when the spec or the version does not parse."""
    have = semver(version)
    if have is None:
        return None
    oks = []
    for block in spec.split(","):
        match = _SIMPLE.match(block)
        if not match:
            return None
        op, *numbers, pre, build = match.groups()
        op = "==" if op in ("", "=") else op
        major, minor, patch = (None if n in (None, "*") else int(n) for n in numbers)
        partial = None in (major, minor, patch)
        if (major is None and op not in ("==", ">=")) or (partial and (pre or build)):
            return None
        if build is not None and op not in ("==", "!="):
            return None
        # A wildcard ends the version: "15.*.1" is "15".
        if major is None:
            text = "0.0.0"
        elif minor is None:
            text = f"{major}.0.0"
        elif patch is None:
            text = f"{major}.{minor}.0"
        else:
            text = f"{major}.{minor}.{patch}" + (f"-{pre}" if pre else "")
        target = semver(text)
        if target is None:
            return None
        up = 0 if minor is None else 1  # the next_major/next_minor a partial stops at
        if major is None or op == ">=":
            ok = have >= target
        elif op == "^":
            ok = have >= target and _below(have, _next(target, 0 if major else 1 if minor else 2))
        elif op in ("~", "~="):
            level = 0 if (minor is None if op == "~" else partial) else 1
            ok = have >= target and _below(have, _next(target, level))
        elif op == "==":
            ok = have >= target and _below(have, _next(target, up)) if partial else have == target
        elif op == "!=":
            if partial:
                ok = _below(have, target) or have >= _next(target, up)
            elif pre == "" or build == "":
                ok = have != target
            else:  # Range OP_NEQ: a prerelease of the target's own X.Y.Z is excluded too
                ok = have != target and not (have[4] and have[:3] == target[:3] and not target[4])
        elif op == ">":
            ok = have >= _next(target, up) if partial else have > target
        elif op == "<":
            ok = _below(have, target, natural=pre != "")
        else:  # "<="
            ok = _below(have, _next(target, up)) if partial else have <= target
        oks.append(ok)
    return all(oks)


def range_parses(spec: object) -> bool:
    """Whether bench can parse ``spec`` at all. bench raises an uncaught ``ValueError``
    on a range it cannot parse (a space-separated ``">=15.0.0 <16.0.0"``), so its
    get-app stops anyway; cwcli says so before anything is fetched."""
    return isinstance(spec, str) and simple_spec(spec, "0.0.0") is not None


def spec_floor_major(spec: str) -> int | None:
    """The major a SimpleSpec's lower bound names (``>=16,<17`` -> 16, ``~=15.2`` -> 15,
    ``==14.*`` -> 14); None when it has no lower bound."""
    for part in spec.split(","):
        m = re.match(r"^\s*(?:>=|~=|==|\^|>)\s*v?(\d+)", part.strip())
        if m:
            return int(m.group(1))
    return None


# --------------------------------------------------------------------- the declarations


def parsed(text: object, loads) -> dict | None:
    """A manifest's top level: {} when absent, None when it does not parse."""
    if not isinstance(text, str) or not text.strip():
        return {}
    try:
        data = loads(text)
    except Exception:  # toml raises a spread of types on malformed input
        return None
    return data if isinstance(data, dict) else None


def table(data: dict, *keys: str) -> dict:
    for key in keys:
        value = data.get(key)
        data = value if isinstance(value, dict) else {}
    return data


_INIT_VERSION = re.compile(r"^\s*__version__\s*=\s*['\"](.+?)['\"]", re.M)


def app_version(pyproject_text: object, init_text: object) -> str | None:
    """The version bench compares a range with (bench/app.py:521-544): the static
    ``[project].version``, else the first ``__version__ =`` line of the module's
    ``__init__.py`` (frappe, erpnext and hrms declare ``dynamic = ["version"]``)."""
    version = table(parsed(pyproject_text, toml.loads) or {}, "project").get("version")
    if isinstance(version, str) and version.strip():
        return version.strip()
    match = _INIT_VERSION.search(init_text) if isinstance(init_text, str) else None
    return match.group(1) if match else None


def frappe_dependencies(pyproject_text: object) -> dict[str, str] | None:
    """``[tool.bench.frappe-dependencies]`` as ``{app: range}``; None when the
    pyproject does not parse (a fact that cannot be read, not an empty one)."""
    data = parsed(pyproject_text, toml.loads)
    if data is None:
        return None
    deps = table(data, "tool", "bench").get("frappe-dependencies")
    if not isinstance(deps, dict):
        return {}
    return {str(k): v for k, v in deps.items() if isinstance(k, str)}


def required_apps(hooks_text: object) -> list[str] | None:
    """The top-level ``required_apps`` of a ``hooks.py``, read with ``ast``, never run.

    A commented line is not an assignment, a multi-line list parses like any other,
    and the last top-level assignment wins, as it would at import. A value that is
    not a literal list of strings (computed, a name) is None: "could not read",
    never an empty list. A file that is absent or declares nothing is ``[]``.
    """
    if not isinstance(hooks_text, str) or not hooks_text.strip():
        return []
    try:
        tree = ast.parse(hooks_text)
    except (SyntaxError, ValueError):
        return None
    found: list[str] | None = []
    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if not any(isinstance(t, ast.Name) and t.id == "required_apps" for t in targets):
            continue
        try:
            literal = ast.literal_eval(value) if value is not None else None
        except (ValueError, SyntaxError, TypeError):
            found = None
            continue
        if isinstance(literal, (list, tuple)) and all(isinstance(x, str) for x in literal):
            found = [x.strip() for x in literal if x.strip()]
        else:
            found = None
    return found


def entry_module(entry: str) -> tuple[str, str | None]:
    """A ``required_apps`` entry as ``(module, pinned_url)``.

    The forms seen in real apps are a bare name (``erpnext``) and ``org/repo``
    (``frappe/erpnext``), which Frappe's ``parse_app_name`` reads as the module
    after the last slash on GitHub; ``@branch`` is discarded, as there. A URL pins
    itself.
    """
    text = entry.strip().split("@", 1)[0] if "://" not in entry else entry.strip()
    base = text.rstrip("/").split("/")[-1]
    base = base[:-4] if base.endswith(".git") else base
    module = base.replace("-", "_").lower()
    if "://" in text:
        return module, text
    if "/" in text:
        return module, f"https://github.com/{text.strip('/')}"
    return module, None


@dataclass(frozen=True, slots=True, kw_only=True)
class Declarations:
    """What one app declares at one ref. ``error`` set means it could not be read."""

    app: str  # the module name
    version: str | None = None
    ranges: dict[str, str] = field(default_factory=dict)
    required: list[str] = field(default_factory=list)
    branch: str | None = None  # the checked-out branch (bench copy) or the ref read
    commit: str | None = None
    remotes: list[str] = field(default_factory=list)  # normalized host/path keys
    error: str | None = None

    @property
    def readable(self) -> bool:
        return self.error is None


def declarations_from(
    app: str,
    *,
    pyproject: object,
    hooks: object,
    init: object,
    branch: str | None = None,
    commit: str | None = None,
    remotes: list[str] | None = None,
) -> Declarations:
    """Parse the three files into :class:`Declarations`; an unparseable one is an error."""
    ranges = frappe_dependencies(pyproject)
    required = required_apps(hooks)
    problems = []
    if ranges is None:
        problems.append("its pyproject.toml does not parse")
    if required is None:
        problems.append("its hooks.py required_apps is not a literal list")
    return Declarations(
        app=app,
        version=app_version(pyproject, init),
        ranges={k: v for k, v in (ranges or {}).items()},
        required=list(required or []),
        branch=branch,
        commit=commit,
        remotes=list(remotes or []),
        error="; ".join(problems) or None,
    )


# ----------------------------------------------------------------------- bench reader

SENTINEL = "@@CWCLI-APP-DEPS@@"

# Stdlib only, Python 3.6 syntax (old benches run old interpreters); the files ride
# back raw and are parsed on the host, where toml and ast are.
_BENCH_PY = r"""
import json, os, re, sys
bench = sys.argv[1].rstrip("/")
out = {}
apps_dir = os.path.join(bench, "apps")
try:
    names = sorted(os.listdir(apps_dir))
except OSError:
    names = []
def read(path, limit=262144):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read(limit)
    except (OSError, ValueError):
        return None
for name in names:
    d = os.path.join(apps_dir, name)
    if not os.path.isdir(d):
        continue
    head = read(os.path.join(d, ".git", "HEAD")) or ""
    m = re.match(r"ref: refs/heads/(\S+)", head)
    urls = []
    conf = read(os.path.join(d, ".git", "config")) or ""
    inr = False
    for line in conf.splitlines():
        s = line.strip()
        if s.startswith("["):
            inr = s.startswith("[remote ")
        elif inr and s.startswith("url"):
            urls.append(s.split("=", 1)[-1].strip())
    out[name] = {
        "pyproject": read(os.path.join(d, "pyproject.toml")),
        "hooks": read(os.path.join(d, name, "hooks.py")),
        "init": read(os.path.join(d, name, "__init__.py"), 8192),
        "branch": m.group(1) if m else None,
        "urls": urls,
    }
sys.stdout.write("\n" + SENTINEL + json.dumps(out) + "\n")
"""

_RUN_SH = (
    'for py in /usr/bin/python3 python3 "$2/env/bin/python"; do '
    'if command -v "$py" >/dev/null 2>&1; then exec "$py" -c "$1" "$2"; fi; '
    'done; echo "no python3 in the container" >&2; exit 127'
)


def _decode(output) -> str:
    raw = output[0] if isinstance(output, tuple) else output
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw).decode("utf-8", errors="replace")
    return str(raw) if raw is not None else ""


def read_bench_apps(container, bench_path: str) -> dict[str, Declarations] | None:
    """Every app directory on the bench with its declarations, in ONE exec.

    None when the read could not run (the caller reports it as unverified, never as
    "the bench holds no apps").
    """
    script = f"SENTINEL = {SENTINEL!r}\n" + _BENCH_PY
    try:
        exit_code, output = container.exec_run(["sh", "-c", _RUN_SH, "sh", script, bench_path])
    except Exception:  # noqa: BLE001 - a read that cannot run is unverified, never fatal
        return None
    text = _decode(output)
    record = None
    for line in reversed(text.splitlines()):
        if line.startswith(SENTINEL):
            try:
                record = json.loads(line[len(SENTINEL) :])
            except ValueError:
                record = None
            break
    if not isinstance(record, dict) or exit_code not in (0, None):
        return None
    found: dict[str, Declarations] = {}
    for name, files in record.items():
        if not isinstance(files, dict):
            continue
        keys = [k for k in (app_catalog.remote_key(u) for u in files.get("urls") or []) if k]
        found[name] = declarations_from(
            name,
            pyproject=files.get("pyproject"),
            hooks=files.get("hooks"),
            init=files.get("init"),
            branch=files.get("branch"),
            remotes=keys,
        )
    return found


def bench_frappe_major(container, bench_path: str) -> int | None:
    """The bench's Frappe major from its frappe copy, through the shared reader."""
    frappe = (read_bench_apps(container, bench_path) or {}).get("frappe")
    # The major alone: a version semver cannot read ("16.0.0.dev0") still names it.
    match = re.match(r"^(\d+)\.", (frappe.version if frappe else None) or "")
    return int(match.group(1)) if match else None


# ---------------------------------------------------------------------- remote reader

_REMOTE_TIMEOUT = 60
_REMOTE_TTL = 300
_remote_memo: dict[tuple[str, str | None], tuple[float, Declarations]] = {}
_remote_lock = threading.Lock()


def _git(args: list[str], cwd: str, timeout: int = _REMOTE_TIMEOUT) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=git_refs._git_env(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)[:200]
    return proc.returncode, proc.stdout, proc.stderr


def _git_error(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    fatal = [line for line in lines if line.startswith("fatal:")]
    return (fatal[0] if fatal else lines[-1] if lines else "git did not answer")[:200]


def read_remote(url: str, ref: str | None) -> Declarations:
    """An app's declarations at ``ref`` (None: the default branch) of ``url``, read on
    the HOST, so a stopped instance and a dialog preview need no container.

    A throwaway repository fetches the one commit with ``--depth 1
    --filter=blob:none`` (only the three files' blobs are then downloaded, lazily),
    with the same credential helpers the Install sheet's branch list uses. The module
    is found by the catalog's own rule. Memoised per URL and ref for a few minutes;
    a job's worker is a fresh process, so the guard always reads afresh.
    """
    key = (url, ref)
    with _remote_lock:
        hit = _remote_memo.get(key)
        if hit and time.monotonic() - hit[0] < _REMOTE_TTL:
            return hit[1]
    found = _read_remote(url, ref)
    if found.readable:
        with _remote_lock:
            _remote_memo[key] = (time.monotonic(), found)
    return found


def _read_remote(url: str, ref: str | None) -> Declarations:
    name = app_catalog.remote_key(url) or url
    repo = name.rstrip("/").split("/")[-1]
    if not git_refs._URL.match(url) or (ref or "").startswith("-"):
        return Declarations(
            app=repo.replace("-", "_"),
            branch=ref,
            error=f"not a repository URL and ref: {url} {ref or ''}".strip()[:200],
        )
    with tempfile.TemporaryDirectory(prefix="cwcli-deps-") as tmp:
        _git(["init", "-q"], tmp)
        _git(["remote", "add", "origin", url], tmp)
        want = ref or "HEAD"
        code, _out, err = _git(
            ["fetch", "-q", "--depth", "1", "--filter=blob:none", "--no-tags", "origin", want], tmp
        )
        if code != 0:  # a server without partial clone: the whole tip, still one commit
            code, _out, err = _git(
                ["fetch", "-q", "--depth", "1", "--no-tags", "origin", want], tmp
            )
        if code != 0:
            return Declarations(
                app=repo.replace("-", "_"),
                branch=ref,
                error=f"could not read {name}: {_git_error(err)}",
            )
        commit = _git(["rev-parse", "FETCH_HEAD"], tmp)[1].strip() or None
        dirs = set(_git(["ls-tree", "-d", "--name-only", "FETCH_HEAD"], tmp)[1].split())

        def show(path: str) -> str | None:
            code, out, _ = _git(["show", f"FETCH_HEAD:{path}"], tmp)
            return out if code == 0 else None

        pyproject = show("pyproject.toml")
        module = app_catalog._module_for(repo, pyproject, dirs)
        if module is None:
            return Declarations(
                app=repo.replace("-", "_"),
                branch=ref,
                commit=commit,
                error=f"{name} at {ref or 'its default branch'} is not a Frappe app layout",
            )
        return declarations_from(
            module,
            pyproject=pyproject,
            hooks=show(f"{module}/hooks.py"),
            init=show(f"{module}/__init__.py"),
            branch=ref,
            commit=commit,
            remotes=[k for k in [app_catalog.remote_key(url)] if k],
        )


def list_branches(url: str) -> list[str] | None:
    """The remote's branch names (None: the list could not be read)."""
    refs = git_refs.list_remote_refs(url)
    return list(refs.branches) if refs.ok else None


# ------------------------------------------------------------------- source discovery


@dataclass(frozen=True, slots=True, kw_only=True)
class Source:
    """One place a required app can be fetched from."""

    url: str
    key: str  # normalized host/path
    default_branch: str | None = None


def find_sources(module: str, pin: str | None = None) -> list[Source]:
    """Where the apps catalog says ``module`` lives (member GitHub repositories, every
    glab host, Frappe's marketplace index), deduplicated by remote.

    An ``org/repo`` entry pins GitHub: only that repository answers, from the catalog
    when it is there, else as the bare URL. bench's own guess (``github.com/frappe``
    and ``github.com/erpnext``) is deliberately not used: it misses every self-hosted
    app and can pick a same-named public repository.
    """
    remote = app_catalog._load_disk().get("remote") or {}
    found: dict[str, Source] = {}
    pin_key = app_catalog.remote_key(pin) if pin else None
    for key, entry in sorted(remote.items()):
        if not isinstance(entry, dict) or entry.get("name") != module:
            continue
        if pin_key and key != pin_key:
            continue
        source = entry.get("source") or {}
        url = app_catalog._safe_remote_url(source.get("url") or f"https://{key}")
        if url and key not in found:
            found[key] = Source(url=url, key=key, default_branch=source.get("default_branch"))
    pin_url = app_catalog._safe_remote_url(pin)
    if pin_key and pin_url and pin_key not in found:
        found[pin_key] = Source(url=pin_url, key=pin_key)
    return list(found.values())


# ------------------------------------------------------------------------------ DTOs


@dataclass(frozen=True, slots=True, kw_only=True)
class RangeCheck:
    """One declared range against the version that will be on the bench."""

    dep: str
    spec: str
    have: str | None  # the version compared; None when that app is not on the bench
    ok: bool | None  # None: not compared (absent app, unreadable version or range)


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanNode:
    """One app in the plan, in install order (dependencies first)."""

    app: str  # the module name install-app is given
    parent: str | None  # the app that requires it; None for a requested app
    requested: bool
    target: str  # what get-app is given: a URL, or the name
    source: str | None  # normalized host/path, when known
    ref: str | None
    commit: str | None
    version: str | None
    ranges: list[RangeCheck]
    required: list[str]
    on_bench: bool
    missing_sites: list[str]  # target sites that do not have it installed
    action: str  # none | fetch | install | fetch+install
    readable: bool = True
    requested_as: str | None = None  # the target string a requested app was asked for by


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanProblem:
    """``severity`` is ``blocker`` (refused), ``consent`` (asked), ``choice`` (a source
    to pick) or ``notice`` (said, never acted on)."""

    code: str
    severity: str
    app: str | None
    message: str
    remedy: str | None = None
    overridable: bool = False
    options: list[dict] = field(default_factory=list)  # choice: [{value, label, ok}]
    default: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class DependencyPlan:
    project: str
    bench_path: str
    frappe_version: str | None
    sites: list[str]
    nodes: list[PlanNode]
    problems: list[PlanProblem]
    needs_consent: bool
    blocked: bool
    needs_choice: bool
    ok: bool  # nothing blocks; consent and choices may still be asked

    @property
    def order(self) -> list[str]:
        return [n.app for n in self.nodes]


@dataclass(frozen=True, slots=True, kw_only=True)
class BenchState:
    """What the plan compares against: the bench's apps and each target site's apps."""

    bench_path: str
    apps: dict[str, Declarations] | None  # None: the bench could not be read
    sites: list[str]
    installed: dict[str, set[str] | None]  # site -> installed apps, None when unreadable
    frappe_version: str | None = None
    planned_bench: bool = False  # a bench that does not exist yet (New bench)


def read_bench_state(
    container, bench_path: str, *, sites: list[str], new_site: str | None = None
) -> BenchState:
    """Read the bench once for the plan: its apps, and the installed apps of each target
    site (a site about to be created holds frappe alone)."""
    apps = read_bench_apps(container, bench_path)
    installed: dict[str, set[str] | None] = {}
    real = [s for s in sites if s != new_site]
    if real:
        try:
            batch = bench_read.read_benches(container, [bench_path], sites=real, installed=True)
            read = batch.benches.get(bench_path)
        except Exception:  # noqa: BLE001 - unreadable is reported, never fatal
            read = None
        by_name = {s.name: s.installed for s in (read.sites or [])} if read else {}
        for site in real:
            got = by_name.get(site)
            installed[site] = set(got) if got else None
    if new_site:
        installed[new_site] = {"frappe"}
    frappe = (apps or {}).get("frappe")
    return BenchState(
        bench_path=bench_path,
        apps=apps,
        sites=list(sites),
        installed=installed,
        frappe_version=frappe.version if frappe else None,
    )


def planned_bench_state(frappe_major: int, *, with_erpnext: bool, site: str) -> BenchState:
    """A bench that ``new-bench`` will build: frappe (and erpnext) at the newest
    ``N.x``, one site holding them. A preview only; the job re-plans on the real
    bench before any app is fetched."""
    version = f"{frappe_major}.9999.0"
    apps = {"frappe": Declarations(app="frappe", version=version)}
    if with_erpnext:
        apps["erpnext"] = Declarations(
            app="erpnext", version=version, required=[], ranges={"frappe": f">={frappe_major}.0.0"}
        )
    return BenchState(
        bench_path="(new bench)",
        apps=apps,
        sites=[site],
        installed={site: set(apps)},
        frappe_version=f"{frappe_major}.x",
        planned_bench=True,
    )


# ----------------------------------------------------------------------------- planning


class _Planner:
    """One plan's working state; :func:`build_plan` is the public entry."""

    def __init__(
        self,
        project: str,
        state: BenchState,
        *,
        fetch_only: bool,
        dep_sources: dict[str, str],
        ignore_frappe_range: bool,
        reader: Callable[[str, str | None], Declarations],
        brancher: Callable[[str], list[str] | None],
        sourcer: Callable[[str, str | None], list[Source]],
    ) -> None:
        self.project = project
        self.state = state
        self.fetch_only = fetch_only
        self.dep_sources = dep_sources
        self.ignore = ignore_frappe_range
        self.read = reader
        self.branches = brancher
        self.sources = sourcer
        self.nodes: dict[str, PlanNode] = {}
        self.decls: dict[str, Declarations] = {}
        self.order: list[str] = []
        self.requesting: str | None = None  # set while a requested target is visited
        self.problems: list[PlanProblem] = []
        self.bench_apps = state.apps or {}
        major = semver(state.frappe_version)
        self.major = major[0] if major else None
        if self.major is None and state.frappe_version:
            m = re.match(r"^(\d+)", state.frappe_version)
            self.major = int(m.group(1)) if m else None

    # -- helpers
    def problem(self, code: str, severity: str, app: str | None, message: str, **kw) -> None:
        if not any(p.code == code and p.app == app and p.message == message for p in self.problems):
            self.problems.append(
                PlanProblem(code=code, severity=severity, app=app, message=message, **kw)
            )

    def missing_sites(self, app: str) -> list[str]:
        if self.fetch_only:
            return []
        return [
            s
            for s in self.state.sites
            if self.state.installed.get(s) is not None
            and app not in (self.state.installed[s] or set())
        ]

    def action(self, on_bench: bool, missing: list[str]) -> str:
        fetch = not on_bench
        install = bool(missing)
        return (
            "fetch+install"
            if fetch and install
            else "fetch" if fetch else "install" if install else "none"
        )

    def bench_copy(self, target: str) -> str | None:
        """The bench app that holds ``target`` (a URL matched by remote, or a name)."""
        key = app_catalog.remote_key(target) if ("/" in target or "@" in target) else None
        if key:
            for name, decl in sorted(self.bench_apps.items()):
                if key in decl.remotes:
                    return name
            base = target.rstrip("/").split("/")[-1]
            base = base[:-4] if base.endswith(".git") else base
            return (
                base
                if base in self.bench_apps
                else (base.replace("-", "_") if base.replace("-", "_") in self.bench_apps else None)
            )
        return target if target in self.bench_apps else None

    def choose_ref(
        self,
        url: str,
        parent_ref: str | None,
        default: str | None,
        need: object = None,
        *,
        ref: str | None = None,
    ) -> tuple[str | None, Declarations]:
        """``ref`` when one was asked for; else the same branch name as the parent, else
        version-N, else the default branch: the first whose own frappe range passes and
        whose version fits ``need`` (the range the requiring app declares for it), else
        the first candidate."""
        if ref:
            return ref, self.read(url, ref)
        branches = self.branches(url)
        candidates: list[str | None] = []
        if branches is not None:
            if parent_ref and parent_ref in branches:
                candidates.append(parent_ref)
            if self.major is not None and f"version-{self.major}" in branches:
                candidates.append(f"version-{self.major}")
        candidates.append(
            default if default and (branches is None or default in branches) else None
        )
        seen: list[str | None] = []
        first: tuple[str | None, Declarations] | None = None
        for cand in candidates:
            if cand in seen:
                continue
            seen.append(cand)
            decl = self.read(url, cand)
            if first is None:
                first = (cand, decl)
            spec = decl.ranges.get("frappe")
            have = self.frappe_have()
            fits_parent = (
                not isinstance(need, str)
                or not decl.version
                or simple_spec(need, decl.version) is not False
            )
            if (
                decl.readable
                and fits_parent
                and (not isinstance(spec, str) or not have or simple_spec(spec, have))
            ):
                return cand, decl
        assert first is not None
        return first

    def source_options(
        self,
        found: list[Source],
        parent_ref: str | None,
        need: object = None,
        *,
        ref: str | None = None,
    ) -> list[dict]:
        """Each candidate repository at the ref it would be fetched at, and whether its
        own Frappe range fits this bench (reads are memoised, so this costs nothing
        the resolution did not already spend)."""
        have = self.frappe_have()
        options = []
        for src in found:
            at, decl = self.choose_ref(src.url, parent_ref, src.default_branch, need, ref=ref)
            spec = decl.ranges.get("frappe")
            ok = decl.readable and (
                not isinstance(spec, str) or not have or bool(simple_spec(spec, have))
            )
            options.append(
                {
                    "value": src.url,
                    "label": f"{src.key} at {at or 'its default branch'}"
                    + (f" ({decl.version})" if decl.version else ""),
                    "ok": ok,
                }
            )
        return options

    def frappe_have(self) -> str | None:
        frappe = self.bench_apps.get("frappe")
        return frappe.version if frappe else None

    # -- resolution
    def resolve_source(
        self,
        module: str,
        pin: str | None,
        parent: str,
        parent_ref: str | None,
        need: object = None,
        *,
        ref: str | None = None,
    ):
        """(url, key, ref, declarations) for an app not on the bench, or None; ``ref``
        forces the ref every candidate is read at."""
        found = self.sources(module, pin)
        if module in self.dep_sources:
            url = self.dep_sources[module]
            known = next((f for f in found if f.url == url), None)
            at, decl = self.choose_ref(
                url, parent_ref, known.default_branch if known else None, need, ref=ref
            )
            if len(found) > 1:
                # Still name the alternatives, so a dialog can show (and change) the pick.
                self.problem(
                    "app.required_app_source_chosen",
                    "notice",
                    module,
                    f"{module} is fetched from {app_catalog.remote_key(url) or url}, as picked "
                    f"from the {len(found)} repositories named {module} in the apps catalog.",
                    options=self.source_options(found, parent_ref, need, ref=ref),
                    default=url,
                )
            return url, app_catalog.remote_key(url), at, decl
        if not found:
            self.problem(
                "app.required_app_source_unknown",
                "blocker",
                module,
                f"{parent} requires {module}, which is not on this bench, and the apps catalog "
                f"knows no repository for it.",
                remedy=(
                    f"Fetch it yourself first: cwcli apps install {self.project} "
                    f"<{module} repository URL> --fetch-only, or add its source to the apps "
                    f"catalog and run the check again."
                ),
            )
            return None
        picked = [
            (s, *self.choose_ref(s.url, parent_ref, s.default_branch, need, ref=ref)) for s in found
        ]
        if len(picked) > 1:
            options = self.source_options(found, parent_ref, need, ref=ref)
            default = next((o["value"] for o in options if o["ok"]), options[0]["value"])
            self.problem(
                "app.required_app_source_ambiguous",
                "choice",
                module,
                f"{parent} requires {module}, and the apps catalog holds {len(picked)} "
                f"repositories named {module}; pick the one to fetch.",
                remedy=f"Re-run with --dep-source {module}=<url>.",
                options=options,
                default=default,
            )
            src, at, decl = next(p for p in picked if p[0].url == default)
        else:
            src, at, decl = picked[0]
        return src.url, src.key, at, decl

    def visit(
        self,
        module: str,
        *,
        target: str,
        parent: str | None,
        requested: bool,
        ref: str | None,
        decl: Declarations | None,
        source: str | None,
        stack: list[str],
    ) -> None:
        if module in stack:
            cycle = " -> ".join(stack[stack.index(module) :] + [module])
            self.problem(
                "app.required_apps_cycle",
                "blocker",
                module,
                f"required_apps form a cycle ({cycle}); neither bench nor Frappe can install it.",
            )
            return
        if module in self.nodes:
            return
        on_bench = module in self.bench_apps
        if decl is None:
            decl = self.bench_apps.get(module) or Declarations(app=module, error="not read")
        if not decl.readable:
            self.problem(
                "app.declarations_unverified",
                "notice",
                module,
                f"{module}'s Frappe range and required apps could not be checked ({decl.error}).",
            )
        stack = stack + [module]
        my_ref = ref or decl.branch
        for entry in decl.required:
            dep, pin = entry_module(entry)
            if dep == "frappe" or dep == module or dep in self.nodes:
                continue  # planned already (each app once)
            if dep in self.bench_apps or dep in stack:  # a dep in the stack is a cycle
                self.visit(
                    dep,
                    target=dep,
                    parent=module,
                    requested=False,
                    ref=None,
                    decl=None,
                    source=None,
                    stack=stack,
                )
                continue
            resolved = self.resolve_source(dep, pin, module, my_ref, decl.ranges.get(dep))
            if resolved is None:
                continue
            url, key, dep_ref, dep_decl = resolved
            self.visit(
                dep,
                target=url,
                parent=module,
                requested=False,
                ref=dep_ref,
                decl=dep_decl,
                source=key,
                stack=stack,
            )
        missing = self.missing_sites(module)
        self.decls[module] = decl
        self.nodes[module] = PlanNode(
            app=module,
            parent=parent,
            requested=requested,
            target=target,
            source=source or (decl.remotes[0] if decl.remotes else None),
            ref=my_ref,
            commit=decl.commit,
            version=decl.version,
            ranges=[],
            required=[entry_module(e)[0] for e in decl.required],
            on_bench=on_bench,
            missing_sites=missing,
            action=self.action(on_bench, missing),
            readable=decl.readable,
            requested_as=self.requesting if requested else None,
        )
        self.order.append(module)
        if not requested:
            node = self.nodes[module]
            if not on_bench:
                self.problem(
                    "app.required_app_missing",
                    "consent",
                    module,
                    f"{parent} requires {module}, which is not on this bench; cwcli will fetch "
                    f"it from {node.source or node.target} at {node.ref or 'its default branch'}"
                    + (f" and install it on {', '.join(missing)}" if missing else "")
                    + ".",
                )
            elif missing:
                self.problem(
                    "app.required_app_not_installed",
                    "consent",
                    module,
                    f"{parent} requires {module}, which is on this bench but not installed on "
                    f"{', '.join(missing)}; cwcli will install it there first.",
                )

    def check_ranges(self) -> None:
        versions = {name: d.version for name, d in self.bench_apps.items()}
        for name in self.order:
            if name not in self.bench_apps:
                versions[name] = self.decls[name].version
        for name in self.order:
            node = self.nodes[name]
            decl = self.decls[name]
            checks = []
            for dep, spec in sorted(decl.ranges.items()):
                if not range_parses(spec):
                    checks.append(
                        RangeCheck(dep=dep, spec=str(spec), have=versions.get(dep), ok=None)
                    )
                    self.problem(
                        "app.range_unparseable",
                        "blocker",
                        name,
                        f"{name} declares {dep} = {json.dumps(spec)} in "
                        "[tool.bench.frappe-dependencies], which bench cannot parse (bench "
                        'stops on it too); ranges are comma-separated, like ">=15.0.0,<16.0.0".',
                    )
                    continue
                have = versions.get(dep)
                ok = simple_spec(spec, have) if have else None
                checks.append(RangeCheck(dep=dep, spec=spec, have=have, ok=ok))
                if ok is not False:
                    continue
                acting = node.action != "none"
                if dep == "frappe":
                    if not acting:
                        severity = "notice"
                    else:
                        severity = "notice" if self.ignore else "blocker"
                    fix = (
                        f"Pick its version-{self.major} branch (--branch version-{self.major})"
                        if self.major is not None
                        else "Pick the branch built for this bench's Frappe"
                    )
                    self.problem(
                        "app.frappe_range_mismatch",
                        severity,
                        name,
                        f"{name} at {node.ref or 'its default branch'} needs frappe {spec}, but "
                        f"this bench runs Frappe {self.state.frappe_version or have}"
                        + ("; installing anyway, as asked." if acting and self.ignore else "."),
                        remedy=(
                            None
                            if self.ignore
                            else f"{fix}, or pick a bench whose Frappe matches. When the "
                            "declared range is wrong, the human 'cwcli apps install' takes "
                            "--ignore-frappe-range."
                        ),
                        overridable=acting and not self.ignore,
                    )
                elif dep in self.bench_apps:
                    self.problem(
                        "app.dependency_range_mismatch",
                        "blocker" if acting else "notice",
                        name,
                        f"{name} needs {dep} {spec}, but {dep} on this bench is {have}.",
                        remedy=(
                            f"Move {dep} to a release that fits first (cwcli apps checkout "
                            f"{self.project} {dep} <branch>), or pick another bench. cwcli never "
                            "switches an app's branch as part of an install: that moves it for "
                            "every site on the bench."
                        ),
                    )
                else:
                    planned = self.nodes.get(dep)
                    self.problem(
                        "app.dependency_range_mismatch",
                        "blocker" if acting else "notice",
                        name,
                        f"{name} needs {dep} {spec}, but no branch of {dep} cwcli may fetch "
                        f"fits both that and this bench (the closest, "
                        f"{planned.ref if planned and planned.ref else 'its default branch'}, "
                        f"is {have}).",
                        remedy=(
                            f"Pick the branch of {name} built for this bench, or fetch a "
                            f"{dep} that fits yourself with --dep-source {dep}=<url>."
                        ),
                    )
            self.nodes[name] = replace(node, ranges=checks)


def build_plan(
    project: str,
    state: BenchState,
    targets: list[tuple[str, str | None]],
    *,
    fetch_only: bool = False,
    dep_sources: dict[str, str] | None = None,
    ignore_frappe_range: bool = False,
    prefer_ref: str | None = None,
    reader: Callable[[str, str | None], Declarations] | None = None,
    brancher: Callable[[str], list[str] | None] | None = None,
    sourcer: Callable[[str, str | None], list[Source]] | None = None,
) -> DependencyPlan:
    """The ordered plan for installing ``targets`` (``(url-or-name, ref)`` pairs).

    A requested app already on the bench is read from the bench's copy (what
    ``install_apps`` installs: it never re-fetches a present app); otherwise the
    remote ref is read. A plain name not on the bench is found in the apps catalog
    like a required app. Each node's ``action`` says what cwcli will do; the
    ``problems`` say why it stops or asks.
    """
    planner = _Planner(
        project,
        state,
        fetch_only=fetch_only,
        dep_sources=dict(dep_sources or {}),
        ignore_frappe_range=ignore_frappe_range,
        reader=reader or read_remote,
        brancher=brancher or list_branches,
        sourcer=sourcer or find_sources,
    )
    if state.apps is None:
        planner.problem(
            "app.declarations_unverified",
            "notice",
            None,
            "the bench's apps could not be read, so required apps and Frappe ranges were not "
            "checked.",
        )
    for target, ref in targets:
        planner.requesting = target
        copy = planner.bench_copy(target)
        if copy is not None:
            planner.visit(
                copy,
                target=target,
                parent=None,
                requested=True,
                ref=None,
                decl=None,
                source=None,
                stack=[],
            )
            continue
        if "/" in target or "@" in target:
            decl = planner.read(target, ref)
            planner.visit(
                decl.app,
                target=target,
                parent=None,
                requested=True,
                ref=ref,
                decl=decl,
                source=app_catalog.remote_key(target),
                stack=[],
            )
            continue
        # A plain name the bench does not hold: the catalog, as for a required app,
        # but a name the catalog does not know is left to bench's own lookup. A name
        # an earlier target (its repository URL) already planned is that same app.
        if target in planner.nodes:
            continue
        found = planner.sources(target, None)
        if len(found) == 1 or (target in planner.dep_sources):
            url = planner.dep_sources.get(target) or found[0].url
            chosen_ref, decl = planner.choose_ref(
                url,
                prefer_ref,
                next((f.default_branch for f in found if f.url == url), None),
                ref=ref,
            )
            planner.visit(
                target,
                target=url,
                parent=None,
                requested=True,
                ref=chosen_ref,
                decl=decl,
                source=app_catalog.remote_key(url),
                stack=[],
            )
        elif len(found) > 1:
            resolved = planner.resolve_source(target, None, "this install", prefer_ref, ref=ref)
            if resolved is None:
                continue
            url, key, chosen_ref, decl = resolved
            planner.visit(
                target,
                target=url,
                parent=None,
                requested=True,
                ref=chosen_ref,
                decl=decl,
                source=key,
                stack=[],
            )
        else:
            planner.visit(
                target,
                target=target,
                parent=None,
                requested=True,
                ref=ref,
                decl=Declarations(
                    app=target, error="not on this bench and not in the apps catalog"
                ),
                source=None,
                stack=[],
            )
    planner.check_ranges()
    nodes = [planner.nodes[name] for name in planner.order]
    blocked = any(p.severity == "blocker" for p in planner.problems)
    needs_choice = any(p.severity == "choice" for p in planner.problems)
    needs_consent = any(not n.requested and n.action != "none" for n in nodes)
    return DependencyPlan(
        project=project,
        bench_path=state.bench_path,
        frappe_version=state.frappe_version,
        sites=list(state.sites),
        nodes=nodes,
        problems=planner.problems,
        needs_consent=needs_consent,
        blocked=blocked,
        needs_choice=needs_choice,
        ok=not blocked,
    )


# ------------------------------------------------------------------------- enforcing


def describe(plan: DependencyPlan) -> str:
    """The install order in one line, per site when there are sites."""
    order = [n.app for n in plan.nodes if n.action != "none"]
    if not order:
        return "nothing to fetch or install"
    if plan.sites:
        return f"order on {', '.join(plan.sites)}: {', '.join(order)}"
    return f"order: {', '.join(order)}"


def plan_rows(plan: DependencyPlan) -> list[dict]:
    """The plan as flat rows (the agent surface's table and a choice's options)."""
    rows = []
    for i, n in enumerate(plan.nodes, 1):
        frappe = next((r for r in n.ranges if r.dep == "frappe"), None)
        rows.append(
            {
                "order": i,
                "app": n.app,
                "action": n.action,
                "required_by": n.parent or "",
                "source": n.source or n.target,
                "ref": n.ref or "",
                "frappe_range": frappe.spec if frappe else "",
                "range_ok": "" if frappe is None or frappe.ok is None else frappe.ok,
                "on_bench": n.on_bench,
                "missing_sites": " ".join(n.missing_sites),
            }
        )
    return rows


def enforce(plan: DependencyPlan, *, with_deps: bool) -> Result | None:
    """Turn the plan into the core's answer before anything changes.

    A blocker RAISES (``CONFLICT``, or ``PRECONDITION`` for an unknown source) with the
    plan in ``detail``. An ambiguous source is ``NEEDS_CHOICE``/``select_app_source``
    (param ``dep_sources``). Missing required apps without ``with_deps`` are
    ``NEEDS_CHOICE``/``confirm_required_apps`` (param ``with_deps``): consent is a core
    parameter, so no frontend can install them unasked. None means go ahead.
    """
    requested = {n.app for n in plan.nodes if n.requested}
    blockers = sorted(
        (p for p in plan.problems if p.severity == "blocker"),
        key=lambda p: p.app not in requested,
    )
    if blockers:
        first = blockers[0]
        kind = (
            ErrorKind.PRECONDITION
            if first.code == "app.required_app_source_unknown"
            else ErrorKind.CONFLICT
        )
        remedies = [p.remedy for p in blockers if p.remedy]
        raise CwcliError(
            kind,
            first.code,
            "Nothing was fetched or installed: " + " ".join(p.message for p in blockers),
            hint=" ".join(dict.fromkeys(remedies)) or None,
            detail={"plan": plan_as_dict(plan)},
        )
    choice = next((p for p in plan.problems if p.severity == "choice"), None)
    if choice is not None:
        return Result(
            status=Status.NEEDS_CHOICE,
            choice=Choice(
                kind="select_app_source",
                param="dep_sources",
                prompt=choice.message,
                options=[dict(o, app=choice.app) for o in choice.options],
                default=choice.default,
            ),
        )
    if plan.needs_consent and not with_deps:
        deps = [n for n in plan.nodes if not n.requested and n.action != "none"]
        names = ", ".join(n.app for n in deps)
        return Result(
            status=Status.NEEDS_CHOICE,
            choice=Choice(
                kind="confirm_required_apps",
                param="with_deps",
                prompt=(
                    f"{', '.join(n.app for n in plan.nodes if n.requested)} requires "
                    f"{names}. Fetch and install {names} first? ({describe(plan)})"
                ),
                options=[
                    dict(r, value=r["app"], label=f"{r['app']}: {r['action']}")
                    for r in plan_rows(plan)
                ],
            ),
        )
    return None


def plan_as_dict(plan: DependencyPlan) -> dict:
    from dataclasses import asdict

    return asdict(plan) | {"order": plan.order}


def changed_since_plan(node: PlanNode, now: Declarations) -> str | None:
    """Why a freshly fetched copy no longer matches what the plan read (the remote moved
    between the check and the fetch), or None when it still matches."""
    planned_ranges = {r.dep: r.spec for r in node.ranges}
    now_required = [entry_module(e)[0] for e in now.required]
    if sorted(now_required) != sorted(node.required):
        return (
            f"{node.app}'s required_apps changed between the check and the fetch "
            f"({', '.join(node.required) or 'none'} -> {', '.join(now_required) or 'none'})"
        )
    if now.ranges != planned_ranges:
        return (
            f"{node.app}'s [tool.bench.frappe-dependencies] changed between the check and the fetch"
        )
    return None


# ------------------------------------------------------------------------- the verb


def plan(
    project: str,
    targets: list[tuple[str, str | None]],
    *,
    bench: str | None = None,
    bench_path: str | None = None,
    sites: list[str] | None = None,
    new_site: str | None = None,
    fetch_only: bool = False,
    dep_sources: dict[str, str] | None = None,
    ignore_frappe_range: bool = False,
    prefer_ref: str | None = None,
) -> Result[DependencyPlan]:
    """The read-only plan for installing ``targets`` on a bench (``cwcli apps plan``,
    ``cwcli axi apps plan``, the dialogs' preview). It changes nothing.

    ``sites`` filters the target sites (default: every site); ``new_site`` names a
    site that does not exist yet, holding frappe alone.
    """
    resolved = resolvers.resolve_container_and_bench(project, bench, bench_path)
    if isinstance(resolved, Result):
        return resolved
    container, path, warnings = resolved
    from ..utils import bench_sites

    if new_site:
        target_sites = [new_site]
    elif fetch_only:
        target_sites = []
    elif sites:
        target_sites = list(dict.fromkeys(sites))
    else:
        target_sites = sorted(bench_sites.list_sites(container, path) or [])
    state = read_bench_state(container, path, sites=target_sites, new_site=new_site)
    for site, apps in state.installed.items():
        if apps is None:
            warnings.append(
                Message(
                    "app.installed_unknown",
                    f"Could not read the installed apps on site '{site}'; it was not planned.",
                )
            )
    result = build_plan(
        project,
        state,
        targets,
        fetch_only=fetch_only,
        dep_sources=dep_sources,
        ignore_frappe_range=ignore_frappe_range,
        prefer_ref=prefer_ref,
    )
    return Result(
        status=Status.WARNING if warnings else Status.OK, data=result, warnings=list(warnings)
    )


def plan_new_bench(
    project: str,
    targets: list[tuple[str, str | None]],
    *,
    frappe_major: int,
    with_erpnext: bool,
    site: str,
    dep_sources: dict[str, str] | None = None,
) -> DependencyPlan:
    """The preview for ``new-bench``'s extra apps, against the bench it will build."""
    state = planned_bench_state(frappe_major, with_erpnext=with_erpnext, site=site)
    return build_plan(project, state, targets, dep_sources=dep_sources)


def _self_check() -> None:
    assert required_apps('# required_apps = ["x"]\napp_name = "a"\n') == []
    assert required_apps('required_apps = [\n  "frappe/erpnext",\n  "hrms",\n]\n') == [
        "frappe/erpnext",
        "hrms",
    ]
    assert required_apps("required_apps = get_apps()\n") is None
    assert entry_module("frappe/erpnext") == ("erpnext", "https://github.com/frappe/erpnext")
    assert entry_module("payments@version-15") == ("payments", None)
    assert simple_spec(">=15.0.0,<16.0.0", "15.125.0") is True
    assert simple_spec(">=16.0.0", "16.0.0-dev") is False
    assert simple_spec("<17.0.0", "17.0.0-dev") is False
    assert simple_spec(">=17.0.0-dev,<18.0.0", "17.0.0-dev") is True
    assert not range_parses(">=15.0.0 <16.0.0")


if __name__ == "__main__":
    _self_check()
