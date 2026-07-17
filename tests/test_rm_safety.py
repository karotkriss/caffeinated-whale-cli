"""Data-safety regression tests for ``cwcli rm`` (audit findings C1, H5, M11).

Every pre-existing rm test passes ``no_backup=True``, so the backup-before-delete
gate was completely unexercised. These tests drive the backup path with a fake
frappe container and pin the corrected contracts:

- **C1** - a live ``bench backup`` that fails, or whose artifacts do not actually
  land non-empty on the host archive, must NOT delete the named volumes/dir, and
  the command must report failure (never a green "Successfully removed").
- **H5** - a project name that could escape ``PROJECTS_DIR`` (``.``/``..``/
  absolute/separator) is rejected before any ``rmtree`` or volume removal.
- **M11** - volume-removal, directory-removal, backup, and container-removal
  failures are tracked and surfaced as a non-zero exit, not a green check + exit 0.
  A caught container-removal error does not fall through to volume/dir destruction.

The fake-container harness mirrors ``tests/test_inspect_partial_refresh.py``'s
``FakeFrappeContainer`` (records every ``exec_run``, answers the exact probes the
code issues) and ``tests/test_rm_truth.py``'s tmp-filesystem approach.
"""

import io
import shlex
import sys
import tarfile
from unittest.mock import MagicMock

import pytest
import typer

from caffeinated_whale_cli.commands import rm
from caffeinated_whale_cli.core import rm as core_rm
from caffeinated_whale_cli.core.envelope import Result, Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from caffeinated_whale_cli.utils import db_utils, docker_utils

BENCH = "/workspace/frappe-bench"

# The destructive logic moved to ``core.rm`` (batch 12); these tests exercise it
# there. ``_patch_attr`` sets the attribute on every rm module that defines it -
# ``core.rm`` (where the reads now live) and, for the CLI-driving tests,
# ``commands.rm`` (which keeps its own run-state reads).
_RM_MODULES = (core_rm, rm)


def _patch_attr(monkeypatch, attr, value):
    for mod in _RM_MODULES:
        if hasattr(mod, attr):
            monkeypatch.setattr(mod, attr, value)


def _render_event(event):
    """A test emit that renders ``core.remove``'s events to stdout/stderr, so the
    capsys-based assertions on warnings/notices keep working after the move."""
    if isinstance(event, core_rm.RmNotice):
        print(f"  {event.text}")
    elif isinstance(event, core_rm.RmWarning):
        print(f"Warning: {event.text}", file=sys.stderr)
        if event.hint:
            print(event.hint, file=sys.stderr)
    elif isinstance(event, core_rm.RmError):
        print(f"Error: {event.text}", file=sys.stderr)


# ---- thin adapters keeping the moved helpers' OLD (dict / verbose) test surface,
# so the safety assertions below re-point with their subject unchanged. The core
# now returns a typed ``Result[RemovalOutcome]`` and takes an ``on_event`` emit;
# the DTO/typed contract is asserted directly in ``tests/test_core_rm.py``.


def _remove_project(name, *, remove_volumes, no_backup):
    result = core_rm.remove(
        name, remove_volumes=remove_volumes, no_backup=no_backup, on_event=_render_event
    )
    d = result.data
    return {
        "found": d.found,
        "orphan": d.orphan,
        "containers": d.containers_removed,
        "volumes": d.volumes_removed,
        "dir_removed": d.dir_removed,
        "backup_ok": d.backup_ok,
        "failures": list(d.failures),
    }


def _backup_sites(project, container, bench_path, archive_dir, verbose=False):
    return core_rm._backup_sites(project, container, bench_path, archive_dir, _render_event)


def _archive_project_directory(project, archive_dir=None, verbose=False):
    return core_rm._archive_project_directory(project, _render_event, archive_dir=archive_dir)


def _delete_project_directory(project, verbose=False, failures=None):
    return core_rm._delete_project_directory(project, _render_event, failures=failures)


def _remove_named_volumes(project, verbose=False, status=None, failures=None):
    return core_rm._remove_named_volumes(project, _render_event, failures=failures)


def _db_name(site):
    return f"backup-{site}-database.sql.gz"


def _cfg_name(site):
    return f"backup-{site}-site_config_backup.json"


