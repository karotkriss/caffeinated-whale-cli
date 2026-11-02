"""Branches and tags of a remote repository, for the Install sheet's branch field
(captain note 47): one ``git ls-remote --heads --tags`` on the HOST, bounded, memoised
per URL for a few minutes. The host's own ``gh``/``glab`` (on a shared install, the
app catalog's credential via ``credential_helper``: the shared credential first)
answers for a private repository; a remote that cannot be read yields empty lists,
never an error the field must show.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

from ..utils import shared_home
from .app_catalog import credential_answer, gitlab_hosts
from .credbridge import neutral_cwd

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


def credential_helper() -> str:
    """The git credential helper a shared install's host ``git`` is given: this
    module's entry, which answers with ``app_catalog.credential_answer`` - the shared
    credential first, then the account's own ``gh``/``glab``. Shared mode is POSIX
    only, so a frozen standalone exe (no ``-m``) never reaches it."""
    return f"!{shlex.quote(sys.executable)} -m caffeinated_whale_cli.core.git_refs credential"


def _git_env() -> dict[str, str]:
    """The caller's environment (HOME and PATH included, so the user's own git config
    and credential helpers apply) with prompts off, plus the credential bridge's
    dispatch as git config: github.com asks ``gh``, each host glab is configured for
    asks ``glab``, each only when installed; on a shared install every one of them
    asks :func:`credential_helper` instead. Config given through the environment sits
    after the user's helpers, so it answers only when nothing of theirs did."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    entries: list[tuple[str, str]] = []
    if shared_home.shared_mode():
        helper = credential_helper()
        entries.extend(
            (f"credential.https://{host}.helper", helper)
            for host in ["github.com", *gitlab_hosts()]
        )
    else:
        if shutil.which("gh"):
            entries.append(("credential.https://github.com.helper", "!gh auth git-credential"))
        if shutil.which("glab"):
            entries.extend(
                (f"credential.https://{host}.helper", "!glab auth git-credential")
                for host in gitlab_hosts()
            )
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
            # git runs the gh/glab credential helpers from ITS working directory;
            # never the caller's (see credbridge.neutral_cwd).
            cwd=neutral_cwd(),
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


def _credential(action: str) -> int:
    """git's credential helper (:func:`credential_helper`). Only ``get`` answers."""
    if action == "get":
        request = sys.stdin.buffer.read().decode("utf-8", "replace")
        host = next((ln[5:] for ln in request.splitlines() if ln.startswith("host=")), "")
        sys.stdout.buffer.write(credential_answer(host) if host else b"")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "credential":
        raise SystemExit(_credential(sys.argv[2]))
    raise SystemExit(2)
