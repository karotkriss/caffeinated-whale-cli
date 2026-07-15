"""``core.backup`` - the reference command taken end to end through the core.

Owns everything ``commands/backup.py`` used to do between "ensure running" and
the printed banner: container resolution, bench resolution, default-site
resolution, site/path shell-metachar validation, backup-directory ensure, the
``bench --site <site> backup`` exec, and locating the produced dump. It prints,
prompts, and exits nothing: it returns ``NEEDS_CHOICE`` for the multi-bench and
stopped-container forks, raises :class:`~.errors.CwcliError` for hard failures,
and returns ``Result(OK, BackupOutcome(...))`` on success.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from . import docker as core_docker
from . import resolvers
from .envelope import Message, Result, Status
from .errors import CwcliError, ErrorKind


@dataclass(frozen=True, slots=True, kw_only=True)
class BackupOutcome:
    """The typed outcome of a successful backup (serializable, no live objects)."""

    site: str
    bench_path: str
    artifact_path: str
    included_files: bool


def _decode(output) -> str:
    if isinstance(output, (bytes, bytearray)):
        return bytes(output).decode("utf-8", errors="replace")
    return str(output) if output is not None else ""


def backup(
    project_name: str,
    *,
    site: str | None = None,
    bench: str | None = None,
    bench_path: str | None = None,
    with_files: bool = False,
) -> Result[BackupOutcome]:
    """Back up a site's database (and optionally files). See module docstring."""
    warnings: list[Message] = []

    # 1. Resolve the frappe container (raises NOT_FOUND / DOCKER).
    frappe_container = core_docker.get_frappe_container(project_name)

    # 2. Container must be running; a stopped container is a confirm_start fork.
    state = resolvers.resolve_container_state(project_name, frappe_container, offer_choice=True)
    if state.status is Status.NEEDS_CHOICE:
        return Result(status=Status.NEEDS_CHOICE, choice=state.choice)

    # 3. Resolve which bench to back up (--bench/--path, else single, else default).
    bench_result = resolvers.resolve_bench(project_name, bench, bench_path)
    if bench_result is None:
        bench_path = resolvers.DEFAULT_BENCH_PATH
        warnings.append(
            Message(
                "bench.default_used",
                f"No cached bench path found. Using default: {resolvers.DEFAULT_BENCH_PATH}",
            )
        )
    elif bench_result.status is Status.NEEDS_CHOICE:
        return Result(status=Status.NEEDS_CHOICE, choice=bench_result.choice)
    else:
        bench_path = bench_result.data
        warnings.extend(bench_result.warnings)

    assert bench_path is not None  # narrowed by the branches above

    # 4. Resolve the default site when --site was not given.
    if not site:
        site = resolvers.resolve_default_site(project_name, bench_path)
        warnings.append(Message("default_site.resolved", f"Using default site: {site}"))

    # 5-6. Validate the site name and bench path (empty / shell metacharacters).
    resolvers.validate_site_name(site)
    resolvers.validate_bench_path(bench_path)

    # 7-8. Verify the bench directory and the site exist. (argv lists: no shell,
    # no quoting to reason about.)
    resolvers.require_bench_dir(frappe_container, bench_path)
    site_path = resolvers.require_site_dir(frappe_container, bench_path, site)

    # 9. Ensure the backup directory exists (create if missing).
    backup_dir = f"{site_path}/private/backups"
    exit_code, _ = frappe_container.exec_run(["test", "-d", backup_dir])
    if exit_code != 0:
        exit_code, output = frappe_container.exec_run(["mkdir", "-p", backup_dir])
        if exit_code != 0:
            raise CwcliError(
                ErrorKind.PRECONDITION,
                "backup_dir.failed",
                f"Failed to create backup directory at {backup_dir}",
                detail={"output": _decode(output)},
            )
        warnings.append(Message("backup_dir.created", f"Creating backup directory at {backup_dir}"))

    # 10. Run the backup.
    cmd = ["bench", "--site", site, "backup"]
    if with_files:
        cmd.append("--with-files")
    exit_code, output = frappe_container.exec_run(cmd, workdir=bench_path)
    if exit_code != 0:
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "backup.failed",
            f"Failed to create backup for site '{site}'",
            detail={"output": _decode(output)},
        )

    # 11. Locate the dump this run produced (newest *.sql.gz), for the outcome DTO.
    artifact_path = _newest_dump(frappe_container, backup_dir) or f"{backup_dir}/"

    return Result(
        status=Status.OK,
        data=BackupOutcome(
            site=site,
            bench_path=bench_path,
            artifact_path=artifact_path,
            included_files=with_files,
        ),
        warnings=warnings,
    )


def _newest_dump(frappe_container, backup_dir: str) -> str | None:
    """Full container path of the newest ``*.sql.gz`` in ``backup_dir``, or None."""
    quoted = shlex.quote(backup_dir)
    exit_code, output = frappe_container.exec_run(
        f'sh -c "ls -1t {quoted}/*.sql.gz 2>/dev/null | head -1"'
    )
    if exit_code != 0:
        return None
    path = _decode(output).strip()
    return path or None
