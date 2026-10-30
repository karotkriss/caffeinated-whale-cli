"""``core.self_update`` - upgrade the install cwcli runs from, then restart its services.

Two halves, shared by ``cwcli self-update`` and the web UI's ``self-update`` job:

* :func:`upgrade` runs the upgrade against the install the RUNNING cwcli lives in,
  not whatever ``uv`` or ``pip`` would pick for the calling user. Under ``sudo`` a
  bare ``uv tool upgrade`` looks in root's own tool folder, so a shared install in
  ``/opt`` stayed old while the command reported success. The uv tool folder and
  bin folder are passed explicitly, the upgrade runs as the install's owner, and
  the installed version is read back afterwards: an upgrade that did not change
  the install is a failure, never "Upgraded".
* :func:`running_services` (read BEFORE the upgrade) and :func:`restart` (after it)
  bring back every cwcli service that was running - the machine-wide system units,
  auto-inspect, the per-user credential bridge and each user's web UI daemon - as
  the user that ran it, and report which came back on the new code.

A failed upgrade raises before anything is restarted, so the old services keep
running the old code. UI-pure: returns DTOs or raises :class:`CwcliError`.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..utils import auto_inspect, cred_daemon, daemon_identity, shared_home, startup
from ..utils.config_utils import cwcli_home
from .errors import CwcliError, ErrorKind
from .version import DIST, VersionInfo, install_root, tool_env

_STOP_TIMEOUT_S = 15.0
_START_TIMEOUT_S = 90.0
_HEALTH_TIMEOUT_S = 20.0


@dataclass(frozen=True, slots=True, kw_only=True)
class Service:
    """One running cwcli service, as found before the upgrade."""

    kind: str  # "ui" | "auto-inspect" | "cred-bridge" | "scheduler"
    name: str  # what the report calls it
    pid: int | None
    owner: int | None = None  # uid the daemon runs as (pid-file daemons)
    unit: str | None = None  # the system unit that owns it, when systemd does
    port: int | None = None  # the web UI's port
    env: dict[str, str] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class ServiceRestart:
    """How one service came back after the upgrade."""

    kind: str
    name: str
    ok: bool
    pid: int | None
    detail: str


# ------------------------------------------------------------------- the upgrade


def upgrade(info: VersionInfo, *, on_output: Callable[[str], None] = lambda _l: None) -> str:
    """Run the upgrade for ``info`` and return the version now installed.

    Raises :class:`CwcliError` when the install cannot be written, the upgrade
    command fails, or the install does not report a new version afterwards; a
    uv tool env is then put back as it was, and nothing is restarted.
    ``on_output`` gets each output line.
    """
    if info.upgrade_command is None:
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "self_update.unavailable",
            "cwcli could not determine a usable upgrade command.",
        )
    root = install_root()
    target = _install_dir()
    _require_writable(target)
    argv, env = list(info.upgrade_command), dict(os.environ)
    run_as = _owner_if_root(target)
    if info.method == "uv":
        argv[0] = _find_uv(run_as)
        env.update(_uv_env(root))
    if run_as is not None:
        env.update(_user_env(run_as))

    # A uv upgrade that fails part way (linking the executables, say) has already
    # replaced the packages, so the env is copied first and put back on failure.
    backup = _backup(root, run_as) if info.method == "uv" else None
    try:
        code, tail = _run(argv, env, run_as, on_output)
        installed = installed_version() if code == 0 else None
    except BaseException:
        _restore(root, backup)
        raise
    if code != 0 or installed is None or installed == info.current:
        restored = _restore(root, backup)
        now = info.current if restored else installed_version()
        state = (
            f"cwcli {info.current} is still installed"
            if now == info.current
            else f"the install now reports cwcli {now or 'an unreadable version'}; "
            f"reinstall {info.current} by hand"
        )
        if code != 0:
            reason = tail[-1].strip() if tail else "no output"
            what = f"The upgrade failed ({os.path.basename(argv[0])} exited {code}: {reason})"
        else:
            what = f"The upgrade command finished, but the install did not change to {info.latest}"
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "self_update.failed" if code != 0 else "self_update.unchanged",
            f"{what}; {state}, and nothing was restarted.",
            detail={"output": "\n".join(tail)},
        )
    _discard(backup)
    return installed


def _run(
    argv: list[str], env: dict[str, str], run_as: int | None, on_output: Callable[[str], None]
) -> tuple[int, list[str]]:
    """Run the upgrade command, streaming its output; return its code and last lines."""
    tail: deque[str] = deque(maxlen=20)
    try:
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            env=env,
            # Shared mode relaxes cwcli's umask to 0007 for its group-owned state;
            # the install itself must stay readable by everyone who runs it.
            umask=0o022 if shared_home.shared_mode() else -1,
            **_as_user(run_as),
        )
    except OSError as exc:
        return 127, [f"could not start {argv[0]}: {exc}"]
    assert process.stdout is not None
    for line in process.stdout:
        on_output(line)
        if line.strip():
            tail.append(line.rstrip())
    return process.wait(), list(tail)


def _backup(root: Path, run_as: int | None) -> Path | None:
    """Copy the tool env beside itself (same filesystem, so the restore is a rename)."""
    backup = root.with_name(root.name + ".cwcli-rollback")
    shutil.rmtree(backup, ignore_errors=True)
    try:
        shutil.copytree(root, backup, symlinks=True)
    except OSError as exc:
        shutil.rmtree(backup, ignore_errors=True)
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "self_update.backup_failed",
            f"Could not copy {root} aside before upgrading ({exc}), so nothing was changed.",
        ) from exc
    # uv removes the old executables before it links the new ones, so a failure
    # there leaves no `cwcli` on PATH unless they are put back too.
    saved = backup / ".cwcli-entrypoints"
    saved.mkdir()
    manifest = {}
    for i, path in enumerate(_entrypoints(root)):
        if path.is_symlink() or path.is_file():
            with contextlib.suppress(OSError):
                shutil.copy2(path, saved / str(i), follow_symlinks=False)
                manifest[str(i)] = str(path)
    (saved / "manifest.json").write_text(json.dumps(manifest))
    if run_as is not None:  # root made the copy; it must stay the install owner's
        owner = root.stat()
        for dirpath, dirnames, filenames in os.walk(backup):
            for name in [*dirnames, *filenames, ""]:
                with contextlib.suppress(OSError):
                    os.lchown(os.path.join(dirpath, name), owner.st_uid, owner.st_gid)
    return backup


def _restore(root: Path, backup: Path | None) -> bool:
    if backup is None or not backup.is_dir():
        return False
    saved = backup / ".cwcli-entrypoints"
    with contextlib.suppress(OSError, ValueError):
        for name, dest in json.loads((saved / "manifest.json").read_text()).items():
            target = Path(dest)
            if target.is_dir() and not target.is_symlink():
                continue  # something else took the name; never delete a folder
            with contextlib.suppress(OSError):
                target.unlink(missing_ok=True)
                shutil.copy2(saved / name, target, follow_symlinks=False)
    shutil.rmtree(saved, ignore_errors=True)
    try:
        shutil.rmtree(root, ignore_errors=True)
        os.replace(backup, root)
    except OSError:
        return False
    return True


def _discard(backup: Path | None) -> None:
    if backup is not None:
        shutil.rmtree(backup, ignore_errors=True)


def installed_version() -> str | None:
    """The cwcli version on disk now, read by a fresh interpreter (this process
    still holds the old metadata)."""
    try:
        out = subprocess.run(
            [sys.executable, "-c", f"import importlib.metadata as m; print(m.version({DIST!r}))"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def _install_dir() -> Path:
    """The folder cwcli's own files are installed in (its site-packages)."""
    import importlib.metadata

    try:
        return Path(str(importlib.metadata.distribution(DIST).locate_file("")))
    except Exception:
        return install_root()


