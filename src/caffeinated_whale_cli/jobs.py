"""Detached, durable jobs for the cwcli web UI.

The HTTP daemon writes a secret-free spec, then starts this module in a new
process session. The child calls the same UI-pure core functions as the CLI and
records their typed events under ``cwcli_home()/jobs/<id>/``. The daemon only
reads and tails those files, so closing a tab or restarting the daemon never
owns or ends the work.

Secrets are the deliberate exception to the spec file. They travel in the
child's environment, are removed from ``os.environ`` as soon as the worker
starts, and are scrubbed from every event, result, and error before persistence.
"""

from __future__ import annotations

import _thread
import argparse
import contextlib
import json
import os
import re
import secrets as secrets_module
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from .commands import rm as rm_cmd
from .commands import start as start_cmd
from .core import apps as core_apps
from .core import backup as core_backup
from .core import bench_ops as core_bench_ops
from .core import failure_causes
from .core import init as core_init
from .core import label as core_label
from .core import resolvers as core_resolvers
from .core import restart as core_restart
from .core import rm as core_rm
from .core import rm_bench as core_rm_bench
from .core import rm_site as core_rm_site
from .core import scale as core_scale
from .core import start as core_start
from .core import stop as core_stop
from .core import unlock as core_unlock
from .core import update as core_update
from .core import version as core_version
from .core.envelope import Choice, Message, Result, Status
from .core.errors import CwcliError, ErrorKind
from .core.version import _is_frozen
from .utils import cache, daemon_identity, shared_home, signals
from .utils.config_utils import cwcli_home

ACTIVE_STATES = frozenset({"queued", "running", "interrupting"})
TERMINAL_STATES = frozenset({"succeeded", "failed", "interrupted", "unknown", "needs_choice"})
ALLOWED_VERBS = frozenset(
    {
        "start",
        "stop",
        "restart",
        "stop-bench",
        "restart-process",
        "set-label",
        "unlock",
        "clear-maintenance",
        "scale",
        "apps-checkout",
        "init",
        "apps-install",
        "apps-update",
        "backup",
        "migrate",
        "run-tests",
        "build",
        "setup-wizard",
        "self-update",
        # Phase 3: the guarded destructive verbs. Each runs the SAME core path the
        # CLI and `axi` use (verified backup gate included), takes consent as a
        # spec field the page sets only after a typed confirmation, and never
        # carries a bypass: there is no `--no-backup` here (`axi rm`'s rule) and
        # no `--force`.
        "rm",
        "rm-site",
        "rm-bench",
    }
)

FINISHED_RETENTION = 100
INTERRUPT_DELIVERY_S = 300.0
INTERRUPT_CLEANUP_S = 120.0
_INTERRUPTIBLE_STATES = frozenset({"queued", "running"})
_INTERRUPT_REQUEST = "interrupt.request"
_CLEANUP_STARTED = "cleanup.started"
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{7,80}$")
_SECRET_ENV = {
    "admin_password": "CWCLI_JOB_ADMIN_PASSWORD",
    "db_root_password": "CWCLI_JOB_DB_ROOT_PASSWORD",
}
_UI_ENV = {
    "pid": "CWCLI_JOB_UI_PID",
    "start": "CWCLI_JOB_UI_START",
    "port": "CWCLI_JOB_UI_PORT",
    "token": "CWCLI_JOB_UI_TOKEN",
    "session": "CWCLI_JOB_UI_SESSION",
}
_CREATE_LOCK = threading.Lock()
# The secrets each job was started with, held in THIS daemon's memory only - never on
# disk and never in a record - so Retry can reuse what the user typed. Forgotten when
# the job succeeds, is pruned, or the daemon stops (a retry then asks for them again).
_RETRY_SECRETS: dict[str, dict[str, str]] = {}
RETRYABLE_STATES = frozenset({"failed", "interrupted", "unknown"})
# The workers this daemon spawned, so they are reaped: an unreaped child stays a
# zombie that ``pid_alive`` reports alive, and a job whose worker died abnormally
# would then never become ``unknown``.
_CHILDREN: dict[str, subprocess.Popen] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def jobs_dir() -> Path:
    return cwcli_home() / "jobs"


def job_dir(job_id: str) -> Path:
    if not _ID_RE.fullmatch(job_id):
        raise CwcliError(ErrorKind.USAGE, "job.id_invalid", "Invalid job id.")
    return jobs_dir() / job_id


def _json_path(job_id: str, name: str) -> Path:
    return job_dir(job_id) / name


def _plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return _plain(asdict(value))
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(v) for v in value]
    return value


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CwcliError(
            ErrorKind.NOT_FOUND,
            "job.not_found",
            f"No readable job record exists at '{path.parent.name}'.",
        ) from exc
    if not isinstance(value, dict):
        raise CwcliError(ErrorKind.INTERNAL, "job.record_invalid", "Job record is not an object.")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    dir_mode = shared_home.dir_mode(0o700)
    file_mode = shared_home.file_mode(0o600)
    path.parent.mkdir(parents=True, exist_ok=True, mode=dir_mode)
    os.chmod(path.parent, dir_mode)
    shared_home.apply_group(path.parent)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets_module.token_hex(4)}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, file_mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, file_mode)
        shared_home.apply_group(path)
    finally:
        temporary.unlink(missing_ok=True)


def _state(job_id: str) -> dict[str, Any]:
    return _read_json(_json_path(job_id, "state.json"))


@contextlib.contextmanager
def _state_lock(job_id: str):
    """Serialize every ``state.json`` read-modify-write across the daemon and the worker.

    Each holder opens its own descriptor, so the kernel lock also excludes the
    recorder's flush thread from the worker's main thread.
    """
    fd = os.open(
        _json_path(job_id, "state.lock"), os.O_CREAT | os.O_RDWR, shared_home.file_mode(0o600)
    )
    try:
        if sys.platform == "win32":  # pragma: no cover - exercised on Windows CI
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        if sys.platform == "win32":  # pragma: no cover - exercised on Windows CI
            import msvcrt

            with contextlib.suppress(OSError):
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        os.close(fd)


