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

**2. The workspace mount covers the whole project directory at a hardcoded target.**
The upstream `- ..:/workspace:cached` mounts the entire `{project}/` directory (including `conf/`, where the compose file itself lives) at `/workspace`, purely because the upstream example did it that way and cwcli never revisited it.
This is why `--bench-parent` can only work at `/workspace`: the mount source and target are both frozen to the upstream shape, and cwcli never rewrote them to follow the flag it exposes.

The fix is to make cwcli own the workspace mount and derive it from `--bench-parent`/`--bench`, exactly as the captain framed it: use `--bench-parent` and `--bench` to determine the mount mapping and mount the correct source.

## What Changes

- **The bench workspace becomes a per-project host bind mount from `{project}/data/` at `--bench-parent`.**
  `init_instance` (stage 1, which owns the compose file) rewrites the frappe service's workspace mount from the upstream `- ..:/workspace:cached` to `- ../data:{bench_parent}:cached`.
  Because the compose file lives at `{project}/conf/docker-compose.yml`, the source `../data` resolves to `{project}/data/` on the host, so the mount now covers **only** the per-project data directory (not the whole project dir including `conf/`), mounted at the resolved `--bench-parent`.
  Bench data (apps, sites, files, logs) therefore persists on the host at `{project}/data/` with direct host access - the captain's explicit goal for this task.
  `init_instance` creates the host `{project}/data/` directory so Docker never materializes it as a root-owned path.
  `mariadb-data` is unchanged and no new named volume is introduced.

- **`--bench-parent`/`--bench` semantics are resolved and made load-bearing (recon item 1):**
  `--bench-parent` is the **container mount point** - a per-instance property fixed when the instance's compose file is created (stage 1); the host source is ALWAYS `{project}/data/`.
  `--bench` names a **subdirectory inside that mount** (`{bench_parent}/{bench}`); it is NOT the `--bench <index|label>` selector other verbs use, and it does NOT get its own mount.
  The workspace mount therefore resolves from `--bench-parent` alone: one mount per project from the single `{project}/data/` host dir, with every bench (multi-bench included: `{parent}/frappe-bench`, `{parent}/frappe-bench-2`) as a subdirectory sharing it.
  This threads `bench_parent` into `init_instance`, which previously only knew it in `init_bench`.

- **The frappe `working_dir` follows the mount point.**
  The upstream compose sets `working_dir: /workspace/development`; `init_instance` rewrites it to the resolved `--bench-parent` so it stays inside the mounted `data/` subtree for any parent and is never a root-owned directory created outside the mount.

- **The mount point is an instance property, so a re-init must agree with it.**
  Because an existing project's compose file is frozen (see below), `cwcli init` on an existing project with a `--bench-parent` that does not match the instance's mounted parent is a `USAGE` error naming the mounted parent, rather than silently creating an ephemeral bench outside the volume.
  A matching (or default-and-unchanged) `--bench-parent` proceeds as today.

- **`rm` needs ZERO code change.**
  The bench workspace lives under the project directory (`{project}/data/`), so `rm`'s existing project-directory removal cleans it, exactly as it already cleans the whole-project bind mount today; the `mariadb-data` named volume is still removed by `_remove_named_volumes`.
  The C1 verified-backup gate (which copies artifacts OUT of the container via exec, not from the host mount) is unaffected.

- **Backward compatibility: existing instances are byte-unchanged (recon item 4).**
  `init_instance` rewrites the workspace mount (and `working_dir`) only on a freshly downloaded compose, behind the pre-existing `if not compose_path.exists()` guard, so every already-created instance keeps its `..:/workspace:cached` whole-project bind mount and behaves exactly as before.
  All read/write verbs (`inspect`, `run`, `open`, `start`, `status`, `logs`, `restart`, `backup`, `unlock`, `apps`, `update`, `restore`) operate on in-container `bench_path`s, which are identical whether the mount source is the whole project dir or `{project}/data/`, so they are backend-agnostic and need no change.
  `rm` cleans both shapes (project-directory removal cleans the bench for old and new instances alike; `_remove_named_volumes` handles `mariadb-data` for both).
  No forced migration; the change applies only to freshly created instances.

- **CWCLI_HOME stays correct (recon item 3).**
  The bind mount source `../data` is relative to the compose file, so it follows `PROJECTS_DIR` under `cwcli_home()` exactly as today's `..` mount does, and `mariadb-data` keeps its project-name namespace.
  Two `CWCLI_HOME` instances therefore get distinct `{cwcli_home}/projects/{project}/data/` workspaces with no new coupling.

- **s4 start/status/logs stay consistent (recon item 5).**
  The mount at `--bench-parent` covers the entire `{bench_parent}/{bench}` subtree, so supervisord's config, launcher, socket, pid, and per-process `{bench}/logs/*.supervisor.log` all persist; the mapping SHALL never mount at a sub-path that excludes `{bench}/logs`.
  The supervisor's unix socket sits on whatever host filesystem backs `cwcli_home()`; it is solid on a native-Linux `CWCLI_HOME` (the default `~/.cwcli`), and only relocating `CWCLI_HOME` onto a networked host filesystem (e.g. WSL2 `/mnt/c`) would reintroduce socket-bind fragility - an accepted caveat of preserving host file access.

- **The product decision is settled: host file access is preserved (design Decision 4).** The captain chose the bind mount (over a named volume) so bench files stay directly accessible on the host at `{project}/data/`; mounting only `data/` - rather than the whole project dir the upstream example bound - keeps `conf/` out of the container and is the cleaner source.
  A future opt-in named-volume flag is explicitly out of scope (YAGNI until requested).

## Impact

- **Changed (implementation phase):** `core/init.py` (thread `bench_parent` into `init_instance`; rewrite the frappe workspace mount to `../data:{bench_parent}:cached` and `working_dir` to `{bench_parent}` alongside the existing port/image rewrites, behind the fresh-compose guard; create the host `data/` dir; the re-init mount-mismatch `USAGE` guard), `commands/init.py` and `commands/axi.py` (pass `bench_parent` to stage 1; no new flags), the init core suite (a new compose-rewrite + re-init-guard test class), `docs/e2e/` evidence, `README.md` (the "workspace volume" wording becomes accurate for new instances), CLAUDE.md ledger, and the `cwcli-lifecycle` skill.
- **Unchanged:** `rm` (`core/rm.py`, `commands/rm.py`) and its backup gate; every non-init command; `core/supervision.py` and the s4 contract; the cache schema; the `axi` surface (no new verb); all existing instances and their compose files.
- **Zero new primitives:** the bind-mount rewrite is a string substitution in the existing customization pass; the mount-point resolution reuses `--bench-parent`/`--bench` as they already exist.
- **Out of scope:** migrating existing bind-mount instances; a named-volume opt-in flag; any change to `mariadb-data`; any `axi` verb change.
