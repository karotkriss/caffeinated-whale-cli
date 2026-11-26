"""sendme is installed on first use by every send/receive, not only `cwcli restore`,
and only the pinned release is ever run.

The web UI's Backups > Receive and a set's Send to another machine run as jobs over
``core.transfer``; they used to refuse with "sendme is not installed" where the
command line installed it. Here the network is faked with a real archive whose
SHA-256 values (computed with ``hashlib``) stand in for the pinned ones;
``tests/e2e/test_sendme_autoinstall_e2e.py`` installs the real release through a
real job.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import shutil
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import requests

from caffeinated_whale_cli.core import doctor as core_doctor
from caffeinated_whale_cli.core import transfer
from caffeinated_whale_cli.core.backup_store import StoreStep
from caffeinated_whale_cli.core.doctor import CheckStatus
from caffeinated_whale_cli.core.errors import CwcliError
from caffeinated_whale_cli.utils import sendme_utils

TICKET = "blob" + "a" * 60
ASSET = "sendme-v0.37.1-linux-x86_64.tar.gz"
# The operator-specified source: the exact v0.37.1 release asset, never "latest".
ASSET_URL = f"https://github.com/n0-computer/sendme/releases/download/v0.37.1/{ASSET}"
GOOD = b"#!/bin/sh\necho pinned sendme\n"
EVIL = b"#!/bin/sh\necho swapped in by someone else\n"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _archive(binary: bytes) -> bytes:
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as tar:
        info = tarfile.TarInfo("sendme")
        info.size, info.mode = len(binary), 0o755
        tar.addfile(info, io.BytesIO(binary))
    return payload.getvalue()


class Release:
    """The network: serves ``archive`` for any URL and records what was asked for."""

    def __init__(self, archive: bytes) -> None:
        self.archive = archive
        self.urls: list[str] = []
        self.offline = False

    def get(self, url, **_k):
        self.urls.append(url)
        if self.offline:
            raise requests.ConnectionError("no route to host")
        release = self

        class Response:
            def raise_for_status(self):
                pass

            def iter_content(self, chunk_size):
                yield release.archive

        return Response()


@pytest.fixture
def pinned(monkeypatch, tmp_path):
    """A Linux x86_64 box whose pinned release is ``_archive(GOOD)``; sendme installs
    into ``tmp_path/bin`` and nothing is on PATH."""
    archive = _archive(GOOD)
    monkeypatch.setitem(sendme_utils._PINNED, "linux-x86_64", (ASSET, _sha(archive), _sha(GOOD)))
    monkeypatch.setattr(sendme_utils, "get_platform_target", lambda: "linux-x86_64")
    monkeypatch.setattr(sendme_utils.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sendme_utils, "get_sendme_install_dir", lambda *_a: tmp_path / "bin")
    monkeypatch.setattr(sendme_utils.shutil, "which", lambda _n: None)
    monkeypatch.setattr(core_doctor.shared_home, "shared_mode", lambda: False)
    release = Release(archive)
    monkeypatch.setattr(sendme_utils.requests, "get", release.get)
    return release


def _binary(path: Path, data: bytes = GOOD) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o755)
    return path


def _steps(events):
    return [(e.phase, e.message, e.status) for e in events if isinstance(e, StoreStep)]


def _never_run(*_a, **_k):
    pytest.fail("sendme was run though it is not the pinned release")


# ------------------------------------------------------------------ the pin


def test_the_pinned_hashes_are_the_published_v0_37_1_values():
    """Spot-check against the release's published digests (the operator's table)."""
    assert sendme_utils.PINNED_VERSION == "v0.37.1"
    assert sendme_utils._PINNED["linux-x86_64"] == (
        "sendme-v0.37.1-linux-x86_64.tar.gz",
        "6250f28ccf082a2f77a5bb2244c821783c8746b6cff4539be32cbcc03ef7d0a0",
        "5203634d8688495e972c7f8a988ff5a371619ace78d93d7eb9dcb062f269be71",
    )
    assert set(sendme_utils._PINNED) == {
        "linux-x86_64",
        "linux-aarch64",
        "darwin-x86_64",
        "darwin-aarch64",
        "windows-x86_64",
    }


def test_the_binary_check_matches_only_the_pinned_bytes(pinned, tmp_path):
    assert sendme_utils.is_pinned_sendme(_binary(tmp_path / "good", GOOD)) is True
    assert sendme_utils.is_pinned_sendme(_binary(tmp_path / "evil", EVIL)) is False
    assert sendme_utils.is_pinned_sendme(tmp_path / "missing") is False


# ------------------------------------------------------------- install and run


def test_a_run_executes_a_private_verified_copy_never_the_installed_path(pinned, tmp_path):
    """The bytes that run are the bytes that were hashed: a private copy in a fresh
    0700 directory, removed once the run is over."""
    mine = _binary(tmp_path / "bin" / "sendme")
    events: list = []
    with transfer._sendme(events.append) as path:
        copy = Path(path)
        assert copy != mine and copy.read_bytes() == GOOD
        assert os.access(copy, os.X_OK)
        assert stat.S_IMODE(copy.parent.stat().st_mode) == 0o700
        assert copy.parent.stat().st_uid == os.getuid()
        assert not copy.parent.is_relative_to(tmp_path / "bin")
    assert not copy.parent.exists()
    assert events == [] and pinned.urls == []


def test_the_private_copy_is_removed_when_the_run_fails(pinned, tmp_path):
    _binary(tmp_path / "bin" / "sendme")
    with pytest.raises(RuntimeError):
        with transfer._sendme(lambda _e: None) as path:
            raise RuntimeError("sendme died")
    assert not Path(path).parent.exists()


def test_a_copy_that_does_not_match_is_never_executed(pinned, tmp_path, monkeypatch):
    """Swap the installed file for another one exactly as it is being copied: the
    copy's own bytes are what is hashed, so it is thrown away, never run."""
    installed = _binary(tmp_path / "bin" / "sendme")
    real_open = open

    def swapping_open(file, mode="r", *a, **k):
        if str(file) == str(installed) and "b" in mode and "r" in mode:
            _binary(installed, EVIL)
        return real_open(file, mode, *a, **k)

    monkeypatch.setattr("builtins.open", swapping_open)
    monkeypatch.setattr(sendme_utils, "fetch_sendme", lambda **_k: _binary(installed, EVIL))
    monkeypatch.setenv("CWCLI_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(transfer, "_spawn_on_pty", _never_run)
    with pytest.raises(CwcliError) as e:
        transfer.receive_sets(TICKET)
    assert "still does not match its sha256" in e.value.message


def test_a_missing_sendme_is_installed_quietly_with_a_visible_step(pinned, tmp_path, capsys):
    events: list = []
    with transfer._sendme(events.append) as path:
        assert Path(path).read_bytes() == GOOD
    installed = tmp_path / "bin" / "sendme"
    assert installed.read_bytes() == GOOD and os.access(installed, os.X_OK)
    assert pinned.urls == [ASSET_URL]
    assert _steps(events) == [
        ("install", "Installing sendme v0.37.1 (sendme is not installed)", "start"),
        ("install", f"Installed sendme to {installed}", "ok"),
    ]
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""  # nothing may reach a job's or --json's stdout


def test_a_mismatched_sendme_is_replaced_by_the_pinned_release_before_it_runs(pinned, tmp_path):
    swapped = _binary(tmp_path / "bin" / "sendme", EVIL)
    events: list = []
    with transfer._sendme(events.append) as path:
        assert Path(path).read_bytes() == GOOD
    assert swapped.read_bytes() == GOOD
    start = _steps(events)[0]
    assert start[2] == "start" and "is not the pinned sendme v0.37.1" in start[1]


def test_a_mismatch_the_reinstall_does_not_fix_is_refused_and_never_run(
    pinned, tmp_path, monkeypatch
):
    swapped = _binary(tmp_path / "bin" / "sendme", EVIL)

    def reswapped(**_k):  # someone swaps it back as soon as it is installed
        return _binary(swapped, EVIL)

    monkeypatch.setattr(sendme_utils, "fetch_sendme", reswapped)
    monkeypatch.setattr(transfer, "_spawn_on_pty", _never_run)
    monkeypatch.setenv("CWCLI_HOME", str(tmp_path / "home"))
    with pytest.raises(CwcliError) as e:
        transfer.receive_sets(TICKET)
    assert e.value.code == "transfer.sendme_missing"
    assert "still does not match its sha256" in e.value.message
    assert e.value.detail["install_command"] in e.value.message


def test_a_mismatch_with_no_network_is_refused_and_never_run(pinned, tmp_path, monkeypatch):
    _binary(tmp_path / "bin" / "sendme", EVIL)
    pinned.offline = True
    monkeypatch.setattr(transfer, "_outbox", _never_run)
    events: list = []
    with pytest.raises(CwcliError) as e:
        transfer.send_sets(["p/s/id"], on_event=events.append)
    assert "is not the pinned sendme v0.37.1" in e.value.message
    assert "no route to host" in e.value.message
    assert _steps(events)[-1][2] == "failed"


def test_a_failed_install_names_the_reason_and_the_manual_command(pinned, tmp_path):
    pinned.offline = True
    events: list = []
    with pytest.raises(CwcliError) as e:
        with transfer._sendme(events.append):
            pytest.fail("ran without sendme")
    command = f"mkdir -p {tmp_path / 'bin'} && cd {tmp_path / 'bin'} && "
    assert e.value.code == "transfer.sendme_missing"
    assert "Network error" in e.value.message and "no route to host" in e.value.message
    assert command in e.value.message
    assert "\n" not in e.value.message
    assert e.value.detail["install_command"].startswith(command)
    assert _steps(events)[-1] == (
        "install",
        "Installing sendme v0.37.1 (sendme is not installed)",
        "failed",
    )


def test_the_manual_command_downloads_the_pinned_asset_and_checks_its_sha256(pinned):
    command = sendme_utils.manual_install_command()
    assert f"curl -fsSLO {ASSET_URL}" in command
    assert f"echo '{_sha(pinned.archive)}  {ASSET}' | sha256sum -c -" in command
    assert "sendme.sh" not in command and "latest" not in command


def test_receive_installs_sendme_before_it_downloads(pinned, monkeypatch, tmp_path):
    """The web UI's receive job path: the install step comes first, and a failed
    install stops the receive before anything is downloaded."""
    monkeypatch.setenv("CWCLI_HOME", str(tmp_path / "home"))
    pinned.offline = True
    monkeypatch.setattr(transfer, "_spawn_on_pty", _never_run)
    events: list = []
    with pytest.raises(CwcliError) as e:
        transfer.receive_sets(TICKET, on_event=events.append)
    assert e.value.code == "transfer.sendme_missing"
    assert _steps(events)[0][2] == "start"


def test_an_archive_that_does_not_match_is_rejected_before_it_is_extracted(
    pinned, tmp_path, monkeypatch
):
    pinned.archive = _archive(EVIL)
    monkeypatch.setattr(tarfile, "open", lambda *a, **k: pytest.fail("extracted"))
    with pytest.raises(sendme_utils.SendmeInstallError) as e:
        sendme_utils.fetch_sendme(progress=False)
    assert f"the downloaded {ASSET} does not match its pinned sha256" in str(e.value)
    assert not (tmp_path / "bin" / "sendme").exists()


def test_a_binary_that_does_not_match_is_not_installed(pinned, tmp_path, monkeypatch):
    archive = _archive(EVIL)
    pinned.archive = archive
    monkeypatch.setitem(sendme_utils._PINNED, "linux-x86_64", (ASSET, _sha(archive), _sha(GOOD)))
    with pytest.raises(sendme_utils.SendmeInstallError) as e:
        sendme_utils.fetch_sendme(progress=False)
    assert "does not match its pinned sha256" in str(e.value)
    assert list((tmp_path / "bin").iterdir()) == []


@pytest.mark.skipif(sys.platform != "linux", reason="ETXTBSY is Linux behavior")
def test_an_install_replaces_a_sendme_another_job_is_running(pinned, tmp_path):
    """Two web UI jobs installing on first use: writing over a running binary fails
    with ETXTBSY on Linux, so the install must swap a new file into place."""
    install_dir = tmp_path / "bin"
    install_dir.mkdir()
    running = install_dir / "sendme"
    shutil.copy2(os.path.realpath(sys.executable), running)
    proc = subprocess.Popen(
        ["python", "-c", "import time; time.sleep(30)"],
        executable=str(running),
        env={**os.environ, "PYTHONHOME": sys.base_prefix},
    )
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            proc.wait(timeout=0.2)
        path = sendme_utils.fetch_sendme(progress=False)
        assert path.read_bytes() == GOOD
        assert sorted(p.name for p in install_dir.iterdir()) == ["sendme"]
    finally:
        proc.kill()
        proc.wait()


# ------------------------------------------ where the private copy is allowed to run


@pytest.fixture(autouse=True)
def _fresh_exec_root():
    chooser = sendme_utils.private_exec_root
    chooser.cache_clear()
    yield
    chooser.cache_clear()


@pytest.fixture
def candidates(monkeypatch, tmp_path):
    """$XDG_RUNTIME_DIR, the temp dir and a 0700 home, with the exec probe faked: only
    the roots in the returned set let a file run (a noexec mount needs no real mount)."""
    runtime, temp, home = tmp_path / "run", tmp_path / "tmp", tmp_path / "home"
    for path in (runtime, temp, home):
        path.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(sendme_utils.tempfile, "gettempdir", lambda: str(temp))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    executable: set[Path] = set()
    monkeypatch.setattr(sendme_utils, "_can_exec_in", lambda root: root in executable)
    return runtime, temp, home, executable


def test_the_runtime_dir_is_preferred_when_it_executes(candidates):
    runtime, temp, _home, executable = candidates
    executable.update({runtime, temp})
    assert sendme_utils.private_exec_root() == runtime


def test_a_noexec_runtime_dir_falls_back_to_the_temp_dir(candidates):
    _runtime, temp, _home, executable = candidates
    executable.add(temp)
    assert sendme_utils.private_exec_root() == temp


def test_a_noexec_temp_dir_falls_back_to_a_private_cache_in_the_home(candidates):
    _runtime, _temp, home, executable = candidates
    cache = home / ".cache" / "cwcli" / "sendme"
    executable.add(cache)
    assert sendme_utils.private_exec_root() == cache
    for level in (home / ".cache", home / ".cache" / "cwcli", cache):
        assert stat.S_IMODE(level.stat().st_mode) == 0o700


def test_a_group_writable_home_is_never_a_candidate(candidates):
    _runtime, _temp, home, executable = candidates
    home.chmod(0o770)
    executable.add(home / ".cache" / "cwcli" / "sendme")
    assert sendme_utils.private_exec_root() is None
    assert not (home / ".cache").exists()


def test_the_exec_probe_runs_a_file_in_a_private_directory(tmp_path):
    assert sendme_utils._can_exec_in(tmp_path) is True
    assert list(tmp_path.iterdir()) == []  # the probe cleans up after itself
    assert sendme_utils._can_exec_in(tmp_path / "missing") is False


def test_with_nowhere_to_execute_the_installed_binary_runs_after_one_warning(
    pinned, tmp_path, monkeypatch
):
    monkeypatch.setattr(sendme_utils, "private_exec_root", lambda: None)
    installed = _binary(tmp_path / "bin" / "sendme")
    events: list = []
    with transfer._sendme(events.append) as path:
        assert path == str(installed)
    notices = [e for e in events if type(e).__name__ == "StoreNotice"]
    assert [(n.text, n.level) for n in notices] == [(sendme_utils.WEAK_CHECK_WARNING, "warning")]


def test_with_nowhere_to_execute_a_mismatched_binary_is_still_never_run(
    pinned, tmp_path, monkeypatch
):
    monkeypatch.setattr(sendme_utils, "private_exec_root", lambda: None)
    _binary(tmp_path / "bin" / "sendme", EVIL)
    pinned.offline = True
    with pytest.raises(CwcliError) as e:
        with transfer._sendme(lambda _e: None):
            pytest.fail("ran a sendme that is not the pinned release")
    assert e.value.code == "transfer.sendme_missing"


@pytest.mark.parametrize("pty", [True, False])
def test_a_sendme_that_cannot_be_executed_is_a_clear_error(pty, monkeypatch, tmp_path):
    """A noexec mount or a wrong-architecture binary refuses at exec: the job gets a
    cwcli error naming the cause, never a raw PermissionError."""
    monkeypatch.setenv("CWCLI_HOME", str(tmp_path / "home"))
    blocked = tmp_path / "sendme"
    blocked.write_bytes(GOOD)
    blocked.chmod(0o600)  # exec refused with EACCES, as on a noexec mount
    monkeypatch.setattr(transfer, "_sendme", lambda _e: contextlib.nullcontext(str(blocked)))
    if not pty:
        monkeypatch.delattr(transfer.os, "openpty")
    with pytest.raises(CwcliError) as e:
        transfer.receive_sets(TICKET)
    assert e.value.code == "transfer.sendme_exec"
    assert f"Could not run sendme ({blocked})" in e.value.message
    assert "Permission denied" in e.value.message


def test_a_send_whose_sendme_cannot_be_executed_is_a_clear_error(monkeypatch, tmp_path):
    blocked = tmp_path / "sendme"
    blocked.write_bytes(GOOD)
    blocked.chmod(0o600)
    box = tmp_path / "outbox" / "set"
    box.mkdir(parents=True)
    monkeypatch.setattr(transfer, "_sendme", lambda _e: contextlib.nullcontext(str(blocked)))
    monkeypatch.setattr(transfer, "_outbox", lambda _k: box)
    with pytest.raises(CwcliError) as e:
        transfer.send_sets(["p/s/id"])
    assert e.value.code == "transfer.sendme_exec"
    assert not box.exists()


# --------------------------------------------------- a platform with no pinned build


@pytest.fixture
def unpinned(monkeypatch, tmp_path):
    """Linux on riscv64, which cwcli pins no sendme build for."""
    monkeypatch.setattr(sendme_utils.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sendme_utils.platform, "machine", lambda: "riscv64")
    monkeypatch.setattr(sendme_utils, "get_sendme_install_dir", lambda *_a: tmp_path / "bin")
    monkeypatch.setattr(sendme_utils.shutil, "which", lambda _n: None)
    monkeypatch.setattr(sendme_utils.requests, "get", lambda *a, **k: pytest.fail("downloaded"))
    monkeypatch.setattr(sendme_utils, "sha256_file", lambda *_a: pytest.fail("hashed"))
    monkeypatch.setattr(core_doctor.shared_home, "shared_mode", lambda: False)


def test_an_unpinned_platform_never_gets_another_platforms_build(unpinned):
    assert sendme_utils.get_platform_target() is None
    assert sendme_utils.pinned_release() is None


def test_an_unpinned_platform_runs_the_sendme_on_path_as_it_is(unpinned, monkeypatch, tmp_path):
    own = _binary(tmp_path / "usr" / "bin" / "sendme", EVIL)
    monkeypatch.setattr(sendme_utils.shutil, "which", lambda _n: str(own))
    events: list = []
    with transfer._sendme(events.append) as path:
        assert path == str(own)
    assert events == [] and not (tmp_path / "bin").exists()


def test_an_unpinned_platform_without_sendme_refuses_clearly(unpinned):
    with pytest.raises(CwcliError) as e:
        with transfer._sendme(lambda _e: None):
            pytest.fail("ran without sendme")
    assert e.value.code == "transfer.sendme_missing"
    assert "no pinned sendme build for this platform" in e.value.message
    assert "install sendme by hand" in e.value.message


def test_doctor_on_an_unpinned_platform_reports_the_path_sendme_unchecked(
    unpinned, monkeypatch, tmp_path
):
    own = _binary(tmp_path / "usr" / "bin" / "sendme")
    monkeypatch.setattr(sendme_utils.shutil, "which", lambda _n: str(own))
    status, detail, _fix = core_doctor._check_sendme()
    assert status is CheckStatus.WARN
    assert detail.startswith("no pinned build for this platform; using sendme from PATH")
    assert str(own) in detail and "verified" not in detail
    monkeypatch.setattr(sendme_utils.shutil, "which", lambda _n: None)
    status, detail, _fix = core_doctor._check_sendme()
    assert status is CheckStatus.WARN and detail.startswith("not installed")


# --------------------------------------------------------------- cwcli doctor


@pytest.fixture
def shared(pinned, monkeypatch, tmp_path):
    """A shared install whose service account (the web UI's) has its home at
    ``tmp_path/service``, and a member whose home is ``tmp_path/member``."""
    service = tmp_path / "service"
    member = tmp_path / "member"
    monkeypatch.setattr(core_doctor.shared_home, "shared_mode", lambda: True)
    monkeypatch.setattr(core_doctor.shared_home, "service_home", lambda: service)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: member))
    monkeypatch.setattr(
        sendme_utils, "get_sendme_install_dir", lambda home=None: (home or member) / ".local/bin"
    )
    return service, member