def _merge_state(job_id: str, **changes: Any) -> dict[str, Any]:
    with _state_lock(job_id):
        state = _state(job_id)
        state.update(_plain(changes))
        state["updated_at"] = _now()
        _write_json(_json_path(job_id, "state.json"), state)
        return state


def _append_event(job_id: str, seq: int, event: dict[str, Any]) -> None:
    """Append one event line. The worker is the ONLY writer of ``events.jsonl``.

    No fsync and no ``state.json`` rewrite per event: a migrate repaints its
    progress bar hundreds of times a second, and the daemon tails this file on the
    same host, so the durable record is ``state.json`` (atomic, flushed by the
    recorder at most twice a second and on every lifecycle event).
    """
    record = {"seq": seq, "at": _now(), **_plain(event)}
    path = _json_path(job_id, "events.jsonl")
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        shared_home.file_mode(0o600),
    )
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
    os.chmod(path, shared_home.file_mode(0o600))
    shared_home.apply_group(path)


def _events(job_id: str, *, after: int = 0) -> list[dict[str, Any]]:
    path = _json_path(job_id, "events.jsonl")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines[after:]:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and int(row.get("seq") or 0) > after:
            rows.append(row)
    return rows


def _process_is_job(state: dict[str, Any]) -> bool:
    pid = state.get("pid")
    recorded = state.get("process_started")
    if not isinstance(pid, int) or not recorded or not daemon_identity.pid_alive(pid):
        return False
    return bool(daemon_identity.process_start_time(pid) == recorded)


def _refresh_unknown(job_id: str, state: dict[str, Any]) -> dict[str, Any]:
    if state.get("state") not in ACTIVE_STATES:
        return state
    if _process_is_job(state):
        return state
    created = state.get("created_epoch")
    if state.get("pid") is None and isinstance(created, (int, float)) and time.time() - created < 5:
        return state
    with _state_lock(job_id):
        state = _state(job_id)
        if state.get("state") not in ACTIVE_STATES or _process_is_job(state):
            return state
        state.update(
            {
                "state": "unknown",
                "finished_at": _now(),
                "updated_at": _now(),
                "error": {
                    "type": "process_vanished",
                    "code": "job.process_vanished",
                    "message": "The job process vanished without recording a final result.",
                    "hint": "Review its last event before deciding whether it is safe to retry.",
                },
            }
        )
        _write_json(_json_path(job_id, "state.json"), state)
    return state


def read_job(job_id: str, *, include_events: bool = False, after: int = 0) -> dict[str, Any]:
    spec = _read_json(_json_path(job_id, "spec.json"))
    state = _refresh_unknown(job_id, _state(job_id))
    if state.get("state") == "succeeded":
        _RETRY_SECRETS.pop(job_id, None)
    record = {**spec, **state}
    record["retryable"] = _retry_refusal(record) is None
    if include_events:
        record["events"] = _events(job_id, after=after)
    return record


def _job_ids() -> list[str]:
    try:
        return [
            path.name
            for path in jobs_dir().iterdir()
            if path.is_dir() and _ID_RE.fullmatch(path.name)
        ]
    except OSError:
        return []


def _summary(record: dict[str, Any]) -> dict[str, Any]:
    """A list row: the full record minus its result payload, which is fetched per job."""
    return {key: value for key, value in record.items() if key != "result"}


def list_jobs() -> list[dict[str, Any]]:
    records = []
    for job_id in _job_ids():
        try:
            records.append(_summary(read_job(job_id)))
        except CwcliError:
            continue
    return sorted(records, key=lambda row: str(row.get("created_at") or ""), reverse=True)


def _prune(records: list[dict[str, Any]]) -> None:
    """Delete finished jobs past the newest ``FINISHED_RETENTION``; ``records`` is newest first."""
    finished = [row for row in records if row.get("state") not in ACTIVE_STATES]
    for row in finished[FINISHED_RETENTION:]:
        _RETRY_SECRETS.pop(str(row["id"]), None)
        shutil.rmtree(job_dir(str(row["id"])), ignore_errors=True)


def _project_key(target: dict[str, Any], verb: str) -> str:
    project = target.get("project")
    return str(project).strip() if isinstance(project, str) and project.strip() else f"@{verb}"


def _secret_field(value: Any) -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if any(token in str(key).lower() for token in ("password", "secret", "token")):
                return str(key)
            nested = _secret_field(item)
            if nested is not None:
                return nested
    elif isinstance(value, (list, tuple)):
        for item in value:
            nested = _secret_field(item)
            if nested is not None:
                return nested
    return None


def _validate_spec(verb: str, target: dict[str, Any], args: dict[str, Any]) -> None:
    if verb not in ALLOWED_VERBS:
        raise CwcliError(
            ErrorKind.USAGE,
            "job.verb_unsupported",
            f"Unsupported job verb '{verb}'.",
            hint="Allowed verbs are " + ", ".join(sorted(ALLOWED_VERBS)) + ".",
        )
    if not isinstance(target, dict) or not isinstance(args, dict):
        raise CwcliError(
            ErrorKind.USAGE,
            "job.payload_invalid",
            "Job target and args must be JSON objects.",
        )
    for container_name, container in (("target", target), ("args", args)):
        secret_key = _secret_field(container)
        if secret_key is not None:
            raise CwcliError(
                ErrorKind.USAGE,
                "job.secret_in_spec",
                f"Job {container_name} cannot contain the secret field '{secret_key}'.",
                hint="Send passwords through the dedicated secrets object.",
            )
    if verb != "self-update":
        project = target.get("project")
        if not isinstance(project, str) or not project.strip():
            raise CwcliError(
                ErrorKind.USAGE,
                "job.project_required",
                "This job requires a non-empty target.project string.",
            )


