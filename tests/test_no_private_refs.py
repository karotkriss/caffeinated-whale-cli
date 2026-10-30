"""No environment-specific name may appear anywhere in the tracked tree.

Examples, comments, fixtures and planning notes once named private hosts,
servers, instances and home directories. Everything here is public (the
repository) or published (the sdist and wheel, whose README is the package
page), so a neutral placeholder - `example.com`, `my-instance`, `<user>` - is
the only acceptable spelling. `tests/test_packaging_contents.py` runs the same
scan over the built artifacts.

Each pattern is written so it does not match its own source text (`[x]`
classes, escaped dots), so this file needs no exemption.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# ponytail: a fixed list of known names; add a name here when a new one leaks.
_TERMS = [
    r"gov\.gy\b",  # any host under the private government domain
    r"\beg[o]v\b",
    r"\b10\.9\.\d{1,3}\.\d{1,3}\b",  # the private network's address range
    r"\bstaging1[6]\b",
    r"\btest55[5]\b",
    r"\bfrappemat[e]\b",
    r"\bsws1[6]\b",
    r"\bfmnc[s]\b",
    r"\bgca[a]\b",
    r"\bner[s]\b",
    r"\bvisa1[5]\b",
    r"\bchp[a]\b",
    r"cmck[a]y",  # the user name, so every home-directory path too
    r"/tmp/clau[d]e-",
    r"treehous[e]",
]
# `g2` is a server name, but minified JavaScript bundles use it as an identifier,
# so it is checked in the tracked tree only (the bundles are built, never tracked).
_TREE_ONLY_TERMS = [r"\bg[2]\b"]

ARTIFACT_PATTERN = re.compile("|".join(_TERMS), re.IGNORECASE)
TREE_PATTERN = re.compile("|".join(_TERMS + _TREE_ONLY_TERMS), re.IGNORECASE)


def find_private_refs(name: str, data: bytes, pattern: re.Pattern[str]) -> list[str]:
    """`name:line: match` for every hit in the file's path or its text content."""
    hits = [f"{name}: path" for _ in pattern.finditer(name)]
    if b"\0" in data[:8192]:  # binary (images, fonts): no text to leak
        return hits
    text = data.decode("utf-8", "replace")
    for lineno, line in enumerate(text.splitlines(), 1):
        hits += [f"{name}:{lineno}: {m.group(0)}" for m in pattern.finditer(line)]
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
            hits += find_private_refs(name, path.read_bytes(), TREE_PATTERN)
    assert not hits, "replace with a neutral placeholder:\n" + "\n".join(hits)


def test_the_scan_catches_each_term():
    """The scan itself works: a stray private name must fail it."""
    # Split so this file's own text does not match.
    samples = [
        "https://dev.gov" + ".gy/group/app",
        "10.9" + ".1.20",
        "cwcli start Staging1" + "6",
        "/home/cmck" + "ay/projects",
        "instance g" + "2",
    ]
    for sample in samples:
        assert find_private_refs("x.md", sample.encode(), TREE_PATTERN), sample
    assert not find_private_refs(
        "x.md", b"https://gitlab.example.com/g/app 192.0.2.7", TREE_PATTERN
    )
