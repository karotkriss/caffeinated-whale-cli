"""Core Docker accessors: resolve a container, raise typed errors, leak nothing.

The frappe container object stays INTERNAL to the core function that execs into
it - it is never placed in a DTO returned across a ``core.<verb>`` boundary. The
CLI-facing ``docker_utils.get_frappe_container`` is re-expressed as a thin
wrapper over :func:`get_frappe_container` here, so there is one resolution
implementation and the CLI's message/exit behavior is preserved.

The UI-free Docker primitives (:func:`get_project_containers`,
:func:`get_project_volumes`, :func:`utf8_stream_decoder`) live HERE rather than
in ``utils/docker_utils.py`` so the core never imports that module - which pulls
``typer``/``rich`` at load time for its CLI error helpers. Keeping them in the
core makes the UI-purity ban transitively true, not just directly true.
"""

from __future__ import annotations

import codecs
import io
import os
import shlex
import subprocess
import tarfile
from collections.abc import Iterator, Sequence
from pathlib import Path

import docker
from docker.errors import APIError, DockerException, NotFound

from ..utils import shared_home
from .errors import DOCKER_UNREACHABLE_HINT, CwcliError, ErrorKind

_COMPOSE_INSTALL_HINT = (
    "Install the Docker Compose v2 plugin: it ships with Docker Desktop, or "
    "install the 'docker-compose-plugin' package on a bare Docker Engine host."
)


def ensure_compose_available() -> None:
    """Fail closed, up front, when ``docker compose`` (the v2 plugin) is missing.

    ``init``/``scale`` both shell out to ``docker compose ...`` well after they
    have already created state (a project dir, a widened compose file, ...); a
    missing plugin used to surface as a raw ``compose.failed``/
    ``compose.recreate_failed`` subprocess error mid-run. One cheap
    ``docker compose version`` probe at the top of each catches it before any
    of that.
    """
    try:
        result = subprocess.run(["docker", "compose", "version"], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "compose.unavailable",
            f"Could not run 'docker compose version': {e}",
            hint=_COMPOSE_INSTALL_HINT,
        ) from e
    if result.returncode != 0:
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "compose.unavailable",
            "The Docker Compose v2 plugin is not available ('docker compose version' failed).",
            hint=_COMPOSE_INSTALL_HINT,
        )


def utf8_stream_decoder() -> codecs.IncrementalDecoder:
    """A UTF-8 decoder that carries a partial character ACROSS exec-stream chunks.

    Docker frames an exec socket at 32KB and ``bench`` (CPython, talking to a pipe)
    flushes in 8KB blocks, so a chunk boundary lands at an arbitrary BYTE offset -
    routinely mid-character, since Frappe emits unicode (``✓ ✗ → ─ │ └ ⚠ ✅``)
    routinely. Decoding a chunk in isolation is therefore wrong: strict decoding
    raises on the split, and ``errors="replace"`` corrupts the character to U+FFFD.
    One incremental decoder held across the whole stream buffers those leftover
    bytes until the next chunk completes the character.

    ``replace`` still applies to input that is genuinely not UTF-8 - streaming
    another program's output must never kill the CLI - but a mere split is no
    longer mistaken for invalid input.

    Each stream needs its OWN decoder: a demuxed exec (``init``) must not feed
    stderr's bytes into stdout's pending character.
    """
    return codecs.getincrementaldecoder("utf-8")("replace")


def get_project_containers(
    project_name: str,
) -> list[docker.models.containers.Container] | None:
    """
    Finds all containers belonging to a specific Docker Compose project.

    Args:
        project_name: The name of the docker-compose project.

    Returns:
        A list of container objects, an empty list if not found,
        or None if there was a Docker connection error.
    """
    try:
        client = docker.from_env()
        client.ping()

        containers: list = client.containers.list(
            all=True, filters={"label": f"com.docker.compose.project={project_name}"}
        )
        return containers

    except DockerException:
        return None


def get_project_volumes(project_name: str):
    """
    Finds all named volumes belonging to a specific Docker Compose project.

    These are the volumes Docker Compose creates and labels (e.g. ``sites``,
    ``db-data``). They are distinct from anonymous container volumes, which
    ``Container.remove(v=True)`` already handles.

    Args:
        project_name: The name of the docker-compose project.

    Returns:
        A list of volume objects, an empty list if none are found,
        or None if there was a Docker connection error.
    """
    try:
        client = docker.from_env()
        client.ping()

        volumes = client.volumes.list(
            filters={"label": f"com.docker.compose.project={project_name}"}
        )
        return volumes

    except DockerException:
        return None


def get_project_networks(project_name: str):
    """
    Finds the Docker network(s) belonging to a specific Docker Compose project.

    Compose labels a project's own network (typically ``<project>_default``)
    with the same ``com.docker.compose.project`` label it puts on containers and
    named volumes, so this is an exact-name lookup via that label, never a
    prefix match or a broad listing.

    Args:
        project_name: The name of the docker-compose project.

    Returns:
        A list of network objects, an empty list if none are found,
        or None if there was a Docker connection error.
    """
    try:
        client = docker.from_env()
        client.ping()

        networks = client.networks.list(
            filters={"label": f"com.docker.compose.project={project_name}"}
        )
        return networks

    except DockerException:
        return None