def _tar_bytes(name, data):
    """Wrap ``data`` in an uncompressed tar with a single member ``name`` -
    exactly the shape docker-py's ``get_archive`` streams back for one file."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=name)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class FakeFrappeContainer:
    """A running frappe container that answers the exact ``exec_run`` probes
    ``_backup_sites``/``_archive_project_config`` issue, and records every call.

    ``sites`` is the list of site directories. ``extra_entries`` are stray
    non-site entries that ``ls -1 sites`` also reports (e.g. ``currentsite.txt``)
    but which have NO ``site_config.json`` and so must never be treated as sites.
    ``ambiguous_entries`` are entries whose classification probe cannot confirm
    non-site status (an unreadable directory / probe error): the fail-safe
    detector must treat them as real sites. ``backup_ok`` is the ``bench backup``
    exit result (bool, or per-site dict). ``artifacts`` maps each site to
    ``{filename: bytes}`` - the files ``ls -1t`` reports and ``get_archive``
    streams out (as a single-member tar, the real copy primitive). A value of
    ``b""`` models a file that copies out empty; a value of ``None`` models a
    file whose ``get_archive`` fails (missing/unreadable). ``list_ok=False``
    makes the post-backup ``ls -1t`` fail.
    """

    def __init__(
        self,
        sites,
        *,
        extra_entries=None,
        ambiguous_entries=None,
        backup_ok=True,
        artifacts=None,
        list_ok=True,
        bench_path=BENCH,
        name="proj-frappe-1",
    ):
        self.bench_path = bench_path
        self.name = name
        self.status = "running"
        self.labels = {"com.docker.compose.service": "frappe"}
        self.calls: list[str] = []
        self.get_archive_calls: list[str] = []
        self.stopped = False
        self.removed = False
        self.sites = list(sites)
        self.extra_entries = list(extra_entries or [])
        self.ambiguous_entries = list(ambiguous_entries or [])
        self._backup_ok = backup_ok
        self._list_ok = list_ok
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
            listing = [
                "apps.txt",
                "common_site_config.json",
                *self.extra_entries,
                *self.ambiguous_entries,
                *self.sites,
            ]
            return (0, "\n".join(listing).encode())

        # Fail-safe site-classification probe: the entry dir is passed as the
        # positional arg (the last shlex token), never interpolated, so classify
        # by that entry's basename. A real site echoes SITE, a readable stray
        # entry NOTASITE, an unreadable/ambiguous entry AMBIGUOUS.
        if cmd.startswith("sh -c '") and "echo SITE" in cmd and "site_config.json" in cmd:
            entry = shlex.split(cmd)[-1].rsplit("/", 1)[-1]
            if entry in self.sites:
                return (0, b"SITE\n")
            if entry in self.ambiguous_entries:
                return (0, b"AMBIGUOUS\n")
            return (0, b"NOTASITE\n")

        if cmd.startswith("sh -c 'cd ") and "bench --site " in cmd and "backup" in cmd:
            site = cmd.split("bench --site ")[1].split(" ")[0]
            return (self._backup_exit(site), b"backup output")

        for site in self.sites:
            sbdir = f"{b}/sites/{site}/private/backups"
            if cmd == f"ls -1t {sbdir}":
                if not self._list_ok:
                    return (1, b"")
                return (0, "\n".join(self.artifacts.get(site, {}).keys()).encode())

        # _archive_project_config probes (compose paths, site_config.json) fail
        # benignly - it archives nothing and still returns True. A backup
        # artifact is copied out via get_archive (below), never `cat`, so any
        # `cat` of a backup file also falls through here and fails the copy -
        # which would make the positive backup tests fail if the code regressed
        # back to the whole-file buffering copy.
        return (1, b"")

    def get_archive(self, path):
        """Stream a single backup artifact out as an uncompressed tar in small
        chunks - the streaming copy primitive ``_backup_sites`` now uses. An
        artifact recorded as ``None`` models a missing/unreadable file, so
        get_archive raises exactly as docker-py does (fail-closed copy)."""
        self.calls.append(f"get_archive {path}")
        self.get_archive_calls.append(path)
        for site in self.sites:
            prefix = f"{self.bench_path}/sites/{site}/private/backups/"
            if path.startswith(prefix):
                fname = path[len(prefix) :]
                data = self.artifacts.get(site, {}).get(fname)
                if data is None:
                    raise FileNotFoundError(path)
                raw = _tar_bytes(fname, data)

                def _chunks(blob=raw):
                    # Small chunks so the consumer is exercised as a stream,
                    # never handed the whole artifact as one blob.
                    for i in range(0, len(blob), 4):
                        yield blob[i : i + 4]

                return _chunks(), {"name": fname, "size": len(data)}
        raise FileNotFoundError(path)

    def stop(self):
        self.stopped = True

    def remove(self, v=False, force=False):
        self.removed = True

    def ran_backup(self) -> bool:
        return any("bench --site " in c and "backup" in c for c in self.calls)


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
    """Neutralize the @handle_docker_errors daemon check."""
    monkeypatch.setattr(docker_utils.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(docker_utils.docker, "from_env", lambda: MagicMock())


def _wire(monkeypatch, container, volumes):
    """Point _remove_project's collaborators at the fakes."""
    _patch_attr(monkeypatch, "get_project_containers", lambda name: [container])
    _patch_attr(monkeypatch, "get_project_volumes", lambda name: list(volumes))
    monkeypatch.setattr(db_utils, "clear_cache_for_project", lambda name: None)
    monkeypatch.setattr(db_utils, "get_cached_project_data", lambda name: None)


# --------------------------------------------------------------------------- #
# C1 - a failed/unverified backup must not delete data
# --------------------------------------------------------------------------- #