def create_job(
    verb: str,
    target: dict[str, Any],
    args: dict[str, Any],
    *,
    consent: bool = False,
    secret_values: dict[str, str] | None = None,
    ui_context: dict[str, str | int] | None = None,
) -> dict[str, Any]:
    """Atomically admit and start one job within this user's UI daemon."""
    with _CREATE_LOCK:
        return _create_job(
            verb,
            target,
            args,
            consent=consent,
            secret_values=secret_values,
            ui_context=ui_context,
        )


def _create_job(
    verb: str,
    target: dict[str, Any],
    args: dict[str, Any],
    *,
    consent: bool = False,
    secret_values: dict[str, str] | None = None,
    ui_context: dict[str, str | int] | None = None,
) -> dict[str, Any]:
    """Persist a secret-free spec and spawn its worker in a new process session."""
    _validate_spec(verb, target, args)
    project_key = _project_key(target, verb)
    existing_jobs = list_jobs()
    _prune(existing_jobs)
    for existing in existing_jobs:
        if existing.get("project_key") == project_key and existing.get("state") in ACTIVE_STATES:
            raise CwcliError(
                ErrorKind.CONFLICT,
                "job.project_busy",
                f"Job '{existing['id']}' is already changing '{project_key}'.",
                hint="Open that job to interrupt it or wait for it to finish.",
                detail={"job_id": existing["id"]},
            )

    created = datetime.now(timezone.utc)
    job_id = created.strftime("%Y%m%d%H%M%S") + "-" + secrets_module.token_hex(6)
    directory = job_dir(job_id)
    directory.mkdir(parents=True, mode=shared_home.dir_mode(0o700))
    os.chmod(directory, shared_home.dir_mode(0o700))
    shared_home.apply_group(directory)
    spec = {
        "id": job_id,
        "verb": verb,
        "target": _plain(target),
        "args": _plain(args),
        "consent": bool(consent),
        "project_key": project_key,
        "created_at": created.isoformat().replace("+00:00", "Z"),
    }
    state = {
        "id": job_id,
        "state": "queued",
        "created_epoch": created.timestamp(),
        "updated_at": spec["created_at"],
        "started_at": None,
        "finished_at": None,
        "pid": None,
        "process_started": None,
        "event_count": 0,
        "exit_code": None,
        "result": None,
        "warnings": [],
        "error": None,
        "cause": None,
    }
    _write_json(directory / "spec.json", spec)
    _write_json(directory / "state.json", state)

    # A frozen standalone exe has no `-m`; it re-enters through the hidden `ui --job`.
    prefix = (
        [sys.argv[0], "ui", "--job", job_id]
        if _is_frozen()
        else [sys.executable, "-m", "caffeinated_whale_cli.jobs", "run", job_id]
    )
    environment = dict(os.environ)
    environment.pop("CWCLI_SERVE_TOKEN", None)
    environment.pop("CWCLI_SERVE_SESSION", None)
    for name, value in (secret_values or {}).items():
        env_name = _SECRET_ENV.get(name)
        if env_name and isinstance(value, str):
            environment[env_name] = value
    if verb == "self-update":
        for name, ui_value in (ui_context or {}).items():
            env_name = _UI_ENV.get(name)
            if env_name and ui_value is not None:
                environment[env_name] = str(ui_value)

    log_path = directory / "runner.log"
    fd = os.open(
        log_path,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        shared_home.file_mode(0o600),
    )
    shared_home.apply_group(log_path)
    creationflags = 0x00000008 | 0x00000200 if sys.platform == "win32" else 0
    with os.fdopen(fd, "a", encoding="utf-8") as log:
        process = subprocess.Popen(
            prefix,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            env=environment,
            creationflags=creationflags,
            start_new_session=sys.platform != "win32",
        )
    _CHILDREN[job_id] = process
    kept = {k: v for k, v in (secret_values or {}).items() if k in _SECRET_ENV and v}
    if kept:
        _RETRY_SECRETS[job_id] = kept
    started = daemon_identity.process_start_time(process.pid)
    current = _state(job_id)
    if current.get("state") not in TERMINAL_STATES:
        _merge_state(job_id, pid=process.pid, process_started=started)
    return read_job(job_id)


def _retry_refusal(record: dict[str, Any]) -> tuple[str, str] | None:
    """Why a job cannot be retried (code, message), or None when it can."""
    if record.get("state") not in RETRYABLE_STATES:
        return "job.retry_not_failed", "Only a failed, interrupted or vanished job can be retried."
    args = record.get("args") or {}
    # Both flags are set by the page only behind a typed confirmation of a name; a
    # retry must not carry that consent over to a run nobody confirmed.
    if (record.get("verb") == "apps-checkout" and args.get("reset") is True) or (
        record.get("verb") == "apps-update" and args.get("force") is True
    ):
        return (
            "job.retry_guarded",
            "This job discards local work, so start it again from its page to confirm it again.",
        )
    return None


def retry_job(job_id: str, *, ui_context: dict[str, str | int] | None = None) -> dict[str, Any]:
    """Start a NEW job with the same verb, target, args and secrets as a finished one.

    Consent is never carried over: a destructive verb comes back as the same
    typed-confirmation choice it first did. A creation retried from the partial
    state its failure left (project dir, compose file) continues from there, since
    ``init`` already reuses whatever it finds.
    """
    record = read_job(job_id)
    refusal = _retry_refusal(record)
    if refusal is not None:
        raise CwcliError(ErrorKind.CONFLICT, *refusal)
    return create_job(
        record["verb"],
        record["target"],
        record["args"],
        secret_values=_held_secrets(record),
        ui_context=ui_context,
    )