def _require_writable(target: Path) -> None:
    if os.access(target, os.W_OK):
        return
    shared = " shared" if shared_home.shared_mode() else ""
    raise CwcliError(
        ErrorKind.PRECONDITION,
        "self_update.not_writable",
        f"The{shared} cwcli install in {target} is not writable by this user, so nothing "
        "was changed.",
        hint="run `sudo cwcli self-update`",
    )


def _owner_if_root(target: Path) -> int | None:
    """The install's owner when root upgrades someone else's install, so the new
    files stay theirs; None to run as ourselves."""
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return None
    owner = target.stat().st_uid
    return owner if owner != 0 else None


def _uv_env(root: Path) -> dict[str, str]:
    """Point uv at the tool folder this install is in, and the bin folder its
    entry points were linked into (read from the tool's receipt)."""
    env = {"UV_TOOL_DIR": str(root.parent)}
    for path in _entrypoints(root):
        if path.name == "cwcli":
            env["UV_TOOL_BIN_DIR"] = str(path.parent)
    return env


def _entrypoints(root: Path) -> list[Path]:
    """The executables uv linked for this tool (``cwcli`` and its alias)."""
    if tool_env() is None:
        return []
    try:
        import toml

        entries = toml.load(root / "uv-receipt.toml")["tool"]["entrypoints"]
        return [Path(e["install-path"]) for e in entries if e.get("install-path")]
    except Exception:
        return []