class TestBackupGate:
    def test_failed_bench_backup_blocks_volume_deletion(self, cwcli_home, monkeypatch):
        _patch_docker(monkeypatch)
        project_dir = _make_project_dir(core_rm.PROJECTS_DIR, "proj")
        container = FakeFrappeContainer(["site1.localhost"], backup_ok=False)
        volumes = [_make_volume("proj_sites"), _make_volume("proj_db-data")]
        _wire(monkeypatch, container, volumes)

        result = _remove_project("proj", remove_volumes=True, no_backup=False)

        # The backup was attempted and failed, so NOTHING destructive may run.
        assert container.ran_backup() is True
        assert result["backup_ok"] is False
        assert result["volumes"] == 0
        assert result["dir_removed"] is False
        assert result["failures"], "a failed backup must be recorded as a failure"
        for volume in volumes:
            volume.remove.assert_not_called()
        assert project_dir.exists()

    def test_failed_backup_aborts_before_removing_containers(self, cwcli_home, monkeypatch):
        # Retry-safety (early abort): under --volumes a failed live backup must
        # abort BEFORE any container is stopped/removed, so the still-running
        # frappe container survives and a retry can still take a live backup. If
        # containers were torn down first, the naive retry would be an orphan (no
        # live DB) and would delete the volumes with NO backup - defeating C1.
        _patch_docker(monkeypatch)
        project_dir = _make_project_dir(core_rm.PROJECTS_DIR, "proj")
        container = FakeFrappeContainer(["site1.localhost"], backup_ok=False)
        volumes = [_make_volume("proj_sites"), _make_volume("proj_db-data")]
        _patch_attr(monkeypatch, "get_project_containers", lambda name: [container])
        _patch_attr(monkeypatch, "get_project_volumes", lambda name: list(volumes))
        monkeypatch.setattr(db_utils, "get_cached_project_data", lambda name: None)
        clear_cache = MagicMock()
        monkeypatch.setattr(db_utils, "clear_cache_for_project", clear_cache)

        result = _remove_project("proj", remove_volumes=True, no_backup=False)

        # The backup was attempted and failed...
        assert container.ran_backup() is True
        assert result["backup_ok"] is False
        # ...and the early abort fired: NO container was stopped or removed.
        assert result["containers"] == 0
        assert container.stopped is False
        assert container.removed is False
        # Nothing destructive ran: volumes untouched, directory intact, cache kept.
        assert result["volumes"] == 0
        assert result["dir_removed"] is False
        for volume in volumes:
            volume.remove.assert_not_called()
        assert project_dir.exists()
        clear_cache.assert_not_called()
        # A failure is recorded so the command exits non-zero and a retry is invited.
        assert result["failures"]

    def test_empty_host_artifact_blocks_volume_deletion(self, cwcli_home, monkeypatch):
        # bench backup exits 0, but the database dump copies out EMPTY (0 bytes),
        # i.e. the "backup" exists only inside the volume we are about to delete.
        _patch_docker(monkeypatch)
        project_dir = _make_project_dir(core_rm.PROJECTS_DIR, "proj")
        site = "site1.localhost"
        artifacts = {site: {_db_name(site): b"", _cfg_name(site): b"{}"}}
        container = FakeFrappeContainer([site], artifacts=artifacts)
        volumes = [_make_volume("proj_sites")]
        _wire(monkeypatch, container, volumes)

        result = _remove_project("proj", remove_volumes=True, no_backup=False)

        assert result["backup_ok"] is False
        assert result["volumes"] == 0
        assert result["dir_removed"] is False
        volumes[0].remove.assert_not_called()
        assert project_dir.exists()

    def test_uncopyable_host_artifact_blocks_volume_deletion(self, cwcli_home, monkeypatch):
        # bench backup exits 0 and the dump is listed, but the copy-out `cat`
        # fails - so nothing lands on the host and deletion must be refused.
        _patch_docker(monkeypatch)
        project_dir = _make_project_dir(core_rm.PROJECTS_DIR, "proj")
        site = "site1.localhost"
        artifacts = {site: {_db_name(site): None, _cfg_name(site): b"{}"}}
        container = FakeFrappeContainer([site], artifacts=artifacts)
        volumes = [_make_volume("proj_sites")]
        _wire(monkeypatch, container, volumes)

        result = _remove_project("proj", remove_volumes=True, no_backup=False)

        assert result["backup_ok"] is False
        assert result["volumes"] == 0
        volumes[0].remove.assert_not_called()
        assert project_dir.exists()

    def test_verified_backup_allows_volume_deletion(self, cwcli_home, monkeypatch):
        # Positive control: a fully verified backup (non-empty db dump on host)
        # lets removal proceed exactly as before.
        _patch_docker(monkeypatch)
        project_dir = _make_project_dir(core_rm.PROJECTS_DIR, "proj")
        site = "site1.localhost"
        container = FakeFrappeContainer([site])
        volumes = [_make_volume("proj_sites"), _make_volume("proj_db-data")]
        _wire(monkeypatch, container, volumes)

        result = _remove_project("proj", remove_volumes=True, no_backup=False)

        assert result["backup_ok"] is True
        assert not result["failures"]
        assert result["volumes"] == 2
        assert result["dir_removed"] is True
        for volume in volumes:
            volume.remove.assert_called_once_with(force=True)
        assert not project_dir.exists()

        # The db dump actually landed non-empty in the archive.
        archives = list(
            (cwcli_home / ".cwcli" / "archive").glob(f"proj_*/backups/{site}/*database*")
        )
        assert archives and archives[0].stat().st_size > 0

    def test_no_backup_flag_bypasses_gate(self, cwcli_home, monkeypatch):
        # --no-backup opts out of the backup net entirely: no backup is attempted
        # and removal proceeds (the user explicitly accepted no fresh backup).
        _patch_docker(monkeypatch)
        _make_project_dir(core_rm.PROJECTS_DIR, "proj")
        container = FakeFrappeContainer(["site1.localhost"], backup_ok=False)
        volumes = [_make_volume("proj_sites")]
        _wire(monkeypatch, container, volumes)

        result = _remove_project("proj", remove_volumes=True, no_backup=True)

        assert container.ran_backup() is False
        assert result["backup_ok"] is True
        assert result["volumes"] == 1
        volumes[0].remove.assert_called_once_with(force=True)

    def test_no_volumes_failed_backup_still_removes_directory(self, cwcli_home, monkeypatch):
        # Under --no-volumes the databases (named volumes) are kept, so no data is
        # destroyed. A failed live backup must therefore NOT block removal of the
        # recreatable project directory nor be recorded as a failure - the backup
        # gate applies only when the volumes will actually be deleted.
        _patch_docker(monkeypatch)
        project_dir = _make_project_dir(core_rm.PROJECTS_DIR, "proj")
        container = FakeFrappeContainer(["site1.localhost"], backup_ok=False)
        volumes = [_make_volume("proj_sites"), _make_volume("proj_db-data")]
        _wire(monkeypatch, container, volumes)

        result = _remove_project("proj", remove_volumes=False, no_backup=False)

        # The backup was attempted and failed, but no volume data is being destroyed.
        assert container.ran_backup() is True
        assert result["backup_ok"] is False
        # The volumes are left untouched (the user chose --no-volumes).
        assert result["volumes"] == 0
        for volume in volumes:
            volume.remove.assert_not_called()
        # The recreatable directory is still removed, and no backup-gate failure
        # is recorded, so the command exits cleanly.
        assert result["dir_removed"] is True
        assert not result["failures"]
        assert not project_dir.exists()

    def test_stray_non_site_entry_does_not_block_deletion(self, cwcli_home, monkeypatch):
        # A real bench sites/ dir also holds non-site entries like currentsite.txt.
        # Such an entry has no site_config.json, so it must NOT be treated as a
        # site - otherwise `bench --site currentsite.txt backup` fails and wrongly
        # blocks the default removal. The one real site backs up fine, so removal
        # proceeds and the volumes are deleted.
        _patch_docker(monkeypatch)
        project_dir = _make_project_dir(core_rm.PROJECTS_DIR, "proj")
        site = "site1.localhost"
        container = FakeFrappeContainer([site], extra_entries=["currentsite.txt"])
        volumes = [_make_volume("proj_sites"), _make_volume("proj_db-data")]
        _wire(monkeypatch, container, volumes)

        result = _remove_project("proj", remove_volumes=True, no_backup=False)

        # The stray entry was never handed to `bench backup`.
        assert not any("currentsite.txt" in c and "backup" in c for c in container.calls)
        assert result["backup_ok"] is True
        assert not result["failures"]
        assert result["volumes"] == 2
        assert result["dir_removed"] is True
        assert not project_dir.exists()

    def test_gate_blocked_removal_preserves_cache(self, cwcli_home, monkeypatch):
        # When the backup gate blocks removal under --volumes, the containers are
        # gone but the named volumes + project directory survive. The cache entry
        # must therefore be KEPT so the half-removed project stays visible in
        # `ls`/`inspect` and can be retried, not cleared into invisibility.
        _patch_docker(monkeypatch)
        project_dir = _make_project_dir(core_rm.PROJECTS_DIR, "proj")
        container = FakeFrappeContainer(["site1.localhost"], backup_ok=False)
        volumes = [_make_volume("proj_sites")]
        _patch_attr(monkeypatch, "get_project_containers", lambda name: [container])
        _patch_attr(monkeypatch, "get_project_volumes", lambda name: list(volumes))
        monkeypatch.setattr(db_utils, "get_cached_project_data", lambda name: None)
        clear_cache = MagicMock()
        monkeypatch.setattr(db_utils, "clear_cache_for_project", clear_cache)

        result = _remove_project("proj", remove_volumes=True, no_backup=False)

        assert result["backup_ok"] is False
        assert result["failures"]
        assert result["volumes"] == 0
        assert result["dir_removed"] is False
        assert project_dir.exists()
        clear_cache.assert_not_called()

    def test_completed_removal_clears_cache(self, cwcli_home, monkeypatch):
        # Positive control: a fully completed removal still clears the cache.
        _patch_docker(monkeypatch)
        _make_project_dir(core_rm.PROJECTS_DIR, "proj")
        container = FakeFrappeContainer(["site1.localhost"])
        volumes = [_make_volume("proj_sites")]
        _patch_attr(monkeypatch, "get_project_containers", lambda name: [container])
        _patch_attr(monkeypatch, "get_project_volumes", lambda name: list(volumes))
        monkeypatch.setattr(db_utils, "get_cached_project_data", lambda name: None)
        clear_cache = MagicMock()
        monkeypatch.setattr(db_utils, "clear_cache_for_project", clear_cache)

        result = _remove_project("proj", remove_volumes=True, no_backup=False)

        assert not result["failures"]
        clear_cache.assert_called_once_with("proj")

    def test_postgate_volume_failure_preserves_cache(self, cwcli_home, monkeypatch):
        # The gate does NOT block here (backup ok, archive ok, containers removed),
        # but a named-volume removal fails AFTER the gate. The data-bearing volume
        # survives, so the cache must still be KEPT (keyed on no failures, not just
        # gate-not-blocked) - otherwise the project vanishes from `ls`/`inspect`
        # while its DB volume remains on disk.
        _patch_docker(monkeypatch)
        _make_project_dir(core_rm.PROJECTS_DIR, "proj")
        container = FakeFrappeContainer(["site1.localhost"])  # backup succeeds
        bad_volume = _make_volume("proj_db-data")
        bad_volume.remove.side_effect = RuntimeError("volume in use")
        _patch_attr(monkeypatch, "get_project_containers", lambda name: [container])
        _patch_attr(monkeypatch, "get_project_volumes", lambda name: [bad_volume])
        monkeypatch.setattr(db_utils, "get_cached_project_data", lambda name: None)
        clear_cache = MagicMock()
        monkeypatch.setattr(db_utils, "clear_cache_for_project", clear_cache)

        result = _remove_project("proj", remove_volumes=True, no_backup=False)

        # Gate did not block (backup verified), but the volume removal failed.
        assert result["backup_ok"] is True
        assert result["volumes"] == 0
        assert any("volume" in f for f in result["failures"])
        clear_cache.assert_not_called()

    def test_ambiguous_entry_is_treated_as_a_site(self, cwcli_home, monkeypatch):
        # Fail-safe detection: an entry whose site_config.json cannot be confirmed
        # (unreadable dir / probe error -> AMBIGUOUS) must be treated as a real
        # site that MUST be backed up. Here its backup fails, so removal is blocked
        # and the volumes are preserved - C1 stays fail-closed under ambiguity.
        _patch_docker(monkeypatch)
        project_dir = _make_project_dir(core_rm.PROJECTS_DIR, "proj")
        container = FakeFrappeContainer(
            ["site1.localhost"],
            ambiguous_entries=["mystery"],
            backup_ok={"site1.localhost": True, "mystery": False},
        )
        volumes = [_make_volume("proj_sites")]
        _wire(monkeypatch, container, volumes)

        result = _remove_project("proj", remove_volumes=True, no_backup=False)

        # The ambiguous entry WAS handed to `bench backup` (treated as a site)...
        assert any("mystery" in c and "backup" in c for c in container.calls)
        # ...and because its backup failed, the whole removal is blocked.
        assert result["backup_ok"] is False
        assert result["volumes"] == 0
        assert result["dir_removed"] is False
        volumes[0].remove.assert_not_called()
        assert project_dir.exists()

    def test_stale_prior_run_artifact_does_not_block_deletion(self, cwcli_home, monkeypatch):
        # Regression for the fixed backup-verify window: a site backed up more than
        # once has a stale artifact from an OLDER run (a different leading timestamp
        # token) whose copy-out `cat` fails. The verify set is scoped to the CURRENT
        # run, so that stale file is never reached and cannot falsely fail an
        # otherwise complete fresh backup - removal proceeds under --volumes.
        _patch_docker(monkeypatch)
        project_dir = _make_project_dir(core_rm.PROJECTS_DIR, "proj")
        site = "site1.localhost"
        # Current-run files first (so `ls -1t`/backup_files[0] is a current file),
        # the stale older-run file last.
        artifacts = {
            site: {
                f"20260703_120000-{site}-database.sql.gz": b"DBDUMPBYTES",
                f"20260703_120000-{site}-site_config_backup.json": b"{}",
                f"20260703_120000-{site}-files.tar": b"TARBYTES",
                f"20250101_000000-{site}-database.sql.gz": None,  # stale, cat fails
            }
        }
        container = FakeFrappeContainer([site], artifacts=artifacts)
        volumes = [_make_volume("proj_sites"), _make_volume("proj_db-data")]
        _wire(monkeypatch, container, volumes)

        result = _remove_project("proj", remove_volumes=True, no_backup=False)

        assert result["backup_ok"] is True
        assert not result["failures"]
        assert result["volumes"] == 2
        assert result["dir_removed"] is True
        for volume in volumes:
            volume.remove.assert_called_once_with(force=True)
        assert not project_dir.exists()
        # The stale prior-run file was never copied out.
        assert not any(
            f"20250101_000000-{site}-database.sql.gz" in p for p in container.get_archive_calls
        )


