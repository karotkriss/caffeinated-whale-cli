"""No environment-specific name may appear anywhere in the tracked tree.

Everything here is public (the repository) or published (the sdist and wheel,
whose README is the package page), so a neutral placeholder - `example.com`,
`my-instance`, `<user>` - is the only acceptable spelling for a host, server,
instance or home directory. `tests/test_packaging_contents.py` runs the same
scan over the built artifacts.

The banned names are stored only as SHA-256 hashes of their lowercased form.
Each word of the text is split into alphanumeric atoms, and the scan hashes
every atom and every two-atom span (bare, with its trailing separator, and with
both neighbouring separators), so a name, a dotted host suffix or a path prefix
is caught wherever it appears.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# ponytail: a fixed hash list; add the hash of a new name's lowercased form when one leaks.
ARTIFACT_HASHES = frozenset(
    {
        "1dc0c6ee433a74ce7080e8d09f1ca2d2cc460484d6aca158e8be1124fb782dff",
        "2c0e177a6c1c2b9236696ff9b38bf0590c8ad2640a8959cdabbb903420856913",
        "47aa4fffe7dfdbf72d9faf71e416089f2fe60c52c8175b4d1e0d5608dfcd9d5e",
        "6530ed5929936ef42c9e9eda5b2e4ac5103273da45a56f4ee2ecb5e259802c6d",
        "84401306a82d7f6587cc7a4738e314607e53c0f42bcc47125aa3c8439534c8c8",
        "88806f1dd277952b2f98f70933907f2b7b199fc92a6e86207906661c28caa93b",
        "97b1a28161826fe647de46e8724b98acd25dedc9fe382705a19c5082addae07f",
        "9956744901a417bc7cae5446ad73ee15e357f20aef9baf223820cc401774ebcb",
        "a8f09d3a10aa970b45568f491c195c73af2df4dd82d2a7d209a8002f4be28949",
        "ac1556a393645d143bfc4dec18d15a4c5e0b3ea156cf2018bbc0f5560e726613",
        "b17a13dfa4cbbf80d59bfb12e4008e670b99272bb526c1c7e05196b1bdde9515",
        "c2637ece8276675116b9401b81f5eea0e6685f7f765d6f7fbc3d46dbcad1bf48",
        "dbdf660341fa2ca3dffeb42ba9bf7da17b80c71f35a0184301b0b0bd9a8c3980",
        "dc12c22f0cab5fd6943c2870d9b8eaf55902c02ced0519a66cdb96bc47ac698a",
        "ec72c3ccd72c601b17a866c716c0db2855ca3be4b59de450731f6905dd2911c7",
    }
)
# Minified JavaScript bundles reuse short identifiers, so this one is checked in
# the tracked tree only (the bundles are built, never tracked).
TREE_HASHES = ARTIFACT_HASHES | {"4d2f4b668cfc48e6059d36e91326bf87de39ba819f35450477bc1ce614a63bbd"}

_WORD = re.compile(r"[a-z0-9./_-]+")
_ATOM = re.compile(r"[a-z0-9]+")
_digests: dict[str, str] = {}


def _candidates(text: str) -> Iterator[str]:
    for word in set(_WORD.findall(text.lower())):
        atoms = [(m.start(), m.end()) for m in _ATOM.finditer(word)]
        for i, (start, end) in enumerate(atoms):
            yield word[start:end]
            if i + 1 < len(atoms):
                stop = atoms[i + 1][1]
                yield word[start:stop]
                yield word[start : stop + 1]
                yield word[max(start - 1, 0) : stop + 1]


def _banned(text: str, hashes: frozenset[str]) -> bool:
    for candidate in _candidates(text):
        digest = _digests.get(candidate)
        if digest is None:
            digest = _digests[candidate] = hashlib.sha256(candidate.encode()).hexdigest()
        if digest in hashes:
            return True
    return False


def find_private_refs(name: str, data: bytes, hashes: frozenset[str]) -> list[str]:
    """`name: path` and `name:line` for the path and each line holding a banned name."""
    hits = [f"{name}: path"] if _banned(name, hashes) else []
    if b"\0" in data[:8192]:  # binary (images, fonts): no text to leak
        return hits
    text = data.decode("utf-8", "replace")
    if _banned(text, hashes):
        lines = enumerate(text.splitlines(), 1)
        hits += [f"{name}:{lineno}" for lineno, line in lines if _banned(line, hashes)]
    return hits


def test_no_tracked_file_names_private_infrastructure():
    listed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z"],
        check=True,
        capture_output=True,
    ).stdout.decode()
    names = [n for n in listed.split("\0") if n]
    assert len(names) > 100, f"git ls-files looks empty: {names}"
    hits: list[str] = []
    for name in names:
        path = REPO_ROOT / name
        if path.is_file():  # a tracked file deleted in the working tree has no content
            hits += find_private_refs(name, path.read_bytes(), TREE_HASHES)
    assert not hits, "replace with a neutral placeholder:\n" + "\n".join(hits)


def test_the_scan_catches_each_token_shape():
    """The scan itself works: a word, a host suffix and a path or address prefix."""
    hashes = frozenset(
        hashlib.sha256(t.encode()).hexdigest()
        for t in ["zebraquux", "alpha.beta", "/opt/sample-", "10.200."]
    )
    samples = [
        "cwcli start ZebraQuux",
        "https://git.alpha.beta/group/app",
        "/opt/sample-1000/session",
        "peer 10.200.4.5",
    ]
    for sample in samples:
        assert find_private_refs("x.md", sample.encode(), hashes) == ["x.md:1"], sample
    assert find_private_refs("docs/zebraquux.md", b"", hashes) == ["docs/zebraquux.md: path"]
    assert not find_private_refs(
        "x.md", b"zebraquuxx alpha.betax opt/sample 10.2004 example.com", hashes
    )
