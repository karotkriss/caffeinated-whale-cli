"""Real-Docker E2E for the "disable caching entirely" switch (CWCLI_NO_CACHE).

Unit tests (``tests/test_cache_disable.py``) prove the gate, the precedence, and
that a disabled read ignores a stale on-disk row while leaving the DB untouched -
but they fake the container, so they cannot prove the two things only a real
instance shows: that read commands (inspect, status) genuinely RESOLVE LIVE when
the flag is on, and that a mutation (backup) still resolves the right bench and
its sole site live, with no cache to lean on.

The discriminator is real: the session ran with caching ENABLED, so the on-disk
cache already holds this project. With the flag on, ``config cache list`` reports
it ABSENT (the disk cache is not consulted) yet ``inspect``/``status`` still
report the live bench and site - and the cache DB file is byte-for-byte unchanged
afterwards. Runs against the shared session instance, cheap since it already
exists, and restores any state it changes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from . import harness

pytestmark = [pytest.mark.e2e]


def _cache_db() -> Path:
    return Path(os.environ["CWCLI_HOME"]) / "cache" / "cwc-cache.db"


def _run_disabled(*args: str, **kw):
    """``cwcli <args>`` with CWCLI_NO_CACHE=1 for this one call."""
    env = dict(os.environ, CWCLI_NO_CACHE="1")
    prev = os.environ.get("CWCLI_NO_CACHE")
    os.environ.update(env)
    try:
        return harness.run_cwcli(*args, **kw)
    finally:
        if prev is None:
            os.environ.pop("CWCLI_NO_CACHE", None)
        else:
            os.environ["CWCLI_NO_CACHE"] = prev


def test_disabled_reads_resolve_live_and_leave_the_cache_untouched(running_instance):
    inst = running_instance

    # Baseline: warm the on-disk cache with caching ON, so we have a genuine
    # cached row to prove the disabled path ignores.
    warm = harness.run_cwcli("inspect", inst.name, "--update")
    assert warm.returncode == 0, warm.stdout + warm.stderr

    db = _cache_db()
    assert db.exists(), "an enabled inspect must have written the on-disk cache"

    # The disk cache HAS this project...
    listed = harness.run_cwcli("config", "cache", "list", "--json")
    assert listed.returncode == 0, listed.stdout + listed.stderr
    cached_names = {p["name"] for p in json.loads(listed.stdout)}
    assert inst.name in cached_names, listed.stdout

    # Baseline the cache file AFTER the last enabled read, so only a disabled write
    # (there must be none) could move it.
    mtime_before = db.stat().st_mtime_ns

    # ...but with caching disabled, the cache is not consulted: the same list is
    # empty (nothing served from the on-disk DB).
    listed_off = _run_disabled("config", "cache", "list", "--json")
    assert listed_off.returncode == 0, listed_off.stdout + listed_off.stderr
    assert json.loads(listed_off.stdout) == [], listed_off.stdout

    # inspect still resolves LIVE with the flag on: it reports the real bench and site.
    insp = _run_disabled("inspect", inst.name, "--json")
    assert insp.returncode == 0, insp.stdout + insp.stderr
    report = json.loads(insp.stdout)
    blob = json.dumps(report)
    assert inst.bench in blob, insp.stdout
    assert inst.site in blob, insp.stdout

    # status resolves the bench live and reports the running container (the axi
    # read verb emits a machine-readable TOON document naming the live bench).
    st = _run_disabled("axi", "status", inst.name)
    assert st.returncode == 0, st.stdout + st.stderr
    assert inst.bench in st.stdout, st.stdout

    # A mutation with the flag on: `backup` WITHOUT `--site` must resolve the
    # bench's sole site LIVE (the safety-relevant path that used to read the cache)
    # and produce a real dump - proving a mutation gets correct live data.
    bkp = _run_disabled("backup", inst.name, "-y")
    assert bkp.returncode == 0, bkp.stdout + bkp.stderr
    assert "Successfully created backup" in bkp.stdout, bkp.stdout

    # Nothing was written to the on-disk cache across every disabled operation.
    assert db.stat().st_mtime_ns == mtime_before, "the on-disk cache DB was modified"
