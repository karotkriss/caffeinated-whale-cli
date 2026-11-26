"""sendme is installed on first use by every send/receive, not only `cwcli restore`.

The web UI's Backups > Receive and a set's Send to another machine run as jobs over
``core.transfer``; they used to refuse with "sendme is not installed" where the
command line installed it. The installer is mocked here; ``tests/e2e/
test_sendme_autoinstall_e2e.py`` installs the real binary through a real job.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from caffeinated_whale_cli.core import doctor as core_doctor
from caffeinated_whale_cli.core import transfer
from caffeinated_whale_cli.core.backup_store import StoreStep
from caffeinated_whale_cli.core.doctor import CheckStatus
from caffeinated_whale_cli.core.errors import CwcliError
from caffeinated_whale_cli.utils import sendme_utils

TICKET = "blob" + "a" * 60


def _steps(events):
    return [(e.phase, e.message, e.status) for e in events if isinstance(e, StoreStep)]


def _no_install(*_a, **_k):
    pytest.fail("sendme was installed though it was already present")


def test_an_installed_sendme_is_used_without_installing(monkeypatch):
    monkeypatch.setattr(sendme_utils, "is_sendme_installed", lambda: True)
    monkeypatch.setattr(sendme_utils, "get_sendme_command", lambda: "/opt/sendme")
    monkeypatch.setattr(sendme_utils, "fetch_sendme", _no_install)
    events: list = []
    assert transfer._sendme(events.append) == "/opt/sendme"
    assert events == []


def test_a_missing_sendme_is_installed_quietly_with_a_visible_step(monkeypatch, tmp_path):
    installed = tmp_path / "sendme"
    state = {"present": False}

    def fetch(*, progress=True, verbose=False):
        assert progress is False  # nothing may reach a job's or --json's stdout
        state["present"] = True
        return installed

    monkeypatch.setattr(sendme_utils, "is_sendme_installed", lambda: state["present"])
    monkeypatch.setattr(sendme_utils, "fetch_sendme", fetch)
    monkeypatch.setattr(sendme_utils, "get_sendme_command", lambda: str(installed))
    events: list = []
    assert transfer._sendme(events.append) == str(installed)
    assert _steps(events) == [
        ("install", "Installing sendme", "start"),
        ("install", f"Installed sendme to {installed}", "ok"),
    ]


def test_a_failed_install_names_the_reason_and_the_manual_command(monkeypatch, tmp_path):
    def fetch(**_k):
        raise sendme_utils.SendmeInstallError("Network error: no route")

    monkeypatch.setattr(sendme_utils, "is_sendme_installed", lambda: False)
    monkeypatch.setattr(sendme_utils, "fetch_sendme", fetch)
    monkeypatch.setattr(sendme_utils, "get_sendme_install_dir", lambda: tmp_path / "bin")
    monkeypatch.setattr(sendme_utils.platform, "system", lambda: "Linux")
    events: list = []
    with pytest.raises(CwcliError) as e:
        transfer._sendme(events.append)
    command = f"mkdir -p {tmp_path / 'bin'} && cd {tmp_path / 'bin'} && "
    assert e.value.code == "transfer.sendme_missing"
    assert "Network error: no route" in e.value.message
    assert command in e.value.message
    assert "\n" not in e.value.message
    assert e.value.detail["install_command"].startswith(command)
    assert _steps(events)[-1] == ("install", "Installing sendme", "failed")


def test_receive_installs_sendme_before_it_downloads(monkeypatch, tmp_path):
    """The web UI's receive job path: the install step comes first, and a failed
    install stops the receive before anything is downloaded."""
    monkeypatch.setenv("CWCLI_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(sendme_utils, "is_sendme_installed", lambda: False)

    def fetch(**_k):
        raise sendme_utils.SendmeInstallError("offline")

    monkeypatch.setattr(sendme_utils, "fetch_sendme", fetch)
    monkeypatch.setattr(
        transfer, "_spawn_on_pty", lambda *a, **k: pytest.fail("downloaded without sendme")
    )
    events: list = []
    with pytest.raises(CwcliError) as e:
        transfer.receive_sets(TICKET, on_event=events.append)
    assert e.value.code == "transfer.sendme_missing"
    assert _steps(events)[0] == ("install", "Installing sendme", "start")


def test_send_installs_sendme_before_it_serves(monkeypatch):
    monkeypatch.setattr(sendme_utils, "is_sendme_installed", lambda: False)

    def fetch(**_k):
        raise sendme_utils.SendmeInstallError("offline")

    monkeypatch.setattr(sendme_utils, "fetch_sendme", fetch)
    monkeypatch.setattr(transfer, "_outbox", lambda _k: pytest.fail("served without sendme"))
    with pytest.raises(CwcliError) as e:
        transfer.send_sets(["p/s/id"])
    assert e.value.code == "transfer.sendme_missing"


def _serve_release(monkeypatch, install_dir: Path) -> None:
    import io
    import tarfile

    url = "https://example.invalid/sendme-v1-linux-x86_64.tar.gz"
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as tar:
        data = b"#!/bin/sh\n"
        info = tarfile.TarInfo("sendme")
        info.size, info.mode = len(data), 0o755
        tar.addfile(info, io.BytesIO(data))

    class Response:
        def __init__(self, url):
            self.url = url

        def raise_for_status(self):
            pass

        def json(self):
            return {"assets": [{"browser_download_url": url}]}

        def iter_content(self, chunk_size):
            yield payload.getvalue()

    monkeypatch.setattr(sendme_utils, "get_platform_target", lambda: "linux-x86_64")
    monkeypatch.setattr(sendme_utils, "get_sendme_install_dir", lambda: install_dir)
    monkeypatch.setattr(sendme_utils.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sendme_utils.requests, "get", lambda u, **k: Response(u))


def test_a_quiet_install_prints_nothing(monkeypatch, tmp_path, capsys):
    install_dir = tmp_path / "bin"
    _serve_release(monkeypatch, install_dir)
    path = sendme_utils.fetch_sendme(progress=False, verbose=True)
    assert path == install_dir / "sendme" and os.access(path, os.X_OK)
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


@pytest.mark.skipif(sys.platform != "linux", reason="ETXTBSY is Linux behavior")
def test_an_install_replaces_a_sendme_another_job_is_running(monkeypatch, tmp_path):
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
        _serve_release(monkeypatch, install_dir)
        path = sendme_utils.fetch_sendme(progress=False)
        assert path.read_bytes() == b"#!/bin/sh\n"
        assert sorted(p.name for p in install_dir.iterdir()) == ["sendme"]
    finally:
        proc.kill()
        proc.wait()


# --------------------------------------------------------------- shared install


@pytest.fixture
def shared(monkeypatch, tmp_path):
    """A shared install whose service account (the web UI's) has its home at
    ``tmp_path/service``, and a member whose home is ``tmp_path/member``; nothing on PATH."""
    service = tmp_path / "service"
    member = tmp_path / "member"
    monkeypatch.setattr(core_doctor.shared_home, "shared_mode", lambda: True)
    monkeypatch.setattr(core_doctor.shared_home, "state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(core_doctor.shared_home, "service_home", lambda: service)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: member))
    monkeypatch.setattr(sendme_utils.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sendme_utils.shutil, "which", lambda _n: None)
    monkeypatch.setattr(core_doctor, "_probe_version", lambda _c: None)
    return service, member


def _binary(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_a_shared_install_still_installs_into_the_running_accounts_own_home(shared):
    """No shared executable directory: every account, the web UI's service account
    included, installs and runs its own copy, which only that account can replace."""
    _service, member = shared
    assert sendme_utils.get_sendme_install_dir() == member / ".local" / "bin"
    assert str(member / ".local" / "bin") in sendme_utils.manual_install_command()
    mine = _binary(member / ".local" / "bin" / "sendme")
    assert sendme_utils.get_sendme_command() == str(mine)


def test_doctor_warns_when_the_web_ui_account_lacks_the_copy_this_user_has(shared):
    service, member = shared
    mine = _binary(member / ".local" / "bin" / "sendme")
    status, detail, _fix = core_doctor._check_sendme()
    assert status is CheckStatus.WARN
    assert str(mine) in detail and "web UI" in detail
    assert str(service / ".local" / "bin" / "sendme") in detail
    assert "installs its own on the first send or receive" in detail


def test_doctor_passes_when_the_web_ui_account_has_its_own_copy(shared):
    service, member = shared
    mine = _binary(member / ".local" / "bin" / "sendme")
    _binary(service / ".local" / "bin" / "sendme")
    status, detail, _fix = core_doctor._check_sendme()
    assert status is CheckStatus.PASS and str(mine) in detail


def test_doctor_passes_on_a_system_sendme_both_accounts_find(shared, monkeypatch, tmp_path):
    system = _binary(tmp_path / "usr" / "bin" / "sendme")
    monkeypatch.setattr(sendme_utils.shutil, "which", lambda _n: str(system))
    status, detail, _fix = core_doctor._check_sendme()
    assert status is CheckStatus.PASS and str(system) in detail


def test_doctor_names_the_web_ui_copy_when_this_user_has_none(shared):
    service, _member = shared
    theirs = _binary(service / ".local" / "bin" / "sendme")
    status, detail, fix = core_doctor._check_sendme()
    assert status is CheckStatus.WARN
    assert detail.startswith("not installed") and str(theirs) in detail
    assert "mkdir -p" in fix


def test_doctor_names_the_install_command_when_sendme_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(sendme_utils, "is_sendme_installed", lambda: False)
    monkeypatch.setattr(sendme_utils, "get_sendme_install_dir", lambda: tmp_path / "bin")
    monkeypatch.setattr(sendme_utils.platform, "system", lambda: "Linux")
    status, _detail, fix = core_doctor._check_sendme()
    assert status is CheckStatus.WARN
    assert f"mkdir -p {tmp_path / 'bin'}" in fix and "sendme.sh | sh" in fix


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
    monkeypatch.setattr(transfer, "_sendme", lambda _emit: str(script))
    events: list = []
    with pytest.raises(CwcliError) as e:
        transfer.receive_sets(TICKET, on_event=events.append)
    assert e.value.code == "transfer.receive_failed"
    assert "boom" in e.value.detail["output"]
    progress = [ev for ev in events if isinstance(ev, transfer.TransferBytes)]
    assert progress and (progress[-1].done, progress[-1].total) == (2 * MIB, 2 * MIB)
