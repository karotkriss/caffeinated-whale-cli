"""Editors (VS Code, VS Code Insiders, Cursor): where they are on this host and
how a bench container is opened in one. UI-pure: no rich/questionary/typer.

Detection (``find_editor``) is the ONE answer every frontend uses - ``cwcli open``,
the web page's editor buttons and the catalog's editor list - so they can never
disagree about what is installed:

1. ``shutil.which`` on the editor's CLI name (native Linux, macOS, Windows, and a
   WSL distro whose PATH carries the Windows editor's ``bin`` dir, which VS Code's
   WSL integration sets up).
2. On WSL (``/proc/version`` says microsoft, or ``WSL_DISTRO_NAME`` is set), the
   Windows editors through the interop mounts: the per-user installs under
   ``/mnt/<drive>/Users/<user>/AppData/Local/Programs/...`` and the system
   installs under ``Program Files``. The ``bin/<cli>`` shell shim there runs the
   Windows executable, and the Windows editor attaches to Docker Desktop's
   container exactly as it does for the CLI's ``cwcli open``.
3. On macOS, the app bundles under ``/Applications`` (and ``~/Applications``)
   when the ``code``/``cursor`` shell command was never installed.

The open (``launch``) builds the Dev Containers attach URI
``vscode-remote://attached-container+<hex(container name)><path>`` and runs
``<editor> --folder-uri <uri>``. The extension preflight is deliberately
fail-OPEN on an UNREADABLE answer: ``--list-extensions`` through WSL interop can
take longer than its timeout, and treating that as "not installed" led to an
install attempt that failed on the same interop hiccup and aborted the open (the
page's "Could not open in VS Code, HTTP 500"). Only an extension that is
POSITIVELY absent after a failed install refuses, as before.
"""

from __future__ import annotations

import glob
import os
import platform
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from .errors import CwcliError, ErrorKind

EDITOR_IDS = ("code", "code-insiders", "cursor")

EDITOR_NAMES = {"code": "VS Code", "code-insiders": "VS Code Insiders", "cursor": "Cursor"}

EDITOR_INSTALL_URLS = {
    "code": "https://code.visualstudio.com/",
    "code-insiders": "https://code.visualstudio.com/insiders/",
    "cursor": "https://cursor.sh/",
}

# Cursor aliases the Microsoft Dev Containers extension id to its own publisher id.
DEV_CONTAINERS_EXTENSION_IDS = ("ms-vscode-remote.remote-containers", "anysphere.remote-containers")
DOCKER_EXTENSION_ID = "ms-azuretools.vscode-docker"
_WSL_INTEROP_MARK = "UtilAcceptVsock"

# Where the Windows installers put each editor, relative to the interop mount of
# a drive: per-user (``Users/<name>/AppData/Local/Programs``) and system-wide.
_WINDOWS_RELATIVE = {
    "code": ("Microsoft VS Code/bin/code", "Microsoft VS Code/bin/code.cmd"),
    "code-insiders": (
        "Microsoft VS Code Insiders/bin/code-insiders",
        "Microsoft VS Code Insiders/bin/code-insiders.cmd",
    ),
    "cursor": (
        "cursor/resources/app/bin/cursor",
        "cursor/resources/app/bin/cursor.cmd",
        "Cursor/resources/app/bin/cursor",
    ),
}
_MAC_BUNDLES = {
    "code": "Visual Studio Code.app/Contents/Resources/app/bin/code",
    "code-insiders": "Visual Studio Code - Insiders.app/Contents/Resources/app/bin/code",
    "cursor": "Cursor.app/Contents/Resources/app/bin/cursor",
}

IS_WINDOWS = platform.system() == "Windows"


def is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        return (
            "microsoft"
            in Path("/proc/version").read_text(encoding="utf-8", errors="replace").lower()
        )
    except OSError:
        return False


def _windows_candidates(editor: str) -> list[str]:
    out: list[str] = []
    for mount in sorted(glob.glob("/mnt/[a-z]")):
        for rel in _WINDOWS_RELATIVE[editor]:
            out.extend(sorted(glob.glob(f"{mount}/Users/*/AppData/Local/Programs/{rel}")))
            out.append(f"{mount}/Program Files/{rel}")
    return out


def find_editor(editor: str) -> str | None:
    """The command that runs ``editor`` on this host, or None when it is not installed."""
    found = shutil.which(editor)
    if found:
        return found
    candidates: list[str] = []
    if is_wsl():
        candidates = _windows_candidates(editor)
    elif platform.system() == "Darwin":
        candidates = [
            f"/Applications/{_MAC_BUNDLES[editor]}",
            str(Path.home() / "Applications" / _MAC_BUNDLES[editor]),
        ]
    for path in candidates:
        # ``which`` on an absolute path answers existence + executability, and keeps
        # detection one seam (a test that stubs ``shutil.which`` stubs all of it).
        if shutil.which(path):
            return path
    return None


def installed_editors() -> list[str]:
    return [e for e in EDITOR_IDS if find_editor(e)]


def editor_paths() -> dict[str, str]:
    """Every installed editor with the command it resolves to (for a page tooltip)."""
    return {e: p for e in EDITOR_IDS if (p := find_editor(e))}


# ------------------------------------------------------------------- the open

Log = Callable[[str], None]


def _nolog(_msg: str) -> None:
    pass


