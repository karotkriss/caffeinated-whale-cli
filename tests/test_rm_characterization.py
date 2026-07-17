"""Command-level characterization net for ``cwcli rm`` (batch 12: migrate-rm-core).

Written and committed GREEN against the UNMIGRATED command (batch 4's discipline),
then run UNCHANGED against the migrated code as the refactor-under-green proof.

These drive the real ``rm.rm(...)`` command entry (which does not move) through
seams that survive the migration:

- container / volume resolution and cache access are patched with
  :func:`_patch_attr`, which sets the attribute on EVERY module that defines it -
  today only ``commands.rm``; after the migration also ``core.rm`` (where the
  destructive helpers live). The file therefore needs no edit when the removal
  logic moves modules.
- ``@handle_docker_errors``'s daemon check, ``sys.stdin.isatty``, and
  ``questionary`` are patched at their own modules, which do not move.

What is pinned (the observable contract, not the internal shape): the confirm
text + stopped-project disclosure, the C1 abort-before-any-container-removal, the
H5 ``rm ..`` refusal, the M11 partial-failure exit code, the exit-0 not-found
no-op, the multi-bench per-bench backup, ``--no-volumes``, and ``--no-backup``.
"""

import importlib
import io
import shlex
import tarfile
from unittest.mock import MagicMock

import pytest
import typer

from caffeinated_whale_cli.commands import rm
from caffeinated_whale_cli.utils import db_utils, docker_utils

BENCH = "/workspace/frappe-bench"

# Modules the removal collaborators may live on. `commands.rm` today; after the
# migration the destructive reads move to `core.rm`. Patching every module that
# defines the attribute keeps this file byte-identical across the move.
_RM_MODULES = ("caffeinated_whale_cli.commands.rm", "caffeinated_whale_cli.core.rm")


def _patch_attr(monkeypatch, attr, value):
    """Set ``attr`` on every rm module that defines it (frontend and/or core)."""
    patched = False
    for modname in _RM_MODULES:
        try:
            mod = importlib.import_module(modname)
        except ModuleNotFoundError:
            continue
        if hasattr(mod, attr):
            monkeypatch.setattr(mod, attr, value)
            patched = True
    assert patched, f"no rm module defines {attr!r}"


def _db_name(site):
    return f"backup-{site}-database.sql.gz"


def _cfg_name(site):
    return f"backup-{site}-site_config_backup.json"


def _tar_bytes(name, data):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=name)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class FakeFrappeContainer:
    """A frappe container that answers the exact probes the backup/archive path
    issues and records every call. Mirrors ``tests/test_rm_safety.py``'s fake
    (the battle-tested copy-out harness), scoped to the command-level tests."""

    def __init__(
        self,
        sites,
        *,
        backup_ok=True,
        artifacts=None,
        bench_path=BENCH,
        name="proj-frappe-1",
        status="running",
        remove_raises=False,
    ):
        self.bench_path = bench_path
        self.name = name
        self.status = status
        self.labels = {"com.docker.compose.service": "frappe"}
        self.calls: list = []
        self.stopped = False
        self.removed = False
        self._remove_raises = remove_raises
        self.sites = list(sites)
        self._backup_ok = backup_ok
        if artifacts is None:
            artifacts = {s: {_db_name(s): b"DBDUMPBYTES", _cfg_name(s): b"{}"} for s in self.sites}
        self.artifacts = artifacts

    def _backup_exit(self, site):
        ok = self._backup_ok
        if isinstance(ok, dict):
            ok = ok.get(site, True)
        return 0 if ok else 1

    def exec_run(self, cmd, workdir=None):
        self.calls.append(cmd)
        b = self.bench_path
        if cmd == f"ls -1 {b}/sites":
            listing = ["apps.txt", "common_site_config.json", *self.sites]
            return (0, "\n".join(listing).encode())
        if cmd.startswith("sh -c '") and "echo SITE" in cmd and "site_config.json" in cmd:
            entry = shlex.split(cmd)[-1].rsplit("/", 1)[-1]
            return (0, b"SITE\n") if entry in self.sites else (0, b"NOTASITE\n")
        if cmd.startswith("sh -c 'cd ") and "bench --site " in cmd and "backup" in cmd:
            site = cmd.split("bench --site ")[1].split(" ")[0]
            return (self._backup_exit(site), b"backup output")
        for site in self.sites:
            sbdir = f"{b}/sites/{site}/private/backups"
            if cmd == f"ls -1t {sbdir}":
                return (0, "\n".join(self.artifacts.get(site, {}).keys()).encode())
        return (1, b"")

    def get_archive(self, path):
        self.calls.append(f"get_archive {path}")
        for site in self.sites:
            prefix = f"{self.bench_path}/sites/{site}/private/backups/"
            if path.startswith(prefix):
                fname = path[len(prefix) :]
                data = self.artifacts.get(site, {}).get(fname)
                if data is None:
                    raise FileNotFoundError(path)
                raw = _tar_bytes(fname, data)

                def _chunks(blob=raw):
                    for i in range(0, len(blob), 4):
                        yield blob[i : i + 4]

                return _chunks(), {"name": fname, "size": len(data)}
        raise FileNotFoundError(path)

    def stop(self):
        self.stopped = True

    def remove(self, v=False, force=False):
        if self._remove_raises:
            raise RuntimeError("container removal boom")
        self.removed = True


