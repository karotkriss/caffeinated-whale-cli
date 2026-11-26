"""``core.log_storage`` - the three log stores behind the Storage page.

Docker container logs (json-file size + rotation, truncate through a root helper that
mounts ONLY each container's log directory, rotation written into the compose file),
bench logs (rotated files removed, current files never touched, a stopped instance
never started), and the host journal (applies / does not apply, usage parsed,
vacuum commands for root). No Docker: containers and the docker client are fakes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from caffeinated_whale_cli.core import log_storage as ls
from caffeinated_whale_cli.core.envelope import Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from caffeinated_whale_cli.core.list import InstanceDTO

COMPOSE = """services:
  mariadb:
    image: docker.io/mariadb:11.8
    command:
      - --character-set-server=utf8mb4
    volumes:
      - mariadb-data:/var/lib/mysql

  # postgresql:
  #   image: postgres:14

  redis-cache:
    image: docker.io/redis:alpine

  frappe:
    image: docker.io/frappe/bench:latest
    command: sleep infinity
    working_dir: /workspace

volumes:
  mariadb-data:
"""


class FakeContainer:
    def __init__(
        self,
        name,
        service,
        *,
        driver="json-file",
        config=None,
        log_path=None,
        image="sha256:img",
        status="running",
        listing=(),
    ):
        self.name = name
        self.status = status
        self.labels = {"com.docker.compose.service": service}
        self.attrs = {
            "HostConfig": {"LogConfig": {"Type": driver, "Config": dict(config or {})}},
            "LogPath": log_path,
            "Image": image,
        }
        self.listing = list(listing)  # [(path, size)] under <bench>/logs
        self.missing_logs: set[str] = set()  # benches whose logs dir does not exist
        self.gone = False  # the container stopped or vanished: docker-py raises
        self.calls: list = []

    def reload(self):
        pass

    def exec_run(self, cmd, workdir=None, environment=None):
        self.calls.append(list(cmd))
        if self.gone:
            raise ls.docker.errors.NotFound("No such container")
        if cmd[0] == "sh" and "find" in cmd[2]:
            bench = cmd[-1]
            if bench in self.missing_logs:
                return 3, b""
            rows = [
                f"{s} {bench}/{p}" if p.startswith("sites/") else f"{s} {bench}/logs/{p}"
                for p, s in self.listing
            ]
            return 0, ("\n".join(rows) + "\n").encode()
        if cmd[0] == "rm":
            return 0, b""
        return 1, b"unexpected"


class FakeClient:
    """``docker.from_env()`` stand-in: records the helper run and answers ``stat``."""

    def __init__(self, stat_lines="", fail=False):
        self.runs: list[dict] = []
        self.stat_lines = stat_lines
        self.fail = fail
        outer = self

        class _Images:
            def list(self, name=None):
                return [type("I", (), {"id": "frappe-bench-image"})()]

        class _Containers:
            def run(self, image, **kw):
                outer.runs.append({"image": image, **kw})
                if outer.fail:
                    raise RuntimeError("helper exploded")
                return outer.stat_lines.encode()

        self.images = _Images()
        self.containers = _Containers()


@pytest.fixture
def projects_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ls.config_utils, "PROJECTS_DIR", tmp_path)
    return tmp_path


# ----------------------------------------------------------------- compose rotation


class TestWithLogRotation:
    def test_every_active_service_gets_one_block_and_nothing_else_changes(self):
        out = ls.with_log_rotation(COMPOSE)
        assert out.count("    logging:\n") == 3  # mariadb, redis-cache, frappe
        assert out.count('        max-size: "10m"\n') == 3
        assert out.count('        max-file: "3"\n') == 3
        # The commented postgresql block and everything outside services are untouched.
        assert "  #   image: postgres:14\n  redis-cache:" in out.replace("\n\n", "\n")
        stripped = "".join(
            line
            for line in out.splitlines(keepends=True)
            if not line.startswith(
                ("    logging:", "      driver:", "      options:", "        max-")
            )
        )
        assert stripped == COMPOSE

    def test_rerunning_replaces_the_existing_block_instead_of_stacking(self):
        once = ls.with_log_rotation(COMPOSE, "10m", 3)
        twice = ls.with_log_rotation(once, "50m", 5)
        assert twice.count("    logging:\n") == 3
        assert '"10m"' not in twice and twice.count('max-size: "50m"') == 3
        assert ls.with_log_rotation(twice, "50m", 5) == twice

    @pytest.mark.parametrize(
        "size,count", [("ten", 3), ("10", 3), ("0m", 3), ("00k", 3), ("10m", 0), ("10m", 101)]
    )
    def test_bad_options_are_usage_errors(self, size, count):
        with pytest.raises(CwcliError) as e:
            ls.with_log_rotation(COMPOSE, size, count)
        assert e.value.kind is ErrorKind.USAGE


class TestSetDockerLogRotation:
    def test_writes_the_compose_file_and_says_it_binds_on_recreate(self, projects_dir):
        conf = projects_dir / "p" / "conf"
        conf.mkdir(parents=True)
        (conf / "docker-compose.yml").write_text(COMPOSE)
        result = ls.set_docker_log_rotation("p", max_size="20m", max_file=2)
        assert result.status is Status.OK
        assert result.data["changed"] is True and result.data["applies_on_recreate"] is True
        assert [w.code for w in result.warnings] == ["logs.rotation_on_recreate"]
        assert ls.compose_rotation("p") == {"max_size": "20m", "max_file": "2"}
        again = ls.set_docker_log_rotation("p", max_size="20m", max_file=2)
        assert again.data["changed"] is False

    def test_a_missing_compose_file_is_not_found(self, projects_dir):
        with pytest.raises(CwcliError) as e:
            ls.set_docker_log_rotation("ghost")
        assert e.value.kind is ErrorKind.NOT_FOUND
        assert ls.compose_rotation("ghost") is None


# --------------------------------------------------------------- docker container logs


def _containers(tmp_path, *, rotation=True, driver="json-file"):
    logs = tmp_path / "containers"
    cid_a, cid_b = "aaa", "bbb"
    (logs / cid_a).mkdir(parents=True)
    (logs / cid_b).mkdir(parents=True)
    (logs / cid_a / f"{cid_a}-json.log").write_bytes(b"x" * 100)
    (logs / cid_a / f"{cid_a}-json.log.1").write_bytes(b"x" * 50)
    (logs / cid_b / f"{cid_b}-json.log").write_bytes(b"x" * 10)
    cfg = {"max-size": "10m", "max-file": "3"} if rotation else {}
    return [
        FakeContainer(
            "p-frappe-1", "frappe", config=cfg, log_path=str(logs / cid_a / f"{cid_a}-json.log")
        ),
        FakeContainer(
            "p-mariadb-1",
            "mariadb",
            driver=driver,
            config=cfg,
            log_path=str(logs / cid_b / f"{cid_b}-json.log"),
        ),
    ]


class TestDockerLogs:
    def test_sizes_read_from_the_host_include_rotated_files(
        self, tmp_path, monkeypatch, projects_dir
    ):
        conts = _containers(tmp_path)
        monkeypatch.setattr(ls.core_docker, "get_project_containers", lambda p: conts)
        report = ls.docker_logs("p")
        assert report.method == "host" and report.total == 160
        frappe = next(r for r in report.containers if r.service == "frappe")
        assert (frappe.size, frappe.files, frappe.rotation, frappe.max_size) == (
            150,
            2,
            True,
            "10m",
        )
        assert report.compose_rotation is None  # no compose file under this home

    def test_an_unreadable_data_root_measures_through_a_read_only_helper(
        self, tmp_path, monkeypatch
    ):
        conts = _containers(tmp_path, rotation=False)
        monkeypatch.setattr(ls.core_docker, "get_project_containers", lambda p: conts)
        monkeypatch.setattr(ls, "_host_sizes", lambda paths: None)
        client = FakeClient(
            "100 /logs/0/aaa-json.log\n50 /logs/0/aaa-json.log.1\n10 /logs/1/bbb-json.log\n"
        )
        monkeypatch.setattr(ls.docker, "from_env", lambda: client)
        report = ls.docker_logs("p")
        assert report.method == "helper" and report.total == 160
        run = client.runs[0]
        assert run["user"] == "0:0" and run["network_mode"] == "none" and run["remove"] is True
        assert all(v["mode"] == "ro" for v in run["volumes"].values())
        assert set(run["volumes"]) == {str(Path(c.attrs["LogPath"]).parent) for c in conts}
        assert not any(r.rotation for r in report.containers)

    def test_a_helper_that_cannot_run_leaves_sizes_unknown_with_a_note(self, tmp_path, monkeypatch):
        conts = _containers(tmp_path)
        monkeypatch.setattr(ls.core_docker, "get_project_containers", lambda p: conts)
        monkeypatch.setattr(ls, "_host_sizes", lambda paths: None)
        monkeypatch.setattr(ls.docker, "from_env", lambda: FakeClient(fail=True))
        report = ls.docker_logs("p")
        assert report.total is None and report.method is None
        assert all(r.size is None for r in report.containers)
        assert "could not be measured" in (report.note or "")

    def test_another_driver_is_reported_not_measured(self, tmp_path, monkeypatch):
        conts = _containers(tmp_path, driver="local")
        monkeypatch.setattr(ls.core_docker, "get_project_containers", lambda p: conts)
        report = ls.docker_logs("p")
        local = next(r for r in report.containers if r.driver == "local")
        assert local.size is None and local.rotation is False
        assert report.total == 150  # only the json-file container counts

    def test_an_unknown_project_and_an_unreachable_daemon_are_typed(self, monkeypatch):
        monkeypatch.setattr(ls.core_docker, "get_project_containers", lambda p: [])
        with pytest.raises(CwcliError) as e:
            ls.docker_logs("p")
        assert e.value.kind is ErrorKind.NOT_FOUND
        monkeypatch.setattr(ls.core_docker, "get_project_containers", lambda p: None)
        with pytest.raises(CwcliError) as e:
            ls.docker_logs("p")
        assert e.value.kind is ErrorKind.DOCKER


class TestTruncateDockerLogs:
    def test_without_consent_it_is_a_choice_and_the_helper_never_runs(self, tmp_path, monkeypatch):
        conts = _containers(tmp_path)
        monkeypatch.setattr(ls.core_docker, "get_project_containers", lambda p: conts)
        client = FakeClient()
        monkeypatch.setattr(ls.docker, "from_env", lambda: client)
        result = ls.truncate_docker_logs("p")
        assert result.status is Status.NEEDS_CHOICE
        assert result.choice.kind == "confirm_truncate_logs" and result.choice.param == "consent"
        assert client.runs == []

    def test_consent_truncates_the_live_file_and_removes_rotated_ones_via_root_helper(
        self, tmp_path, monkeypatch
    ):
        conts = _containers(tmp_path)
        monkeypatch.setattr(ls.core_docker, "get_project_containers", lambda p: conts)
        client = FakeClient()
        monkeypatch.setattr(ls.docker, "from_env", lambda: client)
        result = ls.truncate_docker_logs("p", consent=True)
        assert result.status is Status.OK
        assert result.data == {
            "project": "p",
            "truncated": ["p-frappe-1", "p-mariadb-1"],
            "freed_bytes": 160,
        }
        run = client.runs[-1]
        assert run["user"] == "0:0" and run["network_mode"] == "none"
        assert set(run["volumes"]) == {str(Path(c.attrs["LogPath"]).parent) for c in conts}
        assert all(v["mode"] == "rw" for v in run["volumes"].values())
        script = run["entrypoint"][2]
        assert ': > "$f"' in script and "rm -f /logs/*/*-json.log.*" in script
        # Only the json log files inside the mounted directory are named; never the data root.
        assert "/var/lib/docker" not in script

    def test_unmeasured_sizes_report_freed_bytes_as_unknown_never_zero(self, tmp_path, monkeypatch):
        conts = _containers(tmp_path)
        monkeypatch.setattr(ls.core_docker, "get_project_containers", lambda p: conts)
        monkeypatch.setattr(ls, "_host_sizes", lambda paths: None)
        monkeypatch.setattr(ls.docker, "from_env", lambda: FakeClient(fail=True))
        unmeasured = ls.docker_logs("p")
        monkeypatch.setattr(ls, "docker_logs", lambda p: unmeasured)
        client = FakeClient()
        monkeypatch.setattr(ls.docker, "from_env", lambda: client)
        result = ls.truncate_docker_logs("p", consent=True)
        assert result.data["truncated"] == ["p-frappe-1", "p-mariadb-1"]
        assert result.data["freed_bytes"] is None

    def test_helper_mounts_use_the_daemon_side_posix_path_on_a_windows_host(
        self, tmp_path, monkeypatch, projects_dir
    ):
        from pathlib import PureWindowsPath

        conts = _containers(tmp_path)
        monkeypatch.setattr(ls.core_docker, "get_project_containers", lambda p: conts)
        monkeypatch.setattr(ls, "Path", PureWindowsPath)
        client = FakeClient()
        monkeypatch.setattr(ls.docker, "from_env", lambda: client)
        ls.truncate_docker_logs("p", consent=True)
        posix_dirs = {c.attrs["LogPath"].rsplit("/", 1)[0] for c in conts}
        assert [set(run["volumes"]) for run in client.runs] == [posix_dirs, posix_dirs]

    def test_no_json_file_log_is_an_ok_no_op_with_a_warning(self, tmp_path, monkeypatch):
        conts = _containers(tmp_path, driver="local")
        for c in conts:
            c.attrs["HostConfig"]["LogConfig"]["Type"] = "local"
        monkeypatch.setattr(ls.core_docker, "get_project_containers", lambda p: conts)
        client = FakeClient()
        monkeypatch.setattr(ls.docker, "from_env", lambda: client)
        result = ls.truncate_docker_logs("p", consent=True)
        assert result.status is Status.OK and result.data["truncated"] == []
        assert [w.code for w in result.warnings] == ["logs.nothing_to_truncate"]
        assert client.runs == []


# -------------------------------------------------------------------- bench logs

LISTING = [
    ("web.log", 1000),
    ("web.error.log", 20),
    ("web.log.1", 500),
    ("frappe.log.3.gz", 70),
    ("worker.supervisor.log", 300),
    ("worker.supervisor.log.2", 200),
    ("bench.log", 5),
    (".cwcli-run.sh", 1),
]


class TestBenchLogs:
    @pytest.mark.parametrize(
        "name,rotated",
        [
            ("web.log.1", True),
            ("frappe.log.12.gz", True),
            ("worker.supervisor.log.2", True),
            ("web.log", False),
            ("web.error.log", False),
            ("bench.log", False),
            ("logs.1.log", False),
            (".cwcli-run.sh", False),
        ],
    )
    def test_is_rotated_names_backups_only(self, name, rotated):
        assert ls.is_rotated(f"/workspace/frappe-bench/logs/{name}") is rotated

    def test_bench_logs_splits_current_from_rotated(self):
        c = FakeContainer("p-frappe-1", "frappe", listing=LISTING)
        rep = ls.bench_logs(c, "p", "/workspace/frappe-bench", 0)
        assert rep.current_bytes == 1326 and rep.rotated_bytes == 770 and rep.files == 8
        assert rep.rotated_files == [
            "/workspace/frappe-bench/logs/frappe.log.3.gz",
            "/workspace/frappe-bench/logs/web.log.1",
            "/workspace/frappe-bench/logs/worker.supervisor.log.2",
        ]
        # The bench path rides as an argv positional, never inside the script text.
        find = c.calls[0]
        assert find[-1] == "/workspace/frappe-bench" and "/workspace" not in find[2]

    def test_site_logs_count_and_their_rotated_files_are_prunable(self, running_frappe):
        running_frappe.listing = [
            ("web.log", 10),
            ("sites/s1.localhost/logs/frappe.log", 40),
            ("sites/s1.localhost/logs/frappe.log.1", 300),
            ("sites/s1.localhost/logs/scheduler.log.20", 7),
        ]
        rep = ls.bench_logs(running_frappe, "p", "/workspace/frappe-bench")
        assert (rep.current_bytes, rep.rotated_bytes, rep.files) == (50, 307, 4)
        result = ls.prune_bench_logs("p", consent=True)
        rm = next(call for call in running_frappe.calls if call[0] == "rm")
        assert set(rm[3:]) == {
            "/workspace/frappe-bench/sites/s1.localhost/logs/frappe.log.1",
            "/workspace/frappe-bench/sites/s1.localhost/logs/scheduler.log.20",
        }
        assert result.data["freed_bytes"] == 307

    def test_listing_a_real_bench_tree_reads_site_logs_and_fails_on_a_missing_dir(self, tmp_path):
        import subprocess

        bench = tmp_path / "bench"
        (bench / "logs").mkdir(parents=True)
        (bench / "logs" / "web.log").write_bytes(b"x" * 3)
        site_logs = bench / "sites" / "a.localhost" / "logs"
        site_logs.mkdir(parents=True)
        (site_logs / "frappe.log.2").write_bytes(b"x" * 5)
        (bench / "sites" / "assets").mkdir()

        class ShellContainer:
            def exec_run(self, cmd, **kw):
                proc = subprocess.run(cmd, capture_output=True)
                return proc.returncode, proc.stdout

        rows = dict((p, s) for p, s in ls._bench_log_listing(ShellContainer(), str(bench)))
        assert rows == {str(bench / "logs" / "web.log"): 3, str(site_logs / "frappe.log.2"): 5}
        with pytest.raises(CwcliError) as e:
            ls._bench_log_listing(ShellContainer(), str(tmp_path / "gone"))
        assert e.value.code == "logs.bench_logs_unreadable"


@pytest.fixture
def running_frappe(monkeypatch):
    c = FakeContainer("p-frappe-1", "frappe", listing=LISTING)
    monkeypatch.setattr(ls.core_docker, "get_frappe_container", lambda p: c)
    monkeypatch.setattr(
        ls.resolvers, "cached_benches", lambda p: [{"path": "/workspace/frappe-bench", "index": 0}]
    )
    return c


class TestPruneBenchLogs:
    def test_without_a_selector_a_stale_cached_bench_is_skipped_with_a_warning(
        self, running_frappe, monkeypatch
    ):
        monkeypatch.setattr(
            ls.resolvers,
            "cached_benches",
            lambda p: [
                {"path": "/workspace/gone-bench", "index": 0},
                {"path": "/workspace/frappe-bench", "index": 1},
            ],
        )
        running_frappe.missing_logs = {"/workspace/gone-bench"}
        result = ls.prune_bench_logs("p", consent=True)
        assert result.status is Status.OK
        assert result.data["benches"] == ["/workspace/frappe-bench"]
        assert len(result.data["removed"]) == 3
        (warning,) = result.warnings
        assert warning.code == "logs.bench_skipped" and "/workspace/gone-bench" in warning.text

    def test_a_named_bench_whose_logs_are_missing_is_an_error(self, running_frappe, monkeypatch):
        running_frappe.missing_logs = {"/workspace/gone-bench"}
        monkeypatch.setattr(
            ls.resolvers,
            "resolve_container_and_bench",
            lambda project, bench, path, **kw: (running_frappe, "/workspace/gone-bench", []),
        )
        with pytest.raises(CwcliError) as e:
            ls.prune_bench_logs("p", bench="0", consent=True)
        assert e.value.code == "logs.bench_logs_unreadable"
        assert not any(call[0] == "rm" for call in running_frappe.calls)

    def test_a_stopped_instance_is_refused_never_started(self, running_frappe):
        running_frappe.status = "exited"
        with pytest.raises(CwcliError) as e:
            ls.prune_bench_logs("p", consent=True)
        assert e.value.kind is ErrorKind.NOT_RUNNING
        assert "cwcli start p" in (e.value.hint or "")
        assert not any(call[0] == "rm" for call in running_frappe.calls)

    def test_without_consent_it_is_a_choice_naming_the_count(self, running_frappe):
        result = ls.prune_bench_logs("p")
        assert result.status is Status.NEEDS_CHOICE
        assert result.choice.kind == "confirm_prune_logs" and "3 rotated" in result.choice.prompt
        assert not any(call[0] == "rm" for call in running_frappe.calls)

    def test_consent_removes_exactly_the_rotated_files(self, running_frappe):
        result = ls.prune_bench_logs("p", consent=True)
        assert result.status is Status.OK
        rm = next(call for call in running_frappe.calls if call[0] == "rm")
        assert rm[:3] == ["rm", "-f", "--"]
        assert (
            set(rm[3:])
            == set(result.data["removed"])
            == {
                "/workspace/frappe-bench/logs/frappe.log.3.gz",
                "/workspace/frappe-bench/logs/web.log.1",
                "/workspace/frappe-bench/logs/worker.supervisor.log.2",
            }
        )
        assert result.data["freed_bytes"] == 770
        assert not any(p.endswith("web.log") for p in rm)  # the current file is kept

    def test_a_container_gone_before_the_rm_is_a_typed_docker_error(self, running_frappe):
        real = running_frappe.exec_run

        def exec_run(cmd, **kw):
            if cmd[0] == "rm":
                raise ls.docker.errors.APIError("container is not running")
            return real(cmd, **kw)

        running_frappe.exec_run = exec_run
        with pytest.raises(CwcliError) as e:
            ls.prune_bench_logs("p", consent=True)
        assert e.value.kind is ErrorKind.DOCKER and "not running" in e.value.message

    def test_nothing_rotated_is_an_ok_no_op(self, running_frappe):
        running_frappe.listing = [("web.log", 10)]
        result = ls.prune_bench_logs("p", consent=True)
        assert result.status is Status.OK and result.data["removed"] == []
        assert [w.code for w in result.warnings] == ["logs.nothing_rotated"]
        assert not any(call[0] == "rm" for call in running_frappe.calls)

    def test_a_bench_selector_routes_through_the_shared_prologue(self, running_frappe, monkeypatch):
        seen = {}

        def fake(project, bench, path, **kw):
            seen["bench"] = bench
            return running_frappe, "/workspace/other-bench", []

        monkeypatch.setattr(ls.resolvers, "resolve_container_and_bench", fake)
        result = ls.prune_bench_logs("p", bench="other", consent=True)
        assert seen["bench"] == "other" and result.data["benches"] == ["/workspace/other-bench"]


# ------------------------------------------------------------------------ journal


class TestJournal:
    def test_not_linux_does_not_apply(self, monkeypatch):
        monkeypatch.setattr(ls.platform, "system", lambda: "Darwin")
        rep = ls.journal()
        assert rep.applies is False and "macOS" in rep.reason and rep.commands == []

    def test_linux_without_journald_does_not_apply(self, monkeypatch):
        monkeypatch.setattr(ls.platform, "system", lambda: "Linux")
        monkeypatch.setattr(ls.shutil, "which", lambda name: None)
        rep = ls.journal()
        assert rep.applies is False and "journald" in rep.reason

    def test_linux_with_journald_parses_the_usage_and_names_the_commands(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(ls.platform, "system", lambda: "Linux")
        monkeypatch.setattr(ls.shutil, "which", lambda name: "/usr/bin/journalctl")
        monkeypatch.setattr(ls.Path, "exists", lambda self: True)

        class P:
            stdout = "Archived and active journals take up 1.2G in the file system.\n"
            stderr = ""
            returncode = 0

        monkeypatch.setattr(ls.subprocess, "run", lambda *a, **k: P())
        monkeypatch.setattr(ls.os, "geteuid", lambda: 1000, raising=False)
        rep = ls.journal()
        assert rep.applies is True and rep.size == int(1.2 * 1024**3) and rep.privileged is False
        assert rep.commands == [
            "sudo journalctl --vacuum-size=500M",
            "sudo journalctl --vacuum-time=2weeks",
        ]
        assert "root" in (rep.reason or "")

    def test_a_partial_read_for_an_unprivileged_account_is_unknown_not_the_partial_size(
        self, monkeypatch
    ):
        monkeypatch.setattr(ls.platform, "system", lambda: "Linux")
        monkeypatch.setattr(ls.shutil, "which", lambda name: "/usr/bin/journalctl")
        monkeypatch.setattr(ls.Path, "exists", lambda self: True)

        class P:
            stdout = "Archived and active journals take up 8.0M in the file system.\n"
            stderr = (
                "Hint: You are currently not seeing messages from other users and the system.\n"
                "      Users in groups 'adm', 'systemd-journal' can see all messages.\n"
            )
            returncode = 0

        monkeypatch.setattr(ls.subprocess, "run", lambda *a, **k: P())
        monkeypatch.setattr(ls.os, "geteuid", lambda: 1000, raising=False)
        rep = ls.journal()
        assert rep.applies is True and rep.size is None
        assert "8.0M" in (rep.usage_text or "") and "systemd-journal" in (rep.reason or "")

    def test_usage_parse(self):
        assert ls._parse_usage("take up 512.0M in the file system") == 512 * 1024**2
        assert ls._parse_usage("nothing here") is None


# ------------------------------------------------------------------------- report


class TestReport:
    def test_stopped_instances_get_docker_sizes_but_no_bench_read(
        self, tmp_path, monkeypatch, projects_dir
    ):
        rows = [
            InstanceDTO(project_name="p", status="running", ports=[]),
            InstanceDTO(project_name="q", status="exited", ports=[]),
        ]
        monkeypatch.setattr(ls.core_list, "list_instances", lambda: type("R", (), {"data": rows})())
        conts = _containers(tmp_path)
        monkeypatch.setattr(ls.core_docker, "get_project_containers", lambda p: conts)
        frappe = FakeContainer("p-frappe-1", "frappe", listing=LISTING)
        monkeypatch.setattr(ls.core_docker, "get_frappe_container", lambda p: frappe)
        monkeypatch.setattr(ls.resolvers, "cached_benches", lambda p: [])
        monkeypatch.setattr(ls.core_inspect, "inspect", lambda *a, **k: None)
        monkeypatch.setattr(ls, "journal", lambda: ls.JournalReport(False, "no", None, None, False))
        result = ls.report()
        rep = result.data
        p, q = rep.instances
        assert p.running and len(p.benches) == 1 and p.benches[0].bench_index is None
        assert not q.running and q.benches == [] and "Start the instance" in q.note
        assert rep.docker_total == 320 and rep.bench_total is None
        assert p.benches[0].current_bytes + p.benches[0].rotated_bytes == 1326 + 770
        assert ls.to_dict(rep)["journal"]["applies"] is False

    def test_an_instance_whose_docker_logs_failed_makes_the_total_unknown(
        self, tmp_path, monkeypatch, projects_dir
    ):
        rows = [
            InstanceDTO(project_name="p", status="exited", ports=[]),
            InstanceDTO(project_name="q", status="exited", ports=[]),
        ]
        monkeypatch.setattr(ls.core_list, "list_instances", lambda: type("R", (), {"data": rows})())
        conts = _containers(tmp_path)

        def containers(project):
            return conts if project == "p" else None

        monkeypatch.setattr(ls.core_docker, "get_project_containers", containers)
        monkeypatch.setattr(ls, "journal", lambda: ls.JournalReport(False, "no", None, None, False))
        rep = ls.report().data
        assert rep.instances[0].docker.total == 160 and rep.instances[1].docker is None
        assert rep.docker_total is None

    def test_a_missing_bench_logs_dir_is_unmeasured_never_zero(
        self, tmp_path, monkeypatch, projects_dir
    ):
        rows = [InstanceDTO(project_name="p", status="running", ports=[])]
        monkeypatch.setattr(ls.core_list, "list_instances", lambda: type("R", (), {"data": rows})())
        conts = _containers(tmp_path)
        monkeypatch.setattr(ls.core_docker, "get_project_containers", lambda p: conts)
        frappe = FakeContainer("p-frappe-1", "frappe", listing=LISTING)
        frappe.missing_logs = {"/workspace/stale-bench"}
        monkeypatch.setattr(ls.core_docker, "get_frappe_container", lambda p: frappe)
        monkeypatch.setattr(
            ls.resolvers,
            "cached_benches",
            lambda p: [
                {"path": "/workspace/frappe-bench", "index": 0},
                {"path": "/workspace/stale-bench", "index": 1},
            ],
        )
        monkeypatch.setattr(ls, "journal", lambda: ls.JournalReport(False, "no", None, None, False))
        rep = ls.report().data
        (p,) = rep.instances
        assert [b.bench_path for b in p.benches] == ["/workspace/frappe-bench"]
        assert "/workspace/stale-bench/logs" in (p.note or "")
        assert rep.bench_total is None

    def test_a_cold_cache_is_populated_before_any_bench_is_guessed(
        self, tmp_path, monkeypatch, projects_dir
    ):
        cache: list[dict] = []
        populated = []

        def fake_inspect(project, **kw):
            populated.append((project, kw.get("offer_choice")))
            cache.append({"path": "/workspace/real-bench", "index": 0})

        monkeypatch.setattr(ls.resolvers, "cached_benches", lambda p: list(cache))
        monkeypatch.setattr(ls.core_inspect, "inspect", fake_inspect)
        assert ls._cached_paths("p") == [("/workspace/real-bench", 0)]
        assert populated == [("p", False)]

    def test_every_instance_running_and_measured_gives_a_real_bench_total(
        self, tmp_path, monkeypatch, projects_dir
    ):
        rows = [InstanceDTO(project_name="p", status="running", ports=[])]
        monkeypatch.setattr(ls.core_list, "list_instances", lambda: type("R", (), {"data": rows})())
        conts = _containers(tmp_path)
        monkeypatch.setattr(ls.core_docker, "get_project_containers", lambda p: conts)
        frappe = FakeContainer("p-frappe-1", "frappe", listing=LISTING)
        monkeypatch.setattr(ls.core_docker, "get_frappe_container", lambda p: frappe)
        monkeypatch.setattr(
            ls.resolvers, "cached_benches", lambda p: [{"path": "/workspace/frappe-bench"}]
        )
        monkeypatch.setattr(ls, "journal", lambda: ls.JournalReport(False, "no", None, None, False))
        rep = ls.report().data
        assert rep.docker_total == 160 and rep.bench_total == 1326 + 770

    def test_an_empty_fleet_is_measured_as_zero(self, monkeypatch):
        monkeypatch.setattr(ls.core_list, "list_instances", lambda: type("R", (), {"data": []})())
        monkeypatch.setattr(ls, "journal", lambda: ls.JournalReport(False, "no", None, None, False))
        rep = ls.report().data
        assert rep.instances == [] and rep.docker_total == 0 and rep.bench_total == 0

    def test_a_container_that_vanishes_mid_read_is_a_note_not_a_crash(
        self, tmp_path, monkeypatch, projects_dir
    ):
        rows = [InstanceDTO(project_name="p", status="running", ports=[])]
        monkeypatch.setattr(ls.core_list, "list_instances", lambda: type("R", (), {"data": rows})())
        conts = _containers(tmp_path)
        monkeypatch.setattr(ls.core_docker, "get_project_containers", lambda p: conts)
        frappe = FakeContainer("p-frappe-1", "frappe", listing=LISTING)
        frappe.gone = True
        monkeypatch.setattr(ls.core_docker, "get_frappe_container", lambda p: frappe)
        monkeypatch.setattr(
            ls.resolvers, "cached_benches", lambda p: [{"path": "/workspace/frappe-bench"}]
        )
        monkeypatch.setattr(ls, "journal", lambda: ls.JournalReport(False, "no", None, None, False))
        rep = ls.report().data
        (p,) = rep.instances
        assert p.benches == [] and "No such container" in (p.note or "")
        assert rep.bench_total is None and rep.docker_total == 160

    def test_an_unreadable_data_root_is_measured_by_one_helper_for_every_instance(
        self, tmp_path, monkeypatch, projects_dir
    ):
        rows = [
            InstanceDTO(project_name="p", status="exited", ports=[]),
            InstanceDTO(project_name="q", status="exited", ports=[]),
        ]
        monkeypatch.setattr(ls.core_list, "list_instances", lambda: type("R", (), {"data": rows})())
        per_project = {"p": _containers(tmp_path / "p"), "q": _containers(tmp_path / "q")}
        monkeypatch.setattr(ls.core_docker, "get_project_containers", lambda p: per_project[p])
        monkeypatch.setattr(ls, "_host_sizes", lambda paths: None)
        client = FakeClient(
            stat_lines="7 /logs/0/aaa-json.log\n5 /logs/3/bbb-json.log\n2 /logs/3/bbb-json.log.1\n"
        )
        monkeypatch.setattr(ls.docker, "from_env", lambda: client)
        monkeypatch.setattr(ls, "journal", lambda: ls.JournalReport(False, "no", None, None, False))
        rep = ls.report().data
        assert len(client.runs) == 1 and len(client.runs[0]["volumes"]) == 4
        p, q = rep.instances
        assert [c.size for c in p.docker.containers] == [7, 0]
        assert [c.size for c in q.docker.containers] == [0, 7]
        assert p.docker.method == q.docker.method == "helper" and rep.docker_total == 14

    def test_an_unknown_project_filter_is_not_found(self, monkeypatch):
        monkeypatch.setattr(ls.core_list, "list_instances", lambda: type("R", (), {"data": []})())
        with pytest.raises(CwcliError) as e:
            ls.report("ghost")
        assert e.value.kind is ErrorKind.NOT_FOUND


# ----------------------------------------------------------------- journal vacuum


@pytest.fixture
def journald(monkeypatch):
    """A Linux host with journald; ``journalctl`` calls are recorded and answered."""
    monkeypatch.setattr(ls.platform, "system", lambda: "Linux")
    monkeypatch.setattr(ls.shutil, "which", lambda name: "/usr/bin/journalctl")
    monkeypatch.setattr(ls.Path, "exists", lambda self: True)
    state = {"usage": "take up 2.0G in the file system", "runs": [], "vacuum_code": 0}

    class P:
        def __init__(self, stdout, returncode=0, stderr=""):
            self.stdout, self.returncode, self.stderr = stdout, returncode, stderr

    def run(cmd, **kw):
        state["runs"].append(list(cmd))
        if cmd[1] == "--disk-usage":
            return P(f"Archived and active journals {state['usage']}.\n")
        state["usage"] = "take up 500.0M in the file system"
        return P("", state["vacuum_code"], "Vacuuming done, freed 1.5G.\n")

    monkeypatch.setattr(ls.subprocess, "run", run)
    return state


class TestVacuumJournal:
    def test_not_root_is_refused_with_the_command_to_copy(self, journald, monkeypatch):
        monkeypatch.setattr(ls.os, "geteuid", lambda: 1000, raising=False)
        with pytest.raises(CwcliError) as e:
            ls.vacuum_journal(consent=True)
        assert e.value.kind is ErrorKind.PRECONDITION and e.value.code == "journal.not_root"
        assert "sudo journalctl --vacuum-size=500M --vacuum-time=2weeks" in (e.value.hint or "")
        assert all(run[1] == "--disk-usage" for run in journald["runs"])

    def test_root_without_consent_is_a_choice_and_nothing_is_vacuumed(self, journald, monkeypatch):
        monkeypatch.setattr(ls.os, "geteuid", lambda: 0, raising=False)
        result = ls.vacuum_journal()
        assert result.status is Status.NEEDS_CHOICE
        assert result.choice.kind == "confirm_vacuum_journal" and result.choice.param == "consent"
        assert all(run[1] == "--disk-usage" for run in journald["runs"])

    def test_root_with_consent_vacuums_to_the_chosen_size_and_age(self, journald, monkeypatch):
        monkeypatch.setattr(ls.os, "geteuid", lambda: 0, raising=False)
        result = ls.vacuum_journal(max_size="1G", max_time="14d", consent=True)
        assert ["journalctl", "--vacuum-size=1G", "--vacuum-time=14d"] in journald["runs"]
        assert result.data["size_before"] == 2 * 1024**3
        assert result.data["size_after"] == 500 * 1024**2
        assert result.data["freed_bytes"] == 2 * 1024**3 - 500 * 1024**2

    def test_a_failed_vacuum_is_an_error_not_success(self, journald, monkeypatch):
        monkeypatch.setattr(ls.os, "geteuid", lambda: 0, raising=False)
        journald["vacuum_code"] = 1
        with pytest.raises(CwcliError) as e:
            ls.vacuum_journal(consent=True)
        assert e.value.code == "journal.vacuum_failed"

    @pytest.mark.parametrize(
        "size,age", [("0M", "2weeks"), ("lots", "2weeks"), ("500M", "0d"), ("500M", "soon")]
    )
    def test_bad_limits_are_usage_errors_before_anything_runs(self, journald, size, age):
        with pytest.raises(CwcliError) as e:
            ls.vacuum_journal(max_size=size, max_time=age, consent=True)
        assert e.value.kind is ErrorKind.USAGE and journald["runs"] == []

    def test_a_host_without_journald_does_not_apply(self, monkeypatch):
        monkeypatch.setattr(ls.platform, "system", lambda: "Darwin")
        with pytest.raises(CwcliError) as e:
            ls.vacuum_journal(consent=True)
        assert e.value.code == "journal.not_applicable"