def list_extensions(command: str) -> str | None:
    """The editor's installed extension ids (lowercased), or None when the list
    could not be read (timeout, missing binary): UNKNOWN, never "none"."""
    try:
        result = subprocess.run(
            [command, "--list-extensions"],
            capture_output=True,
            text=True,
            timeout=15,
            shell=IS_WINDOWS,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.lower()


def install_extension(command: str, extension_id: str, name: str, log: Log = _nolog) -> bool:
    log(f"Installing {name}...")
    try:
        result = subprocess.run(
            [command, "--install-extension", extension_id],
            capture_output=True,
            text=True,
            timeout=60,
            shell=IS_WINDOWS,
        )
    except subprocess.TimeoutExpired:
        log("Installation timed out.")
        return False
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        log(f"Installation failed: {e}")
        return False
    if result.returncode == 0:
        log(f"{name} installed.")
        return True
    # Some editor CLIs report "already installed" with a non-zero exit: that is success.
    if "already installed" in f"{result.stdout}\n{result.stderr}".lower():
        log(f"{name} already installed.")
        return True
    log(f"Failed to install {name}: {result.stderr.strip()}")
    return False


def _ensure_extension(
    command: str, ids: tuple[str, ...], name: str, log: Log, *, required: bool
) -> None:
    listed = list_extensions(command)
    if listed is None:
        # Unreadable, not absent: the editor itself tells the user if the extension
        # is missing; refusing here on an interop hiccup was the shipped 500.
        log(
            f"Could not read the editor's extensions; opening anyway ({name} is checked by the editor)."
        )
        return
    if any(i in listed for i in ids):
        return
    # Each id in turn: Cursor's marketplace carries the extension under its own
    # publisher id (``anysphere.*``) and not under Microsoft's, so the first id a
    # given editor can install wins.
    for ext_id in ids:
        if install_extension(command, ext_id, name, log):
            return
    rechecked = list_extensions(command)
    if rechecked is not None and any(i in rechecked for i in ids):
        return  # the install call failed (marketplace 5xx) but it is there locally
    if required:
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "editor.extension_missing",
            f"Cannot open without {name}.",
            hint=f"Install the '{ids[0]}' extension in the editor, then retry.",
        )


def container_name_to_hex(container_name: str) -> str:
    return container_name.encode().hex()


def attach_uri(container_name: str, path: str) -> str:
    return f"vscode-remote://attached-container+{container_name_to_hex(container_name)}{path}"


def launch(editor: str, container_name: str, path: str, log: Log = _nolog) -> str:
    """Open ``path`` inside ``container_name`` in ``editor``; returns the URI opened.

    Raises ``CwcliError`` (NOT_FOUND editor, DOCKER container, PRECONDITION
    extension) - never a frontend exit - so every frontend maps it its own way.
    """
    command = find_editor(editor) or editor  # an unresolvable id fails on its own run below
    try:
        inspected = subprocess.run(
            ["docker", "inspect", container_name], capture_output=True, text=True, timeout=10
        )
    except subprocess.TimeoutExpired:
        raise CwcliError(ErrorKind.DOCKER, "docker.timeout", "Docker inspect timed out.") from None
    except FileNotFoundError:
        raise CwcliError(ErrorKind.DOCKER, "docker.missing", "Docker command not found.") from None
    if inspected.returncode != 0:
        raise CwcliError(
            ErrorKind.NOT_FOUND,
            "container.missing",
            f"Container '{container_name}' does not exist.",
        )
    _ensure_extension(command, (DOCKER_EXTENSION_ID,), "Docker extension", log, required=False)
    dev_ids = (
        DEV_CONTAINERS_EXTENSION_IDS[::-1] if editor == "cursor" else DEV_CONTAINERS_EXTENSION_IDS
    )
    _ensure_extension(command, dev_ids, "Dev Containers extension", log, required=True)
    uri = attach_uri(container_name, path)
    name = EDITOR_NAMES.get(editor, editor)
    for attempt in (1, 2):
        try:
            run = subprocess.run(
                [command, "--folder-uri", uri],
                capture_output=True,
                text=True,
                shell=IS_WINDOWS,
                timeout=60,
            )
        except FileNotFoundError:
            raise CwcliError(
                ErrorKind.NOT_FOUND,
                "editor.not_installed",
                f"{name} is not installed.",
                hint=f"Install it from {EDITOR_INSTALL_URLS.get(editor, '')}".rstrip(),
            ) from None
        except subprocess.TimeoutExpired as e:
            raise CwcliError(
                ErrorKind.PRECONDITION, "editor.launch_failed", f"Failed to open {name}: {e}"
            ) from e
        if run.returncode == 0:
            return uri
        stderr = (run.stderr or "").strip()
        # WSL's interop can time out launching a Windows program ("UtilAcceptVsock
        # ... accept4 failed 110"); it is transient, so one retry before refusing.
        if _WSL_INTEROP_MARK in stderr and attempt == 1:
            log("WSL could not launch the Windows editor (interop timeout); retrying once.")
            time.sleep(2)
            continue
        hint = (
            "WSL could not launch the Windows program (interop timed out). Retry in a moment; "
            "if it keeps failing, Windows interop itself is unwell on this machine."
            if _WSL_INTEROP_MARK in stderr
            else None
        )
        last = stderr.splitlines()[-1][:200] if stderr else ""
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "editor.launch_failed",
            f"Failed to open {name}: exit {run.returncode}" + (f": {last}" if last else ""),
            hint=hint,
        )
    return uri
