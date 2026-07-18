## Why

`cwcli init` provisions a bench at `{--bench-parent}/{--bench}` inside the frappe container, then relies on the downloaded compose file to keep that directory alive across container recreation.
That reliance is broken for any `--bench-parent` other than the hardcoded default, and the reason is a mismatch nobody wired up: **the bench workspace is not a cwcli-managed volume at all.**

What init actually does today (`core/init.py:init_instance`, verified at HEAD): it downloads frappe_docker's `devcontainer-example/docker-compose.yml` and rewrites ONLY the port ranges and the bench image tag.
It never touches `volumes:`.
That upstream compose declares exactly ONE named volume - `mariadb-data:/var/lib/mysql` (the database) - and mounts the bench workspace as a **bind mount**: the frappe service's `- ..:/workspace:cached`.
Because the compose file lives at `PROJECTS_DIR/{project}/conf/docker-compose.yml`, `..` resolves to `~/.cwcli/projects/{project}/` on the host, and that host directory is where every bench, its sites and files, and the s4 supervisord state (`{bench}/logs/*.supervisor.log`, config, launcher, socket, pid) actually persist.
This is confirmed by `docs/e2e/rm-multi-bench-h3.md` ("the single compose volume `cwcle2e-mb_mariadb-data`") and by the fetched upstream compose.

Two problems follow, and the second is silent data loss:

**1. The mount target is hardcoded to `/workspace`, but `--bench-parent` is a free-form absolute path.**
`init_bench` takes `--bench-parent` (default `/workspace`), `mkdir -p`s it, and runs `bench init` there (`core/init.py:715-718`).
Nothing wires the compose mount to that path.
So a `--bench-parent` that is not under `/workspace` (e.g. `--bench-parent /opt/benches`) puts the whole bench in the container's ephemeral writable layer: it is lost on `docker compose down` + `up` or any container recreation, it is invisible to `rm`'s project-directory removal (it is not on the bind mount), and it is invisible to `rm`'s `_remove_named_volumes` (it is not a named volume).
The s4 supervisor's socket, pid, config, and per-process logs land in that same ephemeral layer.
A user who moves the bench off `/workspace` gets a bench that looks provisioned and then evaporates on the next container restart, with no warning.

**2. The persistence mechanism is a host bind mount, which is the fragile choice for cwcli's own state.**
Even at the default `/workspace`, the workspace being a host bind mount (not a Docker-managed named volume like the database) means the supervisor's unix socket at `{bench}/logs/.cwcli-supervisor.sock` sits on whatever host filesystem backs `~/.cwcli` (or `$CWCLI_HOME`).
On WSL2 `/mnt/c` and macOS Docker Desktop (gRPC-FUSE), unix domain sockets on a bind-mounted host directory can fail to bind, which breaks s4 supervision at its foundation.
The database already avoids this by living on a Docker-managed named volume; the workspace does not, purely because the upstream example did it that way and cwcli never revisited it.

The fix is to make cwcli own the workspace mount and derive it from `--bench-parent`/`--bench`, exactly as the captain framed it: use `--bench-parent` and `--bench` to determine the volume mapping and map the correct volume.

## What Changes

- **The bench workspace becomes a per-project NAMED volume mounted at `--bench-parent`.**
  `init_instance` (stage 1, which owns the compose file) rewrites the frappe service's workspace mount from the upstream `- ..:/workspace:cached` bind mount to a Docker-managed named volume declared in the top-level `volumes:` block and mounted at the resolved `--bench-parent`.
  With `docker compose -p {project}` this materializes as `{project}_workspace`, the same project-name-namespaced shape as the existing `{project}_mariadb-data`.
  `mariadb-data` is unchanged.

- **`--bench-parent`/`--bench` semantics are resolved and made load-bearing (recon item 1):**
  `--bench-parent` is the **volume mount point** - a per-instance property fixed when the instance's compose file is created (stage 1).
  `--bench` names a **subdirectory inside that mount** (`{bench_parent}/{bench}`); it is NOT the `--bench <index|label>` selector other verbs use, and it does NOT get its own volume.
  The workspace volume therefore resolves from `--bench-parent` alone: one volume per project, mounted once, with every bench (multi-bench included: `{parent}/frappe-bench`, `{parent}/frappe-bench-2`) as a subdirectory sharing it.
  This threads `bench_parent` into `init_instance`, which previously only knew it in `init_bench`.

