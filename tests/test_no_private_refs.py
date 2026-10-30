"""No environment-specific name may appear anywhere in the tracked tree.

Everything here is public (the repository) or published (the sdist and wheel,
whose README is the package page), so a neutral placeholder - `example.com`,
`my-instance`, `<user>` - is the only acceptable spelling for a host, server,
instance or home directory. `tests/test_packaging_contents.py` runs the same
scan over the built artifacts.

The terms to refuse are never stored in the repository. CI supplies them in the
`PRIVATE_TERMS_DENYLIST` environment variable, one term per line, from the
repository secret of the same name; without it (a local run) the scans skip. A
term matches case-insensitively, and a letter or digit at either end of it must
not continue into a neighbouring letter or digit.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DENYLIST_ENV = "PRIVATE_TERMS_DENYLIST"


def compile_terms(text: str) -> re.Pattern[str] | None:
    """One alternation over the non-blank lines of `text`, or None when there are none."""
    parts = []
    for term in (line.strip() for line in text.splitlines()):
        if term:
            before = r"(?<![a-z0-9])" if term[0].isalnum() else ""
            after = r"(?![a-z0-9])" if term[-1].isalnum() else ""
            parts.append(before + re.escape(term) + after)
    return re.compile("|".join(parts), re.IGNORECASE) if parts else None


def denylist() -> re.Pattern[str]:
    pattern = compile_terms(os.environ.get(DENYLIST_ENV, ""))
    if pattern is None:
        pytest.skip(f"{DENYLIST_ENV} is not set; CI supplies it from the repository secret")
    return pattern


def find_private_refs(name: str, data: bytes, pattern: re.Pattern[str]) -> list[str]:
    """`name: path` and `name:line` for the path and each line holding a denied term."""
    hits = [f"{name}: path"] if pattern.search(name) else []
    if b"\0" in data[:8192]:  # binary (images, fonts): no text to leak
        return hits
    text = data.decode("utf-8", "replace")
    if pattern.search(text):
        lines = enumerate(text.splitlines(), 1)
        hits += [f"{name}:{lineno}" for lineno, line in lines if pattern.search(line)]
    return hits


def test_no_tracked_file_names_private_infrastructure():
    pattern = denylist()
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
            hits += find_private_refs(name, path.read_bytes(), pattern)
    assert not hits, "replace with a neutral placeholder:\n" + "\n".join(hits)


def test_the_scan_catches_each_term_shape():
    """The scan itself works: a word, a host suffix and a path or address prefix."""
    pattern = compile_terms("zebraquux\n\n  alpha.beta  \n/opt/sample-\n10.200.\n")
    assert pattern is not None
    samples = [
        "cwcli start ZebraQuux",
        "https://git.alpha.beta/group/app",
        "/opt/sample-1000/session",
        "peer 10.200.4.5",
    ]
    for sample in samples:
        assert find_private_refs("x.md", sample.encode(), pattern) == ["x.md:1"], sample
    assert find_private_refs("docs/zebraquux.md", b"", pattern) == ["docs/zebraquux.md: path"]
    assert not find_private_refs(
        "x.md", b"zebraquuxx alpha.betax opt/sample 110.200.4.5 10.2004 example.com", pattern
    )


def test_the_scan_skips_without_a_denylist(monkeypatch):
    assert compile_terms(" \n\n") is None
    monkeypatch.delenv(DENYLIST_ENV, raising=False)
    with pytest.raises(pytest.skip.Exception, match=DENYLIST_ENV):
        denylist()
