"""The ``apps update <project> frappe`` preflight, on a real bench.

``bench update --reset`` backs up every site BEFORE it resets any app's git
checkout, and writes ``maintenance_mode``/``pause_scheduler`` bench-wide before
that. So an app whose branch its remote does not have fails the update only after
every backup has run, and leaves the bench in maintenance mode. This plants
exactly that - a throwaway app on a local-only branch, with a real local remote -
and proves both halves on a real bench:

- the preflight refuses in seconds, names the app and the cwcli remedy, raises no
  other blocker on a healthy bench, and changes nothing (common_site_config.json
  byte-identical, no new backup file - bench never started);
- ``--no-preflight`` hands the run to bench, which backs the sites up, sets the
  bench-wide flags, and then fails on the same app: the late failure the preflight
  exists to prevent.

It also proves the data-loss guard on a real bench, with a second throwaway app
holding an uncommitted change and an unpushed commit - work ``--reset`` discards:

- without ``--force`` the update refuses, with and without ``--no-preflight``, names
  the app, and changes nothing;
- with ``--force`` bench runs, the report names the app, and the work is gone.

The shared instance is restored in ``finally`` blocks: the config's exact bytes,
``sites/excluded_apps.txt``, the backups bench made, frappe's remote-tracking ref
(bench's version check fetches it), and the planted app and remote. During the
legs that let bench run, every other app is listed in ``sites/excluded_apps.txt``
so bench resets nothing but the planted app: its version check and backups still
run, which is all those legs need. The ``--force`` leg also stops bench right after
that app's reset, before it reinstalls any app into the bench's environment: an
unreadable untracked directory in the app makes bench's own post-reset
``find . -name "*.pyc" -delete`` fail.

Commit to run on CI (real Docker); do NOT run locally.
"""

from __future__ import annotations

import base64
import shlex
import textwrap
import time

import pytest

from . import harness

pytestmark = pytest.mark.e2e

_BENCH = harness.DEFAULT_BENCH_PATH
_APP = "cwe2e_pfapp"
_BRANCH = "cwe2e-local-only"
_REMOTE = f"/workspace/{_APP}-remote.git"
_CONF = f"{_BENCH}/sites/common_site_config.json"
_EXCLUDED = f"{_BENCH}/sites/excluded_apps.txt"
_ABSENT = "@@ABSENT@@"


def _sh(inst, script: str) -> str:
    code, out = harness.exec_in_frappe(inst.name, script)
    assert code == 0, f"{script}\n{out}"
    return out


def _read(inst, path: str) -> str:
    """The file's bytes as base64 (byte-exact restore), or _ABSENT."""
    q = shlex.quote(path)
    return _sh(inst, f"if [ -f {q} ]; then base64 -w0 {q}; else echo {_ABSENT}; fi").strip()


def _restore(inst, path: str, snapshot: str) -> None:
    q = shlex.quote(path)
    if snapshot == _ABSENT:
        _sh(inst, f"rm -f {q}")
    else:
        _sh(inst, f"echo {snapshot} | base64 -d > {q}")


def _backups(inst) -> set[str]:
    out = _sh(inst, f"find {_BENCH}/sites -path '*/private/backups/*' -type f | sort")
    return {line for line in out.splitlines() if line.strip()}


def _preflight_rows(stdout: str) -> list[str]:
    """The rows of the TOON `preflight[N]{...}:` table."""
    rows, inside = [], False
    for line in stdout.splitlines():
        if line.startswith("preflight["):
            inside = True
            continue
        if inside:
            if not line.startswith("  "):
                break
            rows.append(line.strip())
    return rows


@pytest.fixture()
def planted(running_instance):
    """A throwaway frappe-shaped app checked out on a branch its remote lacks."""
    inst = running_instance
    teardown = f"rm -rf {_BENCH}/apps/{_APP} {_REMOTE}"
    _sh(inst, teardown)  # belt-and-suspenders: clear any prior remnant
    _sh(
        inst,
        textwrap.dedent(
            f"""
            set -e
            A={_BENCH}/apps/{_APP}
            mkdir -p "$A/{_APP}"
            cd "$A"
            printf 'app_name = "{_APP}"\\n' > {_APP}/hooks.py
            printf '__version__ = "0.0.1"\\n' > {_APP}/__init__.py
            : > {_APP}/modules.txt
            : > {_APP}/patches.txt
            git init -q -b main
            git add -A
            git -c user.email=e2e@example.com -c user.name=e2e -c commit.gpgsign=false \\
                commit -qm init
            git clone -q --bare "$A" {_REMOTE}
            git remote add upstream {_REMOTE}
            git fetch -q upstream
            git checkout -q -b {_BRANCH}
            """
        ),
    )
    try:
        yield inst
    finally:
        harness.exec_in_frappe(inst.name, teardown)


