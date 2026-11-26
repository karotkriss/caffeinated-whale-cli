"""Move and clone sites, single and batch, on this machine and across machines
(backups spec phase 2).

A clone is a fresh verified set of the source site restored into the target site
(created first when it does not exist; an existing one is replaced only with consent,
after a verified ``before-overwrite`` set of it lands in the store); a move is a clone whose source is dropped
afterwards through the ordinary ``rm-site`` gate (its own verified archive lands in
the store too), and only after the restored site answered its post-checks. A batch
is the same per site, in order, with ``x of N`` progress; one site's failure stops
the batch before the next source is touched and never undoes a finished site.

Across machines the store set travels as a sendme ticket: ``send_sets`` serves one
set or a bundle of sets (hard-linked into an outbox, so no copy) until the job is
interrupted; ``receive_sets`` downloads a ticket and adopts every ``set.json`` it
finds into this machine's store, where the restore wizard takes over. sendme is
installed on first use and driven under a pty, because ``sendme send`` refuses to run
without a terminal and both directions draw their progress bar only on one; that bar
becomes throttled ``TransferBytes`` events for the web UI's job page. Where the OS has
no pty (Windows), ``sendme receive`` runs piped and the job shows its steps only.
"""

from __future__ import annotations

import codecs
import errno
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..utils import shared_home
from . import backup_store
from . import new_site as core_new_site
from . import rm_site as core_rm_site
from .backup_store import StoreNotice, StoreStep
from .envelope import Choice, Result, Status
from .errors import CwcliError, ErrorKind

OnEvent = Callable[[Any], None]
_TICKET = re.compile(r"sendme receive (blob[a-z2-7]+)")


@dataclass(frozen=True, slots=True)
class TransferProgress:
    """``done`` of ``total`` sites finished; the page shows it as x of N."""

    message: str
    done: int
    total: int
    site: str
    target: str


@dataclass(frozen=True, slots=True)
class Ticket:
    message: str
    ticket: str
    sets: list[str]


@dataclass(frozen=True, slots=True)
class Received:
    message: str
    sets: list[str]


@dataclass(frozen=True, slots=True)
class TransferBytes:
    """Bytes moved over sendme so far, for the web UI's progress bar. ``rate`` is
    bytes a second and ``eta`` seconds left, both None until there is a second sample."""

    message: str
    phase: str  # send | receive
    done: int
    total: int
    rate: float | None = None
    eta: float | None = None


_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")
_UNIT = r"([KMGTPE]i?B|kB|B)"
# indicatif's ``{bytes}/{total_bytes}``: "12.50 MiB/100.00 MiB" (never the "/s" speed)
_PAIR = re.compile(rf"(\d+(?:\.\d+)?)\s*{_UNIT}\s*/\s*(\d+(?:\.\d+)?)\s*{_UNIT}(?![/\w])")
# sendme send names each request "n <endpoint> r <connection>/<request> i <child>"
_REQUEST = re.compile(r"\br (\d+)/(\d+) i (\d+)\b")
# sendme receive labels its steps "[3/4] Downloading ...", "[4/4] Exporting ..."
_STAGE = re.compile(r"\[\d+/\d+\]\W*(\w+)")
_IMPORTED = re.compile(r"imported (?:file|directory) .*, (\d+(?:\.\d+)?)\s*" + _UNIT + r", hash")
_SCALE = {"B": 1, "kB": 1000}
for _i, _p in enumerate("KMGTPE", start=1):
    _SCALE[f"{_p}iB"] = 1024**_i
    _SCALE[f"{_p}B"] = 1000**_i


def _bytes(number: str, unit: str) -> int:
    return int(float(number) * _SCALE[unit])


def _human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"  # pragma: no cover - the loop always returns


