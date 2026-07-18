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

### 1. The workspace becomes a per-project host bind mount from `{project}/data/` at `--bench-parent`

`init_instance` gains a `bench_parent` parameter and, in the same block that rewrites ports and the image tag (and only on a freshly downloaded compose - Decision 6), rewrites the frappe service's workspace mount from the upstream `- ..:/workspace:cached` to `- ../data:{bench_parent}:cached` and sets `working_dir` to `{bench_parent}`.
Because the compose file lives at `{project}/conf/docker-compose.yml`, the source `../data` resolves to `{project}/data/` on the host; `init_instance` creates that host directory so Docker never materializes it as a root-owned path.
`mariadb-data` is left untouched and no new named volume is introduced.

The captain settled the named-volume-vs-bind-mount question in favor of the bind mount (Decision 4), so bench files stay directly accessible on the host. Mounting only `data/` - rather than the whole project dir (`..`) the upstream example bound - keeps `conf/` (the compose file itself) out of the container and is the cleaner source.

### 2. `--bench-parent` is the mount point; `--bench` is a subdirectory; the mount is a stage-1 property

The mount resolves from `--bench-parent` alone (the container mount point); the host source is always `{project}/data/`.
One mount per project, mounted once at `--bench-parent`.
`--bench` (a NAME at init, not the `<index|label>` selector) is a subdirectory `{bench_parent}/{bench}` inside that mount; multi-bench instances place every bench as a sibling subdirectory sharing the one `{project}/data/` host dir, exactly as `/workspace/frappe-bench` and `/workspace/frappe-bench-2` share `/workspace` today.

Because the mount lives in the compose file that stage 1 creates, `--bench-parent` must be known at stage 1.
It is therefore threaded into `init_instance`; `init_bench` continues to take it for the bench directory.
The frontend passes the same value to both (no divergence).

Consequence: the mount point is fixed at instance creation.
On a re-init of an existing project (whose compose is frozen), a `--bench-parent` that does not match the instance's mounted parent is a `USAGE` error naming the mounted parent, instead of `mkdir`-ing an ephemeral directory outside the mount.
Resolving the parent for the comparison reads it from the instance's compose file (the frappe service's workspace mount target); the `_mounted_bench_parent` regex matches both the old whole-project `..:/workspace:cached` and the new `../data:{parent}:cached` shapes, so the guard fires correctly against either kind of existing instance.

### 3. CWCLI_HOME: no new coupling

The bind mount source `../data` is relative to the compose file, so it follows `PROJECTS_DIR` under `cwcli_home()` exactly as today's `..` mount does; `mariadb-data` keeps its `{project}_` namespace in Docker storage.
Two `CWCLI_HOME` instances therefore get distinct `{cwcli_home}/projects/{project}/data/` workspaces with no additional wiring - the mount that already got `CWCLI_HOME` right, narrowed from `..` to `../data`.

### 4. Bind mount vs named volume - the settled product decision

Both fix the silent-ephemeral-bench bug; the difference is host-side file access, and the captain settled it in favor of the bind mount (confirmed on the r&d review surface, 2026-07-18) so bench files stay directly accessible on the host.

- **Chosen - host bind mount from `{project}/data/` at `--bench-parent`** (Decision 1).
  Preserves direct host access to bench files at `{project}/data/`; `rm`'s project-directory removal keeps cleaning the bench with no code change; smallest diff (a string substitution in the existing customization pass).
  Mounting only `data/` (not the whole project dir `..`) keeps `conf/` out of the container and is the cleaner source than the upstream example's whole-project bind mount.
  Accepted caveat: the supervisor socket sits on the host filesystem backing `cwcli_home()` (Decision 5).

- **Rejected - named volume at `--bench-parent`** (materializing as `{project}_workspace`).
  Would give uniform `rm`/socket handling via Docker storage, but LOSES direct host access to bench files - which was the captain's explicit goal for this task, so it is not chosen.

A future opt-in named-volume flag is out of scope (YAGNI until requested).

### 5. s4 start/status/logs consistency

The mount at `--bench-parent` covers `{bench_parent}/{bench}` in full, so supervisord's config, launcher, socket, pid, and per-process `{bench}/logs/*.supervisor.log` persist across container recreation - which is strictly better than today for a custom `--bench-parent`, where they were ephemeral.
The mapping SHALL never mount at a sub-path that excludes `{bench}/logs`.

Accepted caveat of the bind mount: the supervisor's unix socket `{bench}/logs/.cwcli-supervisor.sock` sits on whatever host filesystem backs `cwcli_home()`. It is solid on a native-Linux `CWCLI_HOME` (the default `~/.cwcli`); only relocating `CWCLI_HOME` onto a networked host filesystem (WSL2 `/mnt/c`, macOS gRPC-FUSE) would reintroduce socket-bind fragility. This is the price of preserving host file access (Decision 4) and the captain accepted it.
No `core/supervision.py` change is needed - the paths are identical.

### 6. Backward compatibility and migration

The `if not compose_path.exists()` guard in `init_instance` is the migration boundary and it already exists: existing instances keep their frozen `..:/workspace:cached` compose and behave identically.
The workspace-mount + `working_dir` rewrites are gated on that fresh-compose branch (a `new_instance` flag), so a re-init never re-targets an old instance's mount - the ports/image rewrites stay unconditional as before (idempotent no-ops on an already-customized compose).
New instances get the `../data` bind mount.
`rm` handles both (Decision 1 - project-directory removal cleans the bench either way); every read/write verb is backend-agnostic (operates on in-container `bench_path`).
There is no forced migration of existing instances; a user who wants the new shape re-creates the instance (documented in the implementation phase).
This mixed fleet is safe because nothing outside init inspects the mount source - the only consumer that ever distinguished them was init itself deciding whether to persist, which this change makes uniform for new instances.

## Risks / trade-offs

- **Supervisor socket on a networked `CWCLI_HOME`** (Decision 5) - an accepted caveat of preserving host file access; solid on the default `~/.cwcli`.
- **A mixed fleet** (old bind-mount + new named-volume instances) - safe by Decision 6; `rm` and all verbs handle both. The only visible difference is where a new instance's bench files live on the host (nowhere directly, vs the project directory).
- **Re-init mount-mismatch as a `USAGE` error** (Decision 2) - a small behavior addition, justified by it replacing today's silent ephemeral-bench outcome; the default path (unchanged `--bench-parent`) is unaffected.

## Open questions

- None. The one product decision (Decision 4, host file access) was settled by the captain in favor of the bind mount from `{project}/data/`, confirmed on the r&d review surface (2026-07-18).
