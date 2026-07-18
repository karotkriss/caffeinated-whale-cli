## Context

This design owns both the audit and the proposal; every claim below was read at current HEAD (branch `fm/cwcli-volume-map-v8`) or fetched from the live upstream compose, not inherited from prior prose (batch 1's Non-Goals error is the named anti-pattern).

The captain flagged five things as "resolve at recon, do not assume": the `--bench-parent`/`--bench` mapping semantics, which volume is mapped and how it is wired, the init-time and `CWCLI_HOME` interaction, backward compatibility for existing instances, and consistency with the s4 start/status/logs rework.
Each is resolved from the code below.

### Audit: how the workspace is wired at HEAD

| Where | What it does | Relevance |
| --- | --- | --- |
| `core/init.py:502-531` (`init_instance`) | Downloads `devcontainer-example/docker-compose.yml`, rewrites the two port ranges and the `frappe/bench` image tag, writes it back. Never reads or writes `volumes:`. | The workspace mount is inherited verbatim from upstream; cwcli does not own it. |
| upstream compose, `frappe` service | `volumes: - ..:/workspace:cached` | The bench workspace is a **bind mount**, source `..` = the compose file's parent = `PROJECTS_DIR/{project}/`. |
| upstream compose, `volumes:` block | `mariadb-data:` only | The DB is the **only** named volume; there is no named workspace volume. |
| `core/init.py:674,715-718` (`init_bench`) | `bench_parent` param (default `/workspace`); `mkdir -p {bench_parent}`; `bench init` at `{bench_parent}/{bench_name}` | The bench directory is placed at a path the compose mount does not track unless it happens to be under `/workspace`. |
| `core/init.py:502` | `if not compose_path.exists():` guards the download+customize | An existing project's compose file is **frozen**; re-init never rewrites it. This is the backward-compat boundary. |
| `core/docker.py:get_project_volumes` | Enumerates volumes by `com.docker.compose.project={project}` label | Any compose-declared named volume is auto-discovered; `rm` needs no per-volume knowledge. |
| `core/rm.py:_remove_named_volumes` | Removes every volume `get_project_volumes` returns | A `{project}_workspace` named volume is removed with zero code change. |
| `core/supervision.py:156-179` | supervisord config, launcher, socket, pid, and per-process logs all live under `{bench_path}/logs/` | The workspace mount must cover `{bench}/logs`, and the socket needs a socket-capable filesystem. |
| `utils/config_utils.py:cwcli_home,PROJECTS_DIR` | `CWCLI_HOME` relocates `PROJECTS_DIR`; the relative `..` bind mount follows it; `mariadb-data` is namespaced by project name and needs no relocation | Any replacement must preserve isolation across `CWCLI_HOME` values. |
| `utils/vscode_utils.py:open_in_vscode` | `vscode-remote://attached-container+{hex}{bench_path}` - opens the in-container path | `cwcli open` does not use the host bind-mount path; host-file-visibility is not load-bearing for it. |

**Conclusion of the audit.**
The "workspace volume" the docs and CLAUDE.md describe does not exist as a named volume; it is a host bind mount hardcoded to `/workspace`, and `--bench-parent` can silently escape it.
The mapping the captain asked for is: give cwcli ownership of the workspace mount and derive its target from `--bench-parent` (the mount point), with `--bench` as a subdirectory inside it.

## Goals / Non-Goals

**Goals.**
Make the bench workspace a cwcli-owned, persistent volume whose mount point is `--bench-parent`, so no `--bench-parent` silently produces an ephemeral bench.
Keep every existing instance byte-unchanged.
Keep `rm`, s4 supervision, and `CWCLI_HOME` correct.
Resolve the named-volume-vs-bind-mount question with a recommendation and surface its one product trade-off for the captain.

**Non-Goals.**
Migrating existing bind-mount instances to named volumes (their compose is frozen and they work).
A `--bind-workspace` opt-in flag (YAGNI until requested).
Any change to `mariadb-data`, to `rm`'s logic, to the s4 contract, or to any non-init command.
Any `axi` verb change.
Changing `--bench-parent`'s default (`/workspace`) or `--bench`'s default (`frappe-bench`).

## Decisions

### 1. The workspace becomes a per-project named volume mounted at `--bench-parent`

`init_instance` gains a `bench_parent` parameter and, in the same block that rewrites ports and the image tag, rewrites the frappe service's workspace mount to a named volume mounted at the resolved `--bench-parent`, and adds that volume to the top-level `volumes:` block.
Under `docker compose -p {project}` this becomes `{project}_workspace`, matching the existing `{project}_mariadb-data` shape.

Rationale over the alternatives (Decision 4): a Docker-managed named volume gives uniform lifecycle with the database volume, is removed by the existing `_remove_named_volumes` path with no new code, is always socket-capable for s4 (Decision 5), and is already isolated across `CWCLI_HOME` values by its project-name namespace (Decision 3).

### 2. `--bench-parent` is the mount point; `--bench` is a subdirectory; the mount is a stage-1 property

The volume resolves from `--bench-parent` alone.
One volume per project, mounted once at `--bench-parent`.
`--bench` (a NAME at init, not the `<index|label>` selector) is a subdirectory `{bench_parent}/{bench}` inside that mount; multi-bench instances place every bench as a sibling subdirectory sharing the one volume, exactly as `/workspace/frappe-bench` and `/workspace/frappe-bench-2` share `/workspace` today.

Because the mount lives in the compose file that stage 1 creates, `--bench-parent` must be known at stage 1.
It is therefore threaded into `init_instance`; `init_bench` continues to take it for the bench directory.
The frontend passes the same value to both (no divergence).

Consequence: the mount point is fixed at instance creation.
On a re-init of an existing project (whose compose is frozen), a `--bench-parent` that does not match the instance's mounted parent is a `USAGE` error naming the mounted parent, instead of `mkdir`-ing an ephemeral directory outside the volume.
Resolving the parent for the comparison reads it from the instance's compose file (the frappe service's workspace mount target).