def continue_job(
    job_id: str,
    target: dict[str, Any],
    args: dict[str, Any],
    *,
    consent: bool = False,
    ui_context: dict[str, str | int] | None = None,
) -> dict[str, Any]:
    """Answer a ``needs_choice`` job with a NEW job that reuses the secrets it was started with.

    The page sends the answered target, args and consent; the secrets stay in this
    daemon, held for the waiting job until it is pruned, so it can be answered again.
    """
    record = read_job(job_id)
    if record.get("state") != "needs_choice":
        raise CwcliError(
            ErrorKind.CONFLICT,
            "job.continue_not_pending",
            "Only a job waiting for a decision can be continued.",
        )
    if target.get("project") != record["target"].get("project"):
        raise CwcliError(
            ErrorKind.USAGE,
            "job.continue_project_changed",
            "A continued job must keep the instance it was started for.",
        )
    return create_job(
        record["verb"],
        target,
        args,
        consent=consent,
        secret_values=_held_secrets(record),
        ui_context=ui_context,
    )


def _held_secrets(record: dict[str, Any]) -> dict[str, str]:
    """The secrets this daemon still holds for a job, refusing an init that has lost them."""
    secret_values = dict(_RETRY_SECRETS.get(str(record["id"]), {}))
    if record["verb"] == "init" and not secret_values.get("admin_password"):
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "job.retry_secrets_missing",
            "The passwords for this job are no longer held, so it cannot be run again as it was.",
            hint="They are kept in memory only and are forgotten when the daemon restarts.",
        )
    return secret_values


def interrupt_job(job_id: str) -> dict[str, Any]:
    _refresh_unknown(job_id, _state(job_id))
    # Recorded in state, not events.jsonl: the worker is that file's only writer.
    with _state_lock(job_id):
        state = _state(job_id)
        armed = state.get("state") in _INTERRUPTIBLE_STATES and _process_is_job(state)
        if armed:
            state.update(state="interrupting", interrupt_requested_at=_now(), updated_at=_now())
            _write_json(_json_path(job_id, "state.json"), state)
    if armed:
        if sys.platform != "win32":
            with contextlib.suppress(ProcessLookupError):
                os.killpg(int(state["pid"]), signal.SIGTERM)
        else:
            _json_path(job_id, _INTERRUPT_REQUEST).touch()
            threading.Thread(
                target=_terminate_after_grace, args=(job_id, state), daemon=True
            ).start()
    return read_job(job_id)


def _wait_until(done, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while not done():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)
    return True


def _terminate_after_grace(job_id: str, state: dict[str, Any]) -> None:
    """Windows only: terminate a worker whose cleanup never started or never finished.

    Delivery waits for the worker's main thread to run Python again, so the cleanup
    allowance counts from the worker's ``cleanup.started`` marker, not from the request.
    """
    started = _json_path(job_id, _CLEANUP_STARTED)

    def gone() -> bool:
        return not _process_is_job(state)

    if _wait_until(lambda: started.exists() or gone(), INTERRUPT_DELIVERY_S):
        if gone() or _wait_until(gone, INTERRUPT_CLEANUP_S):
            return
        with contextlib.suppress(OSError):
            os.kill(int(state["pid"]), signal.SIGTERM)
        return
    with contextlib.suppress(OSError):
        os.kill(int(state["pid"]), signal.SIGTERM)
    with _state_lock(job_id):
        current = _state(job_id)
        if current.get("state") not in ACTIVE_STATES:
            return
        current.update(
            state="unknown",
            finished_at=_now(),
            updated_at=_now(),
            error={
                "type": "process_vanished",
                "code": "job.cleanup_not_started",
                "message": (
                    "The job did not respond to the interrupt within "
                    f"{int(INTERRUPT_DELIVERY_S)} seconds and was terminated; "
                    "its cleanup may not have run."
                ),
                "hint": ("Check maintenance mode and the instance's state before retrying."),
            },
        )
        _write_json(_json_path(job_id, "state.json"), current)


def _watch_interrupt_request(job_id: str, stop: threading.Event, interval: float = 0.25) -> None:
    """Turn the daemon's interrupt request into the same KeyboardInterrupt unwind a signal gets."""
    request = _json_path(job_id, _INTERRUPT_REQUEST)
    while not stop.wait(interval):
        if request.exists():
            _thread.interrupt_main()
            return


def detach_job(job_id: str) -> dict[str, Any]:
    """Detach is client-local. Reading the durable record proves the job remains attached to disk."""
    return read_job(job_id)


def _scrub(value: Any, secret_values: list[str]) -> Any:
    if isinstance(value, str):
        text = value
        for secret in secret_values:
            if secret:
                text = text.replace(secret, "[redacted]")
        return text
    if isinstance(value, dict):
        return {key: _scrub(item, secret_values) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item, secret_values) for item in value]
    return value


def _event_type(event: Any) -> str:
    name = type(event).__name__
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


class _Recorder:
    """The job's ``on_event`` callback: scrubs, numbers, and persists every event."""

    FLUSH_INTERVAL_S = 0.5

    def __init__(self, job_id: str, secret_values: list[str]) -> None:
        self.job_id = job_id
        self.secret_values = secret_values
        self.seq = 0
        self._flushed_at = 0.0
        self._pending: threading.Timer | None = None

    def _record(self, record: dict[str, Any], *, flush: bool) -> None:
        self.seq += 1
        _append_event(self.job_id, self.seq, record)
        if flush or time.monotonic() - self._flushed_at >= self.FLUSH_INTERVAL_S:
            self.flush()
        elif self._pending is None:
            # A quiet stretch (a long patch, a frozen command) must not leave the
            # last event unflushed: one trailing timer catches up the count.
            self._pending = threading.Timer(self.FLUSH_INTERVAL_S, self.flush)
            self._pending.daemon = True
            self._pending.start()

    def flush(self) -> None:
        if self._pending is not None:
            self._pending.cancel()
            self._pending = None
        _merge_state(self.job_id, event_count=self.seq)
        self._flushed_at = time.monotonic()

    def __call__(self, event: Any) -> None:
        data = _scrub(_plain(event), self.secret_values)
        record: dict[str, Any] = {"type": _event_type(event), "data": data}
        if isinstance(data, dict):
            for key in ("phase", "action", "stream", "status", "ok", "item"):
                if key in data:
                    record[key] = data[key]
            record["message"] = (
                data.get("message") or data.get("text") or data.get("command") or data.get("label")
            )
        self._record(record, flush=False)

    def lifecycle(self, kind: str, message: str, **data: Any) -> None:
        self._record(
            _scrub({"type": kind, "message": message, "data": data}, self.secret_values),
            flush=True,
        )