def test_doctor_verifies_the_sendme_it_would_run(pinned, tmp_path):
    mine = _binary(tmp_path / "bin" / "sendme")
    status, detail, _fix = core_doctor._check_sendme()
    assert status is CheckStatus.PASS
    assert str(mine) in detail and "sha256 verified" in detail


def test_doctor_warns_on_a_sendme_that_is_not_the_pinned_release(pinned, tmp_path):
    _binary(tmp_path / "bin" / "sendme", EVIL)
    status, detail, fix = core_doctor._check_sendme()
    assert status is CheckStatus.WARN and "is not the pinned sendme v0.37.1" in detail
    assert ASSET_URL in fix


def test_doctor_names_the_install_command_when_sendme_is_missing(pinned, tmp_path):
    status, detail, fix = core_doctor._check_sendme()
    assert status is CheckStatus.WARN and detail.startswith("not installed")
    assert f"mkdir -p {tmp_path / 'bin'}" in fix and "sha256sum -c -" in fix


def test_a_shared_install_still_installs_into_the_running_accounts_own_home(shared):
    _service, member = shared
    assert sendme_utils.get_sendme_install_dir() == member / ".local" / "bin"
    path = sendme_utils.fetch_sendme(progress=False)
    assert path == member / ".local" / "bin" / "sendme"


