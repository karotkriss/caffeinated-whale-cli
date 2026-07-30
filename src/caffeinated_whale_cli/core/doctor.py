"""``core.doctor`` - a system-wide, strictly read-only environment preflight.

``cwcli doctor`` answers "can cwcli operate on this machine at all", not "is this
instance healthy" (that stays ``status``/``inspect``'s job - see the project
``CLAUDE.md`` Axis G boundary). It is a thin renderer over primitives that
already exist and already fail honestly (:func:`core.list.list_instances`,
:func:`core.version.check`/``build_info``, the auto-inspect read-only trio, ...),
plus a handful of genuinely new reads the maintainer's brainstorm
(``cwcli-doctor-brainstorm/report.md``) flagged as gaps (the compose-plugin
probe, the send-path temp-dir check, gh/glab auth status).

Every check is READ-ONLY by construction: none of them starts a container,
installs anything, writes a config file, or triggers an interactive login flow.
Where the tempting mutation sits right next to a read (auto-inspect's
``is_running()`` prunes a stale pid file; ``align_container_user_to_host`` would
fix the very thing a uid/gid check reads), this module calls the read-only
primitive instead - see the report's "read-only tripwires" (T1-T10) section for
the full list of mutations deliberately not called here.

Severity is a closed three-tier set (pass/warn/fail) - no fourth
"not applicable" state, per the maintainer's ruling. An optional tool that is
merely absent (sendme, gh, glab) is reported as WARN, never FAIL: it never
blocks day-to-day cwcli use, only the feature it backs. A check that could not
run because its OWN dependency is missing (e.g. the compose-plugin probe when
`docker` itself is unresolvable) is represented by letting that check's own
probe fail naturally, never a synthetic fourth state.

The registry (``_CHECKS``) is a tiny in-code ``list[Check]`` - explicitly NOT a
plugin system (no entry points, no discovery): both the human and ``axi``
renderers iterate the same list, so the check ordering/grouping lives in
exactly one place.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ..utils.config_utils import cwcli_home
from . import list as core_list
from . import version as core_version
from .envelope import Result, Status
from .errors import CwcliError

# 2 GiB free-space floor, mirroring ``commands/restore.py``'s
# ``_MIN_RECEIVE_FREE_BYTES`` (the only existing free-space guard in the
# codebase). Duplicated as a plain constant rather than imported: that module
# is a Typer frontend and pulls ``typer``/``rich`` at load time, which the core
# must never do.
_MIN_FREE_BYTES = 2 * 1024**3
_SUBPROCESS_TIMEOUT = 5.0


class CheckStatus(Enum):
    """The closed three-tier severity set every check reports (no fourth state)."""

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True, slots=True, kw_only=True)
class CheckResult:
    """One check's outcome: id, grouping, and the finding + suggested fix."""

    id: str
    title: str
    group: str
    status: CheckStatus
    detail: str
    fix: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class DoctorReport:
    """The whole preflight: every check plus the pre-computed exit-code aggregate.

    ``ok`` is the frontends' exit-code source (the ``core.update``/``AppsReport``
    precedent: read the report, never ``Result.status``). Per the maintainer's
    ruled exit contract, a WARN never affects it - only a FAIL does.
    """

    checks: list[CheckResult]
    passed: int
    warned: int
    failed: int
    ok: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class Check:
    """One registry entry: static metadata plus the probe that produces a result."""

    id: str
    title: str
    group: str
    run: Callable[[], tuple[CheckStatus, str, str | None]]


def _outcome(status: CheckStatus, detail: str, fix: str | None = None) -> tuple:
    return (status, detail, fix)


