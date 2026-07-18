## ADDED Requirements

### Requirement: The bench workspace is a cwcli-owned persistent volume mounted at --bench-parent

For a newly created instance, `cwcli init` SHALL make the bench workspace persist on a cwcli-owned volume whose mount point is the resolved `--bench-parent`, rather than relying on the upstream `devcontainer-example` `..:/workspace:cached` bind mount hardcoded to `/workspace`.
`init_instance` (the stage that creates the compose file) SHALL be given `--bench-parent` and SHALL rewrite the frappe service's workspace mount so the volume is mounted at that path, in the same customization pass that rewrites the port ranges and the bench image tag.
The recommended realization is a per-project named volume declared in the compose `volumes:` block, materializing as `{project}_workspace` under `docker compose -p {project}` (the `{project}_mariadb-data` shape); the database volume SHALL be left unchanged.
The mount SHALL cover the entire `{bench_parent}/{bench}` subtree, and SHALL never be placed at a sub-path that excludes `{bench}/logs`.

#### Scenario: A default init persists the bench on the workspace volume

- **WHEN** `cwcli init` creates a fresh instance with `--bench-parent /workspace` and `--bench frappe-bench`
- **THEN** the frappe service mounts the cwcli-owned workspace volume at `/workspace`, the bench at `/workspace/frappe-bench` survives a container recreation (`docker compose down` then `up`), and the workspace volume is discoverable by the project label

#### Scenario: A custom --bench-parent is persisted, not ephemeral

- **WHEN** `cwcli init` creates a fresh instance with `--bench-parent /opt/benches`
- **THEN** the workspace volume is mounted at `/opt/benches`, and the bench at `/opt/benches/{bench}` (with its sites, files, and `{bench}/logs`) survives a container recreation rather than being lost from the container's writable layer

### Requirement: --bench-parent is the mount point and --bench is a subdirectory within it

The workspace volume SHALL resolve from `--bench-parent` alone: one volume per instance, mounted once at `--bench-parent`.
`--bench` (the NAME of the bench created at init, distinct from the `--bench <index|label>` selector other verbs use) SHALL name a subdirectory `{bench_parent}/{bench}` inside that mount and SHALL NOT receive its own volume.
A multi-bench instance SHALL place every bench as a sibling subdirectory under the single `--bench-parent` mount, all sharing the one workspace volume.

#### Scenario: Multiple benches share one workspace volume

- **WHEN** a second bench is created under the same `--bench-parent` as the first (e.g. `/workspace/frappe-bench` and `/workspace/frappe-bench-2`)
- **THEN** both benches persist on the single workspace volume mounted at that parent, with no second volume created for the second bench

### Requirement: The workspace mount point is fixed at instance creation

Because an existing instance's compose file is frozen (`init_instance` skips download and customization when `conf/docker-compose.yml` already exists), the workspace mount point SHALL be a property of the instance set when it is created.
`cwcli init` on an existing instance with a `--bench-parent` that does not match the instance's mounted parent SHALL fail with a `USAGE` error naming the mounted parent, instead of creating a bench directory outside the mounted volume.
A `--bench-parent` that matches the instance's mounted parent (including the unchanged default) SHALL proceed as today.

#### Scenario: A re-init with a mismatched --bench-parent refuses

- **WHEN** an instance was created with the workspace mounted at `/workspace` and `cwcli init` is run again on it with `--bench-parent /elsewhere`
- **THEN** it exits with a usage error naming `/workspace` as the instance's mount point, and no bench is created outside the mounted volume

#### Scenario: A re-init with the matching --bench-parent proceeds

- **WHEN** the same instance is re-inited with `--bench-parent /workspace` (or the default) to add a second bench
- **THEN** the second bench is created under the mounted parent and shares the workspace volume

### Requirement: Existing instances are unchanged

This change SHALL apply only to instances created after it ships.
Every already-created instance SHALL keep its existing `..:/workspace:cached` bind mount and behave exactly as before, guaranteed by the pre-existing `if not compose_path.exists()` guard in `init_instance`.
All read and write verbs (`inspect`, `run`, `open`, `start`, `status`, `logs`, `restart`, `backup`, `unlock`, `apps`, `update`, `restore`) SHALL remain backend-agnostic by operating on in-container bench paths, requiring no change.

#### Scenario: A pre-existing instance keeps its bind mount

- **WHEN** `cwcli init` or any verb runs against an instance whose `conf/docker-compose.yml` predates this change
- **THEN** its workspace stays on the `..:/workspace:cached` bind mount, no compose rewrite occurs, and every verb behaves exactly as before

### Requirement: rm removes the workspace volume through the existing named-volume path with no code change

A newly created instance's workspace volume SHALL be labeled with its compose project so `core/docker.py:get_project_volumes` enumerates it and `core/rm.py:_remove_named_volumes` removes it, identically to `mariadb-data`.
`rm`'s verified-backup gate SHALL be unaffected (it copies backup artifacts out of the container via exec, not from any host mount).
`core/rm.py` and `commands/rm.py` SHALL NOT change.

#### Scenario: rm removes the workspace volume alongside the database volume

- **WHEN** `cwcli rm {project}` (default `--volumes`) removes a new instance after the backup gate passes
- **THEN** both `{project}_workspace` and `{project}_mariadb-data` are removed by `_remove_named_volumes`, and no `rm` code was modified to make that happen

### Requirement: CWCLI_HOME isolation is preserved

The workspace volume SHALL remain isolated across `CWCLI_HOME` values by its project-name namespace, the same mechanism that already isolates `mariadb-data`, without any new coupling to the on-disk footprint under `cwcli_home()`.

#### Scenario: Two CWCLI_HOME instances get distinct workspace volumes

- **WHEN** two instances with distinct project names are created under two different `CWCLI_HOME` values
- **THEN** each has its own `{project}_workspace` volume and neither shares workspace state with the other

### Requirement: s4 supervision state persists and stays socket-capable

The per-process supervisord state under `{bench}/logs/` (config, launcher, socket, pid, and `*.supervisor.log` files) SHALL persist across container recreation because it lives inside the mounted workspace subtree.
The mapping SHALL keep the supervisor's unix socket (`{bench}/logs/.cwcli-supervisor.sock`) on a socket-capable filesystem; a Docker-managed named volume satisfies this on all supported hosts, including WSL2 and macOS where a bind mount onto a networked host filesystem may not.
`core/supervision.py` SHALL NOT change; the in-container paths are identical.

#### Scenario: Supervisor logs survive a container restart

- **WHEN** a bench is supervised (via `cwcli start`) and its container is later recreated
- **THEN** the supervisord config, launcher, socket, pid, and per-process logs under `{bench}/logs/` are still present on the workspace volume after `cwcli start` re-supervises
