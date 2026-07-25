"""``core.init`` - the two-call slice, every branch on fakes (migrate-init-core §2).

Pins the Decision 3 secret audit (secrets ride ``environment=`` and appear in NO
event, DTO field, warning, or command-echo trace), the three choice surfaces,
the tri-state ``reuse_bench`` matrix, exec-order preservation, the honest
lost-stream ``DOCKER`` error where ``exit code None`` used to print, core
silence, and plain-data DTOs.
"""

import dataclasses
import json
import os
import subprocess
import urllib.request
from types import SimpleNamespace

import pytest

from caffeinated_whale_cli.core import exec_stream as exec_stream_mod
from caffeinated_whale_cli.core import init as core_init
from caffeinated_whale_cli.core.envelope import Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from caffeinated_whale_cli.utils import config_utils, db_utils

PROJECT = "proj"
BENCH_PATH = "/workspace/frappe-bench"
SITE = "development.localhost"
SITE_PATH = f"{BENCH_PATH}/sites/{SITE}"

COMPOSE_TEMPLATE = """services:
  frappe:
    image: docker.io/frappe/bench:latest
    working_dir: /workspace/development
    ports:
      - "8000-8005:8000-8005"
      - "9000-9005:9000-9005"
"""

# The real upstream shape (frappe_docker devcontainer-example): the bench
# workspace is a bind mount and working_dir sits under it. Used for the
# workspace-mount rewrite tests, which need the exact lines init_instance
# rewrites when it creates a fresh instance.
COMPOSE_UPSTREAM = """services:
  mariadb:
    image: docker.io/mariadb:11.8
    volumes:
      - mariadb-data:/var/lib/mysql
  frappe:
    image: docker.io/frappe/bench:latest
    volumes:
      - ..:/workspace:cached
    working_dir: /workspace/development
    ports:
      - 8000-8005:8000-8005
      - 9000-9005:9000-9005
volumes:
  mariadb-data:
"""


class FakeApi:
    """Records ``exec_create`` calls; ``fail_command`` marks one failing exec."""

    def __init__(self, fail_command=None, fail_output=b"", lose_stream_on=None):
        self.exec_calls: list[dict] = []
        self.fail_command = fail_command
        self.fail_output = fail_output
        self.lose_stream_on = lose_stream_on
        self._fails: dict[str, bool] = {}
        self._lost: dict[str, bool] = {}

    def exec_create(self, container_id, cmd, **kwargs):
        command = cmd[2] if isinstance(cmd, (list, tuple)) and len(cmd) == 3 else " ".join(cmd)
        exec_id = f"exec-{len(self.exec_calls)}"
        self.exec_calls.append({"command": command, "environment": kwargs.get("environment")})
        self._fails[exec_id] = self.fail_command is not None and self.fail_command in command
        self._lost[exec_id] = self.lose_stream_on is not None and self.lose_stream_on in command
        return {"Id": exec_id}

    def exec_start(self, exec_id, stream=False, demux=False, **kwargs):
        if self._lost[exec_id]:
            # A dropped connection: CancellableStream turns it into a silent,
            # clean-looking end of stream.
            return iter([])
        output = self.fail_output if self._fails[exec_id] else b""
        return iter([(output, None)] if output else [])

    def exec_inspect(self, exec_id):
        if self._lost[exec_id]:
            # The exec is still running as far as the daemon knows.
            return {"ExitCode": None, "Running": True}
        return {"ExitCode": 1 if self._fails[exec_id] else 0, "Running": False}


class FakeContainer:
    """A frappe container whose probes and buffered helpers answer from maps."""

    def __init__(self, api=None, dirs_exist=(), status="running", exec_run_responses=None):
        self.id = "fake-frappe"
        self.status = status
        self.labels = {"com.docker.compose.service": "frappe"}
        self.client = SimpleNamespace(api=api or FakeApi())
        self.dirs_exist = set(dirs_exist)
        # substring -> (exit_code, bytes) for buffered exec_run calls
        self.exec_run_responses = exec_run_responses or {}
        self.exec_run_calls: list[str] = []

    def reload(self):
        pass

    def exec_run(self, cmd, **kwargs):
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "id":
            # `id -u/-g frappe` probe for the host-uid alignment: report the host's
            # own ids so the align step is a clean no-op in these fakes.
            return 0, (str(os.getuid()) if "-u" in cmd else str(os.getgid())).encode()
        script = cmd[2] if isinstance(cmd, (list, tuple)) and len(cmd) == 3 else ""
        self.exec_run_calls.append(script)
        for needle, response in self.exec_run_responses.items():
            if needle in script:
                return response
        if script.startswith("mkdir -p"):
            return 0, b""
        if script.startswith("test -d"):
            path = script.split()[-1].strip("'\"")
            return (0 if path in self.dirs_exist else 1), b""
        return 0, b""


@pytest.fixture
def patched(monkeypatch, tmp_path):
    """Common seams: cache clear, search paths, no real sleeping."""
    cleared: list[str] = []
    added: list[str] = []
    monkeypatch.setattr(db_utils, "clear_cache_for_project", lambda name: cleared.append(name))
    monkeypatch.setattr(config_utils, "add_custom_path", lambda path: (added.append(path), True)[1])
    monkeypatch.setattr(core_init.time, "sleep", lambda _s: None)
    return SimpleNamespace(cleared=cleared, added=added)