- **The mount point is an instance property, so a re-init must agree with it.**
  Because an existing project's compose file is frozen (see below), `cwcli init` on an existing project with a `--bench-parent` that does not match the instance's mounted parent is a `USAGE` error naming the mounted parent, rather than silently creating an ephemeral bench outside the volume.
  A matching (or default-and-unchanged) `--bench-parent` proceeds as today.

- **`rm` needs ZERO code change.**
  `core/docker.py:get_project_volumes` already enumerates volumes by the `com.docker.compose.project={project}` label, so `{project}_workspace` is picked up and removed by `_remove_named_volumes` exactly like `mariadb-data`, and the C1 verified-backup gate (which copies artifacts OUT of the container via exec, not from the host bind mount) is unaffected.
  This also makes `rm`'s "removes the named Docker volumes (databases, sites, and files)" description finally true for new instances, where today "sites and files" lived on the bind-mounted project directory rather than a named volume.

- **Backward compatibility: existing instances are byte-unchanged (recon item 4).**
  `init_instance` already skips the compose download and customization when `conf/docker-compose.yml` exists (`if not compose_path.exists()`), so every already-created instance keeps its `..:/workspace:cached` bind mount and behaves exactly as before.
  All read/write verbs (`inspect`, `run`, `open`, `start`, `status`, `logs`, `restart`, `backup`, `unlock`, `apps`, `update`, `restore`) operate on in-container `bench_path`s, which are identical whether the path is backed by a bind mount or a named volume, so they are backend-agnostic and need no change.
  `rm` cleans both shapes (project-directory removal for old bind-mount benches, `_remove_named_volumes` for new named-volume benches, and `mariadb-data` for both).
  No forced migration; the change applies only to freshly created instances.

- **CWCLI_HOME stays correct (recon item 3).**
  Today the bind mount follows `PROJECTS_DIR` because `..` is relative, and `mariadb-data` is already isolated across `CWCLI_HOME` values by its project-name namespace (it lives in Docker storage, not under `~/.cwcli`).
  A named `{project}_workspace` volume follows the same namespacing, so two `CWCLI_HOME` instances with distinct project names already get distinct workspace volumes - the mariadb-data precedent - with no new coupling to the on-disk footprint.

- **s4 start/status/logs stay consistent (recon item 5).**
  The mount at `--bench-parent` covers the entire `{bench_parent}/{bench}` subtree, so supervisord's config, launcher, socket, pid, and per-process `{bench}/logs/*.supervisor.log` all persist; the mapping SHALL never mount at a sub-path that excludes `{bench}/logs`.
  Moving the workspace onto a Docker-managed volume additionally removes the unix-socket-on-networked-host-filesystem failure mode described above, hardening s4 on WSL/macOS.

- **One product decision is surfaced for the captain, not assumed:** a named volume removes host-side direct file access to bench files (an incidental property of the upstream example's bind mount, not a designed cwcli feature - `cwcli open` uses the in-container path via `vscode-remote://attached-container+...` for all four editor branches, so it is unaffected).
  The proposal recommends the named volume; the alternative (re-target the bind mount to `..:{bench_parent}:cached`, preserving host access with a smaller diff) is documented in `design.md` Decision 4 along with why it is not recommended.
  A future opt-in `--bind-workspace` flag is explicitly out of scope (YAGNI until requested).

## Impact

- **Changed (implementation phase, not this proposal):** `core/init.py` (thread `bench_parent` into `init_instance`; add the compose workspace-volume rewrite alongside the existing port/image rewrites; the re-init mount-mismatch `USAGE` guard), `commands/init.py` and `commands/axi.py` (pass `bench_parent` to stage 1; no new flags), the init characterization/core suites and a new compose-rewrite test, `docs/e2e/` evidence, `README.md` (the "workspace volume" wording becomes literally accurate for new instances), CLAUDE.md ledger, and the `cwcli-lifecycle` skill.
- **Unchanged:** `rm` (`core/rm.py`, `commands/rm.py`) and its backup gate; every non-init command; `core/supervision.py` and the s4 contract; the cache schema; the `axi` surface (no new verb); all existing instances and their compose files.
- **Zero new primitives:** the named volume reuses compose's own project-name namespacing and the existing `get_project_volumes`/`_remove_named_volumes` path; the mount-point resolution reuses `--bench-parent`/`--bench` as they already exist.
- **Out of scope:** migrating existing bind-mount instances to named volumes; a `--bind-workspace` opt-in; any change to `mariadb-data`; any `axi` verb change.
