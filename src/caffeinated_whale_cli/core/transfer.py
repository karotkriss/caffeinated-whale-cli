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
driven under a pty because ``sendme send`` refuses to run without a terminal.
"""

from __future__ import annotations

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


def _sendme() -> str:
    from ..utils.sendme_utils import get_sendme_command, is_sendme_installed

    if not is_sendme_installed():
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "transfer.sendme_missing",
            "sendme is not installed; `cwcli doctor` names how to get it.",
        )
    return get_sendme_command()


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


def send_sets(
    keys: list[str],
    *,
    on_event: OnEvent | None = None,
    stop: Callable[[], bool] | None = None,
) -> Result[dict[str, Any]]:
    """Serve ``keys`` over sendme until interrupted (the ticket rides a ``ticket`` event
    as soon as sendme prints it). Returns when sendme exits or ``stop()`` says so."""
    emit = on_event or _noop
    cmd = _sendme()
    box = _outbox(keys)
    emit(StoreStep("send", f"Serving {len(keys)} set{'s' if len(keys) != 1 else ''} over sendme"))
    master, slave = os.openpty()
    try:
        proc = subprocess.Popen(
            [cmd, "send", str(box)],
            cwd=str(box.parent),
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
        )
    finally:
        os.close(slave)
    ticket = None
    buf = b""
    try:
        while True:
            try:
                chunk = os.read(master, 4096)
            except OSError as e:
                if e.errno == errno.EIO:
                    break
                raise
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
            if stop is not None and stop():
                break
    finally:
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
    cmd = _sendme()
    inbox = backup_store.store_root() / ".inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    shared_home.secure_dir_shared_only(inbox)
    tmp = Path(tempfile.mkdtemp(prefix=time.strftime("%Y%m%d_%H%M%S-"), dir=inbox))
    emit(StoreStep("receive", "Downloading over sendme"))
    try:
        proc = subprocess.run(
            [cmd, "receive", ticket], cwd=str(tmp), capture_output=True, text=True, timeout=None
        )
        if proc.returncode != 0:
            raise CwcliError(
                ErrorKind.PRECONDITION,
                "transfer.receive_failed",
                "sendme could not download the ticket.",
                detail={"output": (proc.stderr or proc.stdout or "")[-800:]},
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
