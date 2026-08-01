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
import os
import subprocess

import docker
from docker.errors import DockerException

from .errors import CwcliError, ErrorKind

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


def align_container_user_to_host(container, *, chown_home: bool = False) -> tuple[bool, str | None]:
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

    Best-effort: returns ``(remapped, failure)``. ``failure`` is a short detail
    string when a step failed (the caller surfaces it as a warning and the bench
    still builds owned by the original uid, exactly as before this remap existed);
    ``remapped`` is True only when the ids were actually changed.

    A clean no-op on a platform without ``os.getuid`` (Windows): there is no host
    uid to align to, and Docker Desktop handles bind-mount ownership itself, so
    the host never hits the Linux permission error this remap exists to prevent.
    """
    if not hasattr(os, "getuid"):
        return (False, None)
    host_uid, host_gid = os.getuid(), os.getgid()
    cur_uid = _read_frappe_id(container, "-u")
    cur_gid = _read_frappe_id(container, "-g")
    if cur_uid is None or cur_gid is None:
        return (False, "could not read the container 'frappe' user's uid/gid")
    ids_changed = cur_uid != host_uid or cur_gid != host_gid
    if not ids_changed and not chown_home:
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
    steps = []
    if cur_gid != host_gid:
        steps.append(f"groupmod -o -g {host_gid} frappe")
    if cur_uid != host_uid:
        steps.append(rf"sed -i 's/^frappe:\([^:]*\):[^:]*:/frappe:\1:{host_uid}:/' /etc/passwd")
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
    try:
        code, out = container.exec_run(["bash", "-c", " && ".join(steps)], user="root")
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