def _bench(target: dict[str, Any]) -> str | None:
    value = target.get("bench")
    return str(value) if value is not None and str(value).strip() else None


def _site(target: dict[str, Any]) -> str | None:
    value = target.get("site")
    return str(value) if value is not None and str(value).strip() else None


def _project(target: dict[str, Any]) -> str:
    return str(target["project"])


def _restart_instance(target: dict[str, Any], emit: _Recorder) -> Result:
    project = _project(target)
    resolved = core_resolvers.resolve_bench(project, _bench(target), None)
    if resolved is None:
        bench_path = core_resolvers.DEFAULT_BENCH_PATH
        bench_warnings = [Message(code="bench.default_used", text=f"Using default: {bench_path}")]
    elif resolved.status is Status.NEEDS_CHOICE:
        return resolved
    else:
        assert resolved.data is not None
        bench_path = resolved.data
        bench_warnings = list(resolved.warnings)
    stopped = core_stop.stop(project, on_event=emit)
    if stopped.data is not None and stopped.data.db_clean_shutdown is False:
        return stopped
    started = core_start.start(project, bench_path=bench_path, on_event=emit)
    return Result(
        status=started.status,
        data={"stopped": _plain(stopped.data), "started": _plain(started.data)},
        warnings=[*bench_warnings, *stopped.warnings, *started.warnings],
        choice=started.choice,
    )


def check_start_port_conflicts(project: str) -> None:
    """The non-interactive half of the CLI start preflight (shared with the daemon)."""
    if start_cmd._frappe_running(project):
        return
    conflicting_projects, blocking_ports = start_cmd.detect_port_conflicts(project)
    if not conflicting_projects and not blocking_ports:
        return
    parts = []
    if conflicting_projects:
        parts.append(
            "Frappe instances already hold required ports: " + ", ".join(conflicting_projects) + "."
        )
    if blocking_ports:
        parts.append(
            "Host processes already hold required ports: "
            + ", ".join(str(port) for port in blocking_ports)
            + "."
        )
    raise CwcliError(
        ErrorKind.CONFLICT,
        "start.port_conflict",
        " ".join(parts),
        hint=(
            f"Free those ports first, or run `cwcli start {project}` in a terminal "
            "for interactive conflict resolution."
        ),
    )


def _clear_maintenance(target: dict[str, Any]) -> Result:
    project = _project(target)
    site = _site(target)
    if site is None:
        raise CwcliError(ErrorKind.USAGE, "site.required", "This job requires a site.")
    core_resolvers.validate_site_name(site)
    resolved = core_resolvers.resolve_container_and_bench(project, _bench(target), None)
    if isinstance(resolved, Result):
        return resolved
    container, bench_path, warnings = resolved
    off = core_bench_ops.clear_maintenance(container, bench_path, site, recheck=False)
    outcome = {
        "project": project,
        "bench_path": bench_path,
        "site": site,
        "maintenance_off": off,
        "ok": off,
    }
    return Result(status=Status.OK if off else Status.WARNING, data=outcome, warnings=warnings)


def _db_root_password(secret_values: dict[str, str]) -> str | None:
    """The MariaDB root password a job was given, else None so the core uses the
    instance's remembered one (`core.db_password.resolve`)."""
    return secret_values.get("db_root_password") or None


def _remove_instance(project: str, args: dict[str, Any], consent: bool, emit: _Recorder) -> Result:
    """`cwcli axi rm` as a job: consent ONLY (never an auto-start), never `--no-backup`.

    `core.remove` has no consent axis (rm's confirmation is a frontend concern),
    so the job supplies the refusal the `axi` verb supplies: without `consent`
    nothing runs and the record ends `needs_choice`/`confirm_remove`. A STOPPED
    project on the volume-deleting path is refused BEFORE the core call, exactly
    as `axi rm` refuses it: the verified live backup that gates volume deletion
    needs a running frappe container, and this surface will not start one. The
    `keep_volumes` arg is `--no-volumes`: containers, network, project directory
    and cache entry go, the named volumes (databases, sites, files) stay.
    """
    remove_volumes = not bool(args.get("keep_volumes"))
    if not consent:
        return Result(
            status=Status.NEEDS_CHOICE,
            choice=Choice(
                kind="confirm_remove",
                param="consent",
                prompt=(
                    f"Permanently remove instance '{project}'? This deletes its containers, "
                    + ("named volumes (databases, sites, files), " if remove_volumes else "")
                    + "project network and project directory."
                ),
            ),
        )
    if remove_volumes and rm_cmd._project_run_state(project) == "stopped":
        raise CwcliError(
            ErrorKind.NOT_RUNNING,
            "rm.not_running",
            f"Refusing to remove '{project}': it is not running, so the verified database "
            "backup that gates volume deletion cannot be taken.",
            hint=(
                f"Start it first (cwcli start {project}) and retry, or keep the named volumes, "
                f"or use 'cwcli rm {project} --no-backup' in a terminal to delete without a backup."
            ),
        )
    result = core_rm.remove(project, remove_volumes=remove_volumes, no_backup=False, on_event=emit)
    outcome = result.data
    assert outcome is not None  # remove always returns an outcome
    warnings = list(result.warnings)
    if not outcome.found:
        warnings.append(
            Message(code="rm.not_found", text=f"No instance named '{project}' was found.")
        )
    if outcome.failures and not outcome.backup_ok:
        warnings.append(
            Message(
                code="rm.backup_gate",
                text=(
                    "Nothing was deleted. Fix the backup failure and retry, or use "
                    f"'cwcli rm {project} --no-backup' in a terminal to delete without a backup."
                ),
            )
        )
    return Result(status=result.status, data=outcome, warnings=warnings)


