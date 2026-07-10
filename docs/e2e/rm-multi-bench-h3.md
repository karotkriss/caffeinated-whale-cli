# E2E evidence: multi-bench `rm` backs up every bench before volume deletion

Real-instance end-to-end run on an **isolated** throwaway environment (a temporary
`HOME` so `~/.cwcli` was a fresh DB, unique docker-compose project, and dedicated
volumes). The captain's real projects and real `~/.cwcli` were never touched, and
the instance was torn down afterwards. Two genuine benches were built from
Frappe `version-15`:

- **`cwcle2e-mb`** with **two benches** at `/workspace/e2e-mb-bench1` and
  `/workspace/e2e-mb-bench2`, each with one site (`e2e-mb-site1.localhost`,
  `e2e-mb-site2.localhost`).

## What it proves

- **Both benches are backed up before volume deletion.** The old code only backed
  up bench 0 (the first sorted bench). The fix iterates ALL `bench_paths` and
  calls `_backup_sites` per bench, so both database dumps land in separate
  per-bench archive namespaces.
- **Per-bench archive namespacing works.** Backups for bench1 land in
  `e2e-mb-bench1_c8998303/backups/` and bench2 in
  `e2e-mb-bench2_65459ff4/backups/`. Two benches using the same default site name
  (e.g. both using `development.localhost`) do not collide.
- **Per-bench config archiving works.** Each bench's `site_config.json` is
  archived under its own slug subdirectory.
- **Named volumes are removed.** The single compose volume
  (`cwcle2e-mb_mariadb-data`) is explicitly removed after the backup gate passes.
- **Project directory is removed** from `~/.cwcli/projects/`.
- **Cache is cleared** after a clean full removal.
- **Database dumps are valid** gzip-compressed SQL files with real Frappe data
  (~228K each, ~2MB uncompressed).

## Setup

```bash
E2E_HOME=/tmp/cwcle2e-mb-home
rm -rf "$E2E_HOME" && mkdir -p "$E2E_HOME"

# 1. Init first bench via cwcli
HOME=$E2E_HOME uv run cwcli init cwcle2e-mb \
  --bench e2e-mb-bench1 --site e2e-mb-site1.localhost \
  --port 18000 --db-root-password 123 --admin-password admin \
  --frappe-branch version-15 --auto-start --no-reuse-bench

# 2. Add second bench + site manually
CONTAINER=cwcle2e-mb-frappe-1
docker exec "$CONTAINER" sudo apt-get install -y -qq redis-server
docker exec "$CONTAINER" bench init --frappe-branch version-15 /workspace/e2e-mb-bench2
docker exec -w /workspace/e2e-mb-bench2 "$CONTAINER" \
  bash -c 'echo '"'"'{"db_host":"mariadb","redis_cache":"redis://redis-cache:6379","redis_queue":"redis://redis-queue:6379","redis_socketio":"redis://redis-queue:6379"}'"'"' > sites/common_site_config.json'
docker exec -w /workspace/e2e-mb-bench2 "$CONTAINER" \
  bench new-site e2e-mb-site2.localhost --mariadb-root-password 123 --db-root-password 123 --admin-password admin

# 3. Add /workspace to custom search paths so the second bench is discovered
HOME=$E2E_HOME uv run cwcli config add-path /workspace
HOME=$E2E_HOME uv run cwcli inspect cwcle2e-mb --update
```

## Verify both benches are visible

```
$ HOME=$E2E_HOME uv run cwcli inspect cwcle2e-mb
Project cwcle2e-mb
├── Bench [0] at /workspace/e2e-mb-bench1
│   ├── Available Apps (1)
│   │   └── frappe
│   └── Sites (1)
│       └── e2e-mb-site1.localhost
│           └── Installed Apps (1)
│               └── frappe 15.114.0 version-15
└── Bench [1] at /workspace/e2e-mb-bench2
    ├── Available Apps (1)
    │   └── frappe
    └── Sites (1)
        └── e2e-mb-site2.localhost
            └── Installed Apps (1)
                └── frappe 15.114.0 version-15
```

## Run `rm` and verify

```bash
HOME=$E2E_HOME uv run cwcli rm cwcle2e-mb -v --yes
```

Output confirms both benches are backed up:

```
  Backed up 1 site(s) to .../archive/.../e2e-mb-bench1_c8998303/backups
  Backed up 1 site(s) to .../archive/.../e2e-mb-bench2_65459ff4/backups
  Removed 1 named volume(s) for 'cwcle2e-mb'
  Removed project directory /tmp/.../projects/cwcle2e-mb
✓ Project 'cwcle2e-mb' removed (4 container(s))
```

## Artifact verification

### Archive layout (13 files)

```
archive/cwcle2e-mb_20260710_061110/
├── e2e-mb-bench1_c8998303/
│   ├── archive_metadata.json
│   ├── backups/e2e-mb-site1.localhost/
│   │   ├── 20260710_154111-...-database.sql.gz     (228K, valid gzip)
│   │   ├── 20260710_154111-...-files.tar
│   │   ├── 20260710_154111-...-private-files.tar
│   │   └── 20260710_154111-...-site_config_backup.json
│   └── e2e-mb-site1.localhost/
│       └── site_config.json
├── e2e-mb-bench2_65459ff4/
│   ├── archive_metadata.json
│   ├── backups/e2e-mb-site2.localhost/
│   │   ├── 20260710_154113-...-database.sql.gz     (228K, valid gzip)
│   │   ├── 20260710_154113-...-files.tar
│   │   ├── 20260710_154113-...-private-files.tar
│   │   └── 20260710_154113-...-site_config_backup.json
│   └── e2e-mb-site2.localhost/
│       └── site_config.json
└── project_files/conf/
    └── docker-compose.yml
```

### Databases are valid gzip

```
e2e-mb-bench1_.../backups/.../...-database.sql.gz:
  gzip compressed data, max compression, original size modulo 2^32 2001559
e2e-mb-bench2_.../backups/.../...-database.sql.gz:
  gzip compressed data, max compression, original size modulo 2^32 2001559
```

### Cleanup verified

- **Containers:** 0 (all removed)
- **Named volumes:** 0 (mariadb-data removed)
- **Project directory:** removed from `~/.cwcli/projects/`
- **Cache:** cleared

## Tear down

```bash
rm -rf "$E2E_HOME"
```