def use_container(monkeypatch, container, *, project_containers=None):
    monkeypatch.setattr(core_init.core_docker, "get_frappe_container", lambda name: container)
    # The self-conflict port skip reads the project's OWN containers. Patched here
    # so no unit test reaches a real Docker daemon to answer it; the default is an
    # absent project, which is the state that KEEPS the port check running.
    monkeypatch.setattr(
        core_init.core_docker,
        "get_project_containers",
        lambda name: list(project_containers or []),
    )


def bench_kwargs(**overrides):
    params = dict(
        bench_name="frappe-bench",
        site_name=SITE,
        bench_parent="/workspace",
        frappe_ref="version-16",
        admin_password="pw-x",
        erpnext_branch="version-16",
    )
    params.update(overrides)
    return params


# ------------------------------------------------------------------ validators


class TestValidators:
    def test_project_slug_required(self):
        with pytest.raises(CwcliError) as exc:
            core_init.validate_project_slug("  ")
        assert exc.value.kind is ErrorKind.USAGE
        assert exc.value.message == "Project name is required."

    def test_project_slug_invalid_chars(self):
        with pytest.raises(CwcliError) as exc:
            core_init.validate_project_slug("my proj!")
        assert exc.value.message == (
            "Project name must contain only lowercase letters, " "numbers, dashes, or underscores."
        )

    def test_project_slug_bad_edges(self):
        with pytest.raises(CwcliError) as exc:
            core_init.validate_project_slug("-proj")
        assert exc.value.message == "Project name cannot start or end with '-' or '_'."

    def test_bench_slug_uses_bench_label(self):
        with pytest.raises(CwcliError) as exc:
            core_init.validate_bench_slug("")
        assert exc.value.message == "Bench name is required."

    def test_slug_normalizes_to_lowercase(self):
        assert core_init.validate_project_slug("  My-Proj  ") == "my-proj"

    def test_site_name_required(self):
        with pytest.raises(CwcliError) as exc:
            core_init.validate_new_site_name(" ")
        assert exc.value.message == "Site name is required."

    def test_site_name_suffix(self):
        with pytest.raises(CwcliError) as exc:
            core_init.validate_new_site_name("mysite.dev")
        assert exc.value.message == "Site name must end with '.localhost'."

    def test_site_name_invalid_chars(self):
        with pytest.raises(CwcliError) as exc:
            core_init.validate_new_site_name("my_site.localhost")
        assert exc.value.message == (
            "Site name may only include lowercase letters, numbers, hyphens, and periods."
        )

    def test_site_name_normalizes(self):
        assert core_init.validate_new_site_name(" Dev.LocalHost ") == "dev.localhost"


class TestResolveFrappeRef:
    def test_bare_major_resolves_to_branch(self):
        assert core_init.resolve_frappe_ref("16") == "version-16"

    def test_semver_resolves_to_tag(self):
        assert core_init.resolve_frappe_ref("16.26.3") == "v16.26.3"

    def test_malformed_is_typed_usage_error_with_todays_message(self):
        with pytest.raises(CwcliError) as exc:
            core_init.resolve_frappe_ref("16.26")
        assert exc.value.kind is ErrorKind.USAGE
        assert exc.value.message == (
            "Invalid --version value '16.26': expected a bare major version "
            "(e.g. 16 -> version-16) or a full semantic version "
            "(e.g. 16.26.3 -> v16.26.3)."
        )


# ------------------------------------------------------------------ stage 1


def instance_setup(
    monkeypatch, tmp_path, *, seed_compose=True, container=None, hub_tags=None, running=False
):
    """Wire stage 1's host seams; returns the recorded host calls and paths."""
    host_calls: list[dict] = []
    downloads: list[str] = []

    def fake_run(cmd, cwd=None, capture_output=False, **kwargs):
        host_calls.append({"cmd": list(cmd), "cwd": cwd})
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    def fake_urlopen(req, timeout=None):
        if hub_tags is None:
            raise OSError("offline")
        body = json.dumps({"results": [{"name": t} for t in hub_tags]}).encode()

        class Resp:
            def read(self):
                return body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return Resp()

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(urllib.request, "urlretrieve", lambda url, dest: downloads.append(url))
    monkeypatch.setattr(config_utils, "PROJECTS_DIR", tmp_path)
    own = container or FakeContainer()
    use_container(monkeypatch, own, project_containers=[own] if running else [])

    conf_dir = tmp_path / PROJECT / "conf"
    compose_path = conf_dir / "docker-compose.yml"
    if seed_compose:
        conf_dir.mkdir(parents=True, exist_ok=True)
        compose_path.write_text(COMPOSE_TEMPLATE)

    return SimpleNamespace(host_calls=host_calls, downloads=downloads, compose_path=compose_path)


