"""Attached and clustered trailing short options, proven against real Docker.

``start``, ``stop``, ``restart`` and ``rm`` are Typer sub-apps, hence Click
Groups, and a Click Group sets ``allow_interspersed_args = False`` - so every
token written after the first project name is dumped into the variadic project
argument without ever reaching Click's parser. Those four commands therefore
parse their own trailing options, and that hand-rolled splitter compared WHOLE
tokens against the option table: ``cwcli restart proj -pweb`` and ``cwcli start
proj -vy`` - forms Click itself accepts everywhere else - were refused as "No
such option".

Fixing it needs a real grammar rather than a substring search, because the naive
repair is dangerous in exactly one direction. Walking a cluster character by
character and keeping whatever matched turns ``cwcli rm proj -yq`` - a typo, a
truncation, or ``-q`` borrowed from a sibling verb - into ``cwcli rm proj -y``,
synthesising the consent that skips rm's destructive confirmation. A cluster is
therefore applied only when EVERY character in it resolves, and a refusal has to
land before a project is selected, before anything is started or stopped, before
any prompt, before the backup gate and before any deletion.

The unit tier (``tests/test_trailing_options.py``) pins the grammar itself,
including a differential comparison against a real Click command. This file is
the runtime half, and it is CI's to execute: ``.github/workflows/e2e.yml`` runs
the real-Docker matrix on an ephemeral runner, where the instance this
provisions is the only one on the host. It is never selected by the local
validation gate, which is pinned to ``-m unit`` (``.no-mistakes.yaml``).

The arc is ordered so no assertion can pass vacuously:

1. A real instance, provisioned and serving.
2. POSITIVE first - valid clusters really do drive the lifecycle verbs
   (``stop -v``, ``start -vy``, ``restart -pweb``), observed as real container
   and supervisord state, so the grammar is proven live before any refusal.
3. A real seeded record, read back through Frappe.
4. ADVERSARIAL - every malformed and ambiguous cluster form is refused on the
   destructive verb at exit 2, printing no confirmation, leaving containers,
   volumes, project directory and the seeded record all intact, on a non-TTY
   AND on a real pty.
5. The valid cluster ``rm -vy`` then genuinely deletes the instance - which is
   what proves step 4's refusals were not vacuous: rm was armed the whole time
   and the only difference was the cluster being well-formed.

Step 4 is the load-bearing half and it is what step 5 exists to qualify. What it
pins is an ORDERING: a malformed cluster is refused before a project is
selected, before any prompt, before the C1 backup gate and before any deletion.
That ordering cannot be observed from a unit test of the parser alone, which is
why this file has to reach the real ``rm`` path rather than assert on a return
value.

Provisions its OWN instance rather than the shared ``session_instance``, since
step 5 destroys it (the ``test_axi_rm_e2e.py`` precedent), and walks the whole
arc in ONE function rather than splitting it across functions that would each
pay for their own provisioning. Option parsing does not vary by Frappe major, so
it runs once on the v16 leg of the matrix - the same reasoning and the same
``v16_only`` shape ``test_axi_rm_e2e.py`` already uses for the C1 gate.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

import pytest

from . import harness
from .conftest import SESSION_ADMIN_PW
from .test_start_status_e2e import BENCH_PY, SUPERVISOR_CFG

# `standalone`: this file provisions its own instance and never touches the
# shared session instance (see the marker's entry in pyproject.toml).
pytestmark = [pytest.mark.e2e, pytest.mark.standalone]

v16_only = pytest.mark.skipif(
    harness.FRAPPE_MAJOR != 16,
    reason="option parsing does not vary by Frappe major; runs once on the v16 leg",
)

MARKER = "CWE2E-CLUSTER-MARKER"

# Every one of these is refused WHOLE. Each holds a character `rm` does not
# define, and all but the last two hold `-y` as well - the consent that must
# never be synthesised out of a token the user did not fully spell.
REFUSED_CLUSTERS = [
    "-yq",  # consent then a typo
    "-qy",  # a typo then consent
    "-vyq",  # a longer cluster, one unknown character
    "-y-",  # a malformed dash inside the cluster
    "-yb",  # -b is a sibling verb's short, not one rm defines
    "-y1",  # a digit that is not an option
    "-vq",  # no consent in it at all, still refused
    "--yes-please",  # the pre-existing long-option refusal, unchanged
]


def _project_dir(project: str) -> Path:
    return Path(os.environ["CWCLI_HOME"]) / "projects" / project


def _seed_marker(project: str, site: str, bench: str) -> None:
    """Insert a real ToDo via `bench execute frappe.client.insert` (not SQL)."""
    kwargs = f'{{"doc": {{"doctype": "ToDo", "description": "{MARKER}", "status": "Open"}}}}'
    script = (
        f"cd {shlex.quote(bench)} && bench --site {shlex.quote(site)} execute "
        f"frappe.client.insert --kwargs '{kwargs}'"
    )
    code, out = harness.exec_in_frappe(project, script)
    assert code == 0, f"seeding the marker record failed: {out}"


def _count_marker(project: str, site: str, bench: str) -> int:
    kwargs = f'{{"doctype": "ToDo", "filters": {{"description": "{MARKER}"}}}}'
    script = (
        f"cd {shlex.quote(bench)} && bench --site {shlex.quote(site)} execute "
        f"frappe.client.get_count --kwargs '{kwargs}'"
    )
    code, out = harness.exec_in_frappe(project, script)
    assert code == 0, f"reading the marker count back failed: {out}"
    stripped = out.strip()
    if not stripped:
        # `bench execute` only prints a truthy return value, so a genuine zero
        # count prints nothing at all rather than "0" (the test_axi_rm_e2e note).
        return 0
    for line in reversed(stripped.splitlines()):
        line = line.strip()
        if line.lstrip("-").isdigit():
            return int(line)
    raise AssertionError(f"could not parse an integer count from bench execute output: {out!r}")


def _supervisord_pid(project: str, program: str) -> int | None:
    """The pid supervisord tracks for ``program``, or None (test_per_process's helper)."""
    _code, out = harness.exec_in_frappe(
        project, f"{BENCH_PY} -m supervisor.supervisorctl -c {SUPERVISOR_CFG} status {program}"
    )
    parts = out.split()
    for i, token in enumerate(parts):
        if token == "pid" and i + 1 < len(parts):
            raw = parts[i + 1].rstrip(",")
            return int(raw) if raw.isdigit() else None
    return None


def _assert_nothing_deleted(project: str, *, why: str) -> None:
    """Every resource `rm` would have destroyed is still there.

    Cheap enough (docker label queries) to run after every refused form.
    """
    assert harness.frappe_container_id(project) is not None, f"{why}: the container is gone"
    assert harness.project_volumes(project), f"{why}: a named volume was removed"
    assert harness.project_networks(project), f"{why}: the compose network was removed"
    assert _project_dir(project).exists(), f"{why}: the project directory was removed"


@v16_only
def test_clustered_shorts_drive_the_lifecycle_and_never_synthesise_consent(port_allocator):
    site = harness.DEFAULT_SITE
    bench = harness.DEFAULT_BENCH_PATH
    project = harness.project_name("cluster")

    try:
        # --- 1. a real, serving instance --------------------------------
        init_result = harness.run_cwcli(
            "init",
            project,
            "--port",
            str(port_allocator.next()),
            "--frappe-branch",
            harness.FRAPPE_BRANCH,
            "--admin-password",
            SESSION_ADMIN_PW,
            "--auto-start",
            timeout=harness.INIT_TIMEOUT,
        )
        assert init_result.returncode == 0, init_result.stdout + init_result.stderr
        harness.wait_for_site_ready(project, site)

        # --- 2. POSITIVE: valid clusters really drive the lifecycle ------
        # Asserted before any refusal so "nothing happened" can never pass by
        # the grammar being broken in the other direction.

        # A trailing short that used to work, still does.
        stopped = harness.run_cwcli("stop", project, "-v")
        assert stopped.returncode == 0, stopped.stdout + stopped.stderr
        harness.wait_until(
            lambda: harness.frappe_container_id(project) is None,
            timeout=180,
            desc="containers stopped by `stop -v`",
        )

        # The clustered form: -vy is -v -y, and both have to arrive.
        started = harness.run_cwcli("start", project, "-vy")
        assert started.returncode == 0, started.stdout + started.stderr
        # -v really arrived: start's verbose narration (on stderr) needs it, so
        # this distinguishes "the cluster was applied" from "-vy was ignored".
        assert "VERBOSE:" in started.stderr, started.stdout + started.stderr
        harness.wait_for_site_ready(project, site)

        # The attached form: -pweb is --process web, so ONE program cycles.
        web_before = _supervisord_pid(project, "web")
        assert web_before, "web must be up before the attached-short restart"
        restarted = harness.run_cwcli("restart", project, "-pweb")
        assert restarted.returncode == 0, restarted.stdout + restarted.stderr
        assert "restarted process" in restarted.stdout, restarted.stdout
        harness.wait_until(
            lambda: _supervisord_pid(project, "web") not in (None, web_before),
            timeout=120,
            desc="`restart -pweb` cycled the web program",
        )
        harness.wait_for_site_ready(project, site)

        # --- 3. real data, proven present before the destructive half ---
        _seed_marker(project, site, bench)
        assert _count_marker(project, site, bench) == 1, "seeded marker did not land"

        # --- 4. ADVERSARIAL: no malformed cluster may reach the removal --
        for token in REFUSED_CLUSTERS:
            result = harness.run_cwcli("rm", project, token, timeout=120)
            combined = result.stdout + result.stderr

            assert result.returncode == 2, f"{token!r} was not refused: {combined}"
            # Refused BEFORE the removal flow starts: rm prints this the moment
            # it begins preparing, ahead of the confirmation and the backup gate.
            assert "Preparing for removal" not in combined, f"{token!r} reached removal: {combined}"
            # And no confirmation was manufactured out of the token.
            assert "Are you sure" not in combined, f"{token!r} prompted: {combined}"
            assert "No such option" in combined, combined

            _assert_nothing_deleted(project, why=f"refusing {token!r}")

        # The same refusal on a REAL pty, where a synthesised -y would show up
        # as an actual confirmation prompt rather than a silent exit.
        import pexpect

        child = harness.spawn_cwcli(["rm", project, "-vyq"], timeout=120)
        child.expect(pexpect.EOF)
        interactive_output = harness.strip_ansi(child.before or "")
        child.close()
        assert child.exitstatus == 2, interactive_output
        assert "Are you sure" not in interactive_output, interactive_output
        assert "Preparing for removal" not in interactive_output, interactive_output
        _assert_nothing_deleted(project, why="refusing '-vyq' on a pty")

        # The site is still live and the seeded record still readable - no
        # refused form reached the backup gate, a prompt, or the database.
        harness.wait_for_site_ready(project, site)
        assert _count_marker(project, site, bench) == 1, "a refused form disturbed the data"

        # --- 5. the valid cluster DOES delete, so step 4 was not vacuous -
        # Same command, same instance, one well-formed cluster: -vy carries
        # rm's consent and the removal really happens.
        removed = harness.run_cwcli("rm", project, "-vy", "--no-backup", timeout=600)
        assert removed.returncode == 0, removed.stdout + removed.stderr
        assert harness.frappe_container_id(project) is None, "a container survived removal"
        assert not harness.project_containers(project), "a container survived removal"
        assert not harness.project_volumes(project), "a named volume survived removal"
        assert not harness.project_networks(project), "the compose network survived removal"
        assert not _project_dir(project).exists(), "the project directory survived removal"
    finally:
        # Tolerates the project already being gone (step 5 deletes it itself);
        # `core.remove` reports a genuinely-absent project as an exit-0 no-op.
        harness.cwcli_rm(project)