class TestMultiBench:
    """Multi-bench instances must back up ALL benches before volume deletion."""

    BENCH0 = "/workspace/frappe-bench"
    BENCH1 = "/workspace/second-bench"

    def _make_cached_data(self, bench_paths):
        return {"bench_instances": [{"path": bp} for bp in bench_paths]}

    def test_two_benches_both_ok_allows_deletion(self, cwcli_home, monkeypatch):
        """Both benches back up fully -> gate passes, volumes are deleted."""
        from .bench_fakes_mb import FakeFrappeContainerMB

        _patch_docker(monkeypatch)
        _make_project_dir(core_rm.PROJECTS_DIR, "proj")
        container = FakeFrappeContainerMB(
            {
                self.BENCH0: {"sites": ["s0.localhost"]},
                self.BENCH1: {"sites": ["s1.localhost"]},
            }
        )
        volumes = [_make_volume("proj_sites"), _make_volume("proj_db-data")]
        _patch_attr(monkeypatch, "get_project_containers", lambda name: [container])
        _patch_attr(monkeypatch, "get_project_volumes", lambda name: list(volumes))
        monkeypatch.setattr(db_utils, "clear_cache_for_project", lambda name: None)
        monkeypatch.setattr(
            db_utils,
            "get_cached_project_data",
            lambda name: self._make_cached_data([self.BENCH0, self.BENCH1]),
        )

        result = _remove_project("proj", remove_volumes=True, no_backup=False)

        assert result["backup_ok"] is True
        assert not result["failures"]
        assert result["volumes"] == 2
        assert result["dir_removed"] is True
        for v in volumes:
            v.remove.assert_called_once_with(force=True)

    def test_two_benches_one_fails_blocks_deletion(self, cwcli_home, monkeypatch):
        """Bench 0 backs up, bench 1 fails -> gate blocks, nothing deleted."""
        from .bench_fakes_mb import FakeFrappeContainerMB

        _patch_docker(monkeypatch)
        project_dir = _make_project_dir(core_rm.PROJECTS_DIR, "proj")
        container = FakeFrappeContainerMB(
            {
                self.BENCH0: {"sites": ["s0.localhost"], "backup_ok": True},
                self.BENCH1: {"sites": ["s1.localhost"], "backup_ok": False},
            }
        )
        volumes = [_make_volume("proj_sites"), _make_volume("proj_db-data")]
        _patch_attr(monkeypatch, "get_project_containers", lambda name: [container])
        _patch_attr(monkeypatch, "get_project_volumes", lambda name: list(volumes))
        monkeypatch.setattr(db_utils, "clear_cache_for_project", lambda name: None)
        monkeypatch.setattr(
            db_utils,
            "get_cached_project_data",
            lambda name: self._make_cached_data([self.BENCH0, self.BENCH1]),
        )

        result = _remove_project("proj", remove_volumes=True, no_backup=False)

        assert result["backup_ok"] is False
        assert result["failures"]
        assert result["volumes"] == 0
        assert result["dir_removed"] is False
        assert project_dir.exists()
        for v in volumes:
            v.remove.assert_not_called()

    def test_single_bench_regression(self, cwcli_home, monkeypatch):
        """Single-bench instances still work (backup_ok=True -> deletion)."""
        from .bench_fakes_mb import FakeFrappeContainerMB

        _patch_docker(monkeypatch)
        _make_project_dir(core_rm.PROJECTS_DIR, "proj")
        container = FakeFrappeContainerMB(
            {
                self.BENCH0: {"sites": ["site1.localhost"]},
            }
        )
        volumes = [_make_volume("proj_sites"), _make_volume("proj_db-data")]
        _patch_attr(monkeypatch, "get_project_containers", lambda name: [container])
        _patch_attr(monkeypatch, "get_project_volumes", lambda name: list(volumes))
        monkeypatch.setattr(db_utils, "clear_cache_for_project", lambda name: None)
        monkeypatch.setattr(
            db_utils,
            "get_cached_project_data",
            lambda name: self._make_cached_data([self.BENCH0]),
        )

        result = _remove_project("proj", remove_volumes=True, no_backup=False)

        assert result["backup_ok"] is True
        assert not result["failures"]
        assert result["volumes"] == 2
        assert result["dir_removed"] is True

    def test_cache_failure_fallback_to_default_bench(
        self,
        cwcli_home,
        monkeypatch,
        capsys,
    ):
        """When the cache lookup raises, _remove_project falls back to the
        default bench path ``/workspace/frappe-bench`` instead of silently
        skipping backup on a multi-bench instance."""
        from .bench_fakes_mb import FakeFrappeContainerMB

        _patch_docker(monkeypatch)
        _make_project_dir(core_rm.PROJECTS_DIR, "proj")
        container = FakeFrappeContainerMB(
            {
                self.BENCH0: {"sites": ["s0.localhost"]},
            }
        )
        volumes = [_make_volume("proj_sites"), _make_volume("proj_db-data")]
        _patch_attr(monkeypatch, "get_project_containers", lambda name: [container])
        _patch_attr(monkeypatch, "get_project_volumes", lambda name: list(volumes))
        monkeypatch.setattr(db_utils, "clear_cache_for_project", lambda name: None)
        monkeypatch.setattr(
            db_utils,
            "get_cached_project_data",
            lambda name: (_ for _ in ()).throw(RuntimeError("cache corrupted")),
        )

        result = _remove_project("proj", remove_volumes=True, no_backup=False)

        # The backup proceeded on the default bench path (single bench) and
        # succeeded, so the gate passes and volumes are deleted.
        assert result["backup_ok"] is True
        assert result["volumes"] == 2
        assert result["dir_removed"] is True
        # A warning was emitted about the cache failure and fallback.
        err = capsys.readouterr().err
        assert "cache" in err.lower()
        assert "falling back" in err.lower()

    def test_cache_failure_with_live_discovery_backs_up_every_bench(
        self,
        cwcli_home,
        monkeypatch,
        capsys,
    ):
        """When the cache lookup raises but live ``find``-based discovery
        succeeds, ALL discovered benches are backed up - not just a single
        default bench path. Isolates ``_find_bench_instances`` from the real
        host's ``~/.cwcli/config`` by stubbing its config lookup."""
        from caffeinated_whale_cli.commands import inspect as inspect_mod

        from .bench_fakes_mb import FakeFrappeContainerMB

        discovered_root = "/workspace/development"
        bench_a = f"{discovered_root}/bench1"
        bench_b = f"{discovered_root}/bench2"

        class DiscoveringContainer(FakeFrappeContainerMB):
            def exec_run(self, cmd, workdir=None):
                if cmd == f"find {discovered_root} -maxdepth 2 -type d -name 'apps'":
                    self.calls.append(cmd)
                    return (0, f"{bench_a}/apps\n{bench_b}/apps".encode())
                if cmd.startswith("find "):
                    self.calls.append(cmd)
                    return (1, b"")
                if cmd.startswith('sh -c "test -d'):
                    self.calls.append(cmd)
                    return (0, b"")
                return super().exec_run(cmd, workdir=workdir)

        monkeypatch.setattr(
            inspect_mod.config_utils,
            "load_config",
            lambda: {"search_paths": {"custom_bench_paths": []}},
        )
        _patch_docker(monkeypatch)
        _make_project_dir(core_rm.PROJECTS_DIR, "proj")
        container = DiscoveringContainer(
            {
                bench_a: {"sites": ["s1.localhost"]},
                bench_b: {"sites": ["s2.localhost"]},
            }
        )
        volumes = [_make_volume("proj_sites"), _make_volume("proj_db-data")]
        _patch_attr(monkeypatch, "get_project_containers", lambda name: [container])
        _patch_attr(monkeypatch, "get_project_volumes", lambda name: list(volumes))
        monkeypatch.setattr(db_utils, "clear_cache_for_project", lambda name: None)
        monkeypatch.setattr(
            db_utils,
            "get_cached_project_data",
            lambda name: (_ for _ in ()).throw(RuntimeError("cache corrupted")),
        )

        result = _remove_project("proj", remove_volumes=True, no_backup=False)

        # Both live-discovered benches were backed up, not just one.
        assert container.ran_bench_backup(bench_a)
        assert container.ran_bench_backup(bench_b)
        assert result["backup_ok"] is True
        assert result["volumes"] == 2
        assert result["dir_removed"] is True
        # Rich may soft-wrap the line, so compare with whitespace collapsed.
        err = " ".join(capsys.readouterr().err.lower().split())
        assert "discovered 2 bench(es) live" in err