class TestInitInstance:
    def test_success_customizes_compose_and_runs_pull_then_up(self, monkeypatch, tmp_path, patched):
        s = instance_setup(monkeypatch, tmp_path)
        result = core_init.init_instance(PROJECT, port=18000)

        assert result.status is Status.OK
        assert result.data is not None
        assert result.data.project == PROJECT
        assert result.data.conf_dir == str(tmp_path / PROJECT / "conf")

        assert [c["cmd"] for c in s.host_calls] == [
            ["docker", "compose", "-p", PROJECT, "-f", "docker-compose.yml", "pull", "--quiet"],
            ["docker", "compose", "-p", PROJECT, "-f", "docker-compose.yml", "up", "-d"],
        ]
        content = s.compose_path.read_text()
        assert "18000-18005:8000-8005" in content
        assert "19000-19005:9000-9005" in content
        # Docker Hub unreachable -> fail-open to the pinned fallback, never :latest.
        assert "docker.io/frappe/bench:v5.29.1" in content
        assert ":latest" not in content
        # The vestigial devcontainer working_dir is repointed at the mount root, so
        # the Docker daemon never creates a root-owned /workspace/development inside
        # the bind-mounted CWCLI_HOME (the shared-/tmp root-owned-file leak).
        assert "working_dir: /workspace\n" in content
        assert "/workspace/development" not in content
        # The compose file was already present, so nothing was downloaded.
        assert s.downloads == []

    def test_stream_output_drops_pull_quiet(self, monkeypatch, tmp_path, patched):
        s = instance_setup(monkeypatch, tmp_path)
        core_init.init_instance(PROJECT, port=18000, stream_output=True)
        assert s.host_calls[0]["cmd"][-1] == "pull"

    def test_docker_hub_success_pins_latest_semver_tag(self, monkeypatch, tmp_path, patched):
        s = instance_setup(monkeypatch, tmp_path, hub_tags=["latest", "v5.99.0", "v5.98.0"])
        events = []
        core_init.init_instance(PROJECT, port=18000, on_event=events.append)
        assert "docker.io/frappe/bench:v5.99.0" in s.compose_path.read_text()
        traces = [e.text for e in events if isinstance(e, core_init.InitTrace)]
        assert "Resolved latest bench image tag: v5.99.0" in traces

    def test_missing_compose_is_downloaded(self, monkeypatch, tmp_path, patched):
        s = instance_setup(monkeypatch, tmp_path, seed_compose=False)

        def fake_retrieve(url, dest):
            s.downloads.append(url)
            dest.write_text(COMPOSE_TEMPLATE)

        monkeypatch.setattr(urllib.request, "urlretrieve", fake_retrieve)
        core_init.init_instance(PROJECT, port=18000)
        assert len(s.downloads) == 1

    def test_download_failure_is_typed_precondition(self, monkeypatch, tmp_path, patched):
        instance_setup(monkeypatch, tmp_path, seed_compose=False)

        def failing_retrieve(url, dest):
            raise OSError("boom")

        monkeypatch.setattr(urllib.request, "urlretrieve", failing_retrieve)
        with pytest.raises(CwcliError) as exc:
            core_init.init_instance(PROJECT, port=18000)
        assert exc.value.kind is ErrorKind.PRECONDITION
        assert exc.value.code == "compose.download_failed"
        assert "Failed to download" in exc.value.message

    def test_compose_failure_is_typed_docker_with_stderr_detail(
        self, monkeypatch, tmp_path, patched
    ):
        instance_setup(monkeypatch, tmp_path)

        def failing_run(cmd, cwd=None, capture_output=False, **kwargs):
            return SimpleNamespace(returncode=1, stdout=b"", stderr=b"pull exploded")

        monkeypatch.setattr(subprocess, "run", failing_run)
        with pytest.raises(CwcliError) as exc:
            core_init.init_instance(PROJECT, port=18000)
        assert exc.value.kind is ErrorKind.DOCKER
        assert exc.value.code == "compose.failed"
        assert "Host command failed:" in exc.value.message
        assert exc.value.detail == {"output": "pull exploded"}

    def test_port_conflict_is_typed_conflict(self, monkeypatch, tmp_path, patched):
        instance_setup(monkeypatch, tmp_path)
        monkeypatch.setattr(
            core_init, "check_ports_in_use", lambda ports: {p: p == 18000 for p in ports}
        )
        with pytest.raises(CwcliError) as exc:
            core_init.init_instance(PROJECT, port=18000)
        assert exc.value.kind is ErrorKind.CONFLICT
        assert exc.value.code == "ports.in_use"
        assert "The following ports are already in use: 18000" in exc.value.message
        assert "--port" in (exc.value.hint or "")

    def test_auto_start_retry_skips_self_port_conflict(self, monkeypatch, tmp_path, patched):
        # The auto_start=True call is the frontend's stage-1 retry AFTER
        # ensure_containers_running already started this project's own
        # containers, which bind exactly these ports - not a real conflict.
        instance_setup(monkeypatch, tmp_path)
        monkeypatch.setattr(core_init, "check_ports_in_use", lambda ports: {p: True for p in ports})
        result = core_init.init_instance(PROJECT, port=18000, auto_start=True)
        assert result.status is Status.OK

    def test_a_live_instances_own_ports_are_not_a_conflict(self, monkeypatch, tmp_path, patched):
        """Re-running init against a RUNNING instance is the ordinary way to add a
        bench or a site to it (`cwcli init existing --reuse-bench --site other`),
        and there every port the check finds bound is bound by that very instance.

        The old check refused it outright and told the caller to pick a different
        --port - advice that cannot be followed, because an existing instance's
        ports are frozen in its compose file. Same self-conflict the auto_start
        retry above already skipped, reached by the other route.
        """
        instance_setup(monkeypatch, tmp_path, running=True)
        monkeypatch.setattr(core_init, "check_ports_in_use", lambda ports: {p: True for p in ports})
        result = core_init.init_instance(PROJECT, port=18000)
        assert result.status is Status.OK

    def test_pull_and_up_are_skipped_against_an_already_running_frappe(
        self, monkeypatch, tmp_path, patched
    ):
        """Adding a bench to a live instance must not touch `compose pull`/`up -d`.

        Both commands re-fetch and (without `--no-deps`) can silently RECREATE a
        container whose image drifted upstream since this instance was created -
        for the frappe service that kills every already-serving bench's
        supervisord with nothing in the report to say so (the defect this pins).
        Skipping them when frappe is already running makes a bench-add
        structurally unable to trigger that recreate at all.
        """
        events: list = []
        s = instance_setup(monkeypatch, tmp_path, running=True)
        result = core_init.init_instance(PROJECT, port=18000, on_event=events.append)

        assert result.status is Status.OK
        assert s.host_calls == []
        notices = [e for e in events if isinstance(e, core_init.InitNotice)]
        assert any(n.code == "instance.already_running" for n in notices)

    def test_pull_and_up_still_run_when_a_sibling_container_is_up_but_frappe_is_not(
        self, monkeypatch, tmp_path, patched
    ):
        """The skip is scoped to the frappe SERVICE, not "any container up".

        A stopped frappe beside a running mariadb/redis must still go through
        compose to come back up - skipping there would leave frappe down.
        """
        s = instance_setup(monkeypatch, tmp_path)
        sibling = SimpleNamespace(
            status="running", labels={"com.docker.compose.service": "mariadb"}
        )
        monkeypatch.setattr(core_init.core_docker, "get_project_containers", lambda name: [sibling])
        result = core_init.init_instance(PROJECT, port=18000)

        assert result.status is Status.OK
        assert [c["cmd"][-1] for c in s.host_calls] == ["--quiet", "-d"]

    def test_a_stopped_instance_still_gets_the_port_check(self, monkeypatch, tmp_path, patched):
        """The skip is scoped to a RUNNING project. A stopped one holds no ports, so
        a bound port there genuinely belongs to somebody else and must still refuse."""
        stopped = FakeContainer(status="exited")
        instance_setup(monkeypatch, tmp_path, container=stopped, running=True)
        monkeypatch.setattr(
            core_init, "check_ports_in_use", lambda ports: {p: p == 18000 for p in ports}
        )
        with pytest.raises(CwcliError) as exc:
            core_init.init_instance(PROJECT, port=18000)
        assert exc.value.code == "ports.in_use"

    def test_poll_timeout_offers_confirm_start(self, monkeypatch, tmp_path, patched):
        stopped = FakeContainer(status="exited")
        instance_setup(monkeypatch, tmp_path, container=stopped)
        result = core_init.init_instance(PROJECT, port=18000)
        assert result.status is Status.NEEDS_CHOICE
        assert result.choice is not None
        assert result.choice.kind == "confirm_start"
        assert result.choice.param == "auto_start"

    def test_readiness_poll_is_bounded(self, monkeypatch, tmp_path, patched):
        # The old _wait_for_containers_running's bound, now the core poll's:
        # ~10 silent attempts, then the choice - never an unbounded spin (and
        # structurally never a prompt: the core cannot prompt at all).
        stopped = FakeContainer(status="exited")
        reloads: list[int] = []
        stopped.reload = lambda: reloads.append(1)  # type: ignore[method-assign]
        instance_setup(monkeypatch, tmp_path, container=stopped)
        result = core_init.init_instance(PROJECT, port=18000)
        assert result.status is Status.NEEDS_CHOICE
        assert len(reloads) == 10

    def test_poll_timeout_with_auto_start_fails_closed(self, monkeypatch, tmp_path, patched):
        # The structural cap: the caller claimed the start was handled and the
        # containers are still down -> typed NOT_RUNNING, never a loop.
        stopped = FakeContainer(status="exited")
        instance_setup(monkeypatch, tmp_path, container=stopped)
        with pytest.raises(CwcliError) as exc:
            core_init.init_instance(PROJECT, port=18000, auto_start=True)
        assert exc.value.kind is ErrorKind.NOT_RUNNING

    def test_bad_slug_fails_before_any_work(self, monkeypatch, tmp_path, patched):
        s = instance_setup(monkeypatch, tmp_path)
        with pytest.raises(CwcliError) as exc:
            core_init.init_instance("Bad Name!", port=18000)
        assert exc.value.kind is ErrorKind.USAGE
        assert s.host_calls == []


