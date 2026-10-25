"""The instance's MariaDB root password: set at init, remembered for later commands.

frappe_docker's compose template hardcodes ``MYSQL_ROOT_PASSWORD: 123``. A
password given to ``init`` used to reach only ``bench new-site``, so MariaDB was
created with ``123`` while bench presented the new value and was refused with a
1045. Now a non-default password is written to the instance's own secret file
``conf/db-root-password`` (owner-only, group-shared in shared mode) and the
compose hands it to MariaDB as a compose secret (``MARIADB_ROOT_PASSWORD_FILE``),
so the value never lands in the compose file, the container's environment, an
argv, or a log. The same file is what later commands that need the root password
(``rm-site``, ``rm-bench``, ``restore``, a re-run ``init``) read when they are
not given one. It lives in ``conf/``, so ``rm`` archives it with the rest of the
project config (next to the archived ``site_config.json`` credentials) and
removes it with the project.

MariaDB reads its root password only when it initializes an EMPTY data volume,
so the compose is changed only while the project's ``mariadb-data`` volume does
not exist yet. A blank password keeps today's default, ``123``, and leaves the
compose exactly as downloaded.
"""

from __future__ import annotations

import contextlib
import os
import re
from pathlib import Path

from ..utils import config_utils, shared_home
from .errors import CwcliError, ErrorKind

DEFAULT = "123"
SECRET_FILE = "db-root-password"
_SECRET_NAME = "db_root_password"

_ENV_LINE = re.compile(r"^(?P<indent>[ \t]+)MYSQL_ROOT_PASSWORD:[^\n]*$", re.MULTILINE)
_SERVICE_LINE = re.compile(r"^(?P<indent>[ \t]*)mariadb:[ \t]*$", re.MULTILINE)


def secret_path(project: str) -> Path:
    return config_utils.PROJECTS_DIR / project / "conf" / SECRET_FILE


def remembered(project: str) -> str | None:
    """The password stored for ``project`` at init, or None when none was set."""
    try:
        value = secret_path(project).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def resolve(project: str, given: str | None) -> str:
    """The root password to use: the one given, else the remembered one, else ``123``."""
    return given or remembered(project) or DEFAULT


def remember(project: str, password: str) -> Path:
    """Write ``password`` to the project's secret file, readable only by its owner."""
    path = secret_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = shared_home.file_mode(0o600)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(password)
    with contextlib.suppress(OSError):
        os.chmod(path, mode)  # an existing file keeps its old mode through O_CREAT
        shared_home.apply_group(path)
    return path


def uses_secret(compose: str) -> bool:
    return "MARIADB_ROOT_PASSWORD_FILE" in compose


def apply_to_compose(compose: str) -> str:
    """Point the compose's MariaDB at the secret file instead of the hardcoded password.

    Idempotent. Raises ``PRECONDITION`` when the template no longer has the
    shape this rewrite expects, rather than starting MariaDB with ``123`` and
    letting ``bench new-site`` fail on a password nobody chose.
    """
    if uses_secret(compose):
        return compose
    env = _ENV_LINE.search(compose)
    service = _SERVICE_LINE.search(compose)
    if env is None or service is None:
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "compose.db_password_unsupported",
            "The downloaded Docker Compose template could not be configured with the "
            "database root password: its MariaDB service no longer has the expected shape.",
            hint="Retry without a database root password (the default is 123), and report "
            "the upstream compose change.",
        )
    child = service.group("indent") + "  "
    compose = (
        compose[: env.start()]
        + f"{env.group('indent')}MARIADB_ROOT_PASSWORD_FILE: /run/secrets/{_SECRET_NAME}"
        + compose[env.end() :]
    )
    service = _SERVICE_LINE.search(compose)
    assert service is not None  # the edit above never touches the service line
    compose = (
        compose[: service.end()]
        + f"\n{child}secrets:\n{child}  - {_SECRET_NAME}"
        + compose[service.end() :]
    )
    if not compose.endswith("\n"):
        compose += "\n"
    return compose + f"secrets:\n  {_SECRET_NAME}:\n    file: ./{SECRET_FILE}\n"
