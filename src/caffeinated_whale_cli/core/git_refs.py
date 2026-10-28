"""Branches and tags of a remote repository, for the Install sheet's branch field
(captain note 47): one ``git ls-remote --heads --tags`` on the HOST, bounded, memoised
per URL for a few minutes. The host's own git credential helpers (``gh``/``glab``, the
same ones the in-container bridge borrows) answer for a private repository; a
remote that cannot be read yields empty lists, never an error the field must show.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field

_TIMEOUT_S = 20
_TTL_S = 300
_URL = re.compile(r"^(https?://|git@|ssh://)[^\s'\"]+$")
_VERSION_TAIL = re.compile(r"(\d+)")


@dataclass(frozen=True, slots=True)
class RemoteRefs:
    url: str
    branches: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    ok: bool = True
    error: str | None = None


_memo: dict[str, tuple[float, RemoteRefs]] = {}
_lock = threading.Lock()


def _git_env() -> dict[str, str]:
    """The caller's environment (HOME and PATH included, so the user's own git config
    and credential helpers apply) with prompts off, plus the credential bridge's
    dispatch as git config: github.com asks ``gh``, every other host asks ``glab``,
    each only when installed. Config given through the environment sits after the
    user's helpers, so it answers only when nothing of theirs did."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    entries: list[tuple[str, str]] = []
    if shutil.which("gh"):
        entries.append(("credential.https://github.com.helper", "!gh auth git-credential"))
    if shutil.which("glab"):
        entries.append(("credential.helper", "!glab auth git-credential"))
    base = int(env.get("GIT_CONFIG_COUNT") or 0)
    for i, (key, value) in enumerate(entries):
        env[f"GIT_CONFIG_KEY_{base + i}"] = key
        env[f"GIT_CONFIG_VALUE_{base + i}"] = value
    if entries:
        env["GIT_CONFIG_COUNT"] = str(base + len(entries))
    return env


def _natural(name: str) -> list[object]:
    return [int(p) if p.isdigit() else p.lower() for p in _VERSION_TAIL.split(name)]


def list_remote_refs(url: str) -> RemoteRefs:
    """Heads and tags of ``url``, newest-looking first (``version-16`` before ``version-15``)."""
    url = url.strip()
    if not _URL.match(url):
        return RemoteRefs(url=url, ok=False, error="not a repository URL")
    with _lock:
        hit = _memo.get(url)
        if hit and time.monotonic() - hit[0] < _TTL_S:
            return hit[1]
    try:
        proc = subprocess.run(
            ["git", "ls-remote", "--heads", "--tags", "--refs", url],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
            env=_git_env(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return RemoteRefs(url=url, ok=False, error=str(exc)[:200])
    if proc.returncode != 0:
        return RemoteRefs(
            url=url, ok=False, error=(proc.stderr or "ls-remote failed").strip()[:200]
        )
    branches: list[str] = []
    tags: list[str] = []
    for line in proc.stdout.splitlines():
        _sha, _tab, ref = line.partition("\t")
        if ref.startswith("refs/heads/"):
            branches.append(ref[len("refs/heads/") :])
        elif ref.startswith("refs/tags/"):
            tags.append(ref[len("refs/tags/") :])
    branches.sort(key=_natural, reverse=True)
    tags.sort(key=_natural, reverse=True)
    refs = RemoteRefs(url=url, branches=branches, tags=tags)
    with _lock:
        _memo[url] = (time.monotonic(), refs)
    return refs