class TestConfigArchiveWarning:
    """A failed config archive is a warning only - it must not block volume/
    directory deletion or cache clearing (unlike a failed backup), and it must
    print exactly once, not twice (the per-bench caller in ``_remove_project``
    owns the warning; ``_archive_project_config`` itself must stay silent on
    failure)."""

    def test_archive_failure_warns_once_and_does_not_block(
        self,
        cwcli_home,
        monkeypatch,
        capsys,
    ):
        class RaisingArchiveContainer(FakeFrappeContainer):
            """Backs up normally, but any `cat` probe (used only by config
            archiving, never by the backup path) raises - simulating a genuine
            docker exec failure during config archiving."""

            def exec_run(self, cmd, workdir=None):
                if cmd.startswith("cat "):
                    raise RuntimeError("simulated docker exec failure")
                return super().exec_run(cmd, workdir=workdir)

        _patch_docker(monkeypatch)
        _make_project_dir(core_rm.PROJECTS_DIR, "proj")
        container = RaisingArchiveContainer(["site1.localhost"])
        volumes = [_make_volume("proj_sites"), _make_volume("proj_db-data")]
        _wire(monkeypatch, container, volumes)

        result = _remove_project("proj", remove_volumes=True, no_backup=False)

        # The backup itself succeeded; only config archiving failed, and a
        # failed config archive must not block deletion or cache clearing.
        assert result["backup_ok"] is True
        assert not result["failures"]
        assert result["volumes"] == 2
        assert result["dir_removed"] is True

        err = capsys.readouterr().err
        assert err.count("Could not archive configuration") == 1


