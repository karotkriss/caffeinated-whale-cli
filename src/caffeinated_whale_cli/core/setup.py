"""``core.setup`` - the privileged, opt-in shared/multi-user provisioning, UI-pure.

Two verbs, both root-only and POSIX-only:

* :func:`provision` (``cwcli setup shared``) stands up the shared state tree - a
  dedicated ``cwcli`` group, a ``cwcli`` service account, ``/var/lib/cwcli`` with
  setgid group-writable subdirs, the machine marker ``/etc/cwcli/shared.toml``,
  and (opt-in) the machine-wide credential-bridge system unit. Idempotent: every
  step checks-then-acts and reports what it actually did.
* :func:`consolidate` (``cwcli setup migrate``) merges existing per-user
  ``~/.cwcli`` homes into the shared tree, re-owned to the group. It REFUSES to
  clobber an existing shared project rather than overwrite it, never deletes a
  source, and is safe to re-run.

UI-pure by construction: returns :class:`Result` / raises :class:`CwcliError`,
never prompts or exits. The renderer is ``commands/setup.py``. Nothing here runs
unless an admin explicitly invokes it, so a default per-user install never
reaches this module.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import toml

from ..utils import shared_home, startup
from .envelope import Result, Status
from .errors import CwcliError, ErrorKind

# The subdirs of the shared state tree, all setgid group-writable.
_STATE_SUBDIRS = ("projects", "cache", "archive", "run")


@dataclass(frozen=True, slots=True, kw_only=True)
class ProvisionOutcome:
    actions: list[str]
    state_dir: str
    group: str
    service_user: str
    marker: str


@dataclass(frozen=True, slots=True, kw_only=True)
class MigrateOutcome:
    actions: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    merged_projects: list[str] = field(default_factory=list)
    # Projects a source held that already exist in the shared tree: skipped, never
    # clobbered. Each entry is "<source>:<project>".
    conflicts: list[str] = field(default_factory=list)


# ------------------------------------------------------------------- preconditions


def _require_posix() -> None:
    if not shared_home.is_posix():
        raise CwcliError(
            ErrorKind.USAGE,
            "setup.windows",
            "Shared home is a POSIX (Linux/macOS) feature; on Windows cwcli "
            "remains per-user (%USERPROFILE%\\.cwcli).",
        )


def _require_root() -> None:
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise CwcliError(
            ErrorKind.USAGE,
            "setup.needs_root",
            "This command provisions system directories and a system group, so it "
            "must run as root.",
            hint="re-run with sudo",
        )


def _require_linux_for_system_unit() -> None:
    if startup.get_platform() != "linux":
        raise CwcliError(
            ErrorKind.USAGE,
            "setup.system_unit_linux_only",
            "The machine-wide credential-bridge system unit is only supported on "
            "Linux (systemd). macOS shared mode is a smaller follow-on (LaunchDaemon).",
        )


# ------------------------------------------------------------------- group / user


def _group_exists(name: str) -> bool:
    import grp

    try:
        grp.getgrnam(name)
        return True
    except KeyError:
        return False


def _user_exists(name: str) -> bool:
    import pwd

    try:
        pwd.getpwnam(name)
        return True
    except KeyError:
        return False


def _run(cmd: list[str], what: str) -> None:
    """Run a provisioning subprocess, raising a typed error on failure."""
    if shutil.which(cmd[0]) is None:
        raise CwcliError(ErrorKind.INTERNAL, "setup.tool_missing", f"{cmd[0]} not found; {what}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise CwcliError(
            ErrorKind.INTERNAL,
            "setup.command_failed",
            f"{what}: {(result.stderr or result.stdout).strip()}",
        )


def _ensure_group(name: str, actions: list[str]) -> None:
    if _group_exists(name):
        return
    _run(["groupadd", "--system", name], f"could not create group '{name}'")
    actions.append(f"group.created:{name}")


def _ensure_service_user(name: str, group: str, home: Path, actions: list[str]) -> None:
    if _user_exists(name):
        return
    _run(
        [
            "useradd",
            "--system",
            "--gid",
            group,
            "--home-dir",
            str(home),
            "--shell",
            "/usr/sbin/nologin",
            name,
        ],
        f"could not create service user '{name}'",
    )
    actions.append(f"user.created:{name}")


def _add_users_to_group(users: list[str], group: str, actions: list[str]) -> None:
    import grp

    for user in users:
        if not _user_exists(user):
            raise CwcliError(
                ErrorKind.USAGE,
                "setup.unknown_user",
                f"Cannot add unknown user '{user}' to group '{group}'.",
            )
        members = grp.getgrnam(group).gr_mem
        if user in members:
            continue
        _run(["usermod", "-aG", group, user], f"could not add '{user}' to group '{group}'")
        actions.append(f"group.member_added:{user}")


# ------------------------------------------------------------------- state tree


def _secure(path: Path, mode: int, gid: int | None) -> None:
    """Set ``root:<cwcli-gid>`` ownership + ``mode`` on a freshly-created path.

    Called only from the privileged provision/migrate paths, so it sets the mode
    directly (the shared marker may not be on disk yet, so the shared_home
    policy helpers would still read per-user)."""
    with contextlib.suppress(OSError):
        os.chmod(path, mode)
    if gid is not None:
        with contextlib.suppress(OSError):
            os.chown(path, 0, gid)  # root owner, cwcli group


def _make_state_tree(state_dir: Path, gid: int | None, actions: list[str]) -> None:
    created_root = not state_dir.exists()
    state_dir.mkdir(parents=True, exist_ok=True)
    _secure(state_dir, shared_home.SHARED_DIR_MODE, gid)
    if created_root:
        actions.append(f"tree.created:{state_dir}")
    for sub in _STATE_SUBDIRS:
        d = state_dir / sub
        d.mkdir(parents=True, exist_ok=True)
        _secure(d, shared_home.SHARED_DIR_MODE, gid)
    # /etc/cwcli holds the marker + the shared config; the run dir default lives
    # under the state tree already, so nothing extra is needed there.
    etc = shared_home.marker_path().parent
    etc.mkdir(parents=True, exist_ok=True)
    _secure(etc, shared_home.SHARED_DIR_MODE, gid)


def _write_marker(state_dir: Path, group: str, service_user: str, actions: list[str]) -> None:
    marker = shared_home.marker_path()
    marker.write_text(
        toml.dumps(
            {
                "enabled": True,
                "state_dir": str(state_dir),
                "group": group,
                "service_user": service_user,
            }
        )
    )
    # Holds no secret (just paths + names). It lives inside the group-owned
    # /etc/cwcli (2770 root:cwcli), so cwcli-group members read it on every
    # invocation via the group execute bit; a non-group user cannot traverse
    # that parent and simply falls back to a per-user ~/.cwcli.
    with contextlib.suppress(OSError):
        os.chmod(marker, 0o644)
    actions.append(f"marker.written:{marker}")


def _enable_shared_bridge(state_dir: Path, group: str, gid: int | None, actions: list[str]) -> None:
    """Enable the credential bridge in the shared config and install its system unit.

    Writes the shared config directly at ``<state_dir>/config/config.toml`` rather
    than through ``config_utils`` constants, which were resolved at import from the
    pre-marker home."""
    _require_linux_for_system_unit()
    config_dir = state_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    _secure(config_dir, shared_home.SHARED_DIR_MODE, gid)
    config_file = config_dir / "config.toml"
    data = {}
    if config_file.is_file():
        with contextlib.suppress(OSError, toml.TomlDecodeError):
            data = toml.load(config_file)
    if not isinstance(data, dict):
        data = {}
    data.setdefault("cred_bridge", {})
    data["cred_bridge"]["enabled"] = True
    data["cred_bridge"]["startup_enabled"] = True
    config_file.write_text(toml.dumps(data))
    _secure(config_file, shared_home.SHARED_FILE_MODE, gid)
    actions.append("cred_bridge.enabled")

    installed = startup.install_system_unit(
        startup.CRED_BRIDGE,
        user=shared_home.SERVICE_USER,
        group=group,
        pid_file=str(state_dir / "run" / "credbridge.pid"),
    )
    actions.append(
        "cred_bridge.system_unit_installed" if installed else "cred_bridge.system_unit_failed"
    )


def provision(
    *,
    group: str = shared_home.DEFAULT_GROUP,
    state_dir: Path | None = None,
    service_user: str = shared_home.SERVICE_USER,
    users: list[str] | None = None,
    cred_bridge: bool = False,
) -> Result[ProvisionOutcome]:
    """Bring the shared state tree to its provisioned desired state (idempotent).

    ``group``/``state_dir`` default to the fixed shared install (the cwcli group,
    /var/lib/cwcli); no CLI flag exposes them. They are overridable only so the
    unit tests can inject a temp state tree they can write as non-root.
    """
    _require_posix()
    _require_root()

    target_state_dir = state_dir or shared_home.DEFAULT_STATE_DIR
    actions: list[str] = []

    _ensure_group(group, actions)
    _ensure_service_user(service_user, group, target_state_dir, actions)

    # Resolve the gid AFTER the group is created.
    import grp

    try:
        gid = grp.getgrnam(group).gr_gid
    except KeyError:
        gid = None

    _make_state_tree(target_state_dir, gid, actions)
    _write_marker(target_state_dir, group, service_user, actions)

    if users:
        _add_users_to_group(users, group, actions)

    if cred_bridge:
        _enable_shared_bridge(target_state_dir, group, gid, actions)

    return Result(
        status=Status.OK,
        data=ProvisionOutcome(
            actions=actions,
            state_dir=str(target_state_dir),
            group=group,
            service_user=service_user,
            marker=str(shared_home.marker_path()),
        ),
    )


# ------------------------------------------------------------------- migration


def _discover_homes() -> list[Path]:
    """Every plausible per-user ``~/.cwcli`` on the box (``/home/*/.cwcli`` + root)."""
    homes: list[Path] = []
    for base in (*Path("/home").glob("*"), Path("/root")):
        candidate = base / ".cwcli"
        if candidate.is_dir():
            homes.append(candidate)
    return homes


def _merge_projects(
    src_home: Path, shared_projects: Path, gid: int | None, outcome_actions: list[str]
) -> tuple[list[str], list[str]]:
    """Copy each ``<src>/projects/<name>`` into the shared tree.

    Returns (merged, conflicts). A project name that already exists in the shared
    tree is a CONFLICT: it is skipped (never overwritten), so a re-run and a
    genuine name clash are both safe.
    """
    merged: list[str] = []
    conflicts: list[str] = []
    src_projects = src_home / "projects"
    if not src_projects.is_dir():
        return merged, conflicts
    for proj in sorted(src_projects.iterdir()):
        if not proj.is_dir():
            continue
        dest = shared_projects / proj.name
        if dest.exists():
            conflicts.append(proj.name)
            continue
        try:
            # symlinks=True copies each link AS a link: a real bench links to
            # paths that exist only inside the container (``env/bin/python*``,
            # ``sites/assets/*``, ``node_modules``), and following them fails.
            shutil.copytree(proj, dest, symlinks=True, ignore=_ignore_special_files)
        except Exception:
            shutil.rmtree(dest, ignore_errors=True)
            raise
        _reown_tree(dest, gid)
        merged.append(proj.name)
    return merged, conflicts


def _ignore_special_files(directory: str, names: list[str]) -> set[str]:
    """copytree filter: skip sockets/fifos/devices a running instance leaves on disk.

    A started instance's workspace holds live UNIX sockets (the supervisord and
    cred-bridge sockets); ``shutil.copytree`` cannot copy those and would abort.
    """
    ignored: set[str] = set()
    for name in names:
        try:
            mode = os.lstat(os.path.join(directory, name)).st_mode
        except OSError:
            continue
        if stat.S_ISSOCK(mode) or stat.S_ISFIFO(mode) or stat.S_ISBLK(mode) or stat.S_ISCHR(mode):
            ignored.add(name)
    return ignored


def _reown_tree(root: Path, gid: int | None) -> None:
    """Re-own a copied subtree to root:cwcli with setgid dirs / group-writable files.

    A symlink is re-owned itself and NEVER followed: a bench links to absolute
    container paths, and chmod/chown as root through one would change whatever
    the host happens to have at that path. An executable keeps its execute bit
    (group included), or the bench's own scripts and binaries stop running.
    """
    _secure(root, shared_home.SHARED_DIR_MODE, gid)
    for dirpath, dirnames, filenames in os.walk(root):  # never descends a symlinked dir
        for name in dirnames + filenames:
            path = Path(dirpath, name)
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                if gid is not None:
                    with contextlib.suppress(OSError):
                        os.lchown(path, 0, gid)
            elif stat.S_ISDIR(mode):
                _secure(path, shared_home.SHARED_DIR_MODE, gid)
            else:
                exec_bits = 0o110 if mode & 0o111 else 0
                _secure(path, shared_home.SHARED_FILE_MODE | exec_bits, gid)


def _copy_config_if_absent(
    src_home: Path, state_dir: Path, gid: int | None, actions: list[str]
) -> None:
    shared_config = state_dir / "config" / "config.toml"
    src_config = src_home / "config" / "config.toml"
    if shared_config.is_file() or not src_config.is_file():
        return
    data = {}
    with contextlib.suppress(OSError, toml.TomlDecodeError):
        data = toml.load(src_config)
    if not isinstance(data, dict):
        data = {}
    # The machine-wide credential-bridge posture is owned SOLELY by shared
    # provisioning (the service-account bridge + its system unit), never
    # inherited from whichever per-user home is consolidated first.
    data.pop("cred_bridge", None)
    shared_config.parent.mkdir(parents=True, exist_ok=True)
    _secure(shared_config.parent, shared_home.SHARED_DIR_MODE, gid)
    shared_config.write_text(toml.dumps(data))
    _secure(shared_config, shared_home.SHARED_FILE_MODE, gid)
    actions.append(f"config.migrated:{src_home}")


def consolidate(
    *,
    sources: list[Path] | None = None,
    discover: bool = False,
) -> Result[MigrateOutcome]:
    """Consolidate one or more per-user homes into the shared tree.

    ``sources`` is an explicit list; ``discover`` adds every ``/home/*/.cwcli``.
    Requires shared mode already provisioned. Never deletes a source; refuses to
    clobber an existing shared project. Idempotent.
    """
    _require_posix()
    _require_root()

    state = shared_home.state_dir()
    if state is None:
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "setup.not_provisioned",
            "Shared mode is not provisioned; run `cwcli setup shared` first.",
        )

    homes: list[Path] = list(sources or [])
    if discover:
        for h in _discover_homes():
            if h not in homes:
                homes.append(h)
    # Never consolidate the shared tree into itself.
    homes = [h for h in homes if h.resolve() != state.resolve()]
    if not homes:
        raise CwcliError(
            ErrorKind.USAGE,
            "setup.no_sources",
            "No source homes to consolidate; pass --from <path> or --discover.",
        )

    import grp

    try:
        gid = grp.getgrnam(shared_home.group_name()).gr_gid
    except KeyError:
        gid = None

    shared_projects = state / "projects"
    shared_projects.mkdir(parents=True, exist_ok=True)
    _secure(shared_projects, shared_home.SHARED_DIR_MODE, gid)

    actions: list[str] = []
    all_merged: list[str] = []
    all_conflicts: list[str] = []
    processed: list[str] = []
    for home in homes:
        if not home.is_dir():
            continue
        processed.append(str(home))
        merged, conflicts = _merge_projects(home, shared_projects, gid, actions)
        all_merged.extend(merged)
        all_conflicts.extend(f"{home}:{c}" for c in conflicts)
        _copy_config_if_absent(home, state, gid, actions)
        # The SQLite cache is per-user derived data (regenerable via `cwcli
        # inspect`), so it is deliberately NOT merged - merging two caches is
        # conflict-prone and buys nothing durable.

    return Result(
        status=Status.WARNING if all_conflicts else Status.OK,
        data=MigrateOutcome(
            actions=actions,
            sources=processed,
            merged_projects=all_merged,
            conflicts=all_conflicts,
        ),
    )
