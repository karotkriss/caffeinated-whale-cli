## ADDED Requirements

### Requirement: The bench workspace is a cwcli-owned persistent bind mount at --bench-parent

For a newly created instance, `cwcli init` SHALL make the bench workspace persist on a cwcli-owned bind mount whose host source is the per-project `{project}/data/` directory and whose container mount point is the resolved `--bench-parent`, rather than relying on the upstream `devcontainer-example` `..:/workspace:cached` bind mount (which mounts the whole project directory at a hardcoded `/workspace`).
`init_instance` (the stage that creates the compose file) SHALL be given `--bench-parent` and SHALL rewrite the frappe service's workspace mount from `- ..:/workspace:cached` to `- ../data:{bench_parent}:cached`, in the same customization pass that rewrites the port ranges and the bench image tag.
Because the compose file lives at `{project}/conf/docker-compose.yml`, the source `../data` resolves to `{project}/data/` on the host, so bench data (apps, sites, files, logs) persists on the host with direct host access - the explicit goal of this change.
`init_instance` SHALL create the host `{project}/data/` directory so Docker does not create it as a root-owned path.
The database's `mariadb-data` named volume SHALL be left unchanged; no new named volume is introduced.
The mount SHALL cover the entire `{bench_parent}/{bench}` subtree, and SHALL never be placed at a sub-path that excludes `{bench}/logs`.

#### Scenario: A default init persists the bench on the host data directory

- **WHEN** `cwcli init` creates a fresh instance with `--bench-parent /workspace` and `--bench frappe-bench`
- **THEN** the frappe service mounts `{project}/data/` at `/workspace`, the bench at `/workspace/frappe-bench` persists on the host at `{project}/data/frappe-bench` and survives a container recreation (`docker compose down` then `up`)

#### Scenario: A custom --bench-parent is persisted, not ephemeral

- **WHEN** `cwcli init` creates a fresh instance with `--bench-parent /opt/benches`
- **THEN** the workspace is mounted from `{project}/data/` at `/opt/benches`, and the bench at `/opt/benches/{bench}` (with its sites, files, and `{bench}/logs`) survives a container recreation rather than being lost from the container's writable layer

### Requirement: The frappe working_dir sits at the mount point

For a newly created instance, `init_instance` SHALL set the frappe service's `working_dir` to the resolved `--bench-parent`, replacing the upstream `working_dir: /workspace/development`.
This keeps `working_dir` inside the mounted `{project}/data/` subtree for every `--bench-parent`, so it is never a root-owned directory created outside the mount.

#### Scenario: working_dir tracks a custom --bench-parent

- **WHEN** a fresh instance is created with `--bench-parent /opt/benches`
- **THEN** the frappe service's `working_dir` is `/opt/benches`, which is the mount point and lies within the mounted data subtree

### Requirement: --bench-parent is the mount point and --bench is a subdirectory within it

The workspace mount SHALL resolve from `--bench-parent` alone: one mount per instance, mounted once at `--bench-parent` from the single host `{project}/data/` directory.
`--bench` (the NAME of the bench created at init, distinct from the `--bench <index|label>` selector other verbs use) SHALL name a subdirectory `{bench_parent}/{bench}` inside that mount and SHALL NOT receive its own mount.
A multi-bench instance SHALL place every bench as a sibling subdirectory under the single `--bench-parent` mount, all sharing the one `{project}/data/` host directory.

#### Scenario: Multiple benches share one workspace mount

- **WHEN** a second bench is created under the same `--bench-parent` as the first (e.g. `/workspace/frappe-bench` and `/workspace/frappe-bench-2`)
- **THEN** both benches persist under `{project}/data/` on the host, with no second mount created for the second bench

### Requirement: The workspace mount point is fixed at instance creation

Because an existing instance's compose file is frozen (`init_instance` skips download and workspace-mount customization when `conf/docker-compose.yml` already exists), the workspace mount point SHALL be a property of the instance set when it is created.
`cwcli init` on an existing instance with a `--bench-parent` that does not match the instance's mounted parent SHALL fail with a `USAGE` error naming the mounted parent, instead of creating a bench directory outside the mount.
A `--bench-parent` that matches the instance's mounted parent (including the unchanged default) SHALL proceed as today.
Resolving the mounted parent for the comparison SHALL read it from the instance's compose file (the frappe service's workspace mount target), which works for both the old whole-project bind mount and the new `{project}/data/` bind mount.