class TestBackupSitesReturn:
    """``_backup_sites`` must report success only when every site is captured."""

    def _archive(self, tmp_path):
        d = tmp_path / "archive"
        d.mkdir()
        return d

    def test_all_sites_backed_up_returns_true(self, tmp_path):
        container = FakeFrappeContainer(["a.localhost", "b.localhost"])
        assert _backup_sites("proj", container, BENCH, self._archive(tmp_path)) is True

    def test_partial_backup_returns_false(self, tmp_path):
        # One site succeeds, the other's `bench backup` fails -> overall False.
        container = FakeFrappeContainer(
            ["a.localhost", "b.localhost"],
            backup_ok={"a.localhost": True, "b.localhost": False},
        )
        assert _backup_sites("proj", container, BENCH, self._archive(tmp_path)) is False

    def test_no_sites_returns_true(self, tmp_path):
        container = FakeFrappeContainer([])
        assert _backup_sites("proj", container, BENCH, self._archive(tmp_path)) is True

    def test_stray_non_site_entry_not_treated_as_site(self, tmp_path):
        # currentsite.txt has no site_config.json, so it is not a site: it must
        # never be backed up nor counted as a failed site.
        container = FakeFrappeContainer(["a.localhost"], extra_entries=["currentsite.txt"])
        assert _backup_sites("proj", container, BENCH, self._archive(tmp_path)) is True
        assert not any("currentsite.txt" in c and "backup" in c for c in container.calls)

    def test_empty_dump_returns_false(self, tmp_path):
        site = "a.localhost"
        container = FakeFrappeContainer([site], artifacts={site: {_db_name(site): b""}})
        assert _backup_sites("proj", container, BENCH, self._archive(tmp_path)) is False

    def test_no_dump_among_files_returns_false(self, tmp_path):
        # bench backup exits 0 but only a config file exists - no database dump.
        site = "a.localhost"
        container = FakeFrappeContainer([site], artifacts={site: {_cfg_name(site): b"{}"}})
        assert _backup_sites("proj", container, BENCH, self._archive(tmp_path)) is False

    def test_secondary_artifact_copy_failure_returns_false(self, tmp_path):
        # The db dump copies out fine, but a companion --with-files artifact fails
        # to copy. Fail closed: a backup missing any part is not trustworthy.
        site = "a.localhost"
        artifacts = {
            site: {
                _db_name(site): b"DBDUMPBYTES",
                f"backup-{site}-files.tar": None,  # cat fails for this one
            }
        }
        container = FakeFrappeContainer([site], artifacts=artifacts)
        assert _backup_sites("proj", container, BENCH, self._archive(tmp_path)) is False

    def test_stale_prior_run_artifact_excluded_from_verification(self, tmp_path):
        # The current run's artifacts share a leading timestamp token; a stale
        # file from an older run carries a DIFFERENT token. Only the current run
        # is copied/verified, so the stale file's failing `cat` is never reached
        # and the fresh, complete backup reports success.
        site = "a.localhost"
        artifacts = {
            site: {
                f"20260703_120000-{site}-database.sql.gz": b"DBDUMPBYTES",
                f"20260703_120000-{site}-site_config_backup.json": b"{}",
                f"20260703_120000-{site}-files.tar": b"TARBYTES",
                f"20250101_000000-{site}-database.sql.gz": None,  # stale, cat fails
            }
        }
        container = FakeFrappeContainer([site], artifacts=artifacts)
        assert _backup_sites("proj", container, BENCH, self._archive(tmp_path)) is True
        assert not any(
            f"20250101_000000-{site}-database.sql.gz" in p for p in container.get_archive_calls
        )

    def test_copy_is_streamed_via_get_archive_not_cat(self, tmp_path):
        # The copy must go through the chunked get_archive stream (so a multi-GB
        # artifact is never buffered whole in host RAM), never a whole-file `cat`.
        site = "a.localhost"
        payload = b"DBDUMPBYTES" * 4096  # large enough to span many stream chunks
        artifacts = {site: {_db_name(site): payload, _cfg_name(site): b"{}"}}
        container = FakeFrappeContainer([site], artifacts=artifacts)
        archive = self._archive(tmp_path)

        assert _backup_sites("proj", container, BENCH, archive) is True

        # get_archive was used to copy the artifacts out...
        assert container.get_archive_calls
        assert any(_db_name(site) in p for p in container.get_archive_calls)
        # ...and no whole-file `cat` of a backup artifact was issued.
        sbdir = f"{BENCH}/sites/{site}/private/backups"
        assert not any(c.startswith(f"cat {sbdir}/") for c in container.calls)
        # The streamed bytes reassembled exactly on the host.
        dumped = archive / "backups" / site / _db_name(site)
        assert dumped.read_bytes() == payload


