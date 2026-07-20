"""``core.version`` - install-method detection + latest-version lookup, UI-pure.

One shared helper behind two consumers: the active ``cwcli self-update`` command
and the passive "update available" notice (:func:`passive_notice`) reuse it
verbatim, so the detection tree, the fail-open PyPI lookup, the PEP 440 compare,
and the TTL cache all live here once.

The distribution name is always ``caffeinated-whale-cli`` (the ``cwcli`` PyPI
name is an abandoned 2016 package - never use it). Like every ``core`` module
this imports NO ``rich``/``questionary``/``typer``: it returns a serializable
:class:`VersionInfo` wrapped in :class:`~.envelope.Result` and prints/exits
nothing. The network lookup is FAIL-OPEN by contract: any error yields
``latest=None`` (with a ``pypi.unreachable`` warning) so a version check can
never break or hang a command.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse
from urllib.request import url2pathname

from ..utils.config_utils import cwcli_home
from .envelope import Message, Result, Status

DIST = "caffeinated-whale-cli"
_PYPI_URL = f"https://pypi.org/pypi/{DIST}/json"
_CACHE_TTL_SECONDS = 86_400  # ~1 day; the passive notice reuses this cache
_DEFAULT_TIMEOUT = 4.0


@dataclass(frozen=True, slots=True, kw_only=True)
class BuildInfo:
    """Which TREE the running cwcli was built from - the ``--version`` discriminator.

    ``source`` is ``"release"`` when the distribution has no PEP 610
    ``direct_url.json``, i.e. it was resolved from an index: the version number
    is then the WHOLE provenance, because it maps to exactly one published tag.
    It is ``"source"`` when a direct URL IS recorded (a local path or a VCS
    reference), where the version number LIES - a working tree can carry any
    number of unreleased commits under the same number. That is the defect this
    exists for: a probe of a published 1.0.0 concluded ``apps checkout`` did not
    exist when it was merged and sitting at the tip of ``develop``.

    ``commit``/``dirty`` are filled only for a ``source`` build (from the PEP 610
    ``vcs_info``, else by reading the recorded tree). A ``release`` build NEVER
    shells to git and never needs a ``.git`` directory to exist at runtime.
    Serializable: every field is a builtin/None.
    """

    source: str
    editable: bool = False
    path: str | None = None
    commit: str | None = None
    dirty: bool | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class VersionInfo:
    """The install-method + version comparison for the running cwcli.

    ``method`` is one of ``uv`` (persistent uv tool), ``pip``, ``dev`` (an
    editable/source checkout), or ``uvx`` (an ephemeral ``uvx --from`` run).
    ``upgrade_command`` is the subprocess argv to run, or ``None`` for the two
    non-upgradable methods (``dev``/``uvx``). ``latest`` is ``None`` iff the
    PyPI lookup failed open. Serializable: every field is a builtin/None.
    """

    current: str
    latest: str | None
    method: str
    upgrade_command: list[str] | None
    is_outdated: bool
    is_dev: bool
    dev_path: str | None = None


def check(*, use_cache: bool = True, timeout: float = _DEFAULT_TIMEOUT) -> Result[VersionInfo]:
    """Detect the install method and compare the running version against PyPI.

    ``use_cache`` reads the shared ≤1-day TTL cache (fast, shared with the
    passive notice); a successful fetch always refreshes that cache regardless.
    A failed lookup rides as a ``pypi.unreachable`` warning with ``latest=None``.
    """
    current = _current_version()
    method, dev_path = _detect_method()
    latest = _latest_version(use_cache=use_cache, timeout=timeout)

    warnings: list[Message] = []
    if latest is None:
        warnings.append(Message("pypi.unreachable", "Could not reach PyPI to check for updates."))

    info = VersionInfo(
        current=current,
        latest=latest,
        method=method,
        upgrade_command=_upgrade_command(method),
        is_outdated=_is_outdated(current, latest),
        is_dev=method == "dev",
        dev_path=dev_path,
    )
    status = Status.WARNING if warnings else Status.OK
    return Result(status=status, data=info, warnings=warnings)


def passive_notice(*, timeout: float = _DEFAULT_TIMEOUT) -> VersionInfo | None:
    """Cache-only, non-blocking gate for the passive "update available" notice.

    Returns a :class:`VersionInfo` to display ONLY when the install method is
    upgradable (not ``dev``/``uvx``) AND the shared ≤1-day cache already knows a
    newer version is published. It NEVER blocks on the network: on a
    missing/stale cache it fires a detached background refresh for the NEXT run
    and returns ``None`` now - throttled to ~once/day by ``attempted_at``, which
    is stamped on EVERY fetch (success or failure), so persistent PyPI failures
    (offline, corporate proxy, blocked) don't re-spawn a refresh on every
    invocation. Fully fail-open - any error yields ``None`` (no notice), so a
    passive check can never break, delay, or hang a command.
    """
    try:
        method, _ = _detect_method()
        upgrade_command = _upgrade_command(method)
        if upgrade_command is None:
            return None  # dev / uvx: nothing to upgrade -> never fetch or notify
        cached = _read_cache()  # fresh value, or None if missing/stale
        if cached is None:
            if not _recently_attempted():
                _spawn_background_refresh(timeout=timeout)
            return None
        current = _current_version()
        if not _is_outdated(current, cached):
            return None
        return VersionInfo(
            current=current,
            latest=cached,
            method=method,
            upgrade_command=upgrade_command,
            is_outdated=True,
            is_dev=False,
        )
    except Exception:
        return None


def _spawn_background_refresh(*, timeout: float = _DEFAULT_TIMEOUT) -> None:
    """Fire-and-forget a fully detached refresh of the shared version cache.

    Runs one ``check(use_cache=False)`` in a separate process that survives this
    (often sub-second) command's exit, so the ≤1-day cache is populated for the
    NEXT run without ever blocking THIS one. ``check`` stamps ``attempted_at`` on
    EVERY fetch it makes, success or failure, so ``_recently_attempted`` throttles
    further refreshes to ~once/day even when PyPI is persistently unreachable.
    Any failure to spawn is swallowed.
    """
    import subprocess

    snippet = (
        "from caffeinated_whale_cli.core import version as v;"
        f"v.check(use_cache=False, timeout={timeout!r})"
    )
    try:
        subprocess.Popen(
            [sys.executable, "-c", snippet],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass  # a background refresh must never break the caller


def _current_version() -> str:
    import importlib.metadata

    return importlib.metadata.version(DIST)


def build_info() -> BuildInfo:
    """Identify the tree the running cwcli was built from; see :class:`BuildInfo`.

    Fully fail-open: any unexpected failure reports ``release`` with no commit,
    which is the honest reading of "no direct-URL provenance was found".
    """
    try:
        info = _direct_url()
    except Exception:
        info = None
    if info is None:
        return BuildInfo(source="release")

    path = _url_to_path(info.get("url", ""))
    commit = info.get("vcs_info", {}).get("commit_id")
    dirty: bool | None = None
    if commit:
        # A VCS reference pins an immutable commit, so the checkout is clean by
        # construction and there is no tree on disk to stat.
        commit = commit[:7]
    else:
        commit, dirty = _git_head(path)
    return BuildInfo(
        source="source",
        editable=bool(info.get("dir_info", {}).get("editable")),
        path=path,
        commit=commit,
        dirty=dirty,
    )


def _git_head(path: str | None) -> tuple[str | None, bool | None]:
    """``(short sha, dirty)`` for a git checkout at ``path``, else ``(None, None)``.

    Reached ONLY from the ``source`` branch of :func:`build_info`, so a release
    install never shells out. ``.git`` is probed with ``exists()`` rather than
    ``is_dir()`` because in a git worktree it is a FILE, and a worktree build is
    precisely the case this has to identify. Fail-open at every step: a missing
    git, a deleted source tree, or a timeout degrades to "unknown commit", never
    an error - ``--version`` must always print.
    """
    import subprocess
    from pathlib import Path

    if not path or not (Path(path) / ".git").exists():
        return None, None
    try:
        head = subprocess.run(
            ["git", "-C", path, "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if head.returncode != 0 or not head.stdout.strip():
            return None, None
        status = subprocess.run(
            ["git", "-C", path, "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        dirty = bool(status.stdout.strip()) if status.returncode == 0 else None
        return head.stdout.strip(), dirty
    except Exception:
        return None, None


def _direct_url() -> dict | None:
    """The distribution's PEP 610 ``direct_url.json``, or ``None`` if absent.

    Absent means the distribution was resolved from an INDEX (PyPI). Shared by
    :func:`build_info` and :func:`_detect_method` so the "was this installed
    from a tree?" question is read from one place.
    """
    import importlib.metadata

    raw = importlib.metadata.distribution(DIST).read_text("direct_url.json")
    return json.loads(raw) if raw else None


def _detect_method() -> tuple[str, str | None]:
    """Three-way (plus uvx) install-method tree; see module docstring.

    Order: editable checkout (PEP 610 ``direct_url.json``) -> uv tool dir ->
    ephemeral uvx cache -> pip fallback. Any unexpected failure defaults to the
    safe ``pip`` branch rather than breaking the command.
    """
    import importlib.metadata

    try:
        info = _direct_url()
        if info and info.get("dir_info", {}).get("editable"):
            return "dev", _url_to_path(info.get("url", ""))

        dist = importlib.metadata.distribution(DIST)
        location = str(dist.locate_file("")).replace("\\", "/")
        if "/uv/tools/" in location:
            return "uv", None
        if "/uv/" in location:
            # Under uv's dir but not a persistent tool -> an ephemeral `uvx` run;
            # a real pip install never has a `/uv/` path segment.
            return "uvx", None
    except Exception:
        pass
    return "pip", None


def _url_to_path(url: str) -> str | None:
    if not url:
        return None
    try:
        return url2pathname(urlparse(url).path)
    except Exception:
        return None


def _upgrade_command(method: str) -> list[str] | None:
    if method == "uv":
        return ["uv", "tool", "upgrade", DIST]
    if method == "pip":
        # sys.executable -m pip, never a bare `pip` (which may target a different env).
        return [sys.executable, "-m", "pip", "install", "--upgrade", DIST]
    return None  # dev / uvx are never upgraded in place


def _is_outdated(current: str, latest: str | None) -> bool:
    if not latest:
        return False
    from packaging.version import InvalidVersion, Version

    try:
        return Version(latest) > Version(current)
    except InvalidVersion:
        return False


# --- latest-version lookup + TTL cache ------------------------------------


def _latest_version(*, use_cache: bool, timeout: float) -> str | None:
    if use_cache:
        cached = _read_cache()
        if cached is not None:
            return cached
    latest = _fetch_latest(timeout)
    _record_attempt(latest)
    return latest


def _fetch_latest(timeout: float) -> str | None:
    """PyPI ``info.version`` (the canonical latest; never sort ``releases``).

    FAIL-OPEN: any error (URLError/timeout/JSON/KeyError) returns ``None``.
    """
    try:
        req = urllib.request.Request(_PYPI_URL, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        version = data["info"]["version"]
        return version if isinstance(version, str) else None
    except Exception:
        return None


def _cache_file():
    return cwcli_home() / "cache" / "version_check.json"


def _read_cache() -> str | None:
    try:
        raw = json.loads(_cache_file().read_text())
        if time.time() - float(raw["checked_at"]) < _CACHE_TTL_SECONDS:
            latest = raw["latest"]
            return latest if isinstance(latest, str) else None
    except Exception:
        pass
    return None


def _recently_attempted() -> bool:
    """Whether a fetch (success or failure) landed within the TTL.

    Fail-open to ``False`` on any error, so a corrupt/missing cache never
    blocks a refresh from being spawned.
    """
    try:
        raw = json.loads(_cache_file().read_text())
        return time.time() - float(raw["attempted_at"]) < _CACHE_TTL_SECONDS
    except Exception:
        return False


def _record_attempt(latest: str | None) -> None:
    """Stamp ``attempted_at`` on every fetch; update ``latest``/``checked_at``
    only on success, preserving any previously known-good value on failure.
    """
    try:
        try:
            raw = json.loads(_cache_file().read_text())
        except Exception:
            raw = {}
        now = time.time()
        raw["attempted_at"] = now
        if latest is not None:
            raw["latest"] = latest
            raw["checked_at"] = now
        path = _cache_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(raw))
    except Exception:
        pass  # a cache write must never break a version check