def _run_init(
    target: dict[str, Any], args: dict[str, Any], secret_values: dict[str, str], emit: _Recorder
) -> Result:
    project = _project(target)
    version = str(args.get("version") or "16")
    frappe_ref = str(args.get("frappe_ref") or core_init.resolve_frappe_ref(version))
    bench_name = str(args.get("bench_name") or "frappe-bench")
    site_name = str(args.get("site_name") or f"{project}.localhost")
    bench_parent = str(args.get("bench_parent") or "/workspace")
    uid = args.get("uid")
    uid = int(uid) if uid is not None else None
    auto_start = args.get("auto_start") is True
    if auto_start:
        check_start_port_conflicts(project)
    instance_result = core_init.init_instance(
        project,
        port=int(args.get("port") or 8000),
        bench_parent=bench_parent,
        bench_image_tag=args.get("bench_image_tag") or None,
        auto_start=auto_start,
        uid=uid,
        db_root_password=_db_root_password(secret_values),
        stream_output=True,
        on_event=emit,
    )
    if instance_result.status is Status.NEEDS_CHOICE:
        return instance_result
    admin_password = secret_values.get("admin_password") or ""
    if not admin_password:
        raise CwcliError(
            ErrorKind.USAGE,
            "init.admin_password_required",
            "Init requires an Administrator password from the wizard.",
        )
    erpnext_branch = str(args.get("erpnext_branch") or frappe_ref)
    bench_result = core_init.init_bench(
        project,
        bench_name=bench_name,
        site_name=site_name,
        bench_parent=bench_parent,
        frappe_ref=frappe_ref,
        frappe_url=args.get("frappe_url") or None,
        db_root_password=_db_root_password(secret_values),
        admin_password=admin_password,
        reuse_bench=args.get("reuse_bench"),
        install_erpnext=bool(args.get("install_erpnext")),
        erpnext_branch=erpnext_branch,
        auto_start=auto_start,
        uid=uid,
        stream_output=True,
        on_event=emit,
    )
    if bench_result.status is Status.NEEDS_CHOICE or bench_result.data is None:
        return bench_result
    report = bench_result.data
    warnings = list(bench_result.warnings)
    if not cache.recache_project(project, bench_path=report.bench_path, warnings=warnings):
        emit.lifecycle("cache_warning", f"Could not refresh the cache for {project}.")
    started = None
    if args.get("start", True):
        started_result = core_start.start(
            project, bench_path=report.bench_path, uid_override=uid, on_event=emit
        )
        started = started_result.data
        warnings.extend(started_result.warnings)
    return Result(
        status=Status.WARNING if warnings else Status.OK,
        data={"init": _plain(report), "started": _plain(started)},
        warnings=warnings,
    )


def _refresh_after_apps(project: str, result: Result, emit: _Recorder) -> Result:
    report = result.data
    if report is None or not any(getattr(row, "ok", False) for row in report.results):
        return result
    warnings = list(result.warnings)
    if not cache.recache_project(project, bench_path=report.bench_path, warnings=warnings):
        emit.lifecycle("cache_warning", f"Could not refresh the cache for {project}.")
    return Result(status=result.status, data=report, warnings=warnings, choice=result.choice)


def _run_self_update(args: dict[str, Any], emit: _Recorder) -> Result:
    info_result = core_version.check(use_cache=not bool(args.get("no_cache")))
    info = info_result.data
    assert info is not None
    if info.is_dev or info.method in {"uvx", "standalone"}:
        return Result(status=info_result.status, data=info, warnings=info_result.warnings)
    if info.latest is None:
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "self_update.network_failed",
            "Could not reach PyPI to check for updates.",
        )
    if not info.is_outdated:
        return Result(status=info_result.status, data=info, warnings=info_result.warnings)
    if info.upgrade_command is None:
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "self_update.unavailable",
            "cwcli could not determine a usable upgrade command.",
        )
    emit.lifecycle(
        "step_start", f"Upgrading cwcli {info.current} to {info.latest}", phase="upgrade"
    )
    try:
        process = subprocess.Popen(
            info.upgrade_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
    except OSError as exc:
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "self_update.command_failed",
            f"Could not start '{info.upgrade_command[0]}': {exc}",
        ) from exc
    assert process.stdout is not None
    for line in process.stdout:
        emit.lifecycle("output", line, phase="upgrade", stream="stdout")
    code = process.wait()
    emit.lifecycle("step_end", "Upgrade finished", phase="upgrade", ok=code == 0)
    if code != 0:
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "self_update.failed",
            f"The upgrade command exited with code {code}.",
        )
    return Result(
        status=Status.OK,
        data={"current": info.current, "latest": info.latest, "updated": True, "ok": True},
        warnings=info_result.warnings,
    )