class TestWorkspaceMount:
    """The bench workspace becomes a per-project host bind mount from
    ``{project}/data`` at the resolved ``--bench-parent`` (the ephemeral-bench
    fix); existing instances keep their frozen mount."""

    def _fresh(self, monkeypatch, tmp_path, *, bench_parent="/workspace"):
        s = instance_setup(monkeypatch, tmp_path, seed_compose=False)
        monkeypatch.setattr(
            urllib.request,
            "urlretrieve",
            lambda url, dest: dest.write_text(COMPOSE_UPSTREAM),
        )
        core_init.init_instance(PROJECT, port=18000, bench_parent=bench_parent)
        return s.compose_path.read_text()

    def test_default_parent_binds_data_dir_at_workspace(self, monkeypatch, tmp_path, patched):
        content = self._fresh(monkeypatch, tmp_path)
        assert "- ../data:/workspace:cached" in content
        assert "- ..:/workspace:cached" not in content
        # working_dir sits at the mount root, inside the mounted subtree.
        assert "working_dir: /workspace\n" in content
        assert "/workspace/development" not in content

    def test_custom_parent_binds_data_dir_at_that_path(self, monkeypatch, tmp_path, patched):
        content = self._fresh(monkeypatch, tmp_path, bench_parent="/opt/benches")
        assert "- ../data:/opt/benches:cached" in content
        assert "working_dir: /opt/benches\n" in content

    def test_no_new_named_volume_and_mariadb_untouched(self, monkeypatch, tmp_path, patched):
        content = self._fresh(monkeypatch, tmp_path)
        # The bind mount adds nothing to the volumes: block.
        assert content.split("volumes:")[-1].strip() == "mariadb-data:"
        assert "- mariadb-data:/var/lib/mysql" in content

    def test_host_data_dir_is_created(self, monkeypatch, tmp_path, patched):
        self._fresh(monkeypatch, tmp_path)
        assert (tmp_path / PROJECT / "data").is_dir()

    def test_reinit_mismatched_parent_is_usage_error_naming_mounted_parent(
        self, monkeypatch, tmp_path, patched
    ):
        # An existing instance whose frozen compose mounts /workspace.
        s = instance_setup(monkeypatch, tmp_path)
        s.compose_path.write_text(COMPOSE_UPSTREAM)
        with pytest.raises(CwcliError) as exc:
            core_init.init_instance(PROJECT, port=18000, bench_parent="/opt/elsewhere")
        assert exc.value.kind is ErrorKind.USAGE
        assert exc.value.code == "bench_parent.mismatch"
        assert "/workspace" in exc.value.message
        # No containers were touched.
        assert s.host_calls == []

    def test_reinit_matching_parent_proceeds_without_rewriting_frozen_compose(
        self, monkeypatch, tmp_path, patched
    ):
        s = instance_setup(monkeypatch, tmp_path)
        s.compose_path.write_text(COMPOSE_UPSTREAM)
        result = core_init.init_instance(PROJECT, port=18000, bench_parent="/workspace")
        assert result.status is Status.OK
        # Frozen: the old bind mount is preserved byte-for-byte, not re-targeted.
        content = s.compose_path.read_text()
        assert "- ..:/workspace:cached" in content
        assert "../data" not in content


