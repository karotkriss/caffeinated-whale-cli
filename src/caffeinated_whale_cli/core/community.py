"""Community integrations: an Admin adds one from a git repository URL (UI-pure).

A community integration is an installable Python package from a GitHub, GitLab.com
or self-hosted GitLab repository that registers one ``core.integrations.Integration``
under the ``cwcli.integrations`` entry-point group. Its repository declares it
statically in ``pyproject.toml``, so cwcli can show what it is before running any of
its code::

    [project.entry-points."cwcli.integrations"]
    hello = "cwcli_hello:INTEGRATION"      # the entry-point name is the id

    [tool.cwcli.integration]
    name = "Hello webhook"
    publisher = "Example Org"
    description = "Posts a hello to a webhook."
    provides = ["backup_target"]            # sign_in and/or backup_target

The life cycle, each step an explicit Admin act:

* ``preview(url, ref)`` fetches the repository at the branch or tag (default branch
  when omitted) and reads that declaration - nothing is built, imported or installed.
  A repository without a valid declaration is refused here. What it returns is the
  trust screen: name, publisher, description, hook points, URL and the exact commit.
* ``install(url, ref, commit=...)`` installs exactly the commit the Admin reviewed
  (the ref moving in between is refused) into its own directory under
  ``<cwcli home>/integrations/community/``, never cwcli's environment or the system.
  pip comes from a private venv there. The integration starts Off like every other.
* ``check_update(id)`` / ``update(id, commit=...)``: nothing updates by itself; an
  update shows the old and new commit and installs only the reviewed one, and turns
  the integration off so the new code's own test runs before it is on again.
* ``remove(id)`` deletes the package, its settings and its secrets.

Sign-in. Every fetch is tried anonymously first, with every git credential helper
and prompt switched off, so a public repository needs no sign-in. Only when the host
answers that sign-in is required does git ask this machine's existing sign-ins,
through the same ``credbridge.host_credential`` the container bridge uses (host
``gh`` for GitHub, ``glab`` for GitLab.com and self-hosted GitLab). cwcli stores no
token; when no sign-in answers, the error names the one to run (``glab auth login
--hostname dev.example.org``). Only ``https`` URLs ever reach a sign-in.

Every add, update and remove appends one line to ``<cwcli home>/integrations-audit.log``:
who, the URL and the commit.
"""

from __future__ import annotations

import contextlib
import dataclasses
import importlib
import importlib.metadata
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import toml

from ..utils import config_utils, shared_home
from . import accounts, credbridge
from . import integrations as core_integrations
from .errors import CwcliError, ErrorKind

ENTRY_POINT_GROUP = "cwcli.integrations"
HOOK_POINTS = ("sign_in", "backup_target")  # the hook points cwcli calls
_ID = re.compile(r"^[a-z][a-z0-9_-]{1,39}$")
_RESERVED_IDS = ("local", "host", "api")  # session and schedule-owner sources
# The test suite installs from local repositories; nothing a user runs sets this.
_TEST_ONLY_FILE_URLS = "CWCLI_TEST_ONLY_ALLOW_FILE_REPOSITORY_URLS"
_SIGN_IN_MISSING = "cwcli-community: no sign-in for "
_AUTH_MARKERS = (
    "terminal prompts disabled",
    "could not read username",
    "could not read password",
    "authentication failed",
    "access denied",
    "returned error: 401",
    "returned error: 403",
)
_GIT_TIMEOUT_S = 300
_PIP_TIMEOUT_S = 900

_lock = threading.RLock()
# id -> (commit, the loaded integration, the site dir it came from, why it did not load)
_loaded: dict[str, tuple[str, core_integrations.Integration, str, str | None]] = {}


@dataclass(frozen=True, slots=True)
class Declaration:
    id: str
    name: str
    publisher: str
    description: str
    provides: tuple[str, ...]
    project: str  # the Python distribution name