def test_doctor_warns_when_the_web_ui_account_has_no_sendme(shared):
    service, member = shared
    _binary(member / ".local" / "bin" / "sendme")
    status, detail, _fix = core_doctor._check_sendme()
    assert status is CheckStatus.WARN
    assert "web UI" in detail and "has no sendme" in detail


def test_doctor_reports_the_web_ui_accounts_copy_and_whether_it_matches(shared):
    service, member = shared
    _binary(member / ".local" / "bin" / "sendme")
    theirs = _binary(service / ".local" / "bin" / "sendme")
    status, detail, _fix = core_doctor._check_sendme()
    assert status is CheckStatus.PASS and f"runs {theirs} (sha256 verified)" in detail
    _binary(theirs, EVIL)
    status, detail, _fix = core_doctor._check_sendme()
    assert status is CheckStatus.WARN
    assert f"would run {theirs}, which is not the pinned release" in detail


def test_doctor_reports_the_system_sendme_the_web_ui_falls_back_to(shared, monkeypatch, tmp_path):
    """This user's own copy shadows a system sendme; the service account, with no copy
    of its own, runs the system one, so nothing needs installing for it."""
    _service, member = shared
    _binary(member / ".local" / "bin" / "sendme")
    system = _binary(tmp_path / "usr" / "bin" / "sendme")
    monkeypatch.setattr(sendme_utils.shutil, "which", lambda _n: str(system))
    status, detail, _fix = core_doctor._check_sendme()
    assert status is CheckStatus.PASS
    assert f"runs {system} (sha256 verified)" in detail and "has no sendme" not in detail


