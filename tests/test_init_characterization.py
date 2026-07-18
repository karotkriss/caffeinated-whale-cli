"""Command-level characterization of ``cwcli init`` (the batch-9 green-before net).

Written against UNMIGRATED code and required to pass UNCHANGED against the
migrated code (batch 4's discipline): every patch target is a seam that
survives the migration, never a private helper that moves with it.

The seams, and why each survives:

- ``core.docker.get_frappe_container`` - the single container-resolution
  implementation; the CLI wrapper resolves it as a module attribute at call
  time, and the migrated core calls the same attribute.
- ``config_utils`` / ``db_utils`` module attributes (``PROJECTS_DIR``,
  ``get_show_tips``, ``add_custom_path``, ``clear_cache_for_project``) - both
  sides read them off the shared module object at call time.
- ``subprocess.run`` / ``urllib.request`` - stdlib module attributes; the host
  compose calls and the Docker Hub tag lookup go through them on both sides.
- the container fake's own ``client.api`` (``exec_create``/``exec_start``/
  ``exec_inspect``) and ``exec_run`` - the Docker SDK surface itself.
- ``sys.stdin.isatty`` and the frontend's ``_is_interactive_session`` (a helper
  the design keeps in the frontend) for TTY-ness.
- a REAL occupied socket for the port conflict - ``check_ports_in_use`` binds,
  so an actually-listening socket is in use on any implementation.
"""

import random
import shlex
import socket
import subprocess
import urllib.request
from types import SimpleNamespace

import pytest
import typer

import caffeinated_whale_cli.commands.init as init_mod
from caffeinated_whale_cli.core import docker as core_docker
from caffeinated_whale_cli.core.envelope import Status
from caffeinated_whale_cli.utils import config_utils, db_utils, port_utils

PROJECT = "proj"
BENCH_PATH = "/workspace/frappe-bench"
SITE_PATH = f"{BENCH_PATH}/sites/development.localhost"

COMPOSE_TEMPLATE = """services:
  frappe:
    image: docker.io/frappe/bench:latest
    ports:
      - "8000-8005:8000-8005"
      - "9000-9005:9000-9005"
"""


class FakeApi:
    """Records every ``exec_create`` and answers ``exec_start``/``exec_inspect``.

    ``fail_command`` marks the one command (by substring) whose exec fails with
    ``fail_output`` / exit 1; everything else succeeds silently. Handles both
    the buffered (``stream=False``) and streamed (``stream=True, demux=True``)
    start modes so the same fake serves both sides of the migration.
    """

    def __init__(self, fail_command: str | None = None, fail_output: bytes = b""):
        self.exec_calls: list[dict] = []
        self.fail_command = fail_command
        self.fail_output = fail_output
        self._fails: dict[str, bool] = {}

    def exec_create(self, container_id, cmd, **kwargs):
        command = cmd[2] if isinstance(cmd, (list, tuple)) and len(cmd) == 3 else " ".join(cmd)
        exec_id = f"exec-{len(self.exec_calls)}"
        self.exec_calls.append({"command": command, "environment": kwargs.get("environment")})
        self._fails[exec_id] = self.fail_command is not None and self.fail_command in command
        return {"Id": exec_id}

    def exec_start(self, exec_id, stream=False, demux=False, **kwargs):
        output = self.fail_output if self._fails[exec_id] else b""
        if stream:
            return iter([(output, None)] if output else [])
        return output

    def exec_inspect(self, exec_id):
        return {"ExitCode": 1 if self._fails[exec_id] else 0, "Running": False}


class FakeContainer:
    """A running frappe container whose probes answer from ``dirs_exist``."""

    def __init__(self, api: FakeApi, dirs_exist=()):
        self.id = "fake-frappe"
        self.status = "running"
        self.labels = {"com.docker.compose.service": "frappe"}
        self.client = SimpleNamespace(api=api)
        self.dirs_exist = set(dirs_exist)

    def reload(self):
        pass

    def exec_run(self, cmd, **kwargs):
        script = cmd[2] if isinstance(cmd, (list, tuple)) and len(cmd) == 3 else ""
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "test":
            # argv-form probe (["test", "-d", path])
            return (0 if cmd[2] in self.dirs_exist else 1), b""
        if script.startswith("mkdir -p"):
            return 0, b""
        if script.startswith("test -d"):
            path = shlex.split(script)[2]
            return (0 if path in self.dirs_exist else 1), b""
        return 0, b""


def _free_base_port() -> int:
    """A starting port whose full init window (12 ports) is genuinely free."""
    for _ in range(50):
        base = random.randint(20000, 40000)
        ports = list(range(base, base + 6)) + list(range(base + 1000, base + 1006))
        if all(not port_utils.is_port_in_use(p) for p in ports):
            return base
    raise RuntimeError("no free port window found")