@dataclass(frozen=True, slots=True)
class Preview:
    """The trust screen: what the repository declares, at which exact commit."""

    url: str
    ref: str | None
    commit: str
    signed_in_with: str | None  # "gh" / "glab", or None when read anonymously
    declaration: Declaration

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def root() -> Path:
    return config_utils.cwcli_home() / "integrations" / "community"


def audit_path() -> Path:
    return config_utils.cwcli_home() / "integrations-audit.log"


# ----------------------------------------------------------------------- fetching


def check_url(url: str) -> str:
    """The repository URL, refused when it is not one cwcli fetches."""
    url = (url or "").strip()
    parts = urlsplit(url)
    schemes = ("https", "file") if os.environ.get(_TEST_ONLY_FILE_URLS) == "1" else ("https",)
    if parts.scheme not in schemes or (parts.scheme != "file" and not parts.hostname):
        raise CwcliError(
            ErrorKind.USAGE,
            "community.url",
            f"Not a repository URL cwcli can add: {url!r}. Use the https:// address, "
            "for example https://github.com/owner/repo.",
        )
    if parts.username or parts.password:
        raise CwcliError(
            ErrorKind.USAGE,
            "community.url_credentials",
            "The URL carries a user name or token. Remove it: cwcli signs in through "
            "this machine's gh or glab sign-in, never a token in the URL.",
        )
    return url


def _check_ref(ref: str | None) -> str | None:
    ref = (ref or "").strip() or None
    if ref is not None and (ref.startswith("-") or any(c.isspace() for c in ref)):
        raise CwcliError(ErrorKind.USAGE, "community.ref", f"Not a branch or tag: {ref!r}.")
    return ref


def _tool(host: str) -> str:
    return credbridge.tool_for_host(host)


def sign_in_fix(host: str) -> str:
    """The command that signs this machine in to ``host``."""
    tool = _tool(host)
    if tool == "gh":
        return "gh auth login" if host == "github.com" else f"gh auth login --hostname {host}"
    return f"glab auth login --hostname {host}"


def _git_env() -> dict[str, str]:
    # An empty GIT_ASKPASS stops git reaching core.askPass/SSH_ASKPASS (an editor's
    # askpass, say); with terminal prompts off a needed sign-in fails instead of waiting.
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": ""}


def _git(args: list[str], *, helper: str | None = None) -> subprocess.CompletedProcess:
    # An empty credential.helper clears every helper configured on this machine, so
    # "anonymous" really is anonymous; the sign-in, when wanted, is the only one left.
    cmd = ["git", "-c", "credential.helper=", "-c", "core.askPass="]
    if helper is not None:
        cmd += ["-c", f"credential.helper={helper}"]
    try:
        return subprocess.run(
            cmd + args, capture_output=True, text=True, env=_git_env(), timeout=_GIT_TIMEOUT_S
        )
    except FileNotFoundError as e:
        raise CwcliError(
            ErrorKind.PRECONDITION, "community.no_git", "git is not installed on this machine."
        ) from e
    except subprocess.TimeoutExpired as e:
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "community.fetch_timeout",
            f"git did not finish within {_GIT_TIMEOUT_S}s.",
        ) from e


def _helper() -> str:
    return f"!{shlex.quote(sys.executable)} -m caffeinated_whale_cli.core.community credential"


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and _SIGN_IN_MISSING not in ln]
    return lines[-1] if lines else "no output"


