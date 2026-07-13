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

from ..utils import db_utils
from . import docker as core_docker
from . import resolvers
from .envelope import Message, Result, Status
from .errors import CwcliError, ErrorKind

# Shell metacharacters rejected in site names and bench paths (command-injection guard).
_INVALID_CHARS = [";", "&", "|", "$", "`", "(", ")", "<", ">", "\n", "\r", "\\"]


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
        try:
            default_site = db_utils.get_default_site(project_name, bench_path)
        except Exception as e:  # noqa: BLE001 - surface as a typed error, never print
            raise CwcliError(
                ErrorKind.INTERNAL,
                "default_site.error",
                f"Failed to retrieve default site: {e}",
            ) from e
        if default_site:
            site = default_site
            warnings.append(Message("default_site.resolved", f"Using default site: {site}"))
        else:
            raise CwcliError(
                ErrorKind.NOT_FOUND,
                "site.no_default",
                "No site specified and no default site found in config.",
            )

    # 5. Validate the site name (empty / shell metacharacters).
    if not site or not site.strip():
        raise CwcliError(ErrorKind.USAGE, "site.empty", "Site name cannot be empty.")
    if any(char in site for char in _INVALID_CHARS):
        raise CwcliError(
            ErrorKind.USAGE,
            "site.invalid_chars",
            f"Invalid site name '{site}'. Site names cannot contain special shell characters.",
        )

    # 6. Validate the bench path.
    if any(char in bench_path for char in _INVALID_CHARS):
        raise CwcliError(
            ErrorKind.USAGE,
            "bench_path.invalid_chars",
            f"Invalid bench path '{bench_path}'. Paths cannot contain special shell characters.",
        )

    # 7. Verify the bench directory exists.
    bench_sites_path = f"{bench_path}/sites"
    quoted_bench_sites_path = shlex.quote(bench_sites_path)
    exit_code, _ = frappe_container.exec_run(f'sh -c "test -d {quoted_bench_sites_path}"')
    if exit_code != 0:
        raise CwcliError(
            ErrorKind.NOT_FOUND,
            "bench.dir_missing",
            f"Bench directory not found at {bench_path}",
        )

    # 8. Verify the site exists.
    site_path = f"{bench_path}/sites/{site}"
    quoted_site_path = shlex.quote(site_path)
    exit_code, _ = frappe_container.exec_run(f'sh -c "test -d {quoted_site_path}"')
    if exit_code != 0:
        raise CwcliError(
            ErrorKind.NOT_FOUND,
            "site.not_found",
            f"Site '{site}' not found at {site_path}",
        )

    # 9. Ensure the backup directory exists (create if missing).
    backup_dir = f"{site_path}/private/backups"
    quoted_backup_dir = shlex.quote(backup_dir)
    exit_code, _ = frappe_container.exec_run(f"sh -c 'test -d {quoted_backup_dir}'")
    if exit_code != 0:
        exit_code, output = frappe_container.exec_run(f"sh -c 'mkdir -p {quoted_backup_dir}'")
        if exit_code != 0:
            raise CwcliError(
                ErrorKind.PRECONDITION,
                "backup_dir.failed",
                f"Failed to create backup directory at {backup_dir}",
                detail={"output": _decode(output)},
            )

    # 10. Run the backup.
    cmd = f"bench --site {site} backup"
    if with_files:
        cmd += " --with-files"
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