def run_init(
    monkeypatch,
    tmp_path,
    *,
    bench_exists=False,
    site_exists=False,
    fail_command=None,
    fail_output=b"",
    add_path_result=True,
    base_port=None,
    interactive=None,
    host_fail_stage=None,
    host_fail_output=b"",
    **overrides,
):
    """Drive the real ``init`` body through migration-surviving seams only.

    Returns a namespace carrying the fake api (exec order/commands), the
    rewritten compose path, and the recorded host/cache/add-path calls.
    """
    api = FakeApi(fail_command=fail_command, fail_output=fail_output)
    dirs = set()
    if bench_exists:
        dirs.add(BENCH_PATH)
    if site_exists:
        dirs.add(SITE_PATH)
    container = FakeContainer(api, dirs_exist=dirs)

    host_calls: list[dict] = []
    cleared: list[str] = []
    added_paths: list[str] = []
    downloads: list[str] = []

    def fake_run(cmd, cwd=None, capture_output=False, **kwargs):
        host_calls.append({"cmd": list(cmd), "cwd": cwd})
        if host_fail_stage is not None and host_fail_stage in cmd:
            return SimpleNamespace(returncode=1, stdout=b"", stderr=host_fail_output)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    def fake_add_custom_path(path):
        added_paths.append(path)
        return add_path_result

    def fail_urlopen(*a, **k):
        raise OSError("offline (characterization)")

    def fake_urlretrieve(url, dest):
        downloads.append(url)

    monkeypatch.setattr(core_docker, "get_frappe_container", lambda name: container)
    monkeypatch.setattr(config_utils, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(config_utils, "get_show_tips", lambda: False)
    monkeypatch.setattr(config_utils, "add_custom_path", fake_add_custom_path)
    monkeypatch.setattr(db_utils, "clear_cache_for_project", lambda name: cleared.append(name))
    # The post-success recache (a real `core.inspect`) is out of scope for this
    # exec-order/command characterization; stub it to a no-op success.
    monkeypatch.setattr(init_mod.cache, "recache_project", lambda *a, **k: True)
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(urllib.request, "urlopen", fail_urlopen)
    monkeypatch.setattr(urllib.request, "urlretrieve", fake_urlretrieve)
    if interactive is not None:
        monkeypatch.setattr(init_mod, "_is_interactive_session", lambda: interactive)

    # Pre-seed the compose file so the download is skipped (the skip-when-present
    # behavior is itself under characterization via `downloads`).
    conf_dir = tmp_path / PROJECT / "conf"
    conf_dir.mkdir(parents=True, exist_ok=True)
    compose_path = conf_dir / "docker-compose.yml"
    compose_path.write_text(COMPOSE_TEMPLATE)

    port = base_port if base_port is not None else _free_base_port()
    params = dict(
        project_name=PROJECT,
        port=port,
        bench_name="frappe-bench",
        site_name="development.localhost",
        bench_parent="/workspace",
        frappe_branch=None,
        version=None,
        db_root_password="123",
        admin_password="pw-x",
        auto_start=False,
        start_services=False,
        reuse_bench=None,
        verbose=False,
        install_erpnext=False,
        erpnext_branch="version-16",
    )
    params.update(overrides)

    init_mod.init.__wrapped__(**params)

    return SimpleNamespace(
        api=api,
        port=port,
        compose_path=compose_path,
        conf_dir=conf_dir,
        host_calls=host_calls,
        cleared=cleared,
        added_paths=added_paths,
        downloads=downloads,
    )


class TestExecOrderAndCommands:
    """The 10-exec order and exact command strings, pinned byte-for-byte."""

    def test_full_init_exec_order_and_exact_command_strings(self, monkeypatch, tmp_path):
        r = run_init(monkeypatch, tmp_path, install_erpnext=True)

        commands = [c["command"] for c in r.api.exec_calls]
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

        # The secrets ride ONLY on the new-site exec's environment.
        new_site = r.api.exec_calls[5]
        assert new_site["environment"] == {
            "CWCLI_DB_ROOT_PASSWORD": "123",
            "CWCLI_ADMIN_PASSWORD": "pw-x",
        }
        for call in r.api.exec_calls[:5] + r.api.exec_calls[6:]:
            assert not call["environment"]

    def test_host_compose_calls_and_compose_rewrite(self, monkeypatch, tmp_path, capsys):
        r = run_init(monkeypatch, tmp_path)

        # Host side: pull (quiet in non-verbose) then up -d, both from conf/.
        assert [c["cmd"] for c in r.host_calls] == [
            ["docker", "compose", "-p", PROJECT, "-f", "docker-compose.yml", "pull", "--quiet"],
            ["docker", "compose", "-p", PROJECT, "-f", "docker-compose.yml", "up", "-d"],
        ]
        assert all(c["cwd"] == str(r.conf_dir) for c in r.host_calls)

        # The pre-existing compose file is customized in place, never re-downloaded.
        assert r.downloads == []
        content = r.compose_path.read_text()
        assert f"{r.port}-{r.port+5}:8000-8005" in content
        assert f"{r.port+1000}-{r.port+1005}:9000-9005" in content
        # Docker Hub is unreachable (patched), so the image pin fails open to the
        # known-good fallback tag rather than staying on :latest.
        assert "docker.io/frappe/bench:v5.29.1" in content
        assert ":latest" not in content

        # The project's stale cache is cleared exactly once.
        assert r.cleared == [PROJECT]

        out = capsys.readouterr().out
        assert "Successfully initialized bench 'frappe-bench'" in out
        assert f"Bench path: {BENCH_PATH}" in out


class TestSkipOnExists:
    """Idempotent re-run gating: what exists is skipped, not re-created."""

    def test_existing_bench_with_reuse_flag_skips_bench_init(self, monkeypatch, tmp_path):
        r = run_init(monkeypatch, tmp_path, bench_exists=True, reuse_bench=True)
        commands = [c["command"] for c in r.api.exec_calls]
        assert not any("bench init" in c for c in commands)
        # Site setup still runs on the reused bench.
        assert any("bench new-site" in c for c in commands)

    def test_existing_site_skips_new_site_and_generated_password_print(
        self, monkeypatch, tmp_path, capsys
    ):
        # Interactive with no --admin-password: a password IS generated, but the
        # site already exists so new-site is skipped and the print would be a lie.
        r = run_init(
            monkeypatch,
            tmp_path,
            site_exists=True,
            admin_password=None,
            interactive=True,
        )
        commands = [c["command"] for c in r.api.exec_calls]
        assert not any("bench new-site" in c for c in commands)
        out = capsys.readouterr().out
        assert "Administrator password (generated)" not in out


class TestPortConflict:
    """The three-line port-conflict error, driven by a genuinely occupied port."""

    def test_port_conflict_prints_tip_and_exits_1(self, monkeypatch, tmp_path, capsys):
        base = _free_base_port()
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("0.0.0.0", base))
        blocker.listen(1)
        try:
            with pytest.raises(typer.Exit) as exc:
                run_init(monkeypatch, tmp_path, base_port=base)
        finally:
            blocker.close()

        assert exc.value.exit_code == 1
        err = capsys.readouterr().err
        assert "already in use" in err
        assert str(base) in err
        assert "--port" in err
        assert f"Example: cwcli init {PROJECT} --port 10000" in err