def _fetch(url: str, ref: str | None, dest: Path) -> tuple[str, str | None]:
    """Check ``url`` at ``ref`` out into ``dest``: (commit, the sign-in used or None)."""
    _git(["init", "-q", str(dest)])
    fetch = ["-C", str(dest), "fetch", "-q", "--depth", "1", "--no-tags", url, ref or "HEAD"]
    done = _git(fetch)
    signed_in_with = None
    if done.returncode != 0:
        host = urlsplit(url).hostname or ""
        needs_sign_in = any(m in done.stderr.lower() for m in _AUTH_MARKERS)
        if not needs_sign_in or urlsplit(url).scheme != "https":
            raise CwcliError(
                ErrorKind.PRECONDITION,
                "community.fetch_failed",
                f"Could not fetch {url}{f' ({ref})' if ref else ''}: {_last_line(done.stderr)}",
            )
        signed_in_with = _tool(host)
        done = _git(fetch, helper=_helper())
        if done.returncode != 0 and _SIGN_IN_MISSING in done.stderr:
            fix = sign_in_fix(host)
            installed = shutil.which(signed_in_with) is not None
            raise CwcliError(
                ErrorKind.PRECONDITION,
                "community.sign_in_missing",
                f"{url} needs a sign-in and this machine has no {signed_in_with} sign-in "
                f"for {host}.",
                hint=f"Run: {fix}" + ("" if installed else f" (install {signed_in_with} first)"),
                detail={"command": fix, "host": host, "tool": signed_in_with},
            )
        if done.returncode != 0:
            raise CwcliError(
                ErrorKind.PRECONDITION,
                "community.unreadable",
                f"The {signed_in_with} sign-in for {host} cannot read {url}"
                f"{f' ({ref})' if ref else ''}: {_last_line(done.stderr)}. Check the URL, "
                "the branch or tag, and that the signed-in account can read the repository.",
            )
    checkout = _git(["-C", str(dest), "checkout", "-q", "--detach", "FETCH_HEAD"])
    head = _git(["-C", str(dest), "rev-parse", "HEAD"])
    if checkout.returncode != 0 or head.returncode != 0:
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "community.checkout_failed",
            f"Could not check {url} out: {_last_line(checkout.stderr + head.stderr)}",
        )
    return head.stdout.strip(), signed_in_with


# -------------------------------------------------------------------- declaration


def _refuse(reason: str) -> CwcliError:
    return CwcliError(
        ErrorKind.PRECONDITION,
        "community.no_declaration",
        f"This repository is not a cwcli community integration: {reason}. Nothing was "
        "installed.",
    )


def read_declaration(src: Path) -> Declaration:
    """The integration ``src``'s ``pyproject.toml`` declares, read without running it."""
    try:
        data = toml.loads((src / "pyproject.toml").read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise _refuse("it has no pyproject.toml at its root") from None
    except (OSError, ValueError, UnicodeDecodeError) as e:
        raise _refuse(f"its pyproject.toml cannot be read ({e})") from None
    project: dict[str, Any] = data["project"] if isinstance(data.get("project"), dict) else {}
    groups = project.get("entry-points")
    eps = groups.get(ENTRY_POINT_GROUP) if isinstance(groups, dict) else None
    if not isinstance(eps, dict) or not eps:
        raise _refuse(f'pyproject.toml has no [project.entry-points."{ENTRY_POINT_GROUP}"]')
    if len(eps) != 1:
        raise _refuse(f"it registers {len(eps)} integrations; a repository provides one")
    (integration_id,) = eps
    if not _ID.match(integration_id):
        raise _refuse(
            f"its id {integration_id!r} is not 2-40 lower-case letters, digits, - or _, "
            "starting with a letter"
        )
    decl = ((data.get("tool") or {}).get("cwcli") or {}).get("integration")
    if not isinstance(decl, dict):
        raise _refuse("pyproject.toml has no [tool.cwcli.integration] table")
    text = {}
    for key in ("name", "publisher", "description"):
        value = decl.get(key)
        if not isinstance(value, str) or not value.strip():
            raise _refuse(f"[tool.cwcli.integration] needs a {key}")
        text[key] = value.strip()
    provides = decl.get("provides")
    if not isinstance(provides, list) or not all(isinstance(p, str) for p in provides):
        raise _refuse("[tool.cwcli.integration] provides must be a list of hook points")
    if not provides:
        raise _refuse(f"it provides no hook point (any of: {', '.join(HOOK_POINTS)})")
    unknown = sorted(set(provides) - set(HOOK_POINTS))
    if unknown:
        raise _refuse(
            f"it provides unknown hook points {', '.join(unknown)} "
            f"(known: {', '.join(HOOK_POINTS)})"
        )
    name = project.get("name")
    if not isinstance(name, str) or not name.strip():
        raise _refuse("[project] has no name")
    return Declaration(
        id=integration_id,
        provides=tuple(dict.fromkeys(provides)),
        project=name.strip(),
        **text,
    )


# ----------------------------------------------------------------------- the store


def records() -> dict[str, dict[str, Any]]:
    """Every installed community integration's record (nothing is imported)."""
    with core_integrations._locked():
        return dict(core_integrations._load().get("community") or {})


def record(integration_id: str) -> dict[str, Any]:
    found = records().get(integration_id)
    if found is None:
        raise CwcliError(
            ErrorKind.NOT_FOUND,
            "community.unknown",
            f"No community integration '{integration_id}' is installed.",
        )
    return found


def _audit(action: str, rec: dict[str, Any], by: str, *, old: str | None = None) -> None:
    line = (
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {action} id={rec['id']} "
        f"by={by or '-'} url={rec['url']} ref={rec.get('ref') or '-'} commit={rec['commit']}"
        + (f" old_commit={old}" if old else "")
        + "\n"
    )
    path = audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, shared_home.file_mode(0o600))
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(line)
    shared_home.apply_group(path)