# ------------------------------------------------------------------ stage 2


class TestInitBenchTriState:
    def test_fresh_bench_creates_everything(self, monkeypatch, patched):
        container = FakeContainer()
        use_container(monkeypatch, container)
        result = core_init.init_bench(PROJECT, **bench_kwargs())

        assert result.status is Status.OK
        report = result.data
        assert report is not None
        assert report.bench_created is True
        assert report.site_created is True
        assert report.erpnext_installed is False
        assert report.bench_path == BENCH_PATH
        assert patched.added == [BENCH_PATH]
        assert patched.cleared == [PROJECT]

    def test_existing_bench_with_reuse_true_skips_bench_init(self, monkeypatch, patched):
        container = FakeContainer(dirs_exist={BENCH_PATH})
        use_container(monkeypatch, container)
        result = core_init.init_bench(PROJECT, **bench_kwargs(reuse_bench=True))

        assert result.data is not None
        assert result.data.bench_created is False
        assert result.data.site_created is True
        commands = [c["command"] for c in container.client.api.exec_calls]
        assert not any("bench init" in c for c in commands)
        assert patched.added == [BENCH_PATH]

    def test_existing_bench_with_reuse_false_is_conflict(self, monkeypatch, patched):
        container = FakeContainer(dirs_exist={BENCH_PATH})
        use_container(monkeypatch, container)
        with pytest.raises(CwcliError) as exc:
            core_init.init_bench(PROJECT, **bench_kwargs(reuse_bench=False))
        assert exc.value.kind is ErrorKind.CONFLICT
        assert exc.value.code == "bench.exists"
        assert exc.value.message == (
            f"Bench '{BENCH_PATH}' already exists and --no-reuse-bench was given. "
            "Pass a different --bench name to create a new bench."
        )
        # Refusal happens before any bench work or search-path registration.
        assert container.client.api.exec_calls == []
        assert patched.added == []

    def test_existing_bench_with_no_answer_is_confirm_reuse_bench(self, monkeypatch, patched):
        container = FakeContainer(dirs_exist={BENCH_PATH})
        use_container(monkeypatch, container)
        result = core_init.init_bench(PROJECT, **bench_kwargs(reuse_bench=None))

        assert result.status is Status.NEEDS_CHOICE
        choice = result.choice
        assert choice is not None
        assert choice.kind == "confirm_reuse_bench"
        assert choice.param == "reuse_bench"
        assert choice.options == [{"value": "frappe-bench", "label": BENCH_PATH}]
        # The decision precedes registration and every exec.
        assert patched.added == []
        assert container.client.api.exec_calls == []

    def test_fresh_bench_with_reuse_false_proceeds(self, monkeypatch, patched):
        # --no-reuse-bench against a fresh name just asserts freshness.
        container = FakeContainer()
        use_container(monkeypatch, container)
        result = core_init.init_bench(PROJECT, **bench_kwargs(reuse_bench=False))
        assert result.data is not None
        assert result.data.bench_created is True


