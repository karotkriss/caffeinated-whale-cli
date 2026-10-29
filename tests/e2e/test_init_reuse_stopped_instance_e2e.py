"""Real-Docker E2E regression: re-running init against a STOPPED instance to
reuse its bench must start the existing containers as they are.

The defect (reported by a user operating a long-lived instance): `cwcli axi
init <project> --reuse-bench` against a stopped instance re-pulled the bench
image and broke the bench's Python environment. `frappe/bench` tags are
re-pushed upstream (a local `v5.31.1` moved from an image with Python 3.14.9 to
one with 3.14.10), so `compose pull` moved the instance's pinned tag and a plain
`compose up -d` RECREATED frappe on the new image. `/home/frappe` reverted to
that image, and the bench venv's `env/bin/python`, a symlink into the old
image's pyenv version, dangled. The fix (`core/init.py`) pulls nothing for an
instance whose containers exist and starts them with `up -d --no-recreate`.

No hermetic test controls an upstream re-push, so this one moves a tag it owns:
the instance's frappe service is pointed at a test-owned local tag of the SAME
image (`pull_policy: never` stands in for the registry), then that tag is moved
to a derived image whose copy of the venv's interpreter is renamed away - the
exact shape of the upstream patch bump. A re-init that recreates frappe lands
on that image and the bench's Python breaks; the fixed re-init leaves the
container, its image, and its Python untouched. The shared `frappe/bench` tags
are never touched.

Each surface re-initializes the stopped instance in turn: `cwcli axi init
--reuse-bench`, the human `cwcli init --reuse-bench`, and the human `cwcli init`
interactively answering its reuse prompt on a real pty.

Builds its own instance (its frappe service is repointed), so it is
`standalone`; the risk lives in `init_instance`'s host-command gating, not in
anything Frappe-version-specific, so it runs once on the v16 leg.
"""

from __future__ import annotations

import io
import os
import re
import subprocess
from pathlib import Path

import pytest

from . import harness
from .conftest import SESSION_ADMIN_PW

pytestmark = [pytest.mark.e2e, pytest.mark.standalone]

v16_only = pytest.mark.skipif(
    harness.FRAPPE_MAJOR != 16,
    reason="the pull/recreate gating lives in init_instance, not anything "
    "Frappe-version-specific; v16 leg only",
)

VENV_PYTHON = f"{harness.DEFAULT_BENCH_PATH}/env/bin/python"


def _conf_dir(name: str) -> Path:
    return Path(os.environ["CWCLI_HOME"]) / "projects" / name / "conf"


def _image_of(ref: str) -> str:
    r = harness._docker("inspect", "-f", "{{.Image}}", ref)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout.strip()


def _image_id(tag: str) -> str:
    r = harness._docker("image", "inspect", "-f", "{{.Id}}", tag)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout.strip()


def _python_works(name: str) -> tuple[bool, str]:
    code, out = harness.exec_in_frappe(name, f"{VENV_PYTHON} -c 'import frappe'")
    return code == 0, out