class _StreamOnlyContainer:
    """A container exposing ONLY ``get_archive`` (no ``exec_run``), used to
    drive ``_stream_container_file`` directly. It yields the tar wrapper in
    tiny chunks and counts how many are pulled, so a test can prove the copy is
    consumed incrementally rather than read as one blob. ``truncate`` drops the
    tail of the tar to model a mid-stream failure."""

    def __init__(self, name, data, *, chunk_size=8, truncate=False, raises=False):
        self._name = name
        self._data = data
        self._chunk_size = chunk_size
        self._truncate = truncate
        self._raises = raises
        self.pulls = 0

    def get_archive(self, path):
        if self._raises:
            raise FileNotFoundError(path)
        raw = _tar_bytes(self._name, self._data)
        if self._truncate:
            raw = raw[: len(raw) // 2]  # cut the data section -> short read

        def _gen():
            for i in range(0, len(raw), self._chunk_size):
                self.pulls += 1
                yield raw[i : i + self._chunk_size]

        return _gen(), {"name": self._name, "size": len(self._data)}


class TestStreamedCopy:
    """``_stream_container_file`` streams the artifact in chunks and fails
    closed on any copy error, never buffering the whole file in RAM."""

    def test_streams_in_chunks_and_reassembles_exactly(self, tmp_path):
        data = b"HELLO-WORLD" * 2000  # ~22 KB, many 8-byte stream chunks
        container = _StreamOnlyContainer("db.sql.gz", data, chunk_size=8)
        dest = tmp_path / "out.gz"

        assert core_rm._stream_container_file(container, "/src/db.sql.gz", dest) is True
        assert dest.read_bytes() == data
        # The stream was pulled in many small chunks, not one giant read.
        assert container.pulls > 1

    def test_truncated_stream_fails_closed(self, tmp_path):
        # A stream that ends before the member's declared size must NOT be
        # trusted as a completed copy.
        data = b"HELLO-WORLD" * 2000
        container = _StreamOnlyContainer("db.sql.gz", data, chunk_size=8, truncate=True)
        dest = tmp_path / "out.gz"

        assert core_rm._stream_container_file(container, "/src/db.sql.gz", dest) is False

    def test_missing_file_fails_closed(self, tmp_path):
        # get_archive raising (missing/unreadable path) must fail closed.
        container = _StreamOnlyContainer("db.sql.gz", b"x", raises=True)
        dest = tmp_path / "out.gz"

        assert core_rm._stream_container_file(container, "/src/db.sql.gz", dest) is False

    def test_empty_artifact_copies_but_lands_zero_bytes(self, tmp_path):
        # A 0-byte artifact copies successfully (the size gate in _backup_sites,
        # not this helper, is what rejects an empty DB dump).
        container = _StreamOnlyContainer("db.sql.gz", b"", chunk_size=8)
        dest = tmp_path / "out.gz"

        assert core_rm._stream_container_file(container, "/src/db.sql.gz", dest) is True
        assert dest.read_bytes() == b""


# --------------------------------------------------------------------------- #
# H5 - project-name validation before any rmtree / volume op
# --------------------------------------------------------------------------- #


class TestProjectNameValidation:
    @pytest.mark.parametrize("name", ["proj", "my-project", "a_b.localhost"])
    def test_valid_names_accepted(self, cwcli_home, name):
        """`_is_valid_project_name` accepts safe project names."""
        assert core_rm.is_valid_project_name(name) is True

    @pytest.mark.parametrize(
        "name",
        ["", ".", "..", "/", "/etc", "a/b", "..\\x", "sub/../../etc", "\0evil"],
    )
    def test_escaping_names_rejected(self, cwcli_home, name):
        """`_is_valid_project_name` rejects path-escaping project names."""
        assert core_rm.is_valid_project_name(name) is False

    def test_delete_directory_refuses_escaping_path(self, cwcli_home):
        # PROJECTS_DIR/.. resolves to the tmp root; a sentinel there must survive.
        sentinel = cwcli_home / "SENTINEL"
        sentinel.write_text("keep me")

        assert _delete_project_directory("..") is False
        assert sentinel.exists(), "rmtree escaped PROJECTS_DIR and deleted the parent"
        assert core_rm.PROJECTS_DIR.exists()

    def test_archive_directory_refuses_escaping_path(self, cwcli_home, tmp_path):
        archive_dir = cwcli_home / "archive"
        archive_dir.mkdir()
        # An escaping name must not be archived (returning False keeps the caller
        # from then proceeding to delete).
        assert _archive_project_directory("..", archive_dir=archive_dir) is False

    def test_remove_project_rejects_invalid_name(self, cwcli_home, monkeypatch):
        # Defense-in-depth: even a direct call must refuse before touching Docker.
        # Changed BY DESIGN in batch 12: the core RAISES CwcliError(USAGE) for an
        # invalid name (a core function does not trust its caller), where the old
        # _remove_project returned a found=False/failures dict.
        _patch_docker(monkeypatch)
        called = MagicMock()
        _patch_attr(monkeypatch, "get_project_containers", called)

        with pytest.raises(CwcliError) as exc:
            _remove_project("..", remove_volumes=True, no_backup=True)

        assert exc.value.kind is ErrorKind.USAGE
        called.assert_not_called()

    @pytest.mark.parametrize("name", ["..", ".", "/etc", "a/b"])
    def test_cli_rejects_escaping_name_before_removal(self, cwcli_home, monkeypatch, name):
        _patch_docker(monkeypatch)
        removed = MagicMock()
        # The CLI pre-filter must reject before the core removal is ever invoked.
        monkeypatch.setattr(rm.core_rm, "remove", removed)
        monkeypatch.setattr(rm.sys.stdin, "isatty", lambda: True)

        with pytest.raises(typer.Exit) as exc:
            rm.rm(
                ctx=MagicMock(),
                verbose=False,
                volumes=True,
                no_backup=True,
                yes=True,
                project_name=[name],
            )

        assert exc.value.exit_code == 1
        removed.assert_not_called()


# --------------------------------------------------------------------------- #
# M11 - honest exit codes / step accounting
# --------------------------------------------------------------------------- #


class TestHonestOutcomes:
    def test_volume_removal_failure_is_recorded(self, cwcli_home, monkeypatch):
        _patch_docker(monkeypatch)
        _make_project_dir(core_rm.PROJECTS_DIR, "proj")
        container = FakeFrappeContainer(["s.localhost"], name="proj-frappe-1")
        bad_volume = _make_volume("proj_db-data")
        bad_volume.remove.side_effect = RuntimeError("volume in use")
        _wire(monkeypatch, container, [bad_volume])

        result = _remove_project("proj", remove_volumes=True, no_backup=True)

        assert result["volumes"] == 0
        assert any("volume" in f for f in result["failures"])

    def test_volume_enumeration_failure_is_recorded(self, cwcli_home, monkeypatch):
        _patch_docker(monkeypatch)
        _make_project_dir(core_rm.PROJECTS_DIR, "proj")
        container = FakeFrappeContainer(["s.localhost"])
        _patch_attr(monkeypatch, "get_project_containers", lambda name: [container])
        # None = a Docker error enumerating volumes (distinct from "no volumes").
        _patch_attr(monkeypatch, "get_project_volumes", lambda name: None)
        monkeypatch.setattr(db_utils, "clear_cache_for_project", lambda name: None)
        monkeypatch.setattr(db_utils, "get_cached_project_data", lambda name: None)

        result = _remove_project("proj", remove_volumes=True, no_backup=True)

        assert any("enumerate" in f for f in result["failures"])

    def test_directory_removal_failure_is_recorded(self, cwcli_home, monkeypatch):
        _patch_docker(monkeypatch)
        _make_project_dir(core_rm.PROJECTS_DIR, "proj")
        container = FakeFrappeContainer(["s.localhost"])
        _wire(monkeypatch, container, [_make_volume("proj_sites")])

        def boom(_path):
            raise OSError("permission denied")

        monkeypatch.setattr(core_rm.shutil, "rmtree", boom)

        result = _remove_project("proj", remove_volumes=True, no_backup=True)

        assert result["dir_removed"] is False
        assert any("project directory" in f for f in result["failures"])

    def test_container_removal_failure_blocks_destruction(self, cwcli_home, monkeypatch):
        # A caught container-removal error must NOT fall through to destroy the
        # volumes/dir, and it must be recorded as a failure.
        _patch_docker(monkeypatch)
        project_dir = _make_project_dir(core_rm.PROJECTS_DIR, "proj")
        container = FakeFrappeContainer(["s.localhost"])
        container.remove = MagicMock(side_effect=RuntimeError("daemon error"))
        volumes = [_make_volume("proj_sites")]
        _wire(monkeypatch, container, volumes)

        result = _remove_project("proj", remove_volumes=True, no_backup=True)

        assert result["containers"] == 0
        assert result["volumes"] == 0
        assert result["dir_removed"] is False
        assert any("container" in f for f in result["failures"])
        volumes[0].remove.assert_not_called()
        assert project_dir.exists()

    def test_container_name_property_raising_does_not_nameerror(self, cwcli_home, monkeypatch):
        # If ``container.name`` itself raises, the except handler must still run
        # cleanly (it previously hit an unbound ``container_name`` NameError).
        _patch_docker(monkeypatch)
        _make_project_dir(core_rm.PROJECTS_DIR, "proj")

        class BadNameContainer:
            status = "running"
            labels = {"com.docker.compose.service": "frappe"}

            @property
            def name(self):
                raise KeyError("Name")

            def exec_run(self, cmd, workdir=None):
                return (1, b"")  # archive-config probes fail benignly

            def stop(self):
                pass

            def remove(self, v=False, force=False):
                pass

        container = BadNameContainer()
        _wire(monkeypatch, container, [_make_volume("proj_sites")])

        # Must not raise NameError; the failure is recorded instead.
        result = _remove_project("proj", remove_volumes=True, no_backup=True)

        assert result["containers"] == 0
        assert any("container" in f for f in result["failures"])

    def test_cli_exits_nonzero_when_a_step_fails(self, cwcli_home, monkeypatch, capsys):
        _patch_docker(monkeypatch)
        monkeypatch.setattr(rm.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(
            rm.core_rm,
            "remove",
            lambda *a, **k: Result(
                status=Status.WARNING,
                data=core_rm.RemovalOutcome(
                    project="proj",
                    found=True,
                    orphan=False,
                    containers_removed=1,
                    volumes_removed=0,
                    dir_removed=False,
                    backup_ok=True,
                    failures=["could not remove volume 'proj_db-data'"],
                ),
            ),
        )

        with pytest.raises(typer.Exit) as exc:
            rm.rm(
                ctx=MagicMock(),
                verbose=False,
                volumes=True,
                no_backup=True,
                yes=True,
                project_name=["proj"],
            )

        assert exc.value.exit_code == 1
        out = capsys.readouterr()
        # No misleading green "Successfully removed" on a partial failure.
        assert "Successfully removed" not in out.out

    def test_cli_exits_zero_on_full_success(self, cwcli_home, monkeypatch, capsys):
        _patch_docker(monkeypatch)
        monkeypatch.setattr(rm.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(
            rm.core_rm,
            "remove",
            lambda *a, **k: Result(
                status=Status.OK,
                data=core_rm.RemovalOutcome(
                    project="proj",
                    found=True,
                    orphan=False,
                    containers_removed=1,
                    volumes_removed=2,
                    dir_removed=True,
                    backup_ok=True,
                    failures=[],
                ),
            ),
        )

        # A clean run must NOT raise typer.Exit and must print the success line.
        rm.rm(
            ctx=MagicMock(),
            verbose=False,
            volumes=True,
            no_backup=True,
            yes=True,
            project_name=["proj"],
        )
        assert "Successfully removed" in capsys.readouterr().out
