"""``core.drop_site`` - every branch, against faked exec + streaming I/O.

Pins the two things this module owns beyond what ``unlock``/``bench_ops`` already
prove work: the ``consent``-gated destructive confirm (the ``apps.uninstall_apps``
pattern), and the archive-relocation contract (copy the site's archived
directory out to the host, prune it from the container ONLY once that copy is
verified, and report the honest ``ok`` either way).
"""

from __future__ import annotations

import types

import pytest

from caffeinated_whale_cli.core import docker as core_docker
from caffeinated_whale_cli.core import resolvers
from caffeinated_whale_cli.core import rm_site as core_rm_site
from caffeinated_whale_cli.core.envelope import Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind

BENCH = "/workspace/frappe-bench"
SITE = "task.localhost"


class FakeAPI:
    """Streaming exec surface (client.api), mirroring docker-py's shape."""

    def __init__(self, container):
        self.container = container
        self._pending = None

    def exec_create(self, cid, cmd, workdir=None, tty=False, environment=None):
        self._pending = (cmd, workdir, environment)
        return {"Id": "exec-1"}

    def exec_start(self, exec_id, stream=True, demux=False):
        cmd, _workdir, _env = self._pending
        code, out = self.container._run_shell(cmd)
        self.container._last_code = code
        raw = out.encode() if isinstance(out, str) else out
        yield (raw, None) if demux else raw

    def exec_inspect(self, exec_id):
        return {"ExitCode": self.container._last_code}


class FakeContainer:
    labels = {"com.docker.compose.service": "frappe"}
    name = "proj-frappe-1"
    id = "cid"

    def __init__(
        self,
        *,
        status="running",
        bench_dir_ok=True,
        site_dir_ok=True,
        drop_site_ok=True,
        get_archive_ok=True,
        prune_ok=True,
        archive_bytes=b"FAKETAR",
    ):
        self.status = status
        self.bench_dir_ok = bench_dir_ok
        self.site_dir_ok = site_dir_ok
        self.drop_site_ok = drop_site_ok
        self.get_archive_ok = get_archive_ok
        self.prune_ok = prune_ok
        self.archive_bytes = archive_bytes
        self.calls: list = []
        self._dropped = False
        self._last_code = 0
        self.client = types.SimpleNamespace(api=FakeAPI(self))

    def reload(self):
        pass

    def _run_shell(self, cmd):
        cmd_str = cmd[-1] if isinstance(cmd, list) else cmd
        self.calls.append(cmd_str)
        if "drop-site" in cmd_str:
            if self.drop_site_ok:
                self._dropped = True
                return 0, "Dropped site\n"
            return 1, "Error dropping site\n"
        return 0, ""

    def exec_run(self, cmd, workdir=None, **kwargs):
        self.calls.append(cmd)
        if cmd[:2] == ["test", "-d"]:
            target = cmd[2]
            if target == f"{BENCH}/sites":
                return (0 if self.bench_dir_ok else 1, b"")
            if target == f"{BENCH}/archived/sites/{SITE}":
                return (0 if self._dropped else 1, b"")
            return (0 if self.site_dir_ok else 1, b"")
        if cmd[:2] == ["rm", "-rf"]:
            return (0 if self.prune_ok else 1, b"")
        return (0, b"")

    def get_archive(self, path):
        if not self.get_archive_ok:
            raise RuntimeError("boom")
        return ([self.archive_bytes], {"size": len(self.archive_bytes)})


@pytest.fixture()
def container(monkeypatch):
    c = FakeContainer()
    monkeypatch.setattr(core_docker, "get_frappe_container", lambda _p: c)
    monkeypatch.setattr(resolvers, "cached_benches", lambda _p: [{"path": BENCH}])
    return c


@pytest.fixture(autouse=True)
def isolated_archive_dir(tmp_path, monkeypatch):
    """Never let a unit test touch the real ``~/.cwcli/archive``."""
    monkeypatch.setattr(core_rm_site, "cwcli_home", lambda: tmp_path)
    return tmp_path


# --------------------------------------------------------------------------- consent


class TestConsent:
    def test_no_consent_is_a_confirm_drop_site_choice_and_nothing_runs(self, container):
        result = core_rm_site.drop_site("proj", SITE)

        assert result.status is Status.NEEDS_CHOICE
        assert result.choice.kind == "confirm_drop_site"
        assert result.choice.param == "consent"
        assert SITE in result.choice.prompt
        assert not any("drop-site" in str(c) for c in container.calls)

    def test_missing_site_refuses_before_any_consent_prompt(self, container):
        container.site_dir_ok = False
        with pytest.raises(CwcliError) as exc:
            core_rm_site.drop_site("proj", SITE)
        assert exc.value.kind is ErrorKind.NOT_FOUND

    def test_consent_true_proceeds(self, container):
        result = core_rm_site.drop_site("proj", SITE, consent=True)
        assert result.status is not Status.NEEDS_CHOICE
        assert any("drop-site" in str(c) for c in container.calls)


# --------------------------------------------------------------------------- success