def _find_uv(run_as: int | None) -> str:
    """``uv`` on PATH, else in the invoking or owning user's home (``sudo`` drops
    ``~/.local/bin`` from PATH)."""
    found = shutil.which("uv")
    if found:
        return found
    homes = []
    if os.environ.get("SUDO_USER"):
        homes.append(_home_of(os.environ["SUDO_USER"]))
    if run_as is not None:
        homes.append(_home_of(run_as))
    for home in filter(None, homes):
        for rel in (".local/bin/uv", ".cargo/bin/uv"):
            if (Path(home) / rel).is_file():
                return str(Path(home) / rel)
    raise CwcliError(
        ErrorKind.PRECONDITION,
        "self_update.uv_missing",
        "cwcli is a uv tool install, but `uv` is not on PATH, so nothing was changed.",
        hint="put uv on PATH (or run `sudo env PATH=$PATH cwcli self-update`)",
    )


# ------------------------------------------------------------------- the services


def running_services(*, exclude_pids: frozenset[int] = frozenset()) -> list[Service]:
    """Every cwcli service running now. Read it BEFORE the upgrade."""
    services: list[Service] = []
    system: set[str] = set()
    for kind, unit in (("cred-bridge", startup.CRED_BRIDGE), ("scheduler", startup.SCHEDULER)):
        if _unit_running(unit):
            services.append(
                Service(
                    kind=kind,
                    name=f"{_LABEL[kind]} (system service {unit.service_name})",
                    pid=_unit_pid(unit.service_name),
                    unit=unit.service_name,
                )
            )
            system.add(kind)
    daemons = [("auto-inspect", auto_inspect.PID_FILE)]
    if "cred-bridge" not in system:
        daemons.append(("cred-bridge", cred_daemon.PID_FILE))
    run = cwcli_home() / "run"
    daemons += [("ui", path) for path in sorted(run.glob("ui*.pid"))]
    for kind, pid_file in daemons:
        if not daemon_identity.is_running(pid_file):
            continue
        pid = daemon_identity.get_pid(pid_file)
        if pid is None or pid in exclude_pids:
            continue
        owner = _owner_of(pid)
        port = _ui_port(pid_file) if kind == "ui" else None
        who = _name_of(owner)
        name = _LABEL[kind] + (f" ({who}, port {port})" if kind == "ui" else f" ({who})")
        services.append(
            Service(kind=kind, name=name, pid=pid, owner=owner, port=port, env=_environ(pid))
        )
    return services


def restart(services: list[Service], version: str) -> list[ServiceRestart]:
    """Restart each service on the new install; one failure never stops the rest."""
    results = []
    for svc in services:
        try:
            results.append(_restart_unit(svc) if svc.unit else _restart_daemon(svc, version))
        except Exception as exc:  # report it and carry on with the next service
            results.append(_result(svc, False, None, f"could not restart: {exc}"))
    return results


_LABEL = {
    "ui": "web UI",
    "auto-inspect": "auto-inspect",
    "cred-bridge": "credential bridge",
    "scheduler": "scheduler",
}
_START_ARGS = {
    "auto-inspect": ("config", "auto-inspect", "start"),
    "cred-bridge": ("config", "cred-bridge", "start"),
}


def _result(svc: Service, ok: bool, pid: int | None, detail: str) -> ServiceRestart:
    return ServiceRestart(kind=svc.kind, name=svc.name, ok=ok, pid=pid, detail=detail)


def _restart_unit(svc: Service) -> ServiceRestart:
    assert svc.unit is not None
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        return _result(svc, False, svc.pid, "not restarted: a system service needs sudo")
    if svc.kind == "scheduler" and startup.repair_system_unit(startup.SCHEDULER):
        _systemctl("daemon-reload")
    done = _systemctl("restart", svc.unit)
    if done.returncode != 0:
        return _result(svc, False, None, f"systemctl restart failed: {done.stderr.strip()}")
    deadline = time.monotonic() + _HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        state = _systemctl("is-active", svc.unit).stdout.strip()
        pid = _unit_pid(svc.unit)
        if state == "active" and pid and pid != svc.pid:
            # A unit that dies right after starting reads active for a moment.
            time.sleep(1)
            if _systemctl("is-active", svc.unit).stdout.strip() == "active":
                return _result(svc, True, pid, "running on the new version")
        time.sleep(0.5)
    return _result(svc, False, None, f"not running after the restart (systemd says {state})")