class SendmeProgress:
    """Reads sendme's terminal output (indicatif bars redrawn with carriage returns
    and cursor moves) and turns it into throttled ``TransferBytes``.

    ``receive`` reads only the download bar: the per-file export bars that follow it
    are ignored, and leaving the download step completes the reading at the whole
    download's size. ``send`` sums the requests of the latest
    connection (one bar per blob, each "done/size") against the size sendme printed
    when it imported the outbox. ``feed`` returns an event at most once per
    ``interval`` seconds, and only when the whole percent moved or ``refresh``
    seconds passed, so a long transfer writes a bounded event log; completion is
    always reported, and ``finish`` reports the last reading if it was held back.
    """

    def __init__(
        self,
        phase: str,
        *,
        interval: float = 1.0,
        refresh: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.phase = phase
        self.interval = interval
        self.refresh = refresh
        self.clock = clock
        self._carry = ""
        self._imported: int | None = None
        self._requests: dict[tuple[int, int, int], tuple[int, int]] = {}
        self._reading: tuple[int, int] | None = None
        self._downloading: bool | None = None  # None before the download step, False after
        self._sent: tuple[float, int, int] | None = None  # time, done, percent
        self._rate: float | None = None

    def feed(self, text: str) -> TransferBytes | None:
        segments = re.split(r"[\r\n]", self._carry + _ANSI.sub("", text))
        self._carry = segments.pop()[-4096:]
        for segment in segments:
            self._parse(segment)
        return self._maybe_emit(final=False)

    def finish(self) -> TransferBytes | None:
        if self._carry:
            self._parse(self._carry)
            self._carry = ""
        return self._maybe_emit(final=True)

    def _parse(self, line: str) -> None:
        imported = _IMPORTED.search(line)
        if imported:
            self._imported = _bytes(imported.group(1), imported.group(2))
        if self.phase != "send":
            stage = _STAGE.search(line)
            if stage is not None:
                downloading = stage.group(1).lower().startswith("download")
                if downloading and self._downloading is None:
                    self._downloading = True
                elif not downloading and self._downloading:
                    self._downloading = False
                    if self._reading is not None:
                        self._reading = (self._reading[1], self._reading[1])
            if not self._downloading:
                return
        pairs = list(_PAIR.finditer(line))
        if not pairs:
            return
        pair = pairs[-1]
        done, size = _bytes(pair.group(1), pair.group(2)), _bytes(pair.group(3), pair.group(4))
        if self.phase != "send":
            self._reading = (min(done, size), size)
            return
        request = _REQUEST.search(line)
        if request is None:
            return
        key = (int(request.group(1)), int(request.group(2)), int(request.group(3)))
        self._requests[key] = (min(done, size), size)
        latest = max(k[0] for k in self._requests)
        mine = [v for k, v in self._requests.items() if k[0] == latest]
        total = self._imported or sum(v[1] for v in mine)
        self._reading = (min(sum(v[0] for v in mine), total), total)

    def _maybe_emit(self, *, final: bool) -> TransferBytes | None:
        if self._reading is None or self._reading[1] <= 0:
            return None
        done, total = self._reading
        now = self.clock()
        percent = done * 100 // total
        if self._sent is not None:
            last_at, last_done, last_percent = self._sent
            if done == last_done:
                return None
            complete = done >= total
            due = now - last_at >= self.interval and (
                percent != last_percent or now - last_at >= self.refresh
            )
            if not (complete or due or final):
                return None
            if now > last_at and done > last_done:
                sample = (done - last_done) / (now - last_at)
                self._rate = sample if self._rate is None else 0.5 * self._rate + 0.5 * sample
        self._sent = (now, done, percent)
        eta = (total - done) / self._rate if self._rate else None
        verb = "Sent" if self.phase == "send" else "Received"
        message = f"{verb} {_human(done)} of {_human(total)} ({percent}%)"
        return TransferBytes(message, self.phase, done, total, self._rate, eta)


def _noop(_e: Any) -> None:
    return None


@dataclass
class TransferItem:
    site: str
    target_site: str
    set_key: str | None = None
    safety_set_key: str | None = None
    created: bool = False
    restored: bool = False
    dropped: bool = False
    ok: bool = False
    error: str | None = None


@dataclass
class TransferReport:
    mode: str  # clone | move
    project: str
    target_project: str
    target_bench: str | None
    items: list[TransferItem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.items) and all(i.ok for i in self.items)


def transfer_sites(
    project: str,
    sites: list[tuple[str, str]],
    *,
    mode: str = "clone",
    bench: str | None = None,
    target_project: str | None = None,
    target_bench: str | None = None,
    admin_password: str,
    mariadb_root_password: str,
    target_mariadb_root_password: str | None = None,
    config_mode: str = "carry",
    with_files: bool = True,
    consent: bool = False,
    on_event: OnEvent | None = None,
) -> Result[TransferReport]:
    """Clone or move ``sites`` (``(source, target_name)`` pairs) from ``project``/``bench``
    to ``target_project``/``target_bench``. A move needs ``consent`` (it drops sources), and
    so does replacing a target site that already exists (``NEEDS_CHOICE`` ``confirm_overwrite``
    without it)."""
    emit = on_event or _noop
    if mode not in ("clone", "move"):
        raise CwcliError(ErrorKind.USAGE, "transfer.mode", "mode must be clone or move")
    if not sites:
        raise CwcliError(ErrorKind.USAGE, "transfer.sites", "Nothing to transfer: no sites given.")
    tproject = target_project or project
    tbench = target_bench if target_project else (target_bench or bench)
    if mode == "move" and not consent:
        raise CwcliError(
            ErrorKind.USAGE,
            "transfer.consent",
            "A move drops each source site once its copy answers; it needs explicit consent.",
        )
    same_bench = tproject == project and _bench_key(project, tbench) == _bench_key(project, bench)
    for src, dst in sites:
        if same_bench and src == dst:
            raise CwcliError(
                ErrorKind.USAGE,
                "transfer.same_site",
                f"{src} cannot be {mode}d onto itself; give the copy another name.",
            )
        core_new_site.check_site_name(dst)
    existing = [dst for _src, dst in sites if _site_exists(tproject, tbench, dst)]
    if existing and not consent:
        return Result(
            status=Status.NEEDS_CHOICE,
            choice=Choice(
                kind="confirm_overwrite",
                param="consent",
                prompt=(
                    f"{', '.join(existing)} already exist{'s' if len(existing) == 1 else ''} on "
                    f"{tproject}; replace {'it' if len(existing) == 1 else 'them'} with the copy? "
                    "Each is backed up into the store first."
                ),
            ),
        )
    report = TransferReport(mode, project, tproject, tbench)
    total = len(sites)
    for n, (src, dst) in enumerate(sites, 1):
        item = TransferItem(site=src, target_site=dst)
        report.items.append(item)
        emit(
            StoreStep("progress", f"{mode.capitalize()} {n} of {total}: {src} -> {tproject}:{dst}")
        )
        emit(TransferProgress(f"{n} of {total}: {src}", n - 1, total, src, dst))
        try:
            captured = backup_store.capture(
                project,
                site=src,
                bench=bench,
                with_files=with_files,
                reason=mode,
                note=f"{mode} to {tproject}:{dst}",
                on_event=emit,
            )
            bset = captured.data
            if bset is None or not bset.verified:
                raise CwcliError(
                    ErrorKind.PRECONDITION,
                    "transfer.set_unverified",
                    f"The fresh set of {src} did not verify; {src} is left untouched.",
                )
            item.set_key = bset.key
            if _site_exists(tproject, tbench, dst):
                emit(StoreStep("backup", f"Backing up {dst} on {tproject} before it is replaced"))
                safety = backup_store.capture(
                    tproject,
                    site=dst,
                    bench=tbench,
                    with_files=with_files,
                    reason="before-overwrite",
                    note=f"before {mode} of {project}:{src}",
                    on_event=emit,
                )
                if safety.data is None or not safety.data.verified:
                    raise CwcliError(
                        ErrorKind.PRECONDITION,
                        "transfer.overwrite_backup_unverified",
                        f"The safety backup of {dst} did not verify; {dst} is left untouched.",
                    )
                item.safety_set_key = safety.data.key
            else:
                emit(StoreStep("create", f"Creating {dst} on {tproject}"))
                made = core_new_site.new_site(
                    tproject,
                    site=dst,
                    admin_password=admin_password,
                    bench=tbench,
                    apps=[a for a in bset.apps if a != "frappe"],
                    db_root_password=target_mariadb_root_password,
                    reload=False,
                    on_event=emit,
                )
                if made.data is None:
                    raise CwcliError(
                        ErrorKind.PRECONDITION, "transfer.create_failed", f"Could not create {dst}."
                    )
                item.created = True
            restored = backup_store.restore_set(
                bset.key,
                project=tproject,
                site=dst,
                bench=tbench,
                mariadb_root_password=target_mariadb_root_password or mariadb_root_password,
                consent=True,
                config_mode=config_mode,
                on_event=emit,
            )
            rep = restored.data
            if (
                rep is None
                or not rep.restored
                or (rep.migrate_ran and not rep.migrate_ok)
                or not all(c.ok for c in rep.post_checks)
            ):
                raise CwcliError(
                    ErrorKind.PRECONDITION,
                    "transfer.restore_failed",
                    f"{dst} did not come up from the set; the source {src} is left untouched.",
                )
            item.restored = True
            if mode == "move":
                emit(
                    StoreStep("drop", f"Dropping the source {src} (its archive goes to the store)")
                )
                dropped = core_rm_site.drop_site(
                    project,
                    src,
                    bench=bench,
                    consent=True,
                    db_root_password=mariadb_root_password,
                    on_event=emit,
                )
                item.dropped = dropped.data is not None
                if not item.dropped:
                    raise CwcliError(
                        ErrorKind.PRECONDITION,
                        "transfer.drop_failed",
                        f"{dst} is up but the source {src} could not be dropped.",
                    )
            item.ok = True
            emit(TransferProgress(f"{src} -> {tproject}:{dst} done", n, total, src, dst))
        except CwcliError as exc:
            item.error = exc.message
            emit(StoreNotice(f"{src}: {exc.message}", "error"))
            emit(
                StoreNotice(
                    f"Stopped at {n} of {total}; the sites before it are done, "
                    "the ones after it were not touched.",
                    "error",
                )
            )
            break
    return Result(status=Status.OK if report.ok else Status.WARNING, data=report)


def _bench_key(project: str, bench: str | None) -> str:
    from . import resolvers

    try:
        res = resolvers.resolve_bench(project, bench or None, None)
    except CwcliError:
        return bench or ""
    return res.data if res is not None and res.data else (bench or "")


def _site_exists(project: str, bench: str | None, site: str) -> bool:
    from . import resolvers

    for b in resolvers.cached_benches(project):
        if bench is not None and str(b.get("index")) != str(bench) and b.get("label") != bench:
            continue
        if any((s.get("name") if isinstance(s, dict) else s) == site for s in b.get("sites") or []):
            return True
    return False


# ------------------------------------------------------------------ across machines


def _sendme(emit: OnEvent) -> str:
    """The sendme binary to run, checked against the pinned release's sha256 right
    before every run; a missing or mismatched one is replaced by the pinned release
    first, the way `cwcli restore --send/--receive` does, into ``~/.local/bin`` of the
    account cwcli runs as (for the web UI of a shared install, the service account).
    The install prints nothing, so a web UI job or ``--json`` output stays clean; a
    sendme that still does not match is never run, and the error names the manual
    command."""
    from ..utils import sendme_utils  # lazy: sendme_utils pulls rich at load time

    installing: list[str] = []

    def announce(problem: str) -> None:
        installing.append(f"Installing sendme {sendme_utils.PINNED_VERSION} ({problem})")
        emit(StoreStep("install", installing[-1]))

    try:
        path = sendme_utils.get_sendme_command(progress=False, on_install=announce)
    except sendme_utils.SendmeInstallError as e:
        if installing:
            emit(StoreStep("install", installing[-1], "failed"))
        command = sendme_utils.manual_install_command()
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "transfer.sendme_missing",
            f"{e}. Install it by hand: {command}",
            detail={"install_command": command, "reason": str(e)},
        ) from e
    if installing:
        emit(StoreStep("install", f"Installed sendme to {path}", "ok"))
    return path