def test_preflight_refuses_in_seconds_and_changes_nothing(planted):
    inst = planted
    conf_before = _read(inst, _CONF)
    backups_before = _backups(inst)

    started = time.monotonic()
    result = harness.run_cwcli("axi", "apps", "update", inst.name, "frappe", timeout=300)
    elapsed = time.monotonic() - started

    assert result.returncode == 1, result.stdout + result.stderr
    assert elapsed < 60, f"the preflight took {elapsed:.0f}s"
    assert "preflight_blocked: true" in result.stdout
    assert "failed_apps[0]:" in result.stdout  # bench never ran, so nothing failed
    rows = _preflight_rows(result.stdout)
    planted_row = f"blocker,git.branch_not_on_remote,{_APP},null,"
    assert any(row.startswith(planted_row) for row in rows), rows
    assert f"cwcli apps checkout {inst.name} {_APP} <published-branch>" in result.stdout
    # A healthy real bench raises no other blocker: no false positive to bypass.
    blockers = [row for row in rows if row.startswith("blocker,")]
    assert all(row.startswith(planted_row) for row in blockers), blockers

    # Nothing changed: bench never started.
    assert _read(inst, _CONF) == conf_before
    assert _backups(inst) == backups_before


def test_no_preflight_reproduces_bench_failing_late(planted):
    inst = planted
    conf_before = _read(inst, _CONF)
    excluded_before = _read(inst, _EXCLUDED)
    backups_before = _backups(inst)
    frappe_dir = f"{_BENCH}/apps/frappe"
    branch = _sh(inst, f"git -C {frappe_dir} symbolic-ref --short HEAD").strip()
    tracking = f"refs/remotes/upstream/{branch}"
    tracking_before = harness.exec_in_frappe(
        inst.name, f"git -C {frappe_dir} rev-parse --verify -q {tracking}"
    )[1].strip()
    # Keep bench's reset to the planted app: every other app is excluded.
    _sh(
        inst,
        f"ls -1 {_BENCH}/apps | grep -vx {_APP} > {_EXCLUDED}",
    )

    try:
        result = harness.run_cwcli(
            "apps", "update", inst.name, "frappe", "--no-preflight", "-v", timeout=900
        )

        output = result.stdout + result.stderr
        assert result.returncode == 1, output
        assert "Preflight" not in output
        # bench's own failure, on the planted app ...
        assert f"couldn't find remote ref {_BRANCH}" in output
        # ... reached only after it had set the bench-wide flags and backed the
        # sites up: the late failure the preflight refuses up front.
        assert '"maintenance_mode": 1' in base64.b64decode(_read(inst, _CONF)).decode()
        assert _backups(inst) - backups_before
    finally:
        _restore(inst, _CONF, conf_before)
        _restore(inst, _EXCLUDED, excluded_before)
        for path in sorted(_backups(inst) - backups_before):
            harness.exec_in_frappe(inst.name, f"rm -f {shlex.quote(path)}")
        if tracking_before:
            harness.exec_in_frappe(
                inst.name, f"git -C {frappe_dir} update-ref {tracking} {tracking_before}"
            )

    assert _read(inst, _CONF) == conf_before
    assert _backups(inst) == backups_before


_DIRTY = "cwe2e_dlapp"
_DIRTY_REMOTE = f"/workspace/{_DIRTY}-remote.git"
_DIRTY_DIR = f"{_BENCH}/apps/{_DIRTY}"


def _list_field(stdout: str, name: str) -> list[str]:
    """A TOON `name[N]: a,b` list field."""
    for line in stdout.splitlines():
        if line.startswith(f"{name}["):
            values = line.split(":", 1)[1].strip()
            return [v for v in values.split(",") if v]
    raise AssertionError(f"{name} not in:\n{stdout}")


def _work(inst) -> tuple[str, str]:
    """The dirty app's HEAD and its tracked changes."""
    head = _sh(inst, f"git -C {_DIRTY_DIR} rev-parse HEAD").strip()
    status = _sh(inst, f"git -C {_DIRTY_DIR} status --porcelain --untracked-files=no")
    return head, status.strip()


