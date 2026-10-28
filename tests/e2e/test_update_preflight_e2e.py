"""The ``apps update <project> frappe`` preflight and data-loss guard, on a real bench.

The core update resets frappe ONLY (fetch + ``reset --hard`` to its remote branch,
never bench's bench-wide ``bench update --reset``), so the preflight refuses what
would stop it before anything runs:

- an app on a branch its remote lacks: the preflight refuses in seconds, names the
  app and the cwcli remedy, raises no other blocker on a healthy bench, and changes
  nothing (common_site_config.json byte-identical, no new backup file);
- frappe's upstream pointed at a local remote whose tip is the next major version:
  the preflight refuses with ``frappe.major_upgrade`` and changes nothing.

It also proves the data-loss guard on a real bench, with frappe itself holding an
uncommitted change and an unpushed commit - work the core reset discards - and its
upstream mirrored offline (so a reset lands back on today's HEAD):

- without ``--force`` the update refuses, with and without ``--no-preflight``, names
  frappe, and changes nothing;
- with ``--force`` the reset runs, the report names frappe, and the work is gone.

The shared instance is restored in ``finally`` blocks: frappe's HEAD and what a
fetch of it changes, and the planted app and remotes.

Commit to run on CI (real Docker); do NOT run locally.
"""

from __future__ import annotations

import shlex
import textwrap
import time

import pytest

from . import harness
from .test_apps_update_e2e import _frappe_remote_mirrored, _install_bench_shim, _remove_bench_shim

pytestmark = pytest.mark.e2e

_BENCH = harness.DEFAULT_BENCH_PATH
_APP = "cwe2e_pfapp"
_BRANCH = "cwe2e-local-only"
_REMOTE = f"/workspace/{_APP}-remote.git"
_CONF = f"{_BENCH}/sites/common_site_config.json"
_ABSENT = "@@ABSENT@@"


def _sh(inst, script: str) -> str:
    code, out = harness.exec_in_frappe(inst.name, script)
    assert code == 0, f"{script}\n{out}"
    return out


def _read(inst, path: str) -> str:
    """The file's bytes as base64 (byte-exact restore), or _ABSENT."""
    q = shlex.quote(path)
    return _sh(inst, f"if [ -f {q} ]; then base64 -w0 {q}; else echo {_ABSENT}; fi").strip()


def _backups(inst) -> set[str]:
    out = _sh(inst, f"find {_BENCH}/sites -path '*/private/backups/*' -type f | sort")
    return {line for line in out.splitlines() if line.strip()}


_FRAPPE = f"{_BENCH}/apps/frappe"


@pytest.fixture(autouse=True)
def _frappe_fetch_state(running_instance):
    """Every leg runs a preflight or bench, and both fetch frappe's upstream branch."""
    with harness.preserve_frappe_fetch_state(running_instance.name):
        yield


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


def _list_field(stdout: str, name: str) -> list[str]:
    """A TOON `name[N]: a,b` list field."""
    for line in stdout.splitlines():
        if line.startswith(f"{name}["):
            values = line.split(":", 1)[1].strip()
            return [v for v in values.split(",") if v]
    raise AssertionError(f"{name} not in:\n{stdout}")


def _work(inst) -> tuple[str, str]:
    """frappe's HEAD and its tracked changes."""
    head = _sh(inst, f"git -C {_FRAPPE} rev-parse HEAD").strip()
    status = _sh(inst, f"git -C {_FRAPPE} status --porcelain --untracked-files=no")
    return head, status.strip()


@pytest.fixture()
def dirty(running_instance):
    """frappe holding an unpushed commit and an uncommitted change, its upstream a bare
    mirror of today's HEAD (taken BEFORE the commit, so the commit is on no remote)."""
    inst = running_instance
    head = _work(inst)[0]
    with _frappe_remote_mirrored(inst):
        try:
            _sh(
                inst,
                f"set -e; cd {_FRAPPE}; git -c user.email=e2e@example.com -c user.name=e2e "
                "-c commit.gpgsign=false commit -q --allow-empty -m 'unpushed work'; "
                "printf 'cwe2e-uncommitted\\n' >> README.md",
            )
            yield inst, head
        finally:
            harness.exec_in_frappe(inst.name, f"git -C {_FRAPPE} reset -q --hard {head}")


@pytest.mark.parametrize("flags", [(), ("--no-preflight",)], ids=["preflight", "no-preflight"])
def test_local_work_refuses_without_force_and_changes_nothing(dirty, flags):
    inst, _head = dirty
    conf_before = _read(inst, _CONF)
    backups_before = _backups(inst)
    work_before = _work(inst)

    result = harness.run_cwcli("axi", "apps", "update", inst.name, "frappe", *flags, timeout=300)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "data_loss_refused: true" in result.stdout
    assert "frappe" in _list_field(result.stdout, "data_loss_apps")
    rows = _preflight_rows(result.stdout)
    for code in ("git.uncommitted", "git.unpushed"):
        assert any(row.startswith(f"data_loss,{code},frappe,") for row in rows), rows
    # Nothing changed: the reset never ran, and the work is still there.
    assert _read(inst, _CONF) == conf_before
    assert _backups(inst) == backups_before
    assert _work(inst) == work_before