class TestInitBenchChoicesAndErrors:
    def test_stopped_container_is_confirm_start_race_backstop(self, monkeypatch, patched):
        container = FakeContainer(status="exited")
        use_container(monkeypatch, container)
        result = core_init.init_bench(PROJECT, **bench_kwargs())
        assert result.status is Status.NEEDS_CHOICE
        assert result.choice is not None
        assert result.choice.kind == "confirm_start"

    def test_mkdir_failure_is_typed_precondition(self, monkeypatch, patched):
        container = FakeContainer(exec_run_responses={"mkdir -p": (1, b"denied")})
        use_container(monkeypatch, container)
        with pytest.raises(CwcliError) as exc:
            core_init.init_bench(PROJECT, **bench_kwargs())
        assert exc.value.kind is ErrorKind.PRECONDITION
        assert exc.value.code == "init.mkdir_failed"
        assert "Failed to create directory '/workspace': denied" in exc.value.message

    def test_failed_exec_is_typed_with_todays_message(self, monkeypatch, patched):
        api = FakeApi(fail_command="set-config -g db_host", fail_output=b"nope")
        container = FakeContainer(api=api)
        use_container(monkeypatch, container)
        with pytest.raises(CwcliError) as exc:
            core_init.init_bench(PROJECT, **bench_kwargs())
        assert exc.value.kind is ErrorKind.PRECONDITION
        assert exc.value.code == "init.exec_failed"
        assert exc.value.message.startswith("Command failed with exit code 1: ")

    def test_enospc_on_drained_exec_is_disk_full(self, monkeypatch, patched):
        api = FakeApi(fail_command="bench init", fail_output=b"fatal: ENOSPC")
        container = FakeContainer(api=api)
        use_container(monkeypatch, container)
        with pytest.raises(CwcliError) as exc:
            core_init.init_bench(PROJECT, **bench_kwargs())
        assert exc.value.code == "init.disk_full"
        assert exc.value.message == (
            "No space left on device inside the container. " "Free up disk space and try again."
        )

    def test_enospc_on_streamed_exec_is_disk_full(self, monkeypatch, patched):
        # stream_output=True renders chunks live (nothing fully buffered), but a
        # bounded tail still feeds the ENOSPC scan - disk exhaustion is most
        # likely during a long streaming bench build, so the hint must fire here.
        api = FakeApi(fail_command="bench init", fail_output=b"fatal: ENOSPC")
        container = FakeContainer(api=api)
        use_container(monkeypatch, container)
        with pytest.raises(CwcliError) as exc:
            core_init.init_bench(PROJECT, **bench_kwargs(stream_output=True))
        assert exc.value.code == "init.disk_full"

    def test_lost_stream_is_typed_docker_never_exit_code_none(self, monkeypatch, patched):
        # The batch's disclosed error-path hardening: a dropped connection used
        # to print "Command failed with exit code None"; it is now the
        # contract's honest typed DOCKER error.
        monkeypatch.setattr(exec_stream_mod, "_EXIT_CODE_POLL_TIMEOUT", 0.01)
        monkeypatch.setattr(exec_stream_mod, "_EXIT_CODE_POLL_INTERVAL", 0.001)
        api = FakeApi(lose_stream_on="bench init")
        container = FakeContainer(api=api)
        use_container(monkeypatch, container)
        with pytest.raises(CwcliError) as exc:
            core_init.init_bench(PROJECT, **bench_kwargs())
        assert exc.value.kind is ErrorKind.DOCKER
        assert exc.value.code == "exec.stream_lost"
        assert "None" not in exc.value.message