# ------------------------------------------------------- progress from sendme's bar

MIB = 1024 * 1024


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _bar(done: str, total: str, prefix: str = "[3/4] ⠁Downloading ...") -> str:
    # indicatif redraws a bar in place: clear the line, carriage return, draw
    return f"\x1b[2K\r\x1b[2m{prefix}\x1b[0m [00:00:03] [####>---] {done}/{total} 4.20 MiB/s"


@pytest.mark.parametrize(
    "done,total,expected",
    [
        ("512 B", "2.00 KiB", (512, 2048)),
        ("1.50 MiB", "3.00 MiB", (int(1.5 * MIB), 3 * MIB)),
        ("1.00 GiB", "2.00 GiB", (1024**3, 2 * 1024**3)),
        ("500 kB", "1.00 MB", (500_000, 1_000_000)),
    ],
)
def test_the_parser_reads_bytes_and_unit_suffixes(done, total, expected):
    meter = transfer.SendmeProgress("receive", clock=Clock())
    event = meter.feed(_bar(done, total) + "\r")
    assert (event.done, event.total) == expected


def test_the_parser_joins_a_bar_split_across_chunks_and_ignores_the_speed():
    meter = transfer.SendmeProgress("receive", clock=Clock())
    text = _bar("10.00 MiB", "40.00 MiB") + _bar("20.00 MiB", "40.00 MiB")
    for cut in range(0, len(text), 7):  # pty reads land anywhere, mid-number included
        meter.feed(text[cut : cut + 7])
    event = meter.finish()
    assert (event.done, event.total) == (20 * MIB, 40 * MIB)
    assert event.message == "Received 20.0 MiB of 40.0 MiB (50%)"


