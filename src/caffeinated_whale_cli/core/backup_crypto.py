"""Client-side encryption for the backup store's off-site copies: AES-256-GCM in
4 MiB frames under one key kept on this machine (``<cwcli home>/backups/keys/
offsite.key``, mode 0600, generated on first use). A copy in the bucket is useless
without that key, so the key can be exported to a file for another machine and
imported there; it is never printed.

File format (``.cwenc``): ``CWENC1`` + 8-byte nonce prefix, then frames of
``[4-byte big-endian ciphertext length][ciphertext+tag]``; frame ``i`` uses nonce
``prefix + i`` (96 bits) and authenticates its index and the total frame flag as
associated data, so frames cannot be dropped, reordered or truncated unnoticed.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path
from typing import BinaryIO

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..utils import shared_home
from ..utils.config_utils import cwcli_home
from .errors import CwcliError, ErrorKind

MAGIC = b"CWENC1"
FRAME = 4 * 1024 * 1024


def key_path() -> Path:
    return cwcli_home() / "backups" / "keys" / "offsite.key"


def load_key(*, create: bool = True) -> bytes:
    p = key_path()
    try:
        data = p.read_bytes()
        if len(data) != 32:
            raise CwcliError(
                ErrorKind.PRECONDITION, "crypto.key_corrupt", f"{p} is not a 32-byte key"
            )
        return data
    except FileNotFoundError:
        if not create:
            raise CwcliError(
                ErrorKind.PRECONDITION,
                "crypto.key_missing",
                f"No off-site encryption key at {p}.",
                hint="Import the key from the machine that made the copies: cwcli backups key import --from <file>",
            ) from None
    p.parent.mkdir(parents=True, exist_ok=True)
    shared_home.secure_dir_shared_only(p.parent)
    key = os.urandom(32)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    shared_home.secure_file_shared_only(p)
    return key


def export_key(to: Path) -> Path:
    key = load_key(create=True)
    fd = os.open(to, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return to


def import_key(src: Path, *, replace: bool = False) -> Path:
    data = Path(src).read_bytes()
    if len(data) != 32:
        raise CwcliError(ErrorKind.USAGE, "crypto.key_corrupt", f"{src} is not a 32-byte key")
    p = key_path()
    if p.exists() and not replace:
        raise CwcliError(
            ErrorKind.CONFLICT,
            "crypto.key_exists",
            f"A key already exists at {p}; replacing it makes this machine's own copies unreadable.",
            hint="Pass --replace if that is intended (export the current key first).",
        )
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return p


def _nonce(prefix: bytes, index: int) -> bytes:
    return prefix + struct.pack(">I", index)


def encrypt_stream(src: BinaryIO, dst: BinaryIO, key: bytes) -> int:
    """Encrypt ``src`` into ``dst``; returns the plaintext size."""
    aead = AESGCM(key)
    prefix = os.urandom(8)
    dst.write(MAGIC + prefix)
    index, total = 0, 0
    while True:
        chunk = src.read(FRAME)
        last = len(chunk) < FRAME
        ct = aead.encrypt(_nonce(prefix, index), chunk, struct.pack(">IB", index, 1 if last else 0))
        dst.write(struct.pack(">I", len(ct)) + ct)
        total += len(chunk)
        index += 1
        if last:
            return total


def decrypt_stream(src: BinaryIO, dst: BinaryIO, key: bytes) -> int:
    head = src.read(len(MAGIC) + 8)
    if head[: len(MAGIC)] != MAGIC or len(head) != len(MAGIC) + 8:
        raise CwcliError(
            ErrorKind.PRECONDITION, "crypto.not_encrypted", "Not a cwcli encrypted file"
        )
    prefix = head[len(MAGIC) :]
    aead = AESGCM(key)
    index, total = 0, 0
    while True:
        hdr = src.read(4)
        if len(hdr) != 4:
            raise CwcliError(
                ErrorKind.PRECONDITION, "crypto.truncated", "Encrypted file is truncated"
            )
        (n,) = struct.unpack(">I", hdr)
        ct = src.read(n)
        if len(ct) != n:
            raise CwcliError(
                ErrorKind.PRECONDITION, "crypto.truncated", "Encrypted file is truncated"
            )
        for last in (0, 1):
            try:
                pt = aead.decrypt(_nonce(prefix, index), ct, struct.pack(">IB", index, last))
                break
            except InvalidTag:
                pt = None
        if pt is None:
            raise CwcliError(
                ErrorKind.PRECONDITION,
                "crypto.bad_key_or_tampered",
                f"Frame {index} does not authenticate: wrong key, or the copy was altered",
            )
        dst.write(pt)
        total += len(pt)
        index += 1
        if last:
            return total


def encrypt_file(src: Path, dst: Path, key: bytes) -> int:
    with open(src, "rb") as i, open(dst, "wb") as o:
        return encrypt_stream(i, o, key)


def decrypt_file(src: Path, dst: Path, key: bytes) -> int:
    with open(src, "rb") as i, open(dst, "wb") as o:
        return decrypt_stream(i, o, key)