#### Scenario: A re-init with a mismatched --bench-parent refuses

- **WHEN** an instance was created with the workspace mounted at `/workspace` and `cwcli init` is run again on it with `--bench-parent /elsewhere`
- **THEN** it exits with a usage error naming `/workspace` as the instance's mount point, and no bench is created outside the mount

#### Scenario: A re-init with the matching --bench-parent proceeds

- **WHEN** the same instance is re-inited with `--bench-parent /workspace` (or the default) to add a second bench
- **THEN** the second bench is created under the mounted parent and shares the workspace mount, and the frozen compose is not re-targeted

### Requirement: Existing instances are unchanged

This change SHALL apply only to instances created after it ships.
Every already-created instance SHALL keep its existing `..:/workspace:cached` bind mount (the whole project directory at `/workspace`) and behave exactly as before, guaranteed by the pre-existing `if not compose_path.exists()` guard in `init_instance`.
All read and write verbs (`inspect`, `run`, `open`, `start`, `status`, `logs`, `restart`, `backup`, `unlock`, `apps`, `update`, `restore`) SHALL remain backend-agnostic by operating on in-container bench paths, requiring no change.

#### Scenario: A pre-existing instance keeps its bind mount

- **WHEN** `cwcli init` or any verb runs against an instance whose `conf/docker-compose.yml` predates this change
- **THEN** its workspace stays on the `..:/workspace:cached` bind mount, no workspace-mount rewrite occurs, and every verb behaves exactly as before

### Requirement: rm removes the workspace with no code change

A newly created instance's bench workspace lives under the project directory (`{project}/data/`), so `rm`'s existing project-directory removal cleans it, exactly as it already cleans the whole-project bind mount today; the `mariadb-data` named volume is removed by the existing `_remove_named_volumes` path.
`rm`'s verified-backup gate SHALL be unaffected (it copies backup artifacts out of the container via exec, not from any host mount).
`core/rm.py` and `commands/rm.py` SHALL NOT change.

#### Scenario: rm removes the workspace data alongside the database volume

- **WHEN** `cwcli rm {project}` (default `--volumes`) removes a new instance after the backup gate passes
- **THEN** the `{project}/data/` bench workspace is removed with the project directory and `{project}_mariadb-data` is removed by `_remove_named_volumes`, and no `rm` code was modified to make that happen

### Requirement: CWCLI_HOME isolation is preserved

The workspace bind mount source `../data` is relative to the compose file, so it SHALL follow `PROJECTS_DIR` under `cwcli_home()` exactly as today's `..` bind mount does, keeping the workspace isolated across `CWCLI_HOME` values with no new coupling.

#### Scenario: Two CWCLI_HOME instances get distinct workspace directories

- **WHEN** two instances are created under two different `CWCLI_HOME` values
- **THEN** each has its own `{cwcli_home}/projects/{project}/data/` workspace and neither shares workspace state with the other

### Requirement: s4 supervision state persists

The per-process supervisord state under `{bench}/logs/` (config, launcher, socket, pid, and `*.supervisor.log` files) SHALL persist across container recreation because it lives inside the mounted `{project}/data/` subtree.
`core/supervision.py` SHALL NOT change; the in-container paths are identical.
Note: on a bind mount the supervisor's unix socket (`{bench}/logs/.cwcli-supervisor.sock`) sits on whatever host filesystem backs `cwcli_home()`; it is socket-capable on a native-Linux `CWCLI_HOME` (the default `~/.cwcli`), and only relocating `CWCLI_HOME` onto a networked host filesystem (e.g. WSL2 `/mnt/c`) would reintroduce socket-bind fragility - an accepted caveat of preserving host file access.

#### Scenario: Supervisor logs survive a container restart

- **WHEN** a bench is supervised (via `cwcli start`) and its container is later recreated
- **THEN** the supervisord config, launcher, socket, pid, and per-process logs under `{bench}/logs/` are still present under `{project}/data/` after `cwcli start` re-supervises