_BUILTIN_NETWORKS = frozenset({"bridge", "host", "none", "default"})


def recreate_missing_project_networks(project_name: str, containers) -> list[str]:
    """Recreate the compose network(s) a project's containers are attached to but
    Docker no longer has, and return the names recreated.

    A WSL/Docker crash can drop ``<project>_default`` while the stopped containers
    keep pointing at it, so ``container.start()`` dies with ``network ... not found``
    and compose is the only way back. Only a network this project OWNS is recreated:
    compose names its own networks ``<project>_<key>``, and a missing one under any
    other name is an external network we must never invent, so that raises instead,
    naming the exact ``docker network create``. Recreated with what compose gives it
    (name, driver ``bridge``, the ``com.docker.compose.{project,network,version}``
    labels), so ``get_project_networks``/``cwcli rm`` find it again.

    A recreated network has a NEW id, and a stopped container keeps the old one (its
    start still dies with ``network <old id> not found``) and would lose its compose
    DNS aliases (``mariadb``, ``frappe``, ...). So each attached container whose
    recorded id is not the live network's is connected to it (which replaces the stale
    entry) with the aliases it had, without being recreated: its filesystem, toolchain
    and data stay as they were. This holds for a network that already exists too - one
    created by hand, by following a hint below, or by an earlier start that failed
    part-way - so every remedy printed here leads to a working start.
    """
    client = docker.from_env()
    wanted: dict[str, list] = {}
    for container in containers:
        attrs = container.attrs
        names = set((attrs.get("NetworkSettings") or {}).get("Networks") or {})
        names.add((attrs.get("HostConfig") or {}).get("NetworkMode") or "")
        for name in names - _BUILTIN_NETWORKS - {""}:
            if not name.startswith("container:"):
                wanted.setdefault(name, []).append(container)

    recreated: list[str] = []
    for name, attached in sorted(wanted.items()):
        prefix = f"{project_name}_"
        try:
            network = client.networks.get(name)
        except NotFound:
            if not name.startswith(prefix) or name == prefix:
                raise CwcliError(
                    ErrorKind.DOCKER,
                    "network.external_missing",
                    f"The network '{name}' is missing and is not owned by project "
                    f"'{project_name}'.",
                    hint=(
                        f"Create it with 'docker network create {name}', then run the start again."
                    ),
                ) from None
            labels = {
                "com.docker.compose.project": project_name,
                "com.docker.compose.network": name[len(prefix) :],
            }
            compose_version = attached[0].labels.get("com.docker.compose.version")
            if compose_version:
                labels["com.docker.compose.version"] = compose_version
            try:
                network = client.networks.create(name, driver="bridge", labels=labels)
            except APIError as e:
                raise CwcliError(
                    ErrorKind.DOCKER,
                    "network.recreate_failed",
                    f"Could not recreate the missing network '{name}': {e.explanation or e}",
                    hint=(
                        f"Create it with 'docker network create --label "
                        f"com.docker.compose.project={project_name} --label "
                        f"com.docker.compose.network={name[len(prefix):]} {name}', "
                        "then run the start again."
                    ),
                ) from e
            recreated.append(name)
        for container in attached:
            endpoint = ((container.attrs.get("NetworkSettings") or {}).get("Networks") or {}).get(
                name
            ) or {}
            if endpoint.get("NetworkID") == network.id:
                continue
            aliases = endpoint.get("Aliases") or [
                container.name,
                container.labels.get("com.docker.compose.service", container.name),
            ]
            try:
                network.connect(container, aliases=aliases)
            except APIError as e:
                alias_flags = "".join(f"--alias {alias} " for alias in aliases)
                raise CwcliError(
                    ErrorKind.DOCKER,
                    "network.reconnect_failed",
                    f"Could not reconnect '{container.name}' to the network '{name}': "
                    f"{e.explanation or e}",
                    hint=(
                        f"Connect it with 'docker network connect {alias_flags}{name} "
                        f"{container.name}', then run the start again."
                    ),
                ) from e
    return recreated


class ChunkStreamReader(io.RawIOBase):
    """Adapt docker-py's ``get_archive`` byte-chunk iterator to a readable stream.

    ``get_archive`` yields the artifact's tar wrapper in chunks; wrapping those
    chunks in this reader lets :mod:`tarfile` consume the tar incrementally
    (streaming mode) so a multi-GB backup artifact is never buffered whole in
    host RAM.
    """

    def __init__(self, chunks: Iterator[bytes]) -> None:
        super().__init__()
        self._chunks = iter(chunks)
        self._buf = b""

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        if not self._buf:
            try:
                self._buf = next(self._chunks)
            except StopIteration:
                return 0
        n = min(len(b), len(self._buf))
        b[:n] = self._buf[:n]
        self._buf = self._buf[n:]
        return n