def test_progress_is_throttled_and_completion_always_reported():
    clock = Clock()
    meter = transfer.SendmeProgress("receive", clock=clock)
    first = meter.feed(_bar("1.00 MiB", "100.00 MiB") + "\r")
    assert first is not None and first.rate is None and first.eta is None
    clock.now = 0.5  # under a second: held back
    assert meter.feed(_bar("5.00 MiB", "100.00 MiB") + "\r") is None
    clock.now = 2.0  # a second on and the percent moved: reported, with a rate and eta
    second = meter.feed(_bar("11.00 MiB", "100.00 MiB") + "\r")
    assert second.done == 11 * MIB and second.rate == pytest.approx(5 * MIB)
    assert second.eta == pytest.approx(89 / 5)
    clock.now = 3.5  # a second on, but within the same whole percent: held back
    assert meter.feed(_bar("11.10 MiB", "100.00 MiB") + "\r") is None
    clock.now = 3.6  # complete: reported at once
    done = meter.feed(_bar("100.00 MiB", "100.00 MiB") + "\r")
    assert done.done == done.total == 100 * MIB and done.eta == 0
    assert meter.finish() is None  # nothing left to report


def test_a_quiet_stretch_still_refreshes_speed_and_time_left():
    clock = Clock()
    meter = transfer.SendmeProgress("receive", clock=clock, refresh=10)
    meter.feed(_bar("10.00 MiB", "1.00 GiB") + "\r")
    clock.now = 5
    assert meter.feed(_bar("10.20 MiB", "1.00 GiB") + "\r") is None  # same percent
    clock.now = 11
    assert meter.feed(_bar("10.40 MiB", "1.00 GiB") + "\r") is not None