def _outbox(keys: list[str]) -> Path:
    """One directory holding the sets to send (hard links, a copy where linking fails)."""
    root = backup_store.store_root() / ".outbox"
    root.mkdir(parents=True, exist_ok=True)
    box = Path(tempfile.mkdtemp(prefix=time.strftime("%Y%m%d_%H%M%S-"), dir=root))
    for key in keys:
        bset = backup_store.find_set(key)
        if bset is None or not bset.verified:
            shutil.rmtree(box, ignore_errors=True)
            raise CwcliError(
                ErrorKind.PRECONDITION,
                "transfer.set_unverified",
                f"{key} is missing or did not verify; it is not sent.",
            )
        dst = box / bset.project / bset.site / bset.id
        dst.mkdir(parents=True)
        for f in Path(bset.path).iterdir():
            if f.is_file():
                try:
                    os.link(f, dst / f.name)
                except OSError:
                    shutil.copy2(f, dst / f.name)
    return box


def _spawn_on_pty(argv: list[str], cwd: Path) -> tuple[subprocess.Popen, int]:
    """Start ``argv`` on a pty and return it with the master fd. sendme needs a
    terminal: ``send`` refuses to run without one, and both directions draw their
    progress bars only on one. The pty is wide so a bar's byte counts are never cut."""
    master, slave = os.openpty()
    try:
        import fcntl
        import struct
        import termios

        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 400, 0, 0))
    except (ImportError, OSError):
        pass
    try:
        proc = subprocess.Popen(
            argv, cwd=str(cwd), stdin=slave, stdout=slave, stderr=slave, close_fds=True
        )
    finally:
        os.close(slave)
    return proc, master