@pytest.fixture()
def dirty(running_instance):
    """A throwaway app holding an uncommitted change and an unpushed commit."""
    inst = running_instance
    # chmod first: the --force leg plants an unreadable directory.
    teardown = f"chmod -R u+rwx {_DIRTY_DIR} 2>/dev/null; rm -rf {_DIRTY_DIR} {_DIRTY_REMOTE}"
    _sh(inst, teardown)
    _sh(
        inst,
        textwrap.dedent(
            f"""
            set -e
            A={_DIRTY_DIR}
            G="git -c user.email=e2e@example.com -c user.name=e2e -c commit.gpgsign=false"
            mkdir -p "$A/{_DIRTY}"
            cd "$A"
            printf 'app_name = "{_DIRTY}"\n' > {_DIRTY}/hooks.py
            printf '__version__ = "0.0.1"\n' > {_DIRTY}/__init__.py
            : > {_DIRTY}/modules.txt
            : > {_DIRTY}/patches.txt
            git init -q -b main
            git add -A
            $G commit -qm init
            git clone -q --bare "$A" {_DIRTY_REMOTE}
            git remote add upstream {_DIRTY_REMOTE}
            git fetch -q upstream
            git branch -q -u upstream/main
            printf 'z = 3\n' > {_DIRTY}/committed.py
            git add {_DIRTY}/committed.py
            $G commit -qm "unpushed work"
            printf 'x = 1\n' >> {_DIRTY}/__init__.py
            """
        ),
    )
    try:
        yield inst
    finally:
        harness.exec_in_frappe(inst.name, teardown)


@pytest.mark.parametrize("flags", [(), ("--no-preflight",)], ids=["preflight", "no-preflight"])
def test_local_work_refuses_without_force_and_changes_nothing(dirty, flags):
    inst = dirty
    conf_before = _read(inst, _CONF)
    backups_before = _backups(inst)
    work_before = _work(inst)

    result = harness.run_cwcli("axi", "apps", "update", inst.name, "frappe", *flags, timeout=300)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "data_loss_refused: true" in result.stdout
    assert _DIRTY in _list_field(result.stdout, "data_loss_apps")
    rows = _preflight_rows(result.stdout)
    for code in ("git.uncommitted", "git.unpushed"):
        assert any(row.startswith(f"data_loss,{code},{_DIRTY},") for row in rows), rows
    # Nothing changed: bench never started, and the work is still there.
    assert _read(inst, _CONF) == conf_before
    assert _backups(inst) == backups_before
    assert _work(inst) == work_before


def test_force_lets_bench_discard_the_work_and_names_the_app(dirty):
    inst = dirty
    conf_before = _read(inst, _CONF)
    excluded_before = _read(inst, _EXCLUDED)
    backups_before = _backups(inst)
    frappe_dir = f"{_BENCH}/apps/frappe"
    branch = _sh(inst, f"git -C {frappe_dir} symbolic-ref --short HEAD").strip()
    tracking = f"refs/remotes/upstream/{branch}"
    tracking_before = harness.exec_in_frappe(
        inst.name, f"git -C {frappe_dir} rev-parse --verify -q {tracking}"
    )[1].strip()
    pushed = _sh(inst, f"git -C {_DIRTY_REMOTE} rev-parse main").strip()
    assert _work(inst)[0] != pushed and _work(inst)[1]  # the work is really there
    # bench resets only the dirty app, then stops at its own post-reset `find`.
    _sh(inst, f"ls -1 {_BENCH}/apps | grep -vx {_DIRTY} > {_EXCLUDED}")
    _sh(inst, f"mkdir {_DIRTY_DIR}/locked && chmod 000 {_DIRTY_DIR}/locked")

    try:
        result = harness.run_cwcli(
            "axi", "apps", "update", inst.name, "frappe", "--force", timeout=900
        )

        output = result.stdout + result.stderr
        assert "data_loss_refused: false" in result.stdout, output
        assert _DIRTY in _list_field(result.stdout, "data_loss_apps")
        assert f"will discard any uncommitted or unpushed work in {_DIRTY}" in result.stdout
        # bench ran over the app: the unpushed commit and the tracked edit are gone.
        assert _work(inst) == (pushed, "")
        # ... then stopped where this test made it stop, as a failed reset.
        assert result.returncode == 1, output
        assert "failed_apps[1]: frappe" in result.stdout
    finally:
        _restore(inst, _CONF, conf_before)
        _restore(inst, _EXCLUDED, excluded_before)
        for path in sorted(_backups(inst) - backups_before):
            harness.exec_in_frappe(inst.name, f"rm -f {shlex.quote(path)}")
        if tracking_before:
            harness.exec_in_frappe(
                inst.name, f"git -C {frappe_dir} update-ref {tracking} {tracking_before}"
            )

    assert _read(inst, _CONF) == conf_before
    assert _backups(inst) == backups_before