def recent_audit(lines: int = 20) -> list[str]:
    try:
        with open(audit_path(), encoding="utf-8") as f:
            return [ln.rstrip("\n") for ln in f.readlines()[-lines:]]
    except OSError:
        return []


# ---------------------------------------------------------------------- installing


def _require_installable() -> None:
    from . import version

    if version._is_frozen():
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "community.standalone",
            "The standalone cwcli build cannot install Python packages; install cwcli "
            "with uv or pip to add community integrations.",
        )


def _make_root() -> None:
    root().mkdir(parents=True, exist_ok=True)
    for path in (root().parent, root()):
        shared_home.secure_dir_shared_only(path)


def _pip() -> list[str]:
    """pip from a private venv beside the packages, made on first use."""
    venv = root() / ".pip"
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        _make_root()
        made = subprocess.run(
            [sys.executable, "-m", "venv", str(venv)], capture_output=True, text=True
        )
        if made.returncode != 0:
            shutil.rmtree(venv, ignore_errors=True)
            raise CwcliError(
                ErrorKind.PRECONDITION,
                "community.no_pip",
                f"Could not make the venv that installs integrations: {_last_line(made.stderr)}",
            )
    return [str(python), "-m", "pip"]


def _install_package(src: Path, decl: Declaration, commit: str) -> str:
    """Build and install ``src`` into a fresh directory; return its name under root()."""
    name = f"{decl.id}-{commit[:12]}-{secrets.token_hex(3)}"
    target = root() / name
    _make_root()
    cmd = _pip() + [
        "install",
        "--quiet",
        "--disable-pip-version-check",
        "--no-input",
        "--no-warn-script-location",
        "--target",
        str(target),
        str(src),
    ]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=_PIP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        shutil.rmtree(target, ignore_errors=True)
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "community.install_failed",
            f"Installing {decl.project} took longer than {_PIP_TIMEOUT_S}s.",
        ) from None
    if done.returncode != 0:
        shutil.rmtree(target, ignore_errors=True)
        tail = "\n".join((done.stderr or done.stdout).strip().splitlines()[-12:])
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "community.install_failed",
            f"Installing {decl.project} failed:\n{tail}",
        )
    # Loaded in a separate process, so a package that breaks on import never takes
    # this one with it; run from root() so nothing in the caller's directory shadows
    # cwcli on ``-m``'s path.
    try:
        verdict = subprocess.run(
            [
                sys.executable,
                "-m",
                "caffeinated_whale_cli.core.community",
                "verify",
                json.dumps({"dir": str(target), **dataclasses.asdict(decl)}),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=root(),
        )
        problem = None if verdict.returncode == 0 else _last_line(verdict.stderr or verdict.stdout)
    except subprocess.TimeoutExpired:
        problem = "importing it took longer than 120s"
    if problem is not None:
        shutil.rmtree(target, ignore_errors=True)
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "community.invalid_package",
            f"{decl.project} installed but does not provide the integration it declares: "
            f"{problem}. It was removed again.",
        )
    return name