def stream_container_file(
    container,
    source_path: str,
    dest_file: Path,
    chunk_size: int = 1024 * 1024,
) -> bool:
    """Copy a single file out of ``container`` to the host, streaming in chunks.

    Uses ``get_archive`` (a tar stream) and extracts the single member
    incrementally, so a multi-GB ``bench backup --with-files`` artifact is never
    materialised in host RAM as one blob. Returns True only on a fully-written
    copy. Any failure - a missing/unreadable file (``get_archive`` raises), a
    tar/read error, or a truncated stream (bytes written do not match the member
    size) - returns False, so the caller keeps the "a backup missing any part is
    not trusted" fail-closed semantics.
    """
    try:
        stream, _stat = container.get_archive(source_path)
        reader = io.BufferedReader(ChunkStreamReader(stream))
        with tarfile.open(fileobj=reader, mode="r|") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                extracted = tar.extractfile(member)
                if extracted is None:
                    return False
                written = 0
                with open(dest_file, "wb") as out:
                    while True:
                        chunk = extracted.read(chunk_size)
                        if not chunk:
                            break
                        out.write(chunk)
                        written += len(chunk)
                # A truncated stream yields fewer bytes than the header promised:
                # fail closed rather than trust a short copy.
                return written == member.size
    except Exception:
        return False
    # No regular-file member in the archive - nothing was copied.
    return False


def get_container(container_id: str):
    """Resolve a container ID back to an exec-usable handle.

    The id-to-handle half of the two-phase exec seam: ``core.run_plan`` returns a
    ``RunPlan`` carrying an ID STRING (a plan crosses a ``core.<verb>`` return
    boundary, where live objects are banned), and this turns it back into
    something ``core.exec_stream`` can exec into. Keeping it here means the
    frontend never touches a container, and the plan execs the container it
    planned rather than whatever a re-resolution by project name would find.
    """
    try:
        return docker.from_env().containers.get(container_id)
    except DockerException as e:
        raise CwcliError(
            ErrorKind.DOCKER,
            "container.unresolvable",
            f"Could not resolve container '{container_id}'.",
            detail={"output": str(e)},
        ) from e


def _read_frappe_id(container, flag: str) -> int | None:
    """Read the container ``frappe`` user's numeric uid/gid (``-u``/``-g``).

    A bare ``id`` (no login shell) so pyenv's ``.profile`` rehash chatter never
    contaminates the number. Returns ``None`` if it cannot be read as an int.
    """
    try:
        code, out = container.exec_run(["id", flag, "frappe"])
    except DockerException:
        return None
    if code != 0:
        return None
    text = out.decode("utf-8", "replace") if isinstance(out, (bytes, bytearray)) else str(out)
    try:
        return int(text.strip())
    except ValueError:
        return None


def _bench_paths_needing_reown(
    container, bench_paths: Sequence[str], host_uid: int, host_gid: int
) -> list[str]:
    """Bench paths that exist and are NOT already owned by ``host_uid:host_gid``.

    Stats each path's current owner via a root exec. A path that is absent (a
    fresh init whose bench dir does not exist yet) or whose owner cannot be read
    is skipped, so it stays a no-op. A path whose owner differs from the target -
    a migrated instance's workspace still owned by the pre-shared uid, OR a path
    the align just remapped away from - is returned for re-owning.
    """
    needing: list[str] = []
    for path in bench_paths:
        try:
            code, out = container.exec_run(["stat", "-c", "%u:%g", path], user="root")
        except DockerException:
            continue
        if code != 0:
            continue
        owner = out.decode("utf-8", "replace") if isinstance(out, (bytes, bytearray)) else str(out)
        if owner.strip() != f"{host_uid}:{host_gid}":
            needing.append(path)
    return needing


def _decode(out) -> str:
    return out.decode("utf-8", "replace") if isinstance(out, (bytes, bytearray)) else str(out)


def _external_app_source_dirs(container, bench_path: str) -> list[str]:
    """Real source dirs of each app under ``<bench>/apps`` that live OUTSIDE the bench.

    A cwcli-native bench holds its apps as real directories under ``<bench>/apps``,
    so the bench re-own already covers them. A devcontainer/externally-provisioned
    bench instead symlinks each app to a shared source tree - e.g.
    ``apps/erpnext -> /workspace/.hdsrc/erpnext`` - which a ``chown -R <bench>`` does
    NOT follow (it changes the link, not its target), leaving the real app repo owned
    by the pre-shared uid. That is exactly the repo ``bench update``/``git pull``
    operates on, so an ownership mismatch there is git's "dubious ownership" refusal.

    Resolves each ``apps/*`` entry through its symlink (``readlink -f``) and returns
    only the targets that resolve OUTSIDE ``bench_path`` (in-bench targets are already
    covered by the bench re-own). One exec; ``[]`` when ``apps/`` is absent or the
    probe fails - the caller stays a no-op on any layout it cannot read.
    """
    q = shlex.quote(bench_path)
    prog = (
        f"for d in {q}/apps/*/; do "
        f'[ -e "$d" ] || continue; '
        f't=$(readlink -f "$d" 2>/dev/null) || continue; '
        f'[ -n "$t" ] || continue; '
        f'case "$t/" in {q}/*) continue;; esac; '
        f'printf "%s\\n" "$t"; '
        f"done"
    )
    try:
        code, out = container.exec_run(["bash", "-c", prog])
    except DockerException:
        return []
    if code != 0:
        return []
    return [line.strip() for line in _decode(out).splitlines() if line.strip()]


