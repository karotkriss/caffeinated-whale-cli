"""Opt-in shared/multi-user home for cwcli (POSIX only).

Several people on one machine share the SAME Frappe instances through the
``docker`` group, but cwcli's own on-disk state is per-user by construction and
locked down hard (cache ``0700``, DB ``0600``, config owned by whoever ran cwcli
first, a per-user credential-bridge daemon). The second person then hits
``EACCES`` on the cache, cannot edit config, and the credential bridge either
locks them out or hands them the FIRST user's token. See the scout report
``firstmate/data/cwcli-shared-home-multiuser/report.md``.

The fix is a well-trodden pattern (Docker's ``root:docker`` socket, Homebrew on
Linux's shared setgid tree): an OPT-IN dedicated ``cwcli`` group, a system state
tree with setgid group-writable dirs, one machine-wide credential-bridge daemon
behind a group-owned socket, and a stable service uid/gid for container-user
alignment.

This module is the single source of truth for two things:

* the shared-mode GATE and home resolution (``shared_mode`` / ``state_dir`` /
  ``group`` / ``gid`` / ``service_uid``), read from the machine marker written by
  ``cwcli setup --shared``;
* the file-mode POLICY threaded through every writer. Per-user modes are
  UNCHANGED - the ``*_mode(per_user)`` helpers return the writer's own per-user
  mode verbatim when shared mode is off, and the ``secure_*_shared_only`` helpers
  are no-ops off. So a default install sees byte-identical behavior; nothing
  changes for anyone who does not opt in.

Windows is single-user by design: :func:`is_posix` is False there, so
:func:`shared_mode` is always False and the whole subsystem is inert.

Imports only stdlib + ``toml`` (already a dep) so ``config_utils`` can import it
without a cycle. ``grp``/``pwd`` are POSIX-only and imported lazily, since they
are only reachable once :func:`shared_mode` is True (never on Windows).
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path

import toml

# The machine marker an admin writes with ``cwcli setup --shared``. Its presence
# (with ``enabled = true``) is what turns shared mode on. ``CWCLI_SHARED_MARKER``
# relocates it - tests point it at a temp file, since ``/etc/cwcli`` needs root.
DEFAULT_MARKER_PATH = Path("/etc/cwcli/shared.toml")
DEFAULT_STATE_DIR = Path("/var/lib/cwcli")
DEFAULT_RUN_DIR = Path("/run/cwcli")
DEFAULT_GROUP = "cwcli"
SERVICE_USER = "cwcli"

# setgid group-writable for dirs, group-writable for files: the two shared-mode
# constants every writer resolves to.
SHARED_DIR_MODE = 0o2770
SHARED_FILE_MODE = 0o660


def is_posix() -> bool:
    """Shared mode is a POSIX (Linux/macOS) feature; Windows stays per-user."""
    return sys.platform != "win32" and hasattr(os, "getuid")


def marker_path() -> Path:
    override = os.environ.get("CWCLI_SHARED_MARKER")
    return Path(override) if override else DEFAULT_MARKER_PATH


def marker_config() -> dict | None:
    """The parsed marker when shared mode is ON, else None.

    Returns None on Windows, when the marker is absent/unreadable/malformed, or
    when it exists but is not ``enabled``. Never raises - a broken marker must
    degrade to per-user, not crash cwcli.
    """
    if not is_posix():
        return None
    try:
        data = toml.load(marker_path())
    except (OSError, toml.TomlDecodeError):
        return None
    if not isinstance(data, dict) or not data.get("enabled", False):
        return None
    return data


def shared_mode() -> bool:
    """Whether cwcli is running in opt-in shared/multi-user mode."""
    return marker_config() is not None


def state_dir() -> Path | None:
    """The shared state tree root (default ``/var/lib/cwcli``), or None if off."""
    cfg = marker_config()
    if cfg is None:
        return None
    return Path(cfg.get("state_dir") or DEFAULT_STATE_DIR)


def group_name() -> str:
    """The dedicated group name (default ``cwcli``)."""
    cfg = marker_config() or {}
    return str(cfg.get("group") or DEFAULT_GROUP)


def gid() -> int | None:
    """The dedicated group's gid, or None when the group is absent/unreadable."""
    if not is_posix():
        return None
    import grp

    try:
        return grp.getgrnam(group_name()).gr_gid
    except (KeyError, OSError):
        return None


def service_uid() -> int | None:
    """The ``cwcli`` service account's uid - the STABLE container-alignment target.

    None when the account is absent/unreadable; callers fall back to their
    per-user behavior (``os.getuid``) so a half-provisioned box never crashes.
    """
    cfg = marker_config() or {}
    if not is_posix():
        return None
    import pwd

    name = str(cfg.get("service_user") or SERVICE_USER)
    try:
        return pwd.getpwnam(name).pw_uid
    except (KeyError, OSError):
        return None


# --------------------------------------------------------------------- mode policy


def dir_mode(per_user: int) -> int:
    """A state dir's mode: setgid group-writable in shared mode, else per-user."""
    return SHARED_DIR_MODE if shared_mode() else per_user


def file_mode(per_user: int) -> int:
    """A state file's mode: group-writable in shared mode, else per-user."""
    return SHARED_FILE_MODE if shared_mode() else per_user


def socket_mode() -> int:
    """The credential-socket mode.

    Per-user keeps ``0666`` (unchanged; the container's ``frappe`` user connects
    regardless of uid, and ``projects/`` hardening is the per-user gate). Shared
    mode drops it to ``0660`` and chgrps the socket to the ``cwcli`` group, so
    "who may ask the bridge for a token" becomes group membership rather than
    everyone - closing the cross-user credential leak (report row 5).
    """
    return SHARED_FILE_MODE if shared_mode() else 0o666


def apply_group(path: Path | str) -> None:
    """chgrp ``path`` to the ``cwcli`` group in shared mode; no-op otherwise.

    Best-effort: a missing group or a chown-less filesystem is silently left
    alone rather than failing a write.
    """
    if not shared_mode():
        return
    group_id = gid()
    if group_id is None:
        return
    with contextlib.suppress(OSError):
        os.chown(path, -1, group_id)


def secure_dir_shared_only(path: Path | str) -> None:
    """Setgid group-writable + cwcli group, in SHARED mode only.

    For dirs that carry NO explicit per-user chmod today (config dir, run dir, rm
    archive): a no-op off, so the per-user default mode is preserved exactly.
    """
    if not shared_mode():
        return
    with contextlib.suppress(OSError):
        os.chmod(path, SHARED_DIR_MODE)
    apply_group(path)


def secure_file_shared_only(path: Path | str) -> None:
    """Group-writable + cwcli group, in SHARED mode only (no-op per-user)."""
    if not shared_mode():
        return
    with contextlib.suppress(OSError):
        os.chmod(path, SHARED_FILE_MODE)
    apply_group(path)


def apply_process_umask() -> None:
    """Relax the process umask to 0007 in shared mode, so incidental files cwcli
    (or a library) creates inside the setgid tree stay group-writable without an
    explicit chmod. A no-op per-user, so the default umask is untouched. Called
    once at the CLI entry, before any command writes state.
    """
    if shared_mode():
        os.umask(0o007)