def _read_pty(master: int) -> bytes:
    """One read from the pty; b"" once the child closed it (EIO on Linux)."""
    try:
        return os.read(master, 4096)
    except OSError as e:
        if e.errno == errno.EIO:
            return b""
        raise


def send_sets(
    keys: list[str],
    *,
    on_event: OnEvent | None = None,
    stop: Callable[[], bool] | None = None,
) -> Result[dict[str, Any]]:
    """Serve ``keys`` over sendme until interrupted (the ticket rides a ``ticket`` event
    as soon as sendme prints it). Returns when sendme exits or ``stop()`` says so."""
    emit = on_event or _noop
    cmd = _sendme(emit)
    box = _outbox(keys)
    emit(StoreStep("send", f"Serving {len(keys)} set{'s' if len(keys) != 1 else ''} over sendme"))
    proc, master = _spawn_on_pty([cmd, "send", str(box)], box.parent)
    meter = SendmeProgress("send")
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    ticket = None
    buf = b""
    try:
        while True:
            chunk = _read_pty(master)
            if not chunk:
                break
            buf = (buf + chunk)[-65536:]
            if ticket is None:
                m = _TICKET.search(buf.decode("utf-8", "replace").replace("\r", ""))
                if m:
                    ticket = m.group(1)
                    emit(
                        Ticket(
                            "Ticket ready; on the other machine: cwcli backups receive <ticket>",
                            ticket,
                            list(keys),
                        )
                    )
            progress = meter.feed(decoder.decode(chunk))
            if progress is not None:
                emit(progress)
            if stop is not None and stop():
                break
    finally:
        last = meter.finish()
        if last is not None:
            emit(last)
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
        os.close(master)
        shutil.rmtree(box, ignore_errors=True)
        for d in (box.parent,):
            try:
                if not any(d.iterdir()):
                    d.rmdir()
            except OSError:
                pass
    if ticket is None:
        tail = buf.decode("utf-8", "replace")[-800:]
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "transfer.no_ticket",
            "sendme did not print a ticket.",
            detail={"output": tail},
        )
    return Result(status=Status.OK, data={"ticket": ticket, "sets": list(keys), "served": True})