class TestSuccess:
    def test_full_success_copies_out_and_prunes_the_archive(self, container, tmp_path):
        result = core_rm_site.drop_site("proj", SITE, consent=True)

        assert result.status is Status.OK
        outcome = result.data
        assert outcome.project == "proj"
        assert outcome.site == SITE
        assert outcome.bench_path == BENCH
        assert outcome.ok is True
        assert outcome.archive_pruned_in_container is True
        assert outcome.archived_host_path is not None
        host_path = __import__("pathlib").Path(outcome.archived_host_path)
        assert host_path.exists()
        assert host_path.read_bytes() == container.archive_bytes
        assert host_path.parent == tmp_path / "archive" / "proj_dropped_sites"

        prune_calls = [c for c in container.calls if isinstance(c, list) and c[:2] == ["rm", "-rf"]]
        assert prune_calls == [["rm", "-rf", f"{BENCH}/archived/sites/{SITE}"]]

    def test_the_drop_site_command_never_carries_the_password(self, container):
        core_rm_site.drop_site("proj", SITE, consent=True, db_root_password="s3cr3t")

        commands = [c for c in container.calls if isinstance(c, str)]
        assert not any("s3cr3t" in c for c in commands)
        assert any("$CWCLI_DB_ROOT_PASSWORD" in c for c in commands)

    def test_uses_force_so_bench_never_prompts(self, container):
        core_rm_site.drop_site("proj", SITE, consent=True)
        commands = [c for c in container.calls if isinstance(c, str) and "drop-site" in c]
        assert commands and all("--force" in c for c in commands)


# --------------------------------------------------------------------- archive honesty


class TestArchiveHonesty:
    def test_copy_failure_leaves_the_in_container_archive_and_reports_not_ok(self, container):
        container.get_archive_ok = False

        result = core_rm_site.drop_site("proj", SITE, consent=True)

        assert result.status is Status.WARNING
        outcome = result.data
        assert outcome.ok is False
        assert outcome.archived_host_path is None
        assert outcome.archive_pruned_in_container is False
        # Fail closed: never prune what was never verified copied out.
        assert not [c for c in container.calls if isinstance(c, list) and c[:2] == ["rm", "-rf"]]
        assert any(w.code == "rm_site.archive_not_copied" for w in result.warnings)

    def test_prune_failure_reports_not_ok_but_keeps_the_verified_host_copy(self, container):
        container.prune_ok = False

        result = core_rm_site.drop_site("proj", SITE, consent=True)

        assert result.status is Status.WARNING
        outcome = result.data
        assert outcome.ok is False
        assert outcome.archived_host_path is not None
        assert outcome.archive_pruned_in_container is False
        assert any(w.code == "rm_site.archive_not_pruned" for w in result.warnings)

    def test_no_new_archive_entry_is_reported_not_silently_ignored(self, monkeypatch, container):
        # bench dropped the site but the archive path cwcli expects is absent
        # (e.g. --no-backup was somehow effective, or a bench version differs).
        def _exec_run(cmd, workdir=None, **kwargs):
            container.calls.append(cmd)
            if cmd[:2] == ["test", "-d"] and cmd[2] == f"{BENCH}/archived/sites/{SITE}":
                return (1, b"")  # never archived, before or after
            if cmd[:2] == ["test", "-d"]:
                return (0, b"")
            return (0, b"")

        monkeypatch.setattr(container, "exec_run", _exec_run)

        result = core_rm_site.drop_site("proj", SITE, consent=True)

        assert result.status is Status.WARNING
        assert result.data.ok is False
        assert result.data.archived_host_path is None
        assert any(w.code == "rm_site.archive_not_found" for w in result.warnings)


# ----------------------------------------------------------------------- hard failures


class TestHardFailures:
    def test_stopped_container_is_a_confirm_start_choice(self, container):
        container.status = "exited"
        result = core_rm_site.drop_site("proj", SITE)

        assert result.status is Status.NEEDS_CHOICE
        assert result.choice.kind == "confirm_start"
        assert not any("drop-site" in str(c) for c in container.calls)

    def test_failed_drop_site_raises_precondition(self, container):
        container.drop_site_ok = False
        with pytest.raises(CwcliError) as exc:
            core_rm_site.drop_site("proj", SITE, consent=True)
        assert exc.value.kind is ErrorKind.PRECONDITION

    def test_shell_unsafe_site_raises_usage(self, container):
        with pytest.raises(CwcliError) as exc:
            core_rm_site.drop_site("proj", "evil;rm -rf /", consent=True)
        assert exc.value.kind is ErrorKind.USAGE

    def test_empty_site_raises_usage(self, container):
        with pytest.raises(CwcliError) as exc:
            core_rm_site.drop_site("proj", "   ", consent=True)
        assert exc.value.kind is ErrorKind.USAGE

    def test_missing_bench_dir_raises_not_found(self, container):
        container.bench_dir_ok = False
        with pytest.raises(CwcliError) as exc:
            core_rm_site.drop_site("proj", SITE, consent=True)
        assert exc.value.kind is ErrorKind.NOT_FOUND