def _make_volume(name):
    volume = MagicMock()
    volume.name = name
    return volume


def _make_project_dir(projects_dir, name):
    project_dir = projects_dir / name
    (project_dir / "conf").mkdir(parents=True)
    (project_dir / "conf" / "docker-compose.yml").write_text("# fake compose\n")
    return project_dir


@pytest.fixture()
def cwcli_home(tmp_path, monkeypatch):
    """Redirect PROJECTS_DIR and the archive root (from Path.home()) into tmp."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    _patch_attr(monkeypatch, "PROJECTS_DIR", projects_dir)
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _patch_docker(monkeypatch):
    monkeypatch.setattr(docker_utils.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(docker_utils.docker, "from_env", lambda: MagicMock())


def _wire(monkeypatch, containers, volumes, *, cached=None):
    """Point both the frontend run-state reads and the removal reads at fakes.

    ``get_project_containers``/``get_project_volumes`` are bare imports (bound per
    module), so patch every rm module. ``db_utils`` is imported as a module, so a
    single patch on the shared module object reaches both frontend and core.
    """
    _patch_attr(monkeypatch, "get_project_containers", lambda name: list(containers))
    _patch_attr(monkeypatch, "get_project_volumes", lambda name: list(volumes))
    monkeypatch.setattr(db_utils, "clear_cache_for_project", lambda name: None)
    monkeypatch.setattr(db_utils, "get_cached_project_data", lambda name: cached)
    # Recache is a frontend concern; no-op it so the command does not touch Docker.
    monkeypatch.setattr(rm.cache, "recache_project", lambda name, verbose=False: True)
    # stdin is a real TTY (so no piped-input path) unless a test overrides it.
    monkeypatch.setattr(rm.sys.stdin, "isatty", lambda: True)


def _run(**kwargs):
    params = dict(
        ctx=MagicMock(),
        verbose=False,
        volumes=True,
        no_backup=False,
        yes=True,
        project_name=["proj"],
    )
    params.update(kwargs)
    return rm.rm(**params)


# --------------------------------------------------------------------------- #
# Clean removal / what-deletes-what
# --------------------------------------------------------------------------- #


class TestCleanRemoval:
    def test_full_removal_deletes_everything(self, cwcli_home, monkeypatch, capsys):
        _patch_docker(monkeypatch)
        _make_project_dir(cwcli_home / "projects", "proj")
        container = FakeFrappeContainer(["a.localhost"])
        volumes = [_make_volume("proj_sites"), _make_volume("proj_db-data")]
        _wire(monkeypatch, [container], volumes)

        _run()  # exits 0 (no raise)

        assert container.removed is True
        for v in volumes:
            v.remove.assert_called_once_with(force=True)
        assert not (cwcli_home / "projects" / "proj").exists()
        out = capsys.readouterr().out
        assert "Successfully removed" in out

    def test_no_volumes_keeps_volumes_removes_dir(self, cwcli_home, monkeypatch):
        _patch_docker(monkeypatch)
        _make_project_dir(cwcli_home / "projects", "proj")
        container = FakeFrappeContainer(["a.localhost"], status="exited")
        volumes = [_make_volume("proj_sites")]
        _wire(monkeypatch, [container], volumes)

        _run(volumes=False)

        assert container.removed is True
        for v in volumes:
            v.remove.assert_not_called()
        assert not (cwcli_home / "projects" / "proj").exists()


# --------------------------------------------------------------------------- #
# C1 - the fail-closed backup gate aborts before any container is removed
# --------------------------------------------------------------------------- #


class TestBackupGateAborts:
    def test_unverified_backup_aborts_before_removing_anything(self, cwcli_home, monkeypatch):
        _patch_docker(monkeypatch)
        project_dir = _make_project_dir(cwcli_home / "projects", "proj")
        # The DB dump copies out EMPTY -> the backup cannot be verified.
        container = FakeFrappeContainer(
            ["a.localhost"],
            artifacts={
                "a.localhost": {_db_name("a.localhost"): b"", _cfg_name("a.localhost"): b"{}"}
            },
        )
        volumes = [_make_volume("proj_sites")]
        _wire(monkeypatch, [container], volumes)

        with pytest.raises(typer.Exit) as exc:
            _run()

        assert exc.value.exit_code == 1
        # Nothing was destroyed: container alive, volumes untouched, dir intact.
        assert container.removed is False
        for v in volumes:
            v.remove.assert_not_called()
        assert project_dir.exists()

    def test_stopped_orphan_refuses_on_volumes_path(self, cwcli_home, monkeypatch):
        """An orphan (no containers) on --volumes without --no-backup cannot be
        started for a backup, so it is refused - nothing deleted."""
        _patch_docker(monkeypatch)
        project_dir = _make_project_dir(cwcli_home / "projects", "proj")
        volumes = [_make_volume("proj_sites")]
        _wire(monkeypatch, [], volumes)

        with pytest.raises(typer.Exit) as exc:
            _run()

        assert exc.value.exit_code == 1
        for v in volumes:
            v.remove.assert_not_called()
        assert project_dir.exists()


# --------------------------------------------------------------------------- #
# H5 - path-traversal names never reach a destructive step
# --------------------------------------------------------------------------- #


class TestPathTraversalGuard:
    @pytest.mark.parametrize("bad", ["..", ".", "/etc", "a/b"])
    def test_escaping_name_refused_before_removal(self, cwcli_home, monkeypatch, bad):
        _patch_docker(monkeypatch)
        sentinel = cwcli_home / "SENTINEL"
        sentinel.write_text("keep me")
        reached = MagicMock()
        # If the command ever resolves containers for a bad name, this fires.
        _patch_attr(monkeypatch, "get_project_containers", reached)
        monkeypatch.setattr(rm.sys.stdin, "isatty", lambda: True)

        with pytest.raises(typer.Exit) as exc:
            _run(project_name=[bad])

        assert exc.value.exit_code == 1
        assert sentinel.exists()
        reached.assert_not_called()


# --------------------------------------------------------------------------- #
# M11 - honest exit codes on partial failure
# --------------------------------------------------------------------------- #


class TestHonestExitCodes:
    def test_container_removal_failure_exits_nonzero_and_spares_data(
        self, cwcli_home, monkeypatch, capsys
    ):
        _patch_docker(monkeypatch)
        project_dir = _make_project_dir(cwcli_home / "projects", "proj")
        container = FakeFrappeContainer(["a.localhost"], remove_raises=True)
        volumes = [_make_volume("proj_sites")]
        _wire(monkeypatch, [container], volumes)

        with pytest.raises(typer.Exit) as exc:
            _run()

        assert exc.value.exit_code == 1
        # The LATE gate spared the volumes/dir because a container removal failed.
        for v in volumes:
            v.remove.assert_not_called()
        assert project_dir.exists()
        out = capsys.readouterr().out
        assert "Successfully removed" not in out

    def test_missing_project_is_exit_zero_no_op(self, cwcli_home, monkeypatch, capsys):
        _patch_docker(monkeypatch)
        _wire(monkeypatch, [], [])

        _run(project_name=["ghost"])  # no raise: exit-0 no-op

        combined = capsys.readouterr()
        text = combined.out + combined.err
        assert "not found" in text.lower() or "No projects were removed" in text


# --------------------------------------------------------------------------- #
# Multi-bench: every bench is backed up
# --------------------------------------------------------------------------- #


class TestMultiBench:
    def test_backs_up_every_bench(self, cwcli_home, monkeypatch):
        _patch_docker(monkeypatch)
        _make_project_dir(cwcli_home / "projects", "proj")
        bench2 = "/workspace/bench-two"
        # One container that answers probes for BOTH bench paths.
        container = FakeFrappeContainer(["a.localhost"])
        # Extend its site listing to answer bench2 probes too.
        orig_exec = container.exec_run

        def exec_run(cmd, workdir=None):
            if cmd == f"ls -1 {bench2}/sites":
                return (0, "\n".join(["apps.txt", "b.localhost"]).encode())
            if cmd.startswith("sh -c '") and "echo SITE" in cmd and bench2 in cmd:
                entry = shlex.split(cmd)[-1].rsplit("/", 1)[-1]
                return (0, b"SITE\n") if entry == "b.localhost" else (0, b"NOTASITE\n")
            if cmd.startswith("sh -c 'cd ") and bench2 in cmd and "backup" in cmd:
                return (0, b"backup output")
            if cmd == f"ls -1t {bench2}/sites/b.localhost/private/backups":
                return (0, _db_name("b.localhost").encode())
            return orig_exec(cmd, workdir=workdir)

        container.exec_run = exec_run
        # bench2's dump copies out fine (served through a get_archive that also
        # answers the bench-two path).
        orig_archive = container.get_archive
        b2_prefix = f"{bench2}/sites/b.localhost/private/backups/"

        def get_archive(path):
            container.calls.append(f"get_archive {path}")
            if path.startswith(b2_prefix):
                fname = path[len(b2_prefix) :]
                raw = _tar_bytes(fname, b"DBDUMPBYTES")

                def _chunks(blob=raw):
                    for i in range(0, len(blob), 4):
                        yield blob[i : i + 4]

                return _chunks(), {"name": fname, "size": len(b"DBDUMPBYTES")}
            return orig_archive(path)

        container.get_archive = get_archive
        volumes = [_make_volume("proj_sites")]
        cached = {"bench_instances": [{"path": BENCH}, {"path": bench2}]}
        _wire(monkeypatch, [container], volumes, cached=cached)

        _run()  # exits 0: both benches verified

        assert container.removed is True
        for v in volumes:
            v.remove.assert_called_once_with(force=True)

    def test_one_bench_backup_failure_blocks_whole_instance(self, cwcli_home, monkeypatch):
        _patch_docker(monkeypatch)
        project_dir = _make_project_dir(cwcli_home / "projects", "proj")
        bench2 = "/workspace/bench-two"
        container = FakeFrappeContainer(["a.localhost"])
        orig_exec = container.exec_run

        def exec_run(cmd, workdir=None):
            if cmd == f"ls -1 {bench2}/sites":
                return (0, "\n".join(["apps.txt", "b.localhost"]).encode())
            if cmd.startswith("sh -c '") and "echo SITE" in cmd and bench2 in cmd:
                return (0, b"SITE\n")
            if cmd.startswith("sh -c 'cd ") and bench2 in cmd and "backup" in cmd:
                return (1, b"backup FAILED")  # bench2 backup fails
            return orig_exec(cmd, workdir=workdir)

        container.exec_run = exec_run
        volumes = [_make_volume("proj_sites")]
        cached = {"bench_instances": [{"path": BENCH}, {"path": bench2}]}
        _wire(monkeypatch, [container], volumes, cached=cached)

        with pytest.raises(typer.Exit) as exc:
            _run()

        assert exc.value.exit_code == 1
        assert container.removed is False
        for v in volumes:
            v.remove.assert_not_called()
        assert project_dir.exists()


# --------------------------------------------------------------------------- #
# --no-backup escape hatch and the confirm + disclosure
# --------------------------------------------------------------------------- #


class TestNoBackupAndConfirm:
    def test_no_backup_deletes_orphan(self, cwcli_home, monkeypatch):
        _patch_docker(monkeypatch)
        project_dir = _make_project_dir(cwcli_home / "projects", "proj")
        volumes = [_make_volume("proj_sites")]
        _wire(monkeypatch, [], volumes)

        _run(no_backup=True)  # orphan cleaned, exit 0

        for v in volumes:
            v.remove.assert_called_once_with(force=True)
        assert not project_dir.exists()

    def test_declining_confirm_deletes_nothing(self, cwcli_home, monkeypatch, capsys):
        _patch_docker(monkeypatch)
        project_dir = _make_project_dir(cwcli_home / "projects", "proj")
        container = FakeFrappeContainer(["a.localhost"])
        volumes = [_make_volume("proj_sites")]
        _wire(monkeypatch, [container], volumes)

        confirm = MagicMock()
        confirm.ask.return_value = False
        monkeypatch.setattr(rm.questionary, "confirm", lambda *a, **k: confirm)

        with pytest.raises(typer.Exit) as exc:
            _run(yes=False)

        assert exc.value.exit_code == 0
        assert container.removed is False
        assert project_dir.exists()
        out = capsys.readouterr().out
        assert "cancelled" in out.lower()

    def test_confirm_warns_all_data_lost_on_volumes(self, cwcli_home, monkeypatch, capsys):
        _patch_docker(monkeypatch)
        _make_project_dir(cwcli_home / "projects", "proj")
        container = FakeFrappeContainer(["a.localhost"])
        _wire(monkeypatch, [container], [_make_volume("proj_sites")])

        confirm = MagicMock()
        confirm.ask.return_value = False
        monkeypatch.setattr(rm.questionary, "confirm", lambda *a, **k: confirm)

        with pytest.raises(typer.Exit):
            _run(yes=False)

        out = capsys.readouterr().out
        assert "ALL DATA WILL BE PERMANENTLY LOST" in out