def _stop(name: str) -> None:
    r = harness.run_cwcli("stop", name, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    assert harness.frappe_container_id(name) is None, "the frappe container is still running"


def _pin_frappe_to_owned_tag(name: str, tag: str) -> None:
    """Point the frappe service at ``tag`` (the SAME image it runs) and recreate
    it once, so the container matches its compose file exactly and only a later
    tag move can make Compose see drift."""
    image = _image_of(harness.frappe_container_id(name) or "")
    r = harness._docker("tag", image, tag)
    assert r.returncode == 0, r.stdout + r.stderr

    compose_path = _conf_dir(name) / "docker-compose.yml"
    text, count = re.subn(
        r"(?m)^(\s+)image: docker\.io/frappe/bench:\S+$",
        rf"\1image: {tag}\n\1pull_policy: never",
        compose_path.read_text(),
    )
    assert count == 1, "the frappe image line was not found in the compose file"
    compose_path.write_text(text)

    r = subprocess.run(
        ["docker", "compose", "-p", name, "-f", "docker-compose.yml"]
        + ["up", "-d", "--no-deps", "frappe"],
        cwd=str(_conf_dir(name)),
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    harness.wait_until(
        lambda: harness.frappe_container_id(name) is not None, timeout=120, desc="frappe up"
    )


def _move_tag_without_interpreter(tag: str, interpreter_dir: str) -> None:
    """Rebuild ``tag`` as a derived image whose copy of ``interpreter_dir`` is gone."""
    dockerfile = f"FROM {tag}\nRUN mv {interpreter_dir} {interpreter_dir}.moved\n"
    r = subprocess.run(
        ["docker", "build", "-q", "-t", tag, "-"],
        input=dockerfile,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert r.returncode == 0, r.stdout + r.stderr


def _assert_preserved(name: str, container: str, image: str, run) -> None:
    after = harness.frappe_container_id(name)
    assert after == container, (
        "re-init recreated the frappe container - its /home/frappe toolchain went with it\n" + run
    )
    assert _image_of(after) == image, "re-init moved the frappe container to another image"
    ok, out = _python_works(name)
    assert ok, f"the bench's Python environment is broken after re-init: {out}\n{run}"


@v16_only
def test_reinit_of_a_stopped_instance_keeps_its_container_and_python(port_allocator):
    harness.enforce_isolation()
    name = harness.project_name("reinit")
    port = str(port_allocator.next())
    tag = f"{name}-bench:pinned"
    common = ("--port", port, "--frappe-branch", harness.FRAPPE_BRANCH)
    try:
        first = harness.run_cwcli(
            "init",
            name,
            *common,
            "--admin-password",
            SESSION_ADMIN_PW,
            "--no-start",
            timeout=harness.INIT_TIMEOUT,
        )
        assert first.returncode == 0, first.stdout + first.stderr

        # POSITIVE FIRST: the venv's interpreter lives in the image's own
        # /home/frappe toolchain, which is exactly what a recreation replaces.
        code, resolved = harness.exec_in_frappe(name, f"readlink -f {VENV_PYTHON}")
        assert code == 0, resolved
        match = re.match(r"(/home/frappe/\.pyenv/versions/[^/]+)/", resolved.strip())
        assert match, f"the venv interpreter is not in the image's pyenv: {resolved}"
        interpreter_dir = match.group(1)

        _pin_frappe_to_owned_tag(name, tag)
        ok, out = _python_works(name)
        assert ok, f"the bench's Python is broken before any re-init: {out}"
        container = harness.frappe_container_id(name) or ""
        image = _image_of(container)

        # Arm the trap: the tag now names an image without the venv's interpreter.
        _stop(name)
        _move_tag_without_interpreter(tag, interpreter_dir)
        assert _image_id(tag) != image, "the owned tag did not move"
        r = harness._docker("run", "--rm", "--entrypoint", "test", tag, "-e", resolved.strip())
        assert r.returncode != 0, "the moved image still has the venv's interpreter"

        # THE ACT, agent surface: `cwcli axi init --reuse-bench` on the stopped instance.
        run = harness.run_cwcli(
            "axi",
            "init",
            name,
            *common,
            "--admin-password",
            SESSION_ADMIN_PW,
            "--reuse-bench",
            "--no-start",
            timeout=harness.INIT_TIMEOUT,
        )
        _assert_preserved(name, container, image, run.stdout + run.stderr)
        assert run.returncode == 0, run.stdout + run.stderr

        # Human surface, non-interactive: `cwcli init --reuse-bench`.
        _stop(name)
        run = harness.run_cwcli(
            "init",
            name,
            *common,
            "--admin-password",
            SESSION_ADMIN_PW,
            "--reuse-bench",
            "--no-start",
            timeout=harness.INIT_TIMEOUT,
        )
        _assert_preserved(name, container, image, run.stdout + run.stderr)
        assert run.returncode == 0, run.stdout + run.stderr

        # Human surface, interactive: answer the reuse prompt on a real pty.
        import pexpect

        _stop(name)
        log = io.StringIO()
        child = harness.spawn_cwcli(
            ["init", name, *common, "--admin-password", SESSION_ADMIN_PW, "--no-start"],
            timeout=harness.INIT_TIMEOUT,
        )
        child.logfile_read = log
        try:
            harness.expect_prompt_ready(child, timeout=600)  # the reuse confirm
            child.send("y")
            child.send("\r")
            child.expect(pexpect.EOF, timeout=harness.INIT_TIMEOUT)
        finally:
            child.close(force=True)
        output = harness.collapse_ws(harness.strip_ansi(log.getvalue()))
        assert "already exists at" in output, output[-2000:]
        _assert_preserved(name, container, image, output[-2000:])
        assert child.exitstatus == 0, output[-2000:]
        assert "Reused existing bench" in output, output[-2000:]
    finally:
        harness.cwcli_rm(name)
        harness._docker("rmi", "-f", tag)