def reown_app_repo_to_frappe(container, app_path: str) -> tuple[bool, str | None]:
    """Re-own an app's real source dir to the container ``frappe`` user when a
    different uid owns it - the root cause of git's "dubious ownership" refusal.

    ``git`` run as ``frappe`` refuses a repository owned by another uid, and neither
    the pull nor ``git status`` can proceed, so ``apps update --force``'s existing
    conflict recovery (which needs a readable ``git status``) never even fires. This
    fixes the mismatch itself rather than silencing the check with a stray
    ``safe.directory``: it resolves ``app_path`` through any symlink (so the real
    repo under e.g. ``/workspace/.hdsrc`` is chowned, not the link) and ``chown -R``s
    it to the ``frappe`` user's CURRENT uid/gid - which after alignment is whoever
    git will actually run as.

    Best-effort, so ``--force`` degrades to the pull's own error rather than raising:
    returns ``(reowned, failure)``. ``reowned`` is True only when a chown actually
    ran; an already-correctly-owned repo is ``(False, None)`` (a plain no-op, not the
    cause of any failure).
    """
    uid = _read_frappe_id(container, "-u")
    gid = _read_frappe_id(container, "-g")
    if uid is None or gid is None:
        return (False, "could not read the container 'frappe' user's uid/gid")
    q = shlex.quote(app_path)
    try:
        code, out = container.exec_run(
            ["bash", "-c", f'p=$(readlink -f {q}) && printf "%s\\n" "$p" && stat -c "%u:%g" "$p"']
        )
    except DockerException as e:
        return (False, f"could not read the app repo owner: {e}")
    if code != 0:
        return (False, None)  # unresolvable; let the pull surface its own error
    lines = _decode(out).splitlines()
    if len(lines) < 2:
        return (False, None)
    real_path, owner = lines[0].strip(), lines[1].strip()
    if not real_path or owner == f"{uid}:{gid}":
        return (False, None)  # already owned by the frappe user: not the cause
    try:
        code, out = container.exec_run(
            ["bash", "-c", f"chown -R {uid}:{gid} {shlex.quote(real_path)}"], user="root"
        )
    except DockerException as e:
        return (False, f"could not re-own the app repo: {e}")
    if code != 0:
        return (False, f"could not re-own the app repo: {_decode(out).strip()}")
    return (True, None)


# The only paths first-provision writes under /home/frappe: the home root itself
# (mkdir/write needs its owner to match), plus pip/npm's caches and pyenv/nvm's
# write targets. Deliberately excludes the ~34.6k-file baked pyenv/nvm toolchain
# these parents contain - re-owning it forces a full overlayfs copy-up of ~1.28 GB
# purely to change owner on files the remapped user only ever reads, which is what
# made the old `chown -R /home/frappe` take 79s (measured) / minutes on a slow
# disk instead of ~3s. See `_install_pyenv_python`/`_install_nvm_node` in
# `core/init.py` for the writes these paths must stay writable for.
_CHOWN_HOME_RECURSIVE_DIRS = (
    "/home/frappe/.cache",
    "/home/frappe/.local",
    "/home/frappe/.config",
    "/home/frappe/.npm",
    "/home/frappe/.pyenv/cache",
    "/home/frappe/.pyenv/shims",
    "/home/frappe/.nvm/.cache",
    "/home/frappe/.nvm/alias",
)
_CHOWN_HOME_SHALLOW_DIRS = (
    "/home/frappe/.pyenv",
    "/home/frappe/.pyenv/versions",
    "/home/frappe/.nvm",
    "/home/frappe/.nvm/versions/node",
)


# The frappe/bench image's own default uid/gid for the `frappe` user, the alignment
# target whenever the requested uid is unusable. A uid is unusable when another
# account in the container's /etc/passwd already has it: 0 (`root`), but equally a
# system account such as `systemd-network` (998) or `redis` (100), which is where a
# shared install's `useradd --system` service account or a CI runner's system user
# lands. `docker exec -u frappe` resolves the user by NUMBER, and that lookup
# returns the FIRST passwd row with the uid (the system account, listed before
# frappe), so the exec lands with that account's HOME (`/root`, `/`,
# `/var/lib/redis`), silently drops every PATH entry bench needs (pyenv shims,
# nvm's node), and `bench init` fails with exit 127 while the workspace fills with
# files the host names as that system account. Root already has unrestricted host
# filesystem permissions, so leaving `frappe` at the image default costs a root
# host nothing (karotkriss/caffeinated-whale-cli#229).
# ponytail: assumes the image's own frappe uid (1000) is free; a custom image that
# gave 1000 to another account would need this read from the image instead.
DEFAULT_FRAPPE_UID = 1000
DEFAULT_FRAPPE_GID = 1000