def test_finish_reports_the_last_reading_it_held_back():
    clock = Clock()
    meter = transfer.SendmeProgress("receive", clock=clock)
    meter.feed(_bar("1.00 MiB", "10.00 MiB") + "\r")
    clock.now = 0.2
    assert meter.feed(_bar("3.00 MiB", "10.00 MiB") + "\r") is None
    assert meter.finish().done == 3 * MIB


def test_receive_reads_the_download_bar_and_ignores_the_export_bars_after_it():
    """sendme receive draws "[3/4] Downloading" for the whole set, then one export bar
    per file under "[4/4] Exporting"; the reading must end at the set's size, never
    jump back to a file's."""
    clock = Clock()
    meter = transfer.SendmeProgress("receive", clock=clock)
    meter.feed(_bar("0 B", "3.00 MiB", "[2/4] Getting sizes ...") + "\r")
    assert meter.finish() is None  # nothing is downloading yet
    clock.now = 1
    meter.feed(_bar("1.00 MiB", "3.00 MiB") + "\r")
    clock.now = 1.2  # held back by the throttle, so only leaving the step completes it
    assert meter.feed(_bar("2.00 MiB", "3.00 MiB") + "\r") is None
    clock.now = 1.3
    ended = meter.feed("\n[4/4] Exporting ...\n")
    assert (ended.done, ended.total) == (3 * MIB, 3 * MIB)
    for name, size in (("a.sql.gz", "2.50 MiB"), ("b.tar", "512.00 KiB")):
        clock.now += 2
        for done in ("0 B", size):
            assert meter.feed(_bar(done, size, f"[4/4] Exporting {name}") + "\r") is None
    assert meter.finish() is None  # the export bars changed nothing
    assert ended.message == "Received 3.0 MiB of 3.0 MiB (100%)"