def _probe_version(cmd: list[str]) -> str | None:
    """Best-effort ``cmd``'s first output line, or None on any failure/timeout."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT)
    except Exception:
        return None
    for stream in (proc.stdout, proc.stderr):
        for line in (stream or "").splitlines():
            if line.strip():
                return line.strip()
    return None


def _format_gib(n: int) -> str:
    return f"{n / (1024**3):.1f} GiB"


# --------------------------------------------------------------------------- Docker


def _check_docker_binary() -> tuple:
    """C1 - docker binary present, with WSL disambiguation.

    ``shutil.which("docker") is None`` alone cannot distinguish "Docker was never
    installed" from "Docker Desktop is stopped on the Windows host, so the WSL2
    symlink into it is unresolvable" - the misdiagnosis the report's C1 names.
    Reused idiom: ``"microsoft" in platform.uname().release.lower()``
    (``commands/serve.py``).
    """
    import platform

    path = shutil.which("docker")
    if path:
        version = _probe_version(["docker", "--version"])
        return _outcome(CheckStatus.PASS, version or path)
    if "microsoft" in platform.uname().release.lower():
        return _outcome(
            CheckStatus.FAIL,
            "docker CLI not found (WSL2 detected) - Docker Desktop is likely stopped "
            "on the Windows host",
            "start Docker Desktop on Windows, then re-run",
        )
    return _outcome(
        CheckStatus.FAIL,
        "docker CLI not found",
        "install Docker: https://www.docker.com/get-started",
    )


def _check_docker_daemon() -> tuple:
    """C2 - Docker daemon reachable, via the same typed primitive ``ls``/``status`` use.

    Independent of C1: docker-py talks to the daemon socket directly, not through
    the ``docker`` CLI, so this can still fail (or pass) when the binary check
    disagrees.
    """
    try:
        result = core_list.list_instances()
    except CwcliError:
        return _outcome(
            CheckStatus.FAIL,
            "could not connect to the Docker daemon",
            "start Docker (Docker Desktop, or `systemctl start docker`)",
        )
    count = len(result.data or [])
    return _outcome(CheckStatus.PASS, f"reachable ({count} instance(s) found)")


def _check_compose_plugin() -> tuple:
    """C3 - the ``docker compose`` v2 plugin, probed with ``version`` (never ``config``,
    which can resolve/pull - T7). A GAP per the report: nothing verifies this today
    and init/scale fail mid-run without it. WARN, not FAIL: the read-only lifecycle
    (ls/status/logs/backup) does not need it, only `init`/`scale` do.
    """
    try:
        proc = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
    except FileNotFoundError:
        return _outcome(CheckStatus.WARN, "docker CLI not found", "see the Docker check above")
    except Exception as e:
        return _outcome(CheckStatus.WARN, f"could not run 'docker compose version': {e}")
    if proc.returncode == 0:
        line = next((ln.strip() for ln in proc.stdout.splitlines() if ln.strip()), "ok")
        return _outcome(CheckStatus.PASS, line)
    return _outcome(
        CheckStatus.WARN,
        "the Docker Compose v2 plugin is missing (needed for `cwcli init`/`cwcli scale`)",
        "Docker Desktop bundles it; on Linux install the `docker-compose-plugin` package",
    )


# --------------------------------------------------------------------------- cwcli


def _check_version() -> tuple:
    """C4 - cwcli version freshness + build provenance.

    A stale local binary and a source-build version number that "lies" (does not
    reflect the working tree's actual commits) both recurred as real confusion;
    surfacing both here is cheap since ``core.version`` already computes them,
    fail-open, for the passive notice.
    """
    result = core_version.check(use_cache=True)
    info = result.data
    assert info is not None
    build = core_version.build_info()
    if build.source == "source":
        kind = "editable source build" if build.editable else "source build"
        provenance = f"{kind}, git {build.commit or 'unknown'}"
        if build.dirty:
            provenance += ", dirty"
    else:
        provenance = "release build"

    if info.latest is None:
        return _outcome(
            CheckStatus.PASS, f"{info.current} ({provenance}); could not check for updates"
        )
    if info.is_outdated:
        return _outcome(
            CheckStatus.WARN,
            f"{info.current} ({provenance}); {info.latest} is available",
            "run `cwcli self-update`",
        )
    return _outcome(CheckStatus.PASS, f"{info.current} ({provenance}); up to date")


def _check_home_layout() -> tuple:
    """C7 - CWCLI_HOME present + writable, cache dir mode 0700.

    Reads (``stat``/``os.access``) only - never ``mkdir`` (T2); the lazy
    ``mkdir(parents=True, exist_ok=True)`` calls scattered across
    ``config_utils``/``db_utils``/``rm`` stay untouched. A not-yet-created home is
    not a fault (nothing has run yet); an existing-but-unwritable home is FAIL
    (nothing can be cached or archived); a loose ``cache/`` mode is WARN
    (secret-hygiene, not a functional break).
    """
    home = cwcli_home()
    if not home.exists():
        return _outcome(CheckStatus.PASS, f"{home} (not yet created; created on first use)")
    if not home.is_dir():
        return _outcome(
            CheckStatus.FAIL,
            f"{home} is not a directory",
            "replace it with a directory, or set CWCLI_HOME to a directory path",
        )
    if not os.access(home, os.W_OK):
        return _outcome(
            CheckStatus.FAIL,
            f"{home} is not writable",
            "fix permissions, or set CWCLI_HOME to a writable path",
        )
    cache_dir = home / "cache"
    if cache_dir.exists():
        if not cache_dir.is_dir():
            return _outcome(
                CheckStatus.FAIL,
                f"{cache_dir} is not a directory",
                "replace it with a directory",
            )
        mode = stat.S_IMODE(cache_dir.stat().st_mode)
        if mode != 0o700:
            return _outcome(
                CheckStatus.WARN,
                f"{home} writable; cache/ mode is {oct(mode)} (expected 0700)",
                f"chmod 700 {cache_dir}",
            )
    return _outcome(CheckStatus.PASS, f"{home} writable")


def _check_auto_inspect() -> tuple:
    """C9 - auto-inspect daemon health, via the read-only identity trio (T4): NEVER
    ``is_running()``, which prunes a stale/recycled pid file as a side effect.
    """
    from ..utils import auto_inspect as daemon
    from ..utils.config_utils import get_auto_inspect_config

    if not get_auto_inspect_config().get("enabled"):
        return _outcome(CheckStatus.PASS, "disabled")

    record = daemon._read_daemon_record()
    if record is None:
        return _outcome(
            CheckStatus.WARN,
            "enabled but no daemon record found",
            "run `cwcli config auto-inspect enable`",
        )
    pid, recorded_start = record
    if not daemon._pid_alive(pid):
        return _outcome(
            CheckStatus.WARN,
            f"enabled but the daemon (pid {pid}) is not running",
            "run `cwcli config auto-inspect enable`",
        )
    current_start = daemon._process_start_time(pid)
    if recorded_start and current_start and recorded_start != current_start:
        return _outcome(
            CheckStatus.WARN,
            f"enabled but pid {pid} was recycled by another process; the daemon is not running",
            "run `cwcli config auto-inspect enable`",
        )
    return _outcome(CheckStatus.PASS, f"running (pid {pid})")


# --------------------------------------------------------------------------- storage


def _check_home_free_space() -> tuple:
    """C5 - free space at cwcli_home() (cache/archive/tmp all live under it)."""
    home = cwcli_home()
    probe = home if home.exists() else home.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError as e:
        return _outcome(CheckStatus.WARN, f"could not check free space at {probe}: {e}")
    if free < _MIN_FREE_BYTES:
        return _outcome(
            CheckStatus.WARN,
            f"{_format_gib(free)} free at {home}",
            "free up space, or set CWCLI_HOME to a disk with more room",
        )
    return _outcome(CheckStatus.PASS, f"{_format_gib(free)} free at {home}")


_TMPDIR_ENV_VARS = ("TMPDIR", "TEMP", "TMP")


def _effective_temp_dir() -> Path:
    override = next((os.environ[name] for name in _TMPDIR_ENV_VARS if os.environ.get(name)), None)
    return Path(override) / "cwcli" if override else cwcli_home() / "tmp"


def _is_tmpfs(path: Path) -> bool | None:
    """Whether ``path``'s mount is tmpfs (RAM-backed), via /proc/mounts. None when
    unknown (non-Linux, or the file could not be read) - never guessed."""
    mounts_file = Path("/proc/mounts")
    if not mounts_file.exists():
        return None
    try:
        resolved = str(path.resolve()) if path.exists() else str(path)
    except OSError:
        resolved = str(path)
    best_match = ""
    best_fstype = None
    try:
        for line in mounts_file.read_text().splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            mount_point, fstype = parts[1], parts[2]
            if resolved == mount_point or resolved.startswith(mount_point.rstrip("/") + "/"):
                if len(mount_point) >= len(best_match):
                    best_match, best_fstype = mount_point, fstype
    except OSError:
        return None
    return best_fstype == "tmpfs" if best_fstype is not None else None


def _check_temp_free_space() -> tuple:
    """C6 - free space (+ tmpfs) at the effective sendme transfer temp dir.

    The send path has NO free-space preflight today (only receive does); doctor
    reports both the space and whether the dir is RAM-backed, independent of the
    current free bytes - a tmpfs with plenty of room right now still cannot hold a
    multi-GiB transfer reliably.
    """
    temp_dir = _effective_temp_dir()
    probe = temp_dir if temp_dir.exists() else temp_dir.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError as e:
        return _outcome(CheckStatus.WARN, f"could not check free space at {probe}: {e}")

    tmpfs = _is_tmpfs(temp_dir)
    low = free < _MIN_FREE_BYTES
    if tmpfs:
        return _outcome(
            CheckStatus.WARN,
            f"{temp_dir} is RAM-backed (tmpfs) with {_format_gib(free)} free",
            "set TMPDIR (or CWCLI_HOME) to a disk-backed location for backup transfers",
        )
    if low:
        return _outcome(
            CheckStatus.WARN,
            f"{_format_gib(free)} free at {temp_dir}",
            "free up space, or set TMPDIR to a location with more room",
        )
    return _outcome(CheckStatus.PASS, f"{_format_gib(free)} free at {temp_dir}")


# --------------------------------------------------------------------------- transfer


def _check_sendme() -> tuple:
    """C8 - sendme installed + resolved path (+ best-effort version).

    Lazy-imported: ``utils/sendme_utils.py`` pulls ``rich`` at load time, and the
    core must never do that transitively (``test_core_envelope.py`` walks the
    whole load-time import graph). WARN, never FAIL, when absent - it is optional
    and auto-installs on first ``restore --send/--receive``.
    """
    from ..utils.sendme_utils import get_sendme_command, is_sendme_installed

    if not is_sendme_installed():
        return _outcome(
            CheckStatus.WARN,
            "not installed (optional; needed only for `restore --send/--receive`)",
            "auto-installs on first `cwcli restore --send/--receive`",
        )
    resolved = get_sendme_command()
    version = _probe_version([resolved, "--version"])
    detail = f"{resolved} ({version})" if version else resolved
    return _outcome(CheckStatus.PASS, detail)


# --------------------------------------------------------------------------- git hosting


def _check_git_host_cli(tool: str, install_hint: str) -> tuple:
    """Shared C17/C18 probe: ``<tool> auth status`` is itself a read-only status
    query (it never triggers a login flow), grounded on cwcli's own credential
    bridge (``core/credbridge.py``) which shells to exactly this for private-repo
    app installs. Absence or missing auth is WARN, never FAIL - the bridge is
    inert for public repos, so neither tool is required for ordinary cwcli use.
    """
    path = shutil.which(tool)
    if not path:
        return _outcome(CheckStatus.WARN, "not installed (optional)", install_hint)
    try:
        proc = subprocess.run(
            [tool, "auth", "status"],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
    except Exception as e:
        return _outcome(CheckStatus.WARN, f"{path} (could not check auth status: {e})")
    line = next((ln.strip() for ln in (proc.stdout + proc.stderr).splitlines() if ln.strip()), None)
    if proc.returncode == 0:
        return _outcome(CheckStatus.PASS, f"{path} - {line or 'authenticated'}")
    return _outcome(
        CheckStatus.WARN,
        f"{path} - not authenticated",
        f"run `{tool} auth login`",
    )


def _check_gh() -> tuple:
    return _check_git_host_cli("gh", "install from https://cli.github.com/")


def _check_glab() -> tuple:
    return _check_git_host_cli("glab", "install from https://gitlab.com/gitlab-org/cli")


# --------------------------------------------------------------------------- instances


def _check_port_collisions() -> tuple:
    """C11 - cross-instance published-port collisions (co-runnability).

    Reuses ``core.list.list_instances`` rather than parsing every project's
    compose file a second time: Docker's ``HostConfig.PortBindings`` is the
    CONFIGURED port mapping and is present regardless of run state (the same
    "don't re-parse the compose file, the binding table is the truth" call
    ``resolvers.resolve_host_web_url`` already made), so this single call catches
    stopped-vs-stopped collisions too, not only a currently-bound conflict.
    """
    from ..utils.port_utils import format_port_list

    try:
        result = core_list.list_instances()
    except CwcliError:
        return _outcome(CheckStatus.WARN, "could not check (Docker daemon unreachable)")
    instances = result.data or []
    if len(instances) < 2:
        return _outcome(CheckStatus.PASS, f"{len(instances)} instance(s); nothing to collide")

    collisions = []
    for i, a in enumerate(instances):
        for b in instances[i + 1 :]:
            shared = set(a.ports) & set(b.ports)
            if shared:
                ports = format_port_list(sorted(int(p) for p in shared))
                collisions.append(f"{a.project_name} & {b.project_name} share {ports}")
    if collisions:
        return _outcome(
            CheckStatus.WARN,
            "; ".join(collisions),
            "re-init one with a different --port (destructive), or run them one at a time",
        )
    return _outcome(CheckStatus.PASS, f"{len(instances)} instances, no overlapping ports")


# --------------------------------------------------------------------------- registry


_CHECKS: list[Check] = [
    Check(id="c1", title="Docker", group="Docker", run=_check_docker_binary),
    Check(id="c2", title="Docker daemon", group="Docker", run=_check_docker_daemon),
    Check(id="c3", title="Docker Compose", group="Docker", run=_check_compose_plugin),
    Check(id="c4", title="cwcli version", group="cwcli", run=_check_version),
    Check(id="c7", title="cwcli home", group="cwcli", run=_check_home_layout),
    Check(id="c9", title="auto-inspect", group="cwcli", run=_check_auto_inspect),
    Check(id="c5", title="Free space (cwcli home)", group="Storage", run=_check_home_free_space),
    Check(
        id="c6",
        title="Free space (transfer temp dir)",
        group="Storage",
        run=_check_temp_free_space,
    ),
    Check(id="c8", title="sendme", group="Transfer", run=_check_sendme),
    Check(id="c17", title="gh", group="Git hosting", run=_check_gh),
    Check(id="c18", title="glab", group="Git hosting", run=_check_glab),
    Check(id="c11", title="Port ranges", group="Instances", run=_check_port_collisions),
]


def run_all() -> Result[DoctorReport]:
    """Run every registered check and aggregate the report.

    Always runs everything (no tiers, no selection flags - every surviving check
    is fast-band); a check that itself raises is caught and reported as its own
    FAIL rather than crashing the whole preflight.
    """
    results: list[CheckResult] = []
    for check in _CHECKS:
        try:
            status, detail, fix = check.run()
        except Exception as e:  # a single check must never take the whole report down
            status, detail, fix = CheckStatus.FAIL, f"check failed unexpectedly: {e}", None
        results.append(
            CheckResult(
                id=check.id,
                title=check.title,
                group=check.group,
                status=status,
                detail=detail,
                fix=fix,
            )
        )

    passed = sum(1 for r in results if r.status is CheckStatus.PASS)
    warned = sum(1 for r in results if r.status is CheckStatus.WARN)
    failed = sum(1 for r in results if r.status is CheckStatus.FAIL)
    result_status = Status.OK if failed == 0 and warned == 0 else Status.WARNING
    return Result(
        status=result_status,
        data=DoctorReport(
            checks=results, passed=passed, warned=warned, failed=failed, ok=failed == 0
        ),
    )