def _dispatch(
    verb: str,
    target: dict[str, Any],
    args: dict[str, Any],
    consent: bool,
    secret_values: dict[str, str],
    emit: _Recorder,
) -> Result:
    project = _project(target) if verb != "self-update" else ""
    bench = _bench(target)
    site = _site(target)
    if verb == "start":
        check_start_port_conflicts(project)
        return core_start.start(project, bench=bench, on_event=emit)
    if verb == "stop":
        return core_stop.stop(project, on_event=emit)
    if verb == "restart":
        check_start_port_conflicts(project)
        return _restart_instance(target, emit)
    if verb == "stop-bench":
        return core_stop.stop_bench(project, bench=bench, on_event=emit)
    if verb == "restart-process":
        emit.lifecycle("step_start", "Restarting the process", phase="restart-process")
        result = core_restart.restart_process(
            project, str(target.get("process") or ""), bench=bench
        )
        emit.lifecycle("step_end", "Process restart finished", phase="restart-process")
        return result
    if verb == "set-label":
        return core_label.set_label(project, bench=bench, label=str(args.get("label") or ""))
    if verb == "unlock":
        return core_unlock.unlock(project, site=site, bench=bench, on_event=emit)
    if verb == "clear-maintenance":
        return _clear_maintenance(target)
    if verb == "scale":
        return core_scale.scale(
            project,
            to=int(args["to"]) if args.get("to") is not None else None,
            consent=consent,
            on_event=emit,
        )
    if verb == "apps-checkout":
        # `reset` (discard tracked local work, `git reset --hard` before the checkout) is the
        # verb's one destructive element; the page sends it only behind a typed
        # confirmation of the app's name. It deletes no site data, so the core has
        # no consent axis for it (the `axi apps checkout --reset` ruling).
        checkout_result = core_apps.checkout_app(
            project,
            str(target.get("app") or ""),
            str(args.get("ref") or ""),
            bench=bench,
            reset=args.get("reset") is True,
            on_event=emit,
        )
        return _refresh_after_apps(project, checkout_result, emit)
    if verb == "rm":
        return _remove_instance(project, args, consent, emit)
    if verb == "rm-site":
        if site is None:
            raise CwcliError(ErrorKind.USAGE, "site.required", "Dropping a site requires a site.")
        return core_rm_site.drop_site(
            project,
            site,
            bench=bench,
            consent=consent,
            db_root_password=_db_root_password(secret_values),
            on_event=emit,
        )
    if verb == "rm-bench":
        return core_rm_bench.remove_bench(
            project,
            bench=bench,
            consent=consent,
            db_root_password=_db_root_password(secret_values),
            on_event=emit,
        )
    if verb == "init":
        return _run_init(target, args, secret_values, emit)
    if verb == "apps-install":
        install_result = core_apps.install_apps(
            project,
            [str(app) for app in args.get("apps") or []],
            bench=bench,
            sites=[str(value) for value in args.get("sites") or []] or None,
            branch=args.get("branch") or None,
            fetch_only=bool(args.get("fetch_only")),
            if_not_present=True,
            on_event=emit,
        )
        return _refresh_after_apps(project, install_result, emit)
    if verb == "apps-update":
        return core_update.update(
            project,
            [str(app) for app in args.get("apps") or []],
            bench=bench,
            sites=[str(value) for value in args.get("sites") or []] or None,
            clear_cache=bool(args.get("clear_cache")),
            clear_website_cache=bool(args.get("clear_website_cache")),
            build=bool(args.get("build")),
            skip_maintenance=bool(args.get("skip_maintenance")),
            no_recache=False,
            force=bool(args.get("force")),
            preflight=not bool(args.get("no_preflight")),
            on_event=emit,
        )
    if verb == "backup":
        return core_backup.backup(
            project,
            site=site,
            bench=bench,
            with_files=bool(args.get("with_files")),
            on_event=emit,
        )
    if verb == "migrate":
        return core_bench_ops.migrate_site(project, site=site, bench=bench, on_event=emit)
    if verb == "run-tests":
        if site is None:
            raise CwcliError(ErrorKind.USAGE, "site.required", "Run-tests requires a site.")
        return core_bench_ops.run_tests(
            project,
            site=site,
            app=str(target.get("app") or ""),
            module=args.get("module") or None,
            bench=bench,
            on_event=emit,
        )
    if verb == "build":
        return core_bench_ops.build_assets(
            project, app=target.get("app") or None, bench=bench, on_event=emit
        )
    if verb == "setup-wizard":
        return core_bench_ops.complete_setup_wizard(
            project,
            site=site,
            bench=bench,
            country=args.get("country") or None,
            currency=args.get("currency") or None,
            timezone=args.get("timezone") or None,
            language=args.get("language") or None,
            on_event=emit,
        )
    if verb == "self-update":
        return _run_self_update(args, emit)
    raise AssertionError(f"unhandled job verb {verb}")


def _result_ok(result: Result) -> bool:
    data = result.data
    if data is None:
        return result.status in {Status.OK, Status.WARNING}
    if isinstance(data, dict) and "ok" in data:
        return bool(data["ok"])
    if hasattr(data, "ok"):
        return bool(data.ok)
    if hasattr(data, "db_clean_shutdown") and data.db_clean_shutdown is False:
        return False
    # `RemovalOutcome` carries no `ok`: the exit code reads its `failures`, never
    # the envelope status (a partial removal is a WARNING-shaped envelope).
    if hasattr(data, "failures") and data.failures:
        return False
    return True


def _cause(*texts: Any) -> dict[str, str] | None:
    """The failure's plain cause, read only from the failing step's own output and
    message, when a known shape matches."""
    cause = failure_causes.explain(*(str(text) for text in texts if text))
    return {"code": cause.code, "title": cause.title, "text": cause.text} if cause else None


