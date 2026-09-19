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
import shlex
import subprocess
from collections.abc import Sequence

import docker
from docker.errors import DockerException

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


def align_container_user_to_host(
    container, *, chown_home: bool = False, bench_paths: Sequence[str] = ()
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

    In SHARED mode (``shared_home.shared_mode()``) each path in ``bench_paths`` (the
    resolved bench dirs) AND the real source dir of any app symlinked outside a bench
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
    next ``cwcli start``/``restart``. A normal box - where align targets
    ``os.getuid()`` and the id already owns the workspace - reads no mismatch and
    emits no workspace chown. ``/workspace`` is a bind mount, not the image overlay,
    so ``chown -R`` there is cheap (no copy-up).

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
    in_shared_mode = shared_home.shared_mode()
    if in_shared_mode:
        # Shared mode: align to the STABLE cwcli service uid/gid, not whoever ran
        # cwcli. With a shared container and two users, aligning to os.getuid()
        # ping-pongs the frappe user's uid on every start - each swing risking a
        # ~79s overlayfs copy-up and thrashing the per-process log / supervisord
        # socket ownership (report 2.4/6.2). Fall back to the host uid/gid when
        # the service account is not fully provisioned, so a half-set-up box still
        # works exactly as per-user.
        svc_uid = shared_home.service_uid()
        svc_gid = shared_home.gid()
        host_uid = svc_uid if svc_uid is not None else os.getuid()
        host_gid = svc_gid if svc_gid is not None else os.getgid()
    else:
        host_uid, host_gid = os.getuid(), os.getgid()
    cur_uid = _read_frappe_id(container, "-u")
    cur_gid = _read_frappe_id(container, "-g")
    if cur_uid is None or cur_gid is None:
        return (False, "could not read the container 'frappe' user's uid/gid")
    ids_changed = cur_uid != host_uid or cur_gid != host_gid

    # Shared mode only: which bench dirs are actually owned by someone other than
    # the target identity. An instance already bricked by v3.1.0 has `frappe`
    # remapped to the service uid ALREADY (so ids match, `ids_changed` is False),
    # yet its bind-mounted workspace is still owned by the pre-shared uid - gating
    # the re-own on `ids_changed` alone would leave it bricked. Gating on the real
    # owner mismatch recovers it in place on the next `cwcli start`/`restart`, and
    # a normal shared box whose workspace already matches gets no chown -R.
    reown_paths: list[str] = []
    if in_shared_mode and bench_paths:
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