class TestComposeFailureOutput:
    """A failed ``docker compose`` host command's captured stderr must print
    exactly once, in both verbose and non-verbose mode.

    ``core.init``'s ``_run_host_command`` always emits the captured
    stdout/stderr as ``InitOutput`` events (regardless of verbosity) AND
    attaches it to the raised ``CwcliError``'s ``detail["output"]``. The
    verbose renderer (``_InitRenderer._on_output``) prints ``InitOutput``
    events live for compose's phases; ``_render_error_exit`` must not also
    print ``detail["output"]`` in that mode, or a compose failure's stderr
    shows up twice on the same run.
    """

    def test_verbose_compose_failure_prints_stderr_once(self, monkeypatch, tmp_path, capsys):
        fail_text = "ERROR: pull access denied for frappe/bench, repository does not exist"
        with pytest.raises(typer.Exit) as exc:
            run_init(
                monkeypatch,
                tmp_path,
                host_fail_stage="up",
                host_fail_output=fail_text.encode(),
                verbose=True,
            )
        assert exc.value.exit_code == 1
        err = capsys.readouterr().err
        assert err.count(fail_text) == 1

    def test_non_verbose_compose_failure_prints_stderr_once(self, monkeypatch, tmp_path, capsys):
        fail_text = "ERROR: pull access denied for frappe/bench, repository does not exist"
        with pytest.raises(typer.Exit) as exc:
            run_init(
                monkeypatch,
                tmp_path,
                host_fail_stage="up",
                host_fail_output=fail_text.encode(),
                verbose=False,
            )
        assert exc.value.exit_code == 1
        err = capsys.readouterr().err
        assert err.count(fail_text) == 1