def test_force_discards_the_core_app_s_work_and_names_it(dirty):
    inst, head = dirty
    conf_before = _read(inst, _CONF)
    backups_before = _backups(inst)
    assert _work(inst)[0] != head and _work(inst)[1]  # the work is really there
    _install_bench_shim(inst, exit_code=0)  # a real requirements reinstall takes minutes
    try:
        result = harness.run_cwcli(
            "axi", "apps", "update", inst.name, "frappe", "--force", "--no-recache", timeout=1800
        )
    finally:
        _remove_bench_shim(inst)

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "data_loss_refused: false" in result.stdout, output
    assert "frappe" in _list_field(result.stdout, "data_loss_apps")
    assert "will discard any uncommitted or unpushed work in frappe" in result.stdout
    # The reset ran over frappe: the unpushed commit and the tracked edit are gone, and
    # it landed on the mirrored remote branch, today's HEAD.
    assert _work(inst) == (head, "")
    # Nothing bench-wide, and no backup taken on this path.
    assert _read(inst, _CONF) == conf_before
    assert _backups(inst) == backups_before


_MAJOR_REMOTE = "/workspace/cwe2e-frappe-next-major.git"


@pytest.fixture()
def next_major(running_instance):
    """frappe's upstream pointed at a local remote whose branch tip is the next major.

    The remote borrows frappe's own objects (alternates) and adds one commit on top
    of frappe's HEAD that only bumps ``__version__``, so nothing is copied or cloned.
    """
    inst = running_instance
    teardown = f"rm -rf {_MAJOR_REMOTE}"
    _sh(inst, teardown)
    try:
        _sh(
            inst,
            textwrap.dedent(
                f"""
                set -e
                F={_FRAPPE}
                R={_MAJOR_REMOTE}
                G="git --git-dir=$R -c user.email=e2e@example.com -c user.name=e2e"
                branch=$(git -C "$F" symbolic-ref --short HEAD)
                head=$(git -C "$F" rev-parse HEAD)
                git init -q --bare "$R"
                (cd "$F/.git/objects" && pwd -P) > "$R/objects/info/alternates"
                # A shallow clone's history ends at its shallow commits; so must the remote's.
                if [ -f "$F/.git/shallow" ]; then cp "$F/.git/shallow" "$R/shallow"; fi
                git -C "$F" show HEAD:frappe/__init__.py > "$R/init.py"
                major=$(sed -n 's/^__version__ = "\\([0-9][0-9]*\\)\\..*/\\1/p' "$R/init.py")
                test -n "$major"
                sed -i "s/^__version__ = .*/__version__ = \\"$((major + 1)).0.0\\"/" "$R/init.py"
                blob=$($G hash-object -w "$R/init.py")
                export GIT_INDEX_FILE="$R/next-major.index"
                $G read-tree "$head"
                $G update-index --cacheinfo "100644,$blob,frappe/__init__.py"
                commit=$($G -c commit.gpgsign=false commit-tree "$($G write-tree)" -p "$head" \\
                    -m "next major")
                $G update-ref "refs/heads/$branch" "$commit"
                git -C "$F" remote set-url upstream "$R"
                """
            ),
        )
        yield inst
    finally:
        harness.exec_in_frappe(inst.name, teardown)


def test_a_frappe_major_upgrade_upstream_refuses_and_changes_nothing(next_major):
    inst = next_major
    conf_before = _read(inst, _CONF)
    backups_before = _backups(inst)
    checkout = f"git -C {_FRAPPE} rev-parse HEAD && git -C {_FRAPPE} status --porcelain"
    checkout_before = _sh(inst, checkout)

    result = harness.run_cwcli("axi", "apps", "update", inst.name, "frappe", timeout=300)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "preflight_blocked: true" in result.stdout
    rows = _preflight_rows(result.stdout)
    major_row = "blocker,frappe.major_upgrade,frappe,null,"
    assert any(row.startswith(major_row) for row in rows), rows
    assert f"cwcli run {inst.name} -i update --reset" in result.stdout
    assert "skips cwcli's data-loss guard" in result.stdout
    # The bump is the only thing wrong: no other blocker on a healthy real bench.
    blockers = [row for row in rows if row.startswith("blocker,")]
    assert all(row.startswith(major_row) for row in blockers), blockers
    # The content tier read the new tip from the fetched ref: nothing checked out, and
    # bench never started.
    assert _sh(inst, checkout) == checkout_before
    assert _read(inst, _CONF) == conf_before
    assert _backups(inst) == backups_before