class TestExecOrderAndSecrets:
    def test_exec_order_is_preserved(self, monkeypatch, patched):
        container = FakeContainer()
        use_container(monkeypatch, container)
        core_init.init_bench(PROJECT, **bench_kwargs(install_erpnext=True))

        commands = [c["command"] for c in container.client.api.exec_calls]
        assert commands == [
            "cd /workspace && bench init --skip-redis-config-generation "
            "--frappe-branch version-16 frappe-bench --verbose",
            "cd /workspace/frappe-bench && bench set-config -g db_host mariadb",
            "cd /workspace/frappe-bench && bench set-config -g redis_cache "
            "redis://redis-cache:6379",
            "cd /workspace/frappe-bench && bench set-config -g redis_queue "
            "redis://redis-queue:6379",
            "cd /workspace/frappe-bench && bench set-config -g redis_socketio "
            "redis://redis-queue:6379",
            "cd /workspace/frappe-bench && bench new-site --db-root-password "
            '"$CWCLI_DB_ROOT_PASSWORD" --admin-password "$CWCLI_ADMIN_PASSWORD" '
            "--mariadb-user-host-login-scope=% development.localhost --verbose",
            "cd /workspace/frappe-bench && bench --site development.localhost "
            "set-config developer_mode 1",
            "cd /workspace/frappe-bench && bench set-config -g server_script_enabled 1",
            "cd /workspace/frappe-bench && bench get-app --branch version-16 "
            "--resolve-deps erpnext",
            "cd /workspace/frappe-bench && bench --site development.localhost "
            "install-app erpnext",
        ]

    def test_secrets_ride_environment_and_reach_no_surface(self, monkeypatch, patched):
        # The Decision 3 audit, pinned: values in environment= ONLY; no event,
        # warning, DTO field, or command-echo trace carries either secret.
        secret = "s3cret-admin-pw"
        db_secret = "db-r00t-pw"
        events: list = []
        container = FakeContainer()
        use_container(monkeypatch, container)
        result = core_init.init_bench(
            PROJECT,
            **bench_kwargs(admin_password=secret, db_root_password=db_secret),
            on_event=events.append,
        )

        new_site = next(
            c for c in container.client.api.exec_calls if "bench new-site" in c["command"]
        )
        assert new_site["environment"] == {
            "CWCLI_DB_ROOT_PASSWORD": db_secret,
            "CWCLI_ADMIN_PASSWORD": secret,
        }
        assert '"$CWCLI_ADMIN_PASSWORD"' in new_site["command"]
        assert secret not in new_site["command"]

        for event in events:
            blob = repr(dataclasses.asdict(event))
            assert secret not in blob
            assert db_secret not in blob

        report_blob = repr(dataclasses.asdict(result.data))
        assert secret not in report_blob
        assert db_secret not in report_blob
        for warning in result.warnings:
            assert secret not in repr(warning)

    def test_command_echo_trace_carries_dollar_refs(self, monkeypatch, patched):
        events: list = []
        container = FakeContainer()
        use_container(monkeypatch, container)
        core_init.init_bench(PROJECT, **bench_kwargs(), on_event=events.append)

        echoes = [
            e.text
            for e in events
            if isinstance(e, core_init.InitTrace) and e.code == "exec.command"
        ]
        new_site_echo = next(e for e in echoes if "bench new-site" in e)
        assert '"$CWCLI_ADMIN_PASSWORD"' in new_site_echo
        assert '"$CWCLI_DB_ROOT_PASSWORD"' in new_site_echo

    def test_idempotent_rerun_skips_new_site(self, monkeypatch, patched):
        container = FakeContainer(dirs_exist={BENCH_PATH, SITE_PATH})
        use_container(monkeypatch, container)
        result = core_init.init_bench(PROJECT, **bench_kwargs(reuse_bench=True))
        assert result.data is not None
        assert result.data.site_created is False
        commands = [c["command"] for c in container.client.api.exec_calls]
        assert not any("bench new-site" in c for c in commands)