class TestRefusals:
    """Non-interactive mode: every prompt has a flag; no flag means honest exit 1."""

    def test_non_tty_without_admin_password_refuses(self, monkeypatch, tmp_path, capsys):
        with pytest.raises(typer.Exit) as exc:
            run_init(monkeypatch, tmp_path, admin_password=None, interactive=False)
        assert exc.value.exit_code == 1
        err = capsys.readouterr().err
        assert "--admin-password" in err

    def test_no_reuse_bench_on_existing_bench_refuses(self, monkeypatch, tmp_path, capsys):
        with pytest.raises(typer.Exit) as exc:
            run_init(monkeypatch, tmp_path, bench_exists=True, reuse_bench=False)
        assert exc.value.exit_code == 1
        err = capsys.readouterr().err
        assert "already exists" in err
        assert "--no-reuse-bench" in err

    def test_non_tty_existing_bench_without_flag_refuses_naming_both_flags(
        self, monkeypatch, tmp_path, capsys
    ):
        monkeypatch.setattr(init_mod.sys.stdin, "isatty", lambda: False)
        with pytest.raises(typer.Exit) as exc:
            run_init(monkeypatch, tmp_path, bench_exists=True, reuse_bench=None)
        assert exc.value.exit_code == 1
        err = capsys.readouterr().err
        assert "--reuse-bench" in err
        assert "--no-reuse-bench" in err


class TestEnospcMessage:
    """A failed drained exec whose output shows ENOSPC gets the actionable message."""

    def test_enospc_on_drained_exec_prints_disk_message(self, monkeypatch, tmp_path, capsys):
        with pytest.raises(typer.Exit) as exc:
            run_init(
                monkeypatch,
                tmp_path,
                fail_command="bench init",
                fail_output=b"fatal: write error: No space left on device\nENOSPC",
            )
        assert exc.value.exit_code == 1
        err = capsys.readouterr().err
        assert "No space left on device" in err
        assert "Free up disk space" in err


class TestAutoStartServices:
    """After a successful create, init starts the bench dev services by default
    (reusing ``core.start``) and its completion message reflects the running
    state; ``--no-start`` skips the start and points at ``cwcli start``."""

    def _patch_start(self, monkeypatch, *, status):
        calls: list[dict] = []

        def fake_start(project_name, **kwargs):
            calls.append({"project": project_name, **kwargs})
            return SimpleNamespace(status=status)

        monkeypatch.setattr(init_mod.core_start, "start", fake_start)
        return calls

    def test_default_starts_services_and_reports_running(self, monkeypatch, tmp_path, capsys):
        calls = self._patch_start(monkeypatch, status=Status.OK)
        run_init(monkeypatch, tmp_path, start_services=True)

        # core.start was reused with the exact created bench path (no re-resolve).
        assert calls == [{"project": PROJECT, "bench_path": BENCH_PATH}]
        out = capsys.readouterr().out
        assert "Dev services are running" in out
        assert "http://development.localhost:8000" in out
        assert f"cwcli logs {PROJECT}" in out
        assert f"cwcli stop {PROJECT}" in out
        assert f"cwcli restart {PROJECT}" in out
        # The old "Once services are running" implication is gone.
        assert "Once services are running" not in out

    def test_no_start_skips_and_points_at_start(self, monkeypatch, tmp_path, capsys):
        calls = self._patch_start(monkeypatch, status=Status.OK)
        run_init(monkeypatch, tmp_path, start_services=False)

        assert calls == []  # never called
        out = capsys.readouterr().out
        assert "Dev services were not started (--no-start)" in out
        assert f"cwcli start {PROJECT}" in out

    def test_start_failure_degrades_to_warning_not_exit(self, monkeypatch, tmp_path, capsys):
        from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind

        def boom(project_name, **kwargs):
            raise CwcliError(ErrorKind.DOCKER, "docker.x", "daemon unreachable")

        monkeypatch.setattr(init_mod.core_start, "start", boom)
        # Must NOT raise: the bench was created; a start failure only warns.
        run_init(monkeypatch, tmp_path, start_services=True)
        captured = capsys.readouterr()
        assert "could not be" in captured.err
        assert "Dev services are not running" in captured.out


class TestAddPathLine:
    """The search-path registration line renders on stdout, on both outcomes."""

    def test_added_line(self, monkeypatch, tmp_path, capsys):
        r = run_init(monkeypatch, tmp_path, add_path_result=True)
        assert r.added_paths == [BENCH_PATH]
        out = capsys.readouterr().out
        assert f"Added '{BENCH_PATH}' to custom search paths." in out

    def test_already_present_line(self, monkeypatch, tmp_path, capsys):
        r = run_init(monkeypatch, tmp_path, add_path_result=False)
        assert r.added_paths == [BENCH_PATH]
        out = capsys.readouterr().out
        assert f"'{BENCH_PATH}' already exists in custom search paths." in out
