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

# POSIX-sh function (the container ``sh`` is dash) that classifies EVERY entry of
# ``"$1/sites"`` in one pass, printing ``VERDICT<TAB>name`` per entry. Three
# positive verdicts: SITE (a dir with site_config.json), NOTASITE (not a dir, or a
# readable dir with no config), AMBIGUOUS (a dir we cannot read, so cannot confirm).
# The subshell body keeps the ``cd`` from leaking into a caller's loop, and
# ``entries=$(ls -1) || exit 3`` keeps "could not list" (non-zero) distinct from
# "no sites" (empty output). Paths only ever arrive as positional arguments, never
# interpolated into the script text, so no directory name can break the quoting.
# Shared with ``core.inspect``'s one-exec partial refresh so the two cannot drift.
SITE_VERDICTS_SH = (
    'site_verdicts() ( cd "$1/sites" 2>/dev/null || exit 3; '
    "entries=$(ls -1 2>/dev/null) || exit 3; "
    'printf "%s\\n" "$entries" | while IFS= read -r e; do [ -n "$e" ] || continue; '
    'if [ ! -d "$e" ]; then v=NOTASITE; elif [ -f "$e/site_config.json" ]; then v=SITE; '
    'elif [ -r "$e" ]; then v=NOTASITE; else v=AMBIGUOUS; fi; '
    'printf "%s\\t%s\\n" "$v" "$e"; done )'
)

_LIST_SITES_SCRIPT = SITE_VERDICTS_SH + '; site_verdicts "$1"'


def site_from_verdict(line: str) -> str | None:
    """The site name one ``site_verdicts`` output line keeps, or None to exclude it.

    Excludes ONLY a positive NOTASITE. SITE, AMBIGUOUS, or an unexpected token all
    fail closed to a site, and a line without the tab separator is kept whole.
    """
    if not line:
        return None
    verdict, sep, name = line.partition("\t")
    if not sep:
        return line
    return None if verdict == "NOTASITE" else name


def list_sites(container, bench_path: str, verbose: bool = False) -> list[str] | None:
    """Return the real Frappe sites under ``{bench_path}/sites``.

    A real site is a directory containing a ``site_config.json``; see the module
    docstring for the fail-safe classification rules. One exec for the whole
    directory, whatever its entry count.

    Returns the list of site names (possibly empty), or ``None`` if the sites
    directory itself could not be listed (a Docker/permission error), so callers
    can tell "no sites" apart from "could not look".
    """
    exit_code, output = container.exec_run(["sh", "-c", _LIST_SITES_SCRIPT, "sh", bench_path])
    if exit_code != 0:
        return None
    try:
        listing = output.decode("utf-8")
    except (UnicodeDecodeError, AttributeError):
        # A non-UTF-8 listing must fail safe (like read_current_site), never raise.
        return None
    return [site for line in listing.split("\n") if (site := site_from_verdict(line))]


def read_current_site(container, bench_path: str, verbose: bool = False) -> str | None:
    """Return the default site recorded in ``{bench_path}/sites/currentsite.txt``.

    ``currentsite.txt`` holds exactly the default site name (written by
    ``bench use``). It is a pointer to the default site, not a site itself, so it
    is the fallback source for the default site when ``common_site_config.json``
    has no ``default_site`` key.

    Reads FAIL SAFE: a missing/unreadable/empty file yields ``None`` (never an
    error), and the content is stripped of surrounding whitespace/newlines.
    """
    # Quoted: docker-py shlex-splits a string command, so an unquoted path with a
    # space would name two files.
    path = shlex.quote(f"{bench_path}/sites/currentsite.txt")
    exit_code, output = container.exec_run(f"cat {path}")
    if exit_code != 0:
        return None
    try:
        name = output.decode("utf-8").strip()
    except (UnicodeDecodeError, AttributeError):
        return None
    return name or None