class TestVersionGating:
    def _v14_container(self, api=None):
        return FakeContainer(
            api=api,
            exec_run_responses={
                "ls ~/.pyenv/versions": (0, b"3.10.14 3.11.2"),
                "ls ~/.nvm/versions/node/": (0, b"v16.20.2 v22.22.0"),
                "npm install -g yarn": (0, b""),
            },
        )

    def test_v14_gates_python_node_and_mariadb_flag(self, monkeypatch, patched):
        container = self._v14_container()
        use_container(monkeypatch, container)
        core_init.init_bench(PROJECT, **bench_kwargs(frappe_ref="version-14"))

        commands = [c["command"] for c in container.client.api.exec_calls]
        bench_init = next(c for c in commands if "bench init" in c)
        assert "PYENV_VERSION=3.10.14" in bench_init
        assert "nvm use v16.20.2" in bench_init
        new_site = next(c for c in commands if "bench new-site" in c)
        assert "--no-mariadb-socket" in new_site
        assert "--mariadb-user-host-login-scope" not in new_site

    def test_v14_tag_gates_like_its_branch(self, monkeypatch, patched):
        container = self._v14_container()
        use_container(monkeypatch, container)
        core_init.init_bench(PROJECT, **bench_kwargs(frappe_ref="v14.80.0"))
        commands = [c["command"] for c in container.client.api.exec_calls]
        new_site = next(c for c in commands if "bench new-site" in c)
        assert "--no-mariadb-socket" in new_site

    def test_v16_uses_container_defaults(self, monkeypatch, patched):
        container = FakeContainer()
        use_container(monkeypatch, container)
        core_init.init_bench(PROJECT, **bench_kwargs())
        commands = [c["command"] for c in container.client.api.exec_calls]
        bench_init = next(c for c in commands if "bench init" in c)
        assert "PYENV_VERSION" not in bench_init
        assert "nvm use" not in bench_init
        # No pyenv/nvm probes at all on the modern default.
        assert not any("pyenv" in s for s in container.exec_run_calls)

    def test_yarn_failure_is_a_warning_not_an_error(self, monkeypatch, patched):
        container = FakeContainer(
            exec_run_responses={
                "ls ~/.pyenv/versions": (0, b"3.10.14"),
                "ls ~/.nvm/versions/node/": (0, b"v16.20.2"),
                "npm install -g yarn": (1, b"yarn kaboom"),
            },
        )
        use_container(monkeypatch, container)
        result = core_init.init_bench(PROJECT, **bench_kwargs(frappe_ref="version-14"))
        assert result.status is Status.WARNING
        assert any(w.code == "yarn.install_failed" for w in result.warnings)
        # The run still completed.
        assert result.data is not None and result.data.site_created is True

    def test_pyenv_install_failure_is_soft(self, monkeypatch, patched):
        container = FakeContainer(
            exec_run_responses={
                "ls ~/.pyenv/versions": (0, b""),  # nothing installed
                "pyenv install --list": (0, b"  3.10.13\n  3.10.14\n"),
                "pyenv install 3.10.14": (1, b"build failed"),
                "ls ~/.nvm/versions/node/": (0, b"v16.20.2"),
                "npm install -g yarn": (0, b""),
            },
        )
        use_container(monkeypatch, container)
        events: list = []
        result = core_init.init_bench(
            PROJECT, **bench_kwargs(frappe_ref="version-14"), on_event=events.append
        )
        assert result.status is Status.WARNING
        assert any(w.code == "python.install_failed" for w in result.warnings)
        # bench init ran WITHOUT the pyenv prefix (soft fall-through, as today).
        commands = [c["command"] for c in container.client.api.exec_calls]
        bench_init = next(c for c in commands if "bench init" in c)
        assert "PYENV_VERSION" not in bench_init
        notices = [e for e in events if isinstance(e, core_init.InitNotice)]
        assert any(n.code == "python.installing" for n in notices)

    def test_v13_pins_setuptools_and_failure_is_a_warning(self, monkeypatch, patched):
        container = FakeContainer(
            exec_run_responses={
                "ls ~/.pyenv/versions": (0, b"3.9.19"),
                "ls ~/.nvm/versions/node/": (0, b"v14.21.3"),
                "npm install -g yarn": (0, b""),
                "setuptools<82": (1, b"pip exploded"),
            },
        )
        use_container(monkeypatch, container)
        result = core_init.init_bench(PROJECT, **bench_kwargs(frappe_ref="version-13"))
        assert any("setuptools<82" in s for s in container.exec_run_calls)
        assert any(w.code == "setuptools.pin_failed" for w in result.warnings)


class TestAxiInitVerbIsRegistered:
    """The ``axi init`` verb SHIPPED (``add-axi-init-verb``, captain-approved
    2026-07-16 in principle, go 2026-07-17). It was DEFERRED, never refused, by
    ``migrate-init-core`` design Decision 9: unlike ``axi open`` (structurally
    impossible - execvp destroys the process) this verb is buildable, and the
    two-call core shape made it thin. It is a thin TOON-rendering frontend over
    the UNCHANGED ``core.init_instance`` then ``core.init_bench``. This test
    replaces the former deferral assertion so the shipped verb cannot silently
    regress out of the registry."""

    def test_axi_registry_has_an_init_command(self):
        from caffeinated_whale_cli.commands import axi as axi_mod

        registered = {c.name for c in axi_mod.app.registered_commands}
        assert "init" in registered


class TestCorePurity:
    def test_the_core_prints_nothing_at_all(self, monkeypatch, tmp_path, patched, capsys):
        s = instance_setup(monkeypatch, tmp_path)
        core_init.init_instance(PROJECT, port=18000)
        container = FakeContainer()
        use_container(monkeypatch, container)
        core_init.init_bench(PROJECT, **bench_kwargs(install_erpnext=True))
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""
        assert s.host_calls  # the run actually did the work

    def test_dtos_are_plain_data(self, monkeypatch, patched):
        container = FakeContainer()
        use_container(monkeypatch, container)
        result = core_init.init_bench(PROJECT, **bench_kwargs())
        report = dataclasses.asdict(result.data)
        assert report == {
            "project": PROJECT,
            "bench_name": "frappe-bench",
            "bench_path": BENCH_PATH,
            "site_name": SITE,
            "bench_created": True,
            "site_created": True,
            "erpnext_installed": False,
        }
        up = dataclasses.asdict(core_init.InstanceUp(project=PROJECT, conf_dir="/x"))
        assert up == {"project": PROJECT, "conf_dir": "/x"}