def preview(url: str, ref: str | None = None) -> Preview:
    """Fetch ``url`` at ``ref`` and read its declaration; installs and runs nothing."""
    url, ref = check_url(url), _check_ref(ref)
    with tempfile.TemporaryDirectory(prefix="cwcli-community-") as tmp:
        commit, signed_in_with = _fetch(url, ref, Path(tmp) / "src")
        decl = read_declaration(Path(tmp) / "src")
    _refuse_taken(decl.id, records())
    return Preview(url, ref, commit, signed_in_with, decl)


def _refuse_taken(integration_id: str, installed: dict) -> None:
    if integration_id in _RESERVED_IDS:
        raise CwcliError(
            ErrorKind.CONFLICT,
            "community.id_taken",
            f"'{integration_id}' is reserved by cwcli; a community integration cannot use it.",
        )
    if integration_id in core_integrations.official():
        raise CwcliError(
            ErrorKind.CONFLICT,
            "community.id_taken",
            f"'{integration_id}' is an official integration's id; a community one cannot use it.",
        )
    if integration_id in installed:
        raise CwcliError(
            ErrorKind.CONFLICT,
            "community.installed",
            f"'{integration_id}' is already installed; check it for an update instead.",
        )


def _fetch_reviewed(url: str, ref: str | None, commit: str, tmp: Path) -> tuple[str | None, Path]:
    src = tmp / "src"
    got, signed_in_with = _fetch(url, ref, src)
    if got != commit:
        raise CwcliError(
            ErrorKind.CONFLICT,
            "community.moved",
            f"{ref or 'The default branch'} now points at {got[:12]}, not the {commit[:12]} "
            "you reviewed. Review it again.",
        )
    return signed_in_with, src


def install(url: str, ref: str | None = None, *, commit: str, by: str = "") -> dict[str, Any]:
    """Install the commit the Admin reviewed in ``preview``; it starts Off."""
    _require_installable()
    url, ref = check_url(url), _check_ref(ref)
    if not commit:
        raise CwcliError(
            ErrorKind.USAGE, "community.commit", "Name the commit you reviewed (preview first)."
        )
    with tempfile.TemporaryDirectory(prefix="cwcli-community-") as tmp:
        signed_in_with, src = _fetch_reviewed(url, ref, commit, Path(tmp))
        decl = read_declaration(src)
        _refuse_taken(decl.id, records())
        directory = _install_package(src, decl, commit)
    rec = {
        **dataclasses.asdict(decl),
        "provides": list(decl.provides),
        "url": url,
        "ref": ref,
        "commit": commit,
        "signed_in_with": signed_in_with,
        "dir": directory,
        "installed_at": time.time(),
        "installed_by": by,
        "updated_at": None,
        "updated_by": None,
    }
    try:
        with core_integrations._locked():
            data = core_integrations._load()
            _refuse_taken(decl.id, data.get("community") or {})
            data.setdefault("community", {})[decl.id] = rec
            data["integrations"].pop(decl.id, None)
            core_integrations._save(data)
    except CwcliError:
        shutil.rmtree(root() / directory, ignore_errors=True)
        raise
    _audit("add", rec, by)
    return rec