### 3. CWCLI_HOME: no new coupling

Today the bind mount follows `PROJECTS_DIR` because `..` is relative.
`mariadb-data` already lives in Docker's storage (not under `~/.cwcli`) and is isolated across `CWCLI_HOME` values purely by its `{project}_` namespace, because distinct instances use distinct project names.
`{project}_workspace` inherits exactly that isolation, so `CWCLI_HOME` remains correct with no additional wiring.
This is a deliberate consequence, not an accident: the workspace joins the volume that already got `CWCLI_HOME` right, rather than the bind mount that got it right by a different mechanism.

### 4. Named volume vs re-targeted bind mount - the one product decision

Both fix the silent-ephemeral-bench bug.
The difference is host-side file access.

- **Recommended - named volume at `--bench-parent`** (Decision 1).
  Loses direct host access to bench files (they move from `~/.cwcli/projects/{project}/` into Docker storage).
  `cwcli open` (all four editor branches, in-container), `cwcli run`, `inspect`, `logs`, and the `rm` backup gate (copies out via exec) are all unaffected, because none of them read the host bench path.
  Gains uniform `rm`, socket safety, and the mariadb-data-consistent `CWCLI_HOME` story.

- **Alternative A - re-target the bind mount** to `- ..:{bench_parent}:cached`.
  Smallest diff (one string), preserves host file access, and `rm`'s project-directory removal keeps cleaning the bench.
  But it perpetuates the unix-socket-on-networked-host-filesystem fragility for s4 (Decision 5), keeps bench data coupled to the project directory rather than a Docker-managed volume, and does not make `rm`'s "named volumes (databases, sites, files)" description true.
  It also mounts the whole project directory (including `conf/`) at an arbitrary container path, which is odd for a non-`/workspace` parent.

The captain owns the host-file-access trade-off; it is called out here and in the proposal so approval is an explicit choice rather than an assumption.
If host access must be preserved, Alternative A is the fallback and the rest of this design (semantics, backward-compat, s4, `CWCLI_HOME`) applies unchanged except that the volume is a bind mount.

### 5. s4 start/status/logs consistency

The mount at `--bench-parent` covers `{bench_parent}/{bench}` in full, so supervisord's config, launcher, socket, pid, and per-process `{bench}/logs/*.supervisor.log` persist across container recreation - which is strictly better than today for a custom `--bench-parent`, where they were ephemeral.
The mapping SHALL never mount at a sub-path that excludes `{bench}/logs`.

The named volume additionally removes a latent s4 failure: the supervisor's unix socket `{bench}/logs/.cwcli-supervisor.sock` on a bind mount backed by a networked host filesystem (WSL2 `/mnt/c`, macOS gRPC-FUSE) can fail to bind; on a Docker-managed named volume (Linux-backed) it is always socket-capable.
No `core/supervision.py` change is needed - the paths are identical; only the filesystem beneath them improves.

### 6. Backward compatibility and migration

The `if not compose_path.exists()` guard in `init_instance` is the migration boundary and it already exists: existing instances keep their frozen `..:/workspace:cached` compose and behave identically.
New instances get the named volume.
`rm` handles both (Decision 1); every read/write verb is backend-agnostic (operates on in-container `bench_path`).
There is no forced migration of existing instances; a user who wants the new shape re-creates the instance (documented in the implementation phase).
This mixed fleet is safe because nothing outside init inspects the mount type - the only consumer that ever distinguished them was init itself deciding whether to persist, which this change makes uniform for new instances.

## Risks / trade-offs

- **Host file access loss for new instances** (Decision 4) - surfaced for the captain; Alternative A is the fallback.
- **A mixed fleet** (old bind-mount + new named-volume instances) - safe by Decision 6; `rm` and all verbs handle both. The only visible difference is where a new instance's bench files live on the host (nowhere directly, vs the project directory).
- **Re-init mount-mismatch as a `USAGE` error** (Decision 2) - a small behavior addition, justified by it replacing today's silent ephemeral-bench outcome; the default path (unchanged `--bench-parent`) is unaffected.

## Open questions

- Only one, and it is Decision 4's host-file-access trade-off, deliberately routed through captain approval of this proposal rather than assumed.