def _run_receive(argv: list[str], cwd: Path, emit: OnEvent) -> tuple[int, str]:
    """Run ``sendme receive`` to the end and return its exit code and output. On a pty
    its download bar becomes progress (the redraws stripped from the output); where
    the OS has no pty it runs piped, with no bar to read."""
    if not hasattr(os, "openpty"):
        piped = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True)
        return piped.returncode, piped.stderr or piped.stdout or ""
    proc, master = _spawn_on_pty(argv, cwd)
    meter = SendmeProgress("receive")
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    tail = ""
    try:
        while True:
            chunk = _read_pty(master)
            if not chunk:
                break
            text = decoder.decode(chunk)
            tail = (tail + text)[-65536:]
            progress = meter.feed(text)
            if progress is not None:
                emit(progress)
        returncode = proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        os.close(master)
    last = meter.finish()
    if last is not None:
        emit(last)
    lines = re.split(r"[\r\n]+", _ANSI.sub("", tail))
    return returncode, "\n".join(line.strip() for line in lines if line.strip())


def receive_sets(
    ticket: str,
    *,
    project: str | None = None,
    site: str | None = None,
    on_event: OnEvent | None = None,
) -> Result[dict[str, Any]]:
    """Download a ticket and adopt every set in it into this machine's store. A bundle
    keeps each set's own project/site; a single set may be filed under ``project``/``site``
    instead (the machine it came from is not this one)."""
    emit = on_event or _noop
    ticket = "".join(ticket.split())
    if not ticket.startswith("blob") or len(ticket) < 40:
        raise CwcliError(ErrorKind.USAGE, "transfer.ticket", "That is not a sendme ticket.")
    cmd = _sendme(emit)
    inbox = backup_store.store_root() / ".inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    shared_home.secure_dir_shared_only(inbox)
    tmp = Path(tempfile.mkdtemp(prefix=time.strftime("%Y%m%d_%H%M%S-"), dir=inbox))
    emit(StoreStep("receive", "Downloading over sendme"))
    try:
        returncode, output = _run_receive([cmd, "receive", ticket], tmp, emit)
        if returncode != 0:
            raise CwcliError(
                ErrorKind.PRECONDITION,
                "transfer.receive_failed",
                "sendme could not download the ticket.",
                detail={"output": output[-800:]},
            )
        adopted = file_downloaded(tmp, project=project, site=site, reason="received")
        emit(
            StoreStep(
                "receive", f"Filed {len(adopted)} set{'s' if len(adopted) != 1 else ''}", "ok"
            )
        )
        emit(Received("Sets are in the store", adopted))
        return Result(status=Status.OK, data={"sets": adopted})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def file_downloaded(
    tmp: Path, *, project: str | None, site: str | None, reason: str = "received"
) -> list[str]:
    """Adopt every ``set.json`` under ``tmp`` into the store (a single set may be filed
    under ``project``/``site`` instead); a plain bench-backup directory needs both."""
    manifests = sorted(tmp.rglob("set.json"))
    plain = [p for p in sorted(tmp.rglob("*")) if p.is_file() and p.name != "set.json"]
    adopted: list[str] = []
    if manifests:
        for man in manifests:
            src = backup_store._read_manifest(man.parent)
            if src is None:
                continue
            p = project if (project and len(manifests) == 1) else src.project
            s = site if (site and len(manifests) == 1) else src.site
            got = backup_store.adopt_dir(
                p, s, man.parent, reason=reason, note=f"from {src.project}:{src.site}"
            )
            if got is not None:
                adopted.append(got.key)
    elif plain and project and site:
        got = backup_store.adopt_dir(project, site, plain[0].parent, reason=reason)
        if got is not None:
            adopted.append(got.key)
    if not adopted:
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "transfer.nothing_adopted",
            "The download held no backup set cwcli could file.",
            hint="A plain backup needs a target project and site to be filed under.",
        )
    return adopted