def check_update(integration_id: str) -> dict[str, Any]:
    """The commit the integration's ref points at now, against the installed one."""
    rec = record(integration_id)
    with tempfile.TemporaryDirectory(prefix="cwcli-community-") as tmp:
        latest, signed_in_with = _fetch(rec["url"], rec.get("ref"), Path(tmp) / "src")
        decl = read_declaration(Path(tmp) / "src") if latest != rec["commit"] else None
    if decl is not None and decl.id != integration_id:
        raise CwcliError(
            ErrorKind.CONFLICT,
            "community.id_changed",
            f"The repository now declares '{decl.id}', not '{integration_id}'. Remove this "
            "integration and add the repository again.",
        )
    return {
        "id": integration_id,
        "url": rec["url"],
        "ref": rec.get("ref"),
        "current": rec["commit"],
        "latest": latest,
        "update_available": latest != rec["commit"],
        "signed_in_with": signed_in_with,
        "declaration": dataclasses.asdict(decl) if decl is not None else None,
    }


def update(integration_id: str, *, commit: str, by: str = "") -> dict[str, Any]:
    """Install the reviewed newer commit in place of the current one. The integration
    is turned off (settings and secrets kept): the new code's test runs when it is
    turned on again."""
    _require_installable()
    rec = record(integration_id)
    if commit == rec["commit"]:
        raise CwcliError(
            ErrorKind.CONFLICT,
            "community.current",
            f"'{integration_id}' is already at {commit[:12]}.",
        )
    with tempfile.TemporaryDirectory(prefix="cwcli-community-") as tmp:
        signed_in_with, src = _fetch_reviewed(rec["url"], rec.get("ref"), commit, Path(tmp))
        decl = read_declaration(src)
        if decl.id != integration_id:
            raise CwcliError(
                ErrorKind.CONFLICT,
                "community.id_changed",
                f"The repository now declares '{decl.id}', not '{integration_id}'.",
            )
        directory = _install_package(src, decl, commit)
    old = rec["commit"]
    with core_integrations._locked():
        data = core_integrations._load()
        current = (data.get("community") or {}).get(integration_id)
        if current is None or current["commit"] != old:
            shutil.rmtree(root() / directory, ignore_errors=True)
            raise CwcliError(
                ErrorKind.CONFLICT,
                "community.changed",
                f"'{integration_id}' changed while updating; check for an update again.",
            )
        new = {
            **current,
            **dataclasses.asdict(decl),
            "provides": list(decl.provides),
            "commit": commit,
            "signed_in_with": signed_in_with,
            "dir": directory,
            "updated_at": time.time(),
            "updated_by": by,
        }
        data["community"][integration_id] = new
        settings = data["integrations"].get(integration_id)
        was_on = bool(settings and settings.get("enabled"))
        if settings is not None:
            settings.update(enabled=False, last_error=None, updated_at=time.time(), updated_by=by)
        core_integrations._save(data)
    if was_on and "sign_in" in current.get("provides", ()):
        accounts.close_directory_sessions()
    shutil.rmtree(root() / current["dir"], ignore_errors=True)
    _audit("update", new, by, old=old)
    return {"id": integration_id, "old": old, "new": commit, "was_on": was_on, "record": new}


def remove(integration_id: str, *, by: str = "") -> dict[str, Any]:
    """Delete the package, its settings and its secrets."""
    with core_integrations._locked():
        data = core_integrations._load()
        rec: dict[str, Any] | None = (data.get("community") or {}).pop(integration_id, None)
        if rec is None:
            raise CwcliError(
                ErrorKind.NOT_FOUND,
                "community.unknown",
                f"No community integration '{integration_id}' is installed.",
            )
        settings = data["integrations"].pop(integration_id, None)
        core_integrations._save(data)
    if settings and settings.get("enabled") and "sign_in" in rec.get("provides", ()):
        accounts.close_directory_sessions()
    _forget(integration_id)
    shutil.rmtree(root() / rec["dir"], ignore_errors=True)
    with core_integrations._locked():
        if not core_integrations._load().get("community"):
            # The last one: the pip venv and the empty directories go too; a package
            # another process is installing keeps its directory.
            shutil.rmtree(root() / ".pip", ignore_errors=True)
            for path in (root(), root().parent):
                with contextlib.suppress(OSError):
                    path.rmdir()
    _audit("remove", rec, by)
    return rec


# ------------------------------------------------------------------------ loading