def run_job(job_id: str) -> int:
    signals.install_unwind_handlers()
    spec = _read_json(_json_path(job_id, "spec.json"))
    secret_values = {name: os.environ.pop(env_name, "") for name, env_name in _SECRET_ENV.items()}
    scrub_values = [value for value in secret_values.values() if value]
    emit = _Recorder(job_id, scrub_values)
    pid = os.getpid()
    _merge_state(
        job_id,
        state="running",
        pid=pid,
        process_started=daemon_identity.process_start_time(pid),
        started_at=_now(),
    )
    emit.lifecycle("job_started", f"{spec['verb']} started", phase="job")
    cleanup_started = _json_path(job_id, _CLEANUP_STARTED)

    def _begin_cleanup(_signum, _frame):
        cleanup_started.touch()
        raise KeyboardInterrupt

    previous_sigint = signal.signal(signal.SIGINT, _begin_cleanup)
    stop_watching = threading.Event()
    threading.Thread(
        target=_watch_interrupt_request, args=(job_id, stop_watching), daemon=True
    ).start()
    try:
        result = _dispatch(
            str(spec["verb"]),
            dict(spec.get("target") or {}),
            dict(spec.get("args") or {}),
            bool(spec.get("consent")),
            secret_values,
            emit,
        )
        if result.status is Status.NEEDS_CHOICE:
            emit.lifecycle(
                "needs_choice", result.choice.prompt if result.choice else "Choice required"
            )
            _merge_state(
                job_id,
                state="needs_choice",
                finished_at=_now(),
                exit_code=2,
                choice=_scrub(_plain(result.choice), scrub_values),
                warnings=_scrub(_plain(result.warnings), scrub_values),
            )
            return 2
        ok = _result_ok(result)
        state = "succeeded" if ok else "failed"
        emit.lifecycle("job_finished", f"{spec['verb']} {state}", phase="job", ok=ok)
        _merge_state(
            job_id,
            state=state,
            finished_at=_now(),
            exit_code=0 if ok else 1,
            result=_scrub(_plain(result.data), scrub_values),
            warnings=_scrub(_plain(result.warnings), scrub_values),
        )
        if ok and spec["verb"] == "self-update" and isinstance(result.data, dict):
            if result.data.get("updated"):
                _schedule_ui_restart()
        return 0 if ok else 1
    except KeyboardInterrupt:
        emit.lifecycle("job_interrupted", "The job was interrupted after cleanup.", phase="job")
        _merge_state(job_id, state="interrupted", finished_at=_now(), exit_code=130)
        return 130
    except CwcliError as exc:
        error = {
            "type": exc.kind.value,
            "code": exc.code,
            "message": exc.message,
            "hint": exc.hint,
            "detail": exc.detail,
        }
        error = _scrub(_plain(error), scrub_values)
        cause = _scrub(_cause((exc.detail or {}).get("output"), exc.message), scrub_values)
        emit.lifecycle("job_failed", error["message"], phase="job", code=error["code"])
        _merge_state(
            job_id, state="failed", finished_at=_now(), exit_code=1, error=error, cause=cause
        )
        return 1
    except Exception as exc:  # pragma: no cover - defensive detached-process boundary
        message = _scrub(str(exc), scrub_values)
        emit.lifecycle("job_failed", message, phase="job", code="job.internal_error")
        _merge_state(
            job_id,
            state="failed",
            finished_at=_now(),
            exit_code=1,
            error={
                "type": ErrorKind.INTERNAL.value,
                "code": "job.internal_error",
                "message": message,
                "hint": "Review the last event and the daemon log.",
            },
        )
        return 1
    finally:
        stop_watching.set()
        signal.signal(signal.SIGINT, previous_sigint)


def _schedule_ui_restart() -> None:
    required = [_UI_ENV[key] for key in ("pid", "start", "port", "token", "session")]
    if not all(os.environ.get(name) for name in required):
        return
    creationflags = 0x00000008 | 0x00000200 if sys.platform == "win32" else 0
    subprocess.Popen(
        [sys.executable, "-m", "caffeinated_whale_cli.jobs", "restart-ui"],
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=dict(os.environ),
        creationflags=creationflags,
        start_new_session=sys.platform != "win32",
    )


def restart_ui() -> int:
    """Replace the daemon after self-update, preserving its token and browser session."""
    from .commands import serve as serve_cmd
    from .commands import ui as ui_cmd

    try:
        pid = int(os.environ[_UI_ENV["pid"]])
        port = int(os.environ[_UI_ENV["port"]])
        recorded_start = os.environ[_UI_ENV["start"]]
        token = os.environ[_UI_ENV["token"]]
        session_id = os.environ[_UI_ENV["session"]]
    except (KeyError, ValueError):
        return 2
    if daemon_identity.pid_alive(pid) and daemon_identity.process_start_time(pid) == recorded_start:
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and daemon_identity.pid_alive(pid):
        time.sleep(0.1)
    if daemon_identity.pid_alive(pid):
        return 1
    ui_cmd._clear_state()
    os.environ[serve_cmd.TOKEN_ENV] = token
    os.environ[serve_cmd.SESSION_ENV] = session_id
    ui_cmd._spawn_detached(port, token, session_id)
    return 0


def reap_children() -> None:
    """Collect every worker that has exited, so its pid stops reading as alive."""
    for job_id, process in list(_CHILDREN.items()):
        if process.poll() is not None:
            del _CHILDREN[job_id]


def watch_loop(publish, stop, *, active=lambda: True, interval: float = 0.25) -> None:
    """Publish changed job summaries to the daemon's SSE hub.

    A finished job is re-read only when its ``state.json`` changes. An active one is
    re-read every tick, because a vanished worker becomes ``unknown`` on read.
    """
    seen: dict[str, tuple[int, dict[str, Any]]] = {}
    while not stop.wait(interval):
        reap_children()
        if not active():
            continue
        current: dict[str, tuple[int, dict[str, Any]]] = {}
        for job_id in _job_ids():
            prior = seen.get(job_id)
            try:
                mtime = _json_path(job_id, "state.json").stat().st_mtime_ns
                if prior and prior[0] == mtime and prior[1].get("state") not in ACTIVE_STATES:
                    current[job_id] = prior
                    continue
                record = _summary(read_job(job_id))
            except (OSError, CwcliError):
                continue
            current[job_id] = (mtime, record)
            if prior is None or prior[1] != record:
                publish(record)
        for removed in set(seen) - set(current):
            publish({"id": removed, "removed": True})
        seen = current


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m caffeinated_whale_cli.jobs")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("job_id")
    sub.add_parser("restart-ui")
    parsed = parser.parse_args(argv)
    if parsed.command == "run":
        return run_job(parsed.job_id)
    return restart_ui()


if __name__ == "__main__":
    raise SystemExit(main())