def _restart_daemon(svc: Service, version: str) -> ServiceRestart:
    me = os.geteuid() if hasattr(os, "geteuid") else None
    if me not in (None, 0) and svc.owner not in (None, me):
        return _result(svc, False, svc.pid, "not restarted: it belongs to another user (use sudo)")
    run_as = svc.owner if me == 0 and svc.owner not in (None, 0) else None
    assert svc.pid is not None
    if not _terminate(svc.pid):
        return _result(svc, False, svc.pid, "the old process did not stop")
    if svc.kind == "ui":
        args: tuple[str, ...] = ("ui", "--no-browser", "--port", str(svc.port))
    else:
        args = _START_ARGS[svc.kind]
    env = {**(svc.env or os.environ), "CWCLI_NO_UPDATE_CHECK": "1"}
    env.pop("CWCLI_SERVE_TOKEN", None)
    if run_as is not None and not svc.env:
        env.update(_user_env(run_as))
    try:
        started = subprocess.run(
            [sys.executable, "-m", "caffeinated_whale_cli", *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=env,
            cwd=_home_of(run_as) if run_as is not None else None,
            timeout=_START_TIMEOUT_S,
            **_as_user(run_as),
        )
    except subprocess.TimeoutExpired:
        return _result(svc, False, None, "did not start within 90s")
    if started.returncode != 0:
        lines = (started.stderr or started.stdout).strip().splitlines()
        return _result(svc, False, None, f"did not start: {lines[-1] if lines else 'no output'}")
    if svc.kind == "ui":
        served = _ui_version(svc.port)
        if served != version:
            return _result(svc, False, None, f"answers as cwcli {served or 'nothing'}")
        return _result(svc, True, None, f"running cwcli {version}")
    pid_file = auto_inspect.PID_FILE if svc.kind == "auto-inspect" else cred_daemon.PID_FILE
    pid = daemon_identity.get_pid(pid_file) if daemon_identity.is_running(pid_file) else None
    if pid is None or pid == svc.pid:
        return _result(svc, False, None, "not running after the restart")
    return _result(svc, True, pid, "running on the new version")


def _terminate(pid: int) -> bool:
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + _STOP_TIMEOUT_S
    while time.monotonic() < deadline and daemon_identity.pid_alive(pid):
        time.sleep(0.1)
    if daemon_identity.pid_alive(pid):
        with contextlib.suppress(OSError):
            os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        time.sleep(0.5)
    return not daemon_identity.pid_alive(pid)


def _ui_port(pid_file: Path) -> int | None:
    try:
        port = json.loads(pid_file.with_suffix(".json").read_text()).get("port")
    except (OSError, ValueError, AttributeError):
        return None
    return port if isinstance(port, int) else None


def _ui_version(port: int | None) -> str | None:
    deadline = time.monotonic() + _HEALTH_TIMEOUT_S
    while port and time.monotonic() < deadline:
        try:
            url = f"http://127.0.0.1:{port}/api/v1/meta"
            with urllib.request.urlopen(url, timeout=2) as r:  # noqa: S310 - loopback
                return str(json.loads(r.read()).get("version"))
        except (OSError, ValueError):
            time.sleep(0.3)
    return None


# ------------------------------------------------------------------- systemd


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=120)


def _unit_running(unit: startup.BootUnit) -> bool:
    if startup.get_platform() != "linux" or shutil.which("systemctl") is None:
        return False
    if not startup.system_unit_path(unit).exists():
        return False
    try:
        state = _systemctl("is-active", unit.service_name).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    # activating: a scheduler unit written before it was Type=simple never leaves it.
    return state in ("active", "activating", "reloading")


def _unit_pid(service_name: str) -> int | None:
    try:
        out = _systemctl("show", "-p", "MainPID", "--value", service_name).stdout.strip()
        return int(out) or None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


# ------------------------------------------------------------------- process owners


def _owner_of(pid: int) -> int | None:
    try:
        return os.stat(f"/proc/{pid}").st_uid
    except OSError:
        return os.getuid() if hasattr(os, "getuid") else None


def _environ(pid: int) -> dict[str, str]:
    """The daemon's own environment (``DOCKER_HOST``, ``CWCLI_HOME``, PATH...), so
    its replacement runs under the same one; empty when it cannot be read."""
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return {}
    env = {}
    for item in raw.split(b"\0"):
        key, sep, value = item.decode(errors="replace").partition("=")
        if sep and key:
            env[key] = value
    return env


def _name_of(uid: int | None) -> str:
    if uid is None:
        return "this user"
    try:
        import pwd

        return pwd.getpwuid(uid).pw_name
    except (ImportError, KeyError):
        return f"uid {uid}"


def _home_of(user: int | str) -> str | None:
    try:
        import pwd

        entry = pwd.getpwuid(user) if isinstance(user, int) else pwd.getpwnam(user)
        return entry.pw_dir
    except (ImportError, KeyError):
        return None


def _user_env(uid: int) -> dict[str, str]:
    import pwd

    entry = pwd.getpwuid(uid)
    return {"HOME": entry.pw_dir, "USER": entry.pw_name, "LOGNAME": entry.pw_name}


def _as_user(uid: int | None) -> dict:
    """Popen keywords that run the child as ``uid`` with its own groups (root only)."""
    if uid is None:
        return {}
    import pwd

    entry = pwd.getpwuid(uid)
    return {
        "user": uid,
        "group": entry.pw_gid,
        "extra_groups": os.getgrouplist(entry.pw_name, entry.pw_gid),
    }