def _import(site: str, decl: dict[str, Any]) -> core_integrations.Integration:
    """Load the integration ``decl`` names from the package installed in ``site``."""
    if site not in sys.path:
        # Appended, so cwcli's own packages always win over a copy the integration
        # brought along.
        sys.path.append(site)
    importlib.invalidate_caches()
    wanted = _normalize_project(decl["project"])
    dists = [
        d
        for d in importlib.metadata.distributions(path=[site])
        if _normalize_project(d.metadata["Name"] or "") == wanted
    ]
    if not dists:
        raise ValueError(f"no installed distribution named {decl['project']}")
    eps = [
        ep
        for ep in dists[0].entry_points
        if ep.group == ENTRY_POINT_GROUP and ep.name == decl["id"]
    ]
    if not eps:
        raise ValueError(f"{decl['project']} registers no '{decl['id']}' integration")
    obj = eps[0].load()
    if not isinstance(obj, core_integrations.Integration):
        raise ValueError(f"{eps[0].value} is not a cwcli Integration")
    if obj.id != decl["id"]:
        raise ValueError(f"it calls itself '{obj.id}', not '{decl['id']}'")
    if set(obj.provides) != set(decl["provides"]):
        raise ValueError(
            f"it provides {sorted(obj.provides)}, not the declared {sorted(decl['provides'])}"
        )
    missing = [hook for hook in obj.provides if not callable(obj.hooks.get(hook))]
    if missing:
        raise ValueError(f"it has no function for {', '.join(missing)} in its hooks")
    if not isinstance(obj.fields, (tuple, list)) or not all(
        isinstance(f, core_integrations.Field) for f in obj.fields
    ):
        raise ValueError("its fields are not a list of cwcli Field")
    if not isinstance(obj.actions, Mapping) or not all(
        isinstance(name, str) and callable(fn) for name, fn in obj.actions.items()
    ):
        raise ValueError("its actions are not a mapping of names to functions")
    return _guarded(
        dataclasses.replace(
            obj,
            publisher=core_integrations.COMMUNITY,
            name=decl["name"],
            description=decl["description"],
        )
    )