def export_tar(key: str, dest_dir: Path) -> Path:
    """One set as ``<project>__<site>__<id>.tar`` (uncompressed: the parts already are),
    laid out as the sender's outbox is, so ``import_tar`` and a receive file it alike."""
    import tarfile

    bset = backup_store.find_set(key)
    if bset is None:
        raise CwcliError(ErrorKind.NOT_FOUND, "backups.set_not_found", f"No backup set {key}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / f"{bset.project}__{bset.site}__{bset.id}.tar"
    with tarfile.open(out, "w") as tar:
        for f in sorted(Path(bset.path).iterdir()):
            if f.is_file():
                tar.add(f, arcname=f"{bset.project}/{bset.site}/{bset.id}/{f.name}")
    os.chmod(out, shared_home.file_mode(0o600))
    return out


def import_tar(
    src: Path,
    *,
    project: str | None = None,
    site: str | None = None,
    on_event: OnEvent | None = None,
) -> Result[dict[str, Any]]:
    """File a tar written by ``export_tar`` (or a bundle of sets) into the store."""
    import tarfile

    emit = on_event or _noop
    inbox = backup_store.store_root() / ".inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=time.strftime("%Y%m%d_%H%M%S-up-"), dir=inbox))
    try:
        with tarfile.open(src, "r:*") as tar:
            for m in tar.getmembers():
                target = (tmp / m.name).resolve()
                if (
                    not str(target).startswith(str(tmp.resolve()) + os.sep)
                    or m.islnk()
                    or m.issym()
                ):
                    raise CwcliError(
                        ErrorKind.USAGE, "transfer.tar_unsafe", f"Refusing tar member {m.name!r}"
                    )
            tar.extractall(tmp)  # noqa: S202 - members checked above
        adopted = file_downloaded(tmp, project=project, site=site, reason="received")
        emit(Received("Sets are in the store", adopted))
        return Result(status=Status.OK, data={"sets": adopted})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