# The environment twin of `cwcli init --uid`: the uid/gid every command that aligns
# the container `frappe` user (init, start, restart, scale, new-bench) uses instead
# of the invoking user's own. An env var rather than a per-command flag, because
# alignment re-runs on every start: a value given to `init` alone would be undone
# by the next `cwcli start`.
UID_ENV = "CWCLI_UID"


def env_uid_override() -> int | None:
    """The validated ``CWCLI_UID`` override, or ``None`` when it is unset or empty.

    Raises a ``USAGE`` :class:`CwcliError` for a value that is not a positive
    integer: a mistyped override silently ignored would align ``frappe`` to the
    wrong uid and re-own a workspace to it.
    """
    raw = os.environ.get(UID_ENV, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value <= 0:
        raise CwcliError(
            ErrorKind.USAGE,
            "uid.invalid",
            f"{UID_ENV} must be a positive, non-root integer (got {raw!r}): 0 collides with "
            "the container's own root user and breaks bench's PATH.",
        )
    return value


def container_account_uids(container) -> dict[int, str]:
    """uid -> name of every account in the container's ``/etc/passwd`` except ``frappe``.

    The accounts whose uid ``frappe`` must never be aligned to (see
    :data:`DEFAULT_FRAPPE_UID`). The first row wins for a duplicated uid, matching
    the lookup that resolves the exec's HOME. ``{}`` when the file cannot be read,
    which leaves the uid-0 guard in :func:`resolve_frappe_alignment_ids` in force.
    """
    try:
        code, out = container.exec_run(["cat", "/etc/passwd"])
    except DockerException:
        return {}
    if code != 0:
        return {}
    accounts: dict[int, str] = {}
    for line in _decode(out).splitlines():
        fields = line.split(":")
        if len(fields) < 3 or fields[0] == "frappe":
            continue
        try:
            accounts.setdefault(int(fields[2]), fields[0])
        except ValueError:
            continue
    return accounts


def frappe_alignment_target(
    container, uid_override: int | None = None
) -> tuple[int | None, int | None, str | None]:
    """:func:`resolve_frappe_alignment_ids` checked against this container's own accounts."""
    return resolve_frappe_alignment_ids(uid_override, taken=container_account_uids(container))


def resolve_frappe_alignment_ids(
    uid_override: int | None = None,
    *,
    taken: dict[int, str] | None = None,
) -> tuple[int | None, int | None, str | None]:
    """The uid/gid target :func:`align_container_user_to_host` remaps ``frappe`` to.

    The single source of truth for the alignment target, shared by the align itself
    and init's workspace-dir chown so the two can never disagree. Returns
    ``(uid, gid, note)``; ``note`` is a non-silent explanation the caller should
    surface when a fallback was applied. ``(None, None, None)`` on a non-POSIX host
    (Windows), where there is nothing to align to. ``taken`` maps the uids other
    container accounts already hold to their names (:func:`container_account_uids`).

    Precedence: an explicit ``uid_override`` (init's ``--uid``, already validated
    positive by the frontend) wins over everything; then ``CWCLI_UID``
    (:func:`env_uid_override`), which every aligning command honours; then the
    shared-mode service uid/gid (stable across users; see below); else the host's
    own uid/gid. A resolved uid that is 0 or already belongs to another container
    account is unusable (see :data:`DEFAULT_FRAPPE_UID`) and falls back to the image
    default with a note; the gid is kept unless it is 0, since a shared duplicate
    gid is harmless and keeps a shared install's group access.
    """
    if not hasattr(os, "getuid"):
        return (None, None, None)
    env_uid = env_uid_override() if uid_override is None else None
    if uid_override is not None:
        uid, gid = uid_override, uid_override
    elif env_uid is not None:
        uid, gid = env_uid, env_uid
    elif shared_home.shared_mode():
        # Shared mode: align to the STABLE cwcli service uid/gid, not whoever ran
        # cwcli. With a shared container and two users, aligning to os.getuid()
        # ping-pongs the frappe user's uid on every start - each swing risking a
        # ~79s overlayfs copy-up and thrashing the per-process log / supervisord
        # socket ownership (report 2.4/6.2). Fall back to the host uid/gid when
        # the service account is not fully provisioned, so a half-set-up box still
        # works exactly as per-user.
        svc_uid = shared_home.service_uid()
        svc_gid = shared_home.gid()
        uid = svc_uid if svc_uid is not None else os.getuid()
        gid = svc_gid if svc_gid is not None else os.getgid()
    else:
        uid, gid = os.getuid(), os.getgid()
    owner = "root" if uid == 0 else (taken or {}).get(uid)
    if owner is None:
        return (uid, gid, None)
    if uid == 0:
        why = "the host is running as uid 0 (root)"
    else:
        why = f"uid {uid} already belongs to the container's own '{owner}' account"
    note = (
        f"Using uid {DEFAULT_FRAPPE_UID} for the container 'frappe' user because {why}; "
        f"giving 'frappe' that uid would hand bench the {owner} account's home and break "
        f"its PATH (`bench init` exits 127). Set {UID_ENV}=<n> (or pass --uid <n> to init) "
        "to choose a specific uid."
    )
    return (DEFAULT_FRAPPE_UID, DEFAULT_FRAPPE_GID if gid == 0 else gid, note)


def _syncs_workspace_owner(uid: int, gid: int) -> bool:
    """Whether align must reconcile the bench workspace's owner with ``uid:gid``.

    On a normal box ``frappe`` is aligned to the invoking user, who built and owns
    the workspace, so there is nothing to reconcile and no probe runs (byte-identical
    to before this existed). Shared mode aligns to the service account, and a root
    host, a colliding uid, or ``CWCLI_UID``/``--uid`` align to a uid other than the
    invoking user's: there the workspace can be owned by an earlier, different uid
    (an instance an older cwcli built as uid 0 or as a system account), and the
    re-owned ``frappe`` user could not write its own bench.
    """
    return shared_home.shared_mode() or (uid, gid) != (os.getuid(), os.getgid())


def align_workspace_dir_owner(container, path: str, uid: int, gid: int) -> bool:
    """Give the aligned ``frappe`` user the workspace dir ``bench init`` creates benches in.

    When ``frappe`` is aligned to a uid other than the invoking user's (a root host,
    a colliding uid, or ``CWCLI_UID``/``--uid``), the bind-mounted workspace dir this
    process created is not writable by ``frappe`` and ``bench init`` cannot create
    the bench inside it. Re-owns ``path`` (not recursively) to ``uid:gid`` from
    inside the container as root, which works on any host uid, and only when its
    owner differs, so a retry after a failed init repairs a dir an older cwcli left
    root-owned. Skipped in shared mode, whose group-writable state tree owns
    bind-mount permissions, and a no-op when ``path`` is absent or when the target
    is the invoking user. Returns True when it re-owned the dir.
    """
    if not hasattr(os, "getuid") or shared_home.shared_mode():
        return False
    if (uid, gid) == (os.getuid(), os.getgid()):
        return False
    q = shlex.quote(path)
    script = (
        f'[ -d {q} ] && [ "$(stat -c %u:%g {q})" != "{uid}:{gid}" ] '
        f"&& chown {uid}:{gid} {q} && echo REOWNED"
    )
    try:
        _, out = container.exec_run(["bash", "-c", script], user="root")
    except DockerException:
        return False
    return "REOWNED" in _decode(out)


def align_container_user_to_host(
    container,
    *,
    chown_home: bool = False,
    bench_paths: Sequence[str] = (),
    uid_override: int | None = None,
) -> tuple[bool, str | None]:
    """Align the container's ``frappe`` user's uid/gid with the host user's.

    Bench commands run as the image's default ``frappe`` user (uid 1000), so every
    file they write to the bind-mounted workspace lands owned by uid 1000 on the
    host. When the host user's uid differs - a CI runner is uid 1001, a dev box is
    commonly 1000 - the host cannot recurse into those directories to delete them
    and ``cwcli rm`` fails with ``[Errno 13] Permission denied``. Remapping
    ``frappe`` to the host uid/gid (the frappe devcontainer's ``updateRemoteUserUID``
    trick) makes the workspace host-owned and host-removable on ANY host uid. This
    fix depends only on ``/etc/passwd``'s uid mapping being correct BEFORE any bench
    command writes the workspace (this function runs at that point) - it does not
    depend on anything walking the workspace itself, so it is unaffected by how the
    uid field gets there (see the ``sed``-vs-``usermod`` note below).

    Identity remapping is a no-op when the ids already match (the common dev-box
    case). A uid change also re-owns the handful of
    paths under ``/home/frappe`` that a first provision actually WRITES (the home
    root, pip/npm's caches, and pyenv/nvm's write targets - see
    ``_CHOWN_HOME_RECURSIVE_DIRS``/``_CHOWN_HOME_SHALLOW_DIRS`` above). This is
    required after a container recreation too: a login shell runs ``pyenv rehash``,
    which must be able to rewrite the existing shims under the new uid.
    ``chown_home`` forces the same repair during first provisioning even when the
    ids already match. Deliberately NOT a full ``chown -R /home/frappe``: that
    re-owns the baked toolchain too, forcing an overlayfs copy-up of the whole
    1.28 GB/36.7k files it contains, measured at 79s (minutes on a slow disk)
    versus ~3s narrowed.

    In SHARED mode (``shared_home.shared_mode()``), and whenever the target is not
    the invoking user's own uid/gid (a root host, a colliding uid, or
    ``CWCLI_UID``/``--uid``; see ``_syncs_workspace_owner``), each path in
    ``bench_paths`` (the resolved bench dirs) AND the real source dir of any app
    symlinked outside a bench
    (``apps/<app> -> /workspace/.hdsrc/<app>`` on a devcontainer bench; see
    ``_external_app_source_dirs``) is re-owned to the target identity when its CURRENT
    owner differs, not merely when the ids change this call. Covering the app repos is
    what lets a subsequent ``apps update`` git pull run without a "dubious ownership"
    refusal - a bench re-own alone does not follow those symlinks. The remap target is the
    stable ``cwcli`` service account, NOT ``os.getuid()``, so a migrated instance's
    bind-mounted workspace - built under the pre-shared uid - is not owned by the
    remapped ``frappe`` user and ``bench start`` can no longer write
    ``<bench>/logs/bench.log``, crash-looping the instance. An instance already
    bricked by v3.1.0 has ``frappe`` remapped to the service uid ALREADY (its ids
    match now), so gating the re-own on an id change alone would never recover it;
    gating on the real owner mismatch (stat'd per path) recovers it in place on the
    next ``cwcli start``/``restart``; the same re-own repairs a bench an older cwcli
    built on a root host as uid 0 or as a system account. A normal box - where align
    targets ``os.getuid()``, which already owns the workspace - runs no owner probe
    and emits no workspace chown. ``/workspace`` is a bind mount, not the image overlay,
    so ``chown -R`` there is cheap (no copy-up).

    ``uid_override`` (init's ``--uid``), ``CWCLI_UID``, and the unusable-uid
    fallback are all resolved by :func:`frappe_alignment_target`, the shared target
    computation checked against this container's own accounts - so ``frappe`` is
    never given uid 0 or a uid a system account in the image already holds (which
    breaks bench; see :data:`DEFAULT_FRAPPE_UID`), and a properly provisioned shared
    install still wins with its service uid when that uid is free.

    Best-effort: returns ``(remapped, failure)``. ``failure`` is a short detail
    string when a step failed (the caller surfaces it as a warning and the bench
    still builds owned by the original uid, exactly as before this remap existed);
    ``remapped`` is True only when the ids were actually changed.

    A clean no-op on a platform without ``os.getuid`` (Windows): there is no host
    uid to align to, and Docker Desktop handles bind-mount ownership itself, so
    the host never hits the Linux permission error this remap exists to prevent.
    """
    if not hasattr(os, "getuid"):  # non-POSIX (Windows): nothing to align to.
        return (False, None)
    host_uid, host_gid, _note = frappe_alignment_target(container, uid_override)
    if host_uid is None or host_gid is None:
        return (False, None)
    cur_uid = _read_frappe_id(container, "-u")
    cur_gid = _read_frappe_id(container, "-g")
    if cur_uid is None or cur_gid is None:
        return (False, "could not read the container 'frappe' user's uid/gid")
    ids_changed = cur_uid != host_uid or cur_gid != host_gid

    # Shared mode, or a target other than the invoking user (`_syncs_workspace_owner`):
    # which bench dirs are actually owned by someone other than the target identity.
    # An instance already bricked by v3.1.0 has `frappe` remapped to the service uid
    # ALREADY (so ids match, `ids_changed` is False), yet its bind-mounted workspace is
    # still owned by the pre-shared uid - gating the re-own on `ids_changed` alone
    # would leave it bricked. The same holds for a bench an older cwcli built as uid 0
    # or as a system account on a root host. Gating on the real owner mismatch
    # recovers it in place on the next `cwcli start`/`restart`, and a box whose
    # workspace already matches gets no chown -R.
    reown_paths: list[str] = []
    if bench_paths and _syncs_workspace_owner(host_uid, host_gid):
        # Each bench dir PLUS the real source dir of any app symlinked outside it
        # (a devcontainer bench points `apps/<app>` at a shared tree like
        # `/workspace/.hdsrc/<app>`, which a `chown -R <bench>` does not follow).
        # Those app repos are what `bench update`/`git pull` operate on, so leaving
        # them owned by the pre-shared uid is exactly git's "dubious ownership"
        # refusal on the next `apps update`. Owner-mismatch gated per path, so the
        # bench dir being already correct never hides an app repo that is not.
        candidates: list[str] = []
        seen: set[str] = set()
        for path in bench_paths:
            for candidate in (path, *_external_app_source_dirs(container, path)):
                if candidate not in seen:
                    seen.add(candidate)
                    candidates.append(candidate)
        reown_paths = _bench_paths_needing_reown(container, candidates, host_uid, host_gid)

    if not ids_changed and not chown_home and not reown_paths:
        return (False, None)

    # `-o` allows a non-unique id (the host uid may already exist in the image's
    # passwd/group db). `bash -c` (not `-lc`) avoids sourcing any profile.
    #
    # `groupmod -g` is genuinely cheap - verified it only rewrites /etc/group's
    # gid field plus every /etc/passwd row whose primary gid matched the old
    # value (here, frappe's own row), never walking the filesystem - so it stays
    # as-is. `usermod -u` is NOT: shadow-utils' usermod unconditionally chowns
    # every file under the target's $HOME from the old uid to the new one as a
    # documented side effect of changing a uid (measured ~90s / a full 1.28 GB
    # overlayfs copy-up on this image, reproduced twice - the actual cost this
    # function used to attribute entirely to the separate `chown -R`). The uid
    # field is edited directly in /etc/passwd instead, which produces the
    # byte-identical passwd/group state and `id frappe` output usermod would
    # have (verified), without walking $HOME at all - that walk is replaced by
    # the narrowed, explicit chown below.
    #
    # ORDER IS LOAD-BEARING: every chown runs BEFORE the identity edit, and the
    # identity edit runs last; `&&` means a failed re-own never reaches the edit.
    # On an instance whose PID 1 is a bench stack running as `frappe` (a
    # devcontainer-style compose, not cwcli's `sleep infinity`), both halves can
    # kill PID 1 - the chown takes its own bench away from it, the passwd edit
    # takes its passwd row - and a PID 1 exit makes Docker kill this exec with the
    # container, stranding a half-applied remap (bench and `frappe` on different
    # uids) whose next boot dies on `logs/bench.log` before any start can retry.
    # So when the identity changes, PID 1 is frozen (SIGSTOP from the host, which
    # no process can refuse) before the exec, and the script itself thaws it
    # (`kill -CONT 1` after the chain, success or failure): PID 1 cannot exit while
    # the chain runs, and an interrupted cwcli cannot thaw it early once Docker
    # has started the exec. An interrupt after SIGSTOP but before the exec starts
    # can leave PID 1 frozen; the next core.start runs `kill -CONT 1` inside the
    # container before checking liveness. A Docker error also gets that same
    # in-container fallback thaw. This does not make an individual chown atomic or
    # protect against the container or host dying mid-chain.
    steps = []
    if chown_home or cur_uid != host_uid:
        steps.append(f"chown {host_uid}:{host_gid} /home/frappe")
        steps.extend(
            f"[ ! -e {path} ] || chown -R {host_uid}:{host_gid} {path}"
            for path in _CHOWN_HOME_RECURSIVE_DIRS
        )
        steps.extend(
            f"[ ! -e {path} ] || chown {host_uid}:{host_gid} {path}"
            for path in _CHOWN_HOME_SHALLOW_DIRS
        )
    # Shared mode only: reconcile the bind-mounted bench workspace's ownership
    # with the service identity. `reown_paths` already holds exactly the bench
    # dirs whose owner differs from the target (a migrated instance's workspace
    # still owned by the pre-shared uid, OR one the remap just moved away from) -
    # a normal box has none. Existence-guarded so a dir that vanished between the
    # stat and here is a no-op.
    steps.extend(
        f"[ ! -e {q} ] || chown -R {host_uid}:{host_gid} {q}"
        for q in (shlex.quote(p) for p in reown_paths)
    )
    if cur_gid != host_gid:
        steps.append(f"groupmod -o -g {host_gid} frappe")
    if cur_uid != host_uid:
        steps.append(rf"sed -i 's/^frappe:\([^:]*\):[^:]*:/frappe:\1:{host_uid}:/' /etc/passwd")
    script = " && ".join(steps)
    if ids_changed:
        script = f"rc=0; ( {script} ) || rc=$?; kill -CONT 1; exit $rc"
    try:
        if ids_changed:
            container.kill(signal="SIGSTOP")
        try:
            code, out = container.exec_run(["bash", "-c", script], user="root")
        except DockerException:
            if ids_changed:
                container.exec_run(["kill", "-CONT", "1"], user="root")
            raise
    except DockerException as e:
        return (False, f"could not align the container 'frappe' user to the host: {e}")
    if code != 0:
        detail = out.decode("utf-8", "replace") if isinstance(out, (bytes, bytearray)) else str(out)
        return (False, f"could not align the container 'frappe' user to the host: {detail.strip()}")
    return (ids_changed, None)


def get_frappe_container(project_name: str):
    """Resolve a project's frappe container to an exec-usable handle.

    Raises :class:`CwcliError` (``DOCKER`` when the daemon is unreachable,
    ``NOT_FOUND`` when the project or its frappe service is absent) instead of
    printing and calling ``typer.Exit``. The returned container is for the
    caller's own exec calls; it must not be surfaced in any returned DTO.
    """
    containers = get_project_containers(project_name)

    if containers is None:
        # get_project_containers returns None ONLY on a Docker connection error.
        raise CwcliError(
            ErrorKind.DOCKER,
            "docker.unreachable",
            "Could not connect to Docker daemon.",
            hint=DOCKER_UNREACHABLE_HINT,
        )

    if not containers:
        raise CwcliError(
            ErrorKind.NOT_FOUND,
            "project.not_found",
            f"Project '{project_name}' not found.",
        )

    frappe_container = next(
        (c for c in containers if c.labels.get("com.docker.compose.service") == "frappe"),
        None,
    )
    if frappe_container is None:
        raise CwcliError(
            ErrorKind.NOT_FOUND,
            "frappe.not_found",
            f"No 'frappe' service found for project '{project_name}'.",
        )

    return frappe_container