def test_send_sums_the_latest_connections_blobs_against_the_imported_size():
    meter = transfer.SendmeProgress("send", clock=Clock())
    meter.feed("imported directory /x/.outbox/20260101-ab, 30.00 MiB, hash bafkr4i\r\n")
    meter.feed(_bar("10.00 MiB", "10.00 MiB", "n e1 r 0/0 i 1 # abc⠁") + "\n")
    meter.feed(_bar("5.00 MiB", "20.00 MiB", "n e1 r 0/0 i 2 # def⠁") + "\n")
    event = meter.finish()
    assert (event.done, event.total, event.phase) == (15 * MIB, 30 * MIB, "send")
    assert event.message.startswith("Sent 15.0 MiB of 30.0 MiB")
    # a second receiver starts again from zero rather than adding to the first
    meter.feed(_bar("2.00 MiB", "10.00 MiB", "n e2 r 1/0 i 1 # abc⠁") + "\n")
    assert meter.finish().done == 2 * MIB


def test_receive_reports_progress_from_sendmes_bar_on_a_pty(monkeypatch, tmp_path):
    """sendme draws its bar only on a terminal: the fake checks it has one."""
    monkeypatch.setenv("CWCLI_HOME", str(tmp_path / "home"))
    script = tmp_path / "sendme"
    script.write_text(
        "#!/bin/sh\n"
        "[ -t 2 ] || { echo 'no terminal' >&2; exit 3; }\n"
        "printf '\\r[3/4] Downloading ... [####>--] 1.00 MiB/2.00 MiB 1.00 MiB/s' >&2\n"
        "printf '\\r[3/4] Downloading ... [#######] 2.00 MiB/2.00 MiB 1.00 MiB/s\\n' >&2\n"
        "echo 'boom' >&2; exit 1\n"
    )
    script.chmod(0o755)
    monkeypatch.setattr(transfer, "_sendme", lambda _emit: contextlib.nullcontext(str(script)))
    events: list = []
    with pytest.raises(CwcliError) as e:
        transfer.receive_sets(TICKET, on_event=events.append)
    assert e.value.code == "transfer.receive_failed"
    assert "boom" in e.value.detail["output"]
    progress = [ev for ev in events if isinstance(ev, transfer.TransferBytes)]
    assert progress and (progress[-1].done, progress[-1].total) == (2 * MIB, 2 * MIB)
