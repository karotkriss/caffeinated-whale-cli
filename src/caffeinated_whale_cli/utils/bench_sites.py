"""Canonical Frappe site detection inside a bench container.

A bench ``sites/`` directory holds far more than sites: ``apps.txt``,
``apps.json``, ``assets``, ``common_site_config.json``, ``currentsite.txt``
(written by ``bench use``), lock files, and so on. A real Frappe site is a
*directory* that contains a ``site_config.json``. Detecting sites by that shape -
rather than denylisting known non-site names - is the only correct approach: a
denylist can never be complete, so any unlisted stray entry (``currentsite.txt``
is the classic one) gets mistaken for a site.

This module is the single home for that logic so ``rm`` (which must back up every
real site before deletion) and ``inspect`` (which must not display a stray file as
a site) share one implementation instead of drifting apart.

``list_sites`` is deliberately FAIL-SAFE: an entry is excluded only when we can
positively confirm it is not a site (a non-directory, or a readable directory with
no ``site_config.json``). Anything ambiguous - the probe erroring, an unreadable
directory, or unexpected output - is treated as a real site. For ``rm`` this keeps
data safe under ambiguity (worst case it blocks a delete, never loses data); for
``inspect`` it errs toward showing a possible site rather than hiding one.

``read_current_site`` reads ``sites/currentsite.txt``, the pointer ``bench use``
writes to record the default site. It is a *pointer*, not a site, so it is never
returned by ``list_sites``; it is the second place (besides
``common_site_config.json``'s ``default_site``) a bench records which site is the
default.
"""

from __future__ import annotations

import shlex


def list_sites(container, bench_path: str, verbose: bool = False) -> list[str] | None:
    """Return the real Frappe sites under ``{bench_path}/sites``.

    A real site is a directory containing a ``site_config.json``; see the module
    docstring for the fail-safe classification rules.

    Returns the list of site names (possibly empty), or ``None`` if the sites
    directory itself could not be listed (a Docker/permission error), so callers
    can tell "no sites" apart from "could not look".
    """
    exit_code, output = container.exec_run(f"ls -1 {bench_path}/sites")
    if exit_code != 0:
        return None
    try:
        listing = output.decode("utf-8")
    except (UnicodeDecodeError, AttributeError):
        # A non-UTF-8 listing must fail safe (like read_current_site), never raise.
        return None

    # One shell probe with three positive verdicts: SITE (has site_config.json),
    # NOTASITE (not a dir, or a readable dir with no config), AMBIGUOUS (dir exists
    # but is unreadable so we cannot confirm). The entry path is passed as the
    # positional argument ``$1`` (referenced only via ``$1`` in the script, never
    # interpolated into it) and shlex-quoted, so a directory name containing quotes
    # or shell metacharacters can neither break the quoting (defeating the fail-safe
    # promise by raising) nor inject. docker-py shlex-splits the string command, so
    # ``sh -c <script> sh <entry_dir>`` runs the script with ``$1 = entry_dir``.
    probe_script = (
        'if [ ! -d "$1" ]; then echo NOTASITE; '
        'elif [ -f "$1/site_config.json" ]; then echo SITE; '
        'elif [ -r "$1" ]; then echo NOTASITE; '
        "else echo AMBIGUOUS; fi"
    )
    quoted_script = shlex.quote(probe_script)

    sites: list[str] = []
    for entry in listing.split("\n"):
        entry = entry.strip()
        if not entry:
            continue
        entry_dir = f"{bench_path}/sites/{entry}"
        probe = f"sh -c {quoted_script} sh {shlex.quote(entry_dir)}"
        probe_code, probe_out = container.exec_run(probe)
        try:
            verdict = probe_out.decode("utf-8").strip() if probe_code == 0 else ""
        except (UnicodeDecodeError, AttributeError):
            # Can't read the verdict -> not a positive NOTASITE -> fail safe (site).
            verdict = ""
        # Exclude ONLY on a positive NOTASITE. SITE, AMBIGUOUS, an unexpected
        # token, empty output, or a non-zero probe exit all fail closed -> site.
        if verdict != "NOTASITE":
            sites.append(entry)
    return sites


def read_current_site(container, bench_path: str, verbose: bool = False) -> str | None:
    """Return the default site recorded in ``{bench_path}/sites/currentsite.txt``.

    ``currentsite.txt`` holds exactly the default site name (written by
    ``bench use``). It is a pointer to the default site, not a site itself, so it
    is the fallback source for the default site when ``common_site_config.json``
    has no ``default_site`` key.

    Reads FAIL SAFE: a missing/unreadable/empty file yields ``None`` (never an
    error), and the content is stripped of surrounding whitespace/newlines.
    """
    exit_code, output = container.exec_run(f"cat {bench_path}/sites/currentsite.txt")
    if exit_code != 0:
        return None
    try:
        name = output.decode("utf-8").strip()
    except (UnicodeDecodeError, AttributeError):
        return None
    return name or None