def _normalize_project(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _guarded(integ: core_integrations.Integration) -> core_integrations.Integration:
    """The integration with its callbacks kept from raising into cwcli: a failing test
    is a failed check, bad input a usage error, a failing hook a no-op."""

    def test(settings, secret_values):
        try:
            return integ.test(settings, secret_values)
        except Exception as e:  # noqa: BLE001 - community code
            return core_integrations.Check(False, f"{integ.name} failed its test: {e}")

    def normalize(settings):
        try:
            return integ.normalize(settings)
        except CwcliError:
            raise
        except Exception as e:  # noqa: BLE001 - community code
            raise CwcliError(ErrorKind.USAGE, "community.settings", f"{integ.name}: {e}") from e

    def on_change():
        if "sign_in" in integ.provides:
            # Like LDAP: turning sign-in off or changing it signs its users out.
            accounts.close_directory_sessions()
        try:
            if integ.on_change is not None:
                integ.on_change()
        except Exception:  # noqa: BLE001 - community code
            pass

    def notices():
        try:
            return [str(n) for n in integ.notices()] if integ.notices is not None else []
        except Exception as e:  # noqa: BLE001 - community code
            return [f"{integ.name} could not list its notices: {e}"]

    def action(fn):
        def run(settings, secret_values, args):
            try:
                return fn(settings, secret_values, args)
            except CwcliError:
                raise
            except Exception as e:  # noqa: BLE001 - community code
                raise CwcliError(ErrorKind.USAGE, "community.action", f"{integ.name}: {e}") from e

        return run

    return dataclasses.replace(
        integ,
        fields=tuple(integ.fields),
        test=test,
        normalize=normalize,
        on_change=on_change,
        notices=notices,
        actions={name: action(fn) for name, fn in integ.actions.items()},
    )


def _broken(rec: dict[str, Any], error: str) -> core_integrations.Integration:
    """A card for an installed integration whose code does not load: it cannot be
    turned on, and it can still be updated or removed."""
    provides = tuple(rec.get("provides") or ())

    def on_change():
        if "sign_in" in provides:
            accounts.close_directory_sessions()

    return core_integrations.Integration(
        id=rec["id"],
        name=rec["name"],
        publisher=core_integrations.COMMUNITY,
        description=rec["description"],
        provides=provides,
        fields=(),
        normalize=lambda settings: {},
        test=lambda settings, secret_values: core_integrations.Check(
            False, f"The package does not load: {error}"
        ),
        on_change=on_change,
    )


def _unload(site: str) -> None:
    # ponytail: drops the modules whose files live in the old package directory; a
    # module that copied state into cwcli's own modules keeps it until a restart.
    while site in sys.path:
        sys.path.remove(site)
    prefix = site.rstrip(os.sep) + os.sep
    for name, mod in list(sys.modules.items()):
        origin = getattr(mod, "__file__", None) or ""
        if origin.startswith(prefix):
            del sys.modules[name]
    importlib.invalidate_caches()


def _forget(integration_id: str) -> None:
    with _lock:
        cached = _loaded.pop(integration_id, None)
        if cached is not None:
            _unload(cached[2])


def load_all() -> dict[str, core_integrations.Integration]:
    """Every installed community integration, loaded (or a card naming why it did not).
    A changed commit reloads it; a removed one is unloaded."""
    recs = records()
    with _lock:
        for gone in set(_loaded) - set(recs):
            _forget(gone)
        return {
            integration_id: _load_one(rec)
            for integration_id, rec in sorted(recs.items(), key=lambda kv: kv[1]["name"].lower())
        }


def load(integration_id: str) -> core_integrations.Integration | None:
    """One installed community integration, loaded (or its error card); None when it
    is not installed. No other community package is imported."""
    rec = records().get(integration_id)
    with _lock:
        if rec is None:
            _forget(integration_id)
            return None
        return _load_one(rec)


def _load_one(rec: dict[str, Any]) -> core_integrations.Integration:
    integration_id = rec["id"]
    cached = _loaded.get(integration_id)
    if cached is not None and cached[0] == rec["commit"]:
        return cached[1]
    _forget(integration_id)
    site = str(root() / rec["dir"])
    error = None
    try:
        integ = _import(site, rec)
    except Exception as e:  # noqa: BLE001 - community code
        _unload(site)
        error = str(e) or type(e).__name__
        integ = _broken(rec, error)
    _loaded[integration_id] = (rec["commit"], integ, site, error)
    return integ


def load_error(integration_id: str) -> str | None:
    """Why an installed integration did not load, when it did not."""
    with _lock:
        cached = _loaded.get(integration_id)
    return cached[3] if cached is not None else None


# --------------------------------------------------------------- the module entry


def _credential(action: str) -> int:
    """git's credential helper during a fetch: the host's gh/glab sign-in, through
    the same ``host_credential`` the container bridge uses. Only ``get`` answers."""
    if action != "get":
        return 0
    request = sys.stdin.buffer.read()
    answer = credbridge.host_credential(request)
    if not answer:
        host = next(
            (
                ln[5:]
                for ln in request.decode("utf-8", "replace").splitlines()
                if ln.startswith("host=")
            ),
            "",
        )
        sys.stderr.write(f"{_SIGN_IN_MISSING}{host}\n")
    sys.stdout.buffer.write(answer)
    return 0


def _verify(payload: str) -> int:
    decl = json.loads(payload)
    try:
        _import(decl.pop("dir"), decl)
    except Exception as e:  # noqa: BLE001 - community code
        sys.stderr.write(f"{e or type(e).__name__}\n")
        return 1
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "credential":
        raise SystemExit(_credential(sys.argv[2]))
    if len(sys.argv) == 3 and sys.argv[1] == "verify":
        raise SystemExit(_verify(sys.argv[2]))
    raise SystemExit(2)
