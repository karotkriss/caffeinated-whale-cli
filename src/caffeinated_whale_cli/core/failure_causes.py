"""Name the real cause of a failed Frappe/bench/git command in plain words.

A failed exec used to surface only as "Command failed with exit code 1: <command>",
leaving the reader to dig the reason out of a long traceback (a MariaDB 1045
buried under a `bench new-site` stack, an expired git token under `get-app`).
:func:`explain` scans a command's own output for the common failure shapes and
returns one plain sentence for the top of a job page, an ``axi`` error, or a CLI
message. It never replaces the raw output; it only says what that output means.

The table is ordered most-specific first and the first match wins, so a 1045 is
reported as a rejected password even when bench's traceback also mentions a
generic ``OperationalError``. ``None`` means no known shape matched; callers keep
their existing message then, never a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class FailureCause:
    """A recognized failure: a stable ``code``, a short ``title``, one plain ``text``."""

    code: str
    title: str
    text: str


# (code, title, pattern, text). A text naming {0} gets the pattern's first
# matched group, so every alternative of its pattern must capture one.
_SHAPES: list[tuple[str, str, re.Pattern[str], str]] = [
    (
        "db.access_denied",
        "MariaDB rejected the root password",
        re.compile(r"\(1045, ['\"]Access denied for user '([^']*)'", re.IGNORECASE),
        "MariaDB rejected the password for database user '{0}' (error 1045, access denied). "
        "The database root password given does not match this instance's MariaDB root password.",
    ),
    (
        "db.unreachable",
        "MariaDB is not reachable",
        re.compile(r"\((?:2002|2003|2005|2013), ['\"](?:Can't connect|Unknown MySQL server|Lost)"),
        "The database server could not be reached (MariaDB is not running or not ready yet).",
    ),
    (
        "db.exists",
        "The database already exists",
        re.compile(r"\(1007, ['\"]Can't create database '([^']*)'"),
        "A database named '{0}' already exists in MariaDB, usually left behind by an earlier "
        "site with the same name.",
    ),
    (
        "site.exists",
        "The site already exists",
        re.compile(r"Site (\S+) already exists"),
        "A site named {0} already exists on this bench.",
    ),
    (
        "disk.full",
        "The disk is full",
        re.compile(r"ENOSPC|No space left on device", re.IGNORECASE),
        "The disk ran out of space. Free up disk space and try again.",
    ),
    (
        "memory.exhausted",
        "Out of memory",
        re.compile(r"JavaScript heap out of memory|MemoryError|Cannot allocate memory"),
        "The command ran out of memory. Close other instances or give Docker more memory.",
    ),
    (
        "git.auth_failed",
        "Git could not authenticate",
        re.compile(
            r"Authentication failed for '([^']*)'|could not read Username for '([^']*)'",
            re.IGNORECASE,
        ),
        "Git could not authenticate to {0}. Log in with gh or glab on the host, or check the "
        "repository URL.",
    ),
    (
        "git.repo_not_found",
        "Repository not found",
        re.compile(r"fatal: repository '([^']*)' not found"),
        "The git repository {0} was not found, or this account cannot see it.",
    ),
    (
        "git.repo_not_found",
        "Repository not found",
        re.compile(r"ERROR: Repository not found"),
        "The git repository was not found, or this account cannot see it.",
    ),
    (
        "git.ref_not_found",
        "Branch or tag not found",
        re.compile(
            r"Remote branch (\S+) not found|couldn't find remote ref (\S+)|"
            r"pathspec '([^']*)' did not match"
        ),
        "The branch, tag, or commit {0} does not exist in the repository.",
    ),
    (
        "network.dns",
        "No network connection",
        re.compile(r"Could not resolve host:? '?([\w.-]+)", re.IGNORECASE),
        "The host name {0} could not be resolved. Check the network connection.",
    ),
    (
        "network.dns",
        "No network connection",
        re.compile(r"Temporary failure in name resolution", re.IGNORECASE),
        "A host name could not be resolved. Check the network connection.",
    ),
    (
        "python.module_missing",
        "A Python module is missing",
        re.compile(r"ModuleNotFoundError: No module named '([^']*)'"),
        "Python could not import the module '{0}'. The app may be missing from this bench, "
        "or its dependencies were not installed.",
    ),
    (
        "redis.unreachable",
        "Redis is not reachable",
        re.compile(r"Error \d+ connecting to (redis[\w.:-]*)", re.IGNORECASE),
        "Redis ({0}) could not be reached. Start the instance and try again.",
    ),
    (
        "port.in_use",
        "A port is already in use",
        re.compile(r"[Aa]ddress already in use|port is already allocated"),
        "A port this command needs is already in use by another process.",
    ),
]


def explain(*texts: str | None) -> FailureCause | None:
    """Return the cause of the first known failure shape in the first text that has one.

    ``texts`` go most specific first, so a later, wider text never outranks them.
    """
    for text in texts:
        if not text:
            continue
        for code, title, pattern, template in _SHAPES:
            match = pattern.search(text)
            if match:
                groups = [group for group in match.groups() if group]
                subject = groups[0] if groups else ""
                return FailureCause(code=code, title=title, text=template.format(subject))
    return None
