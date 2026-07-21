# E2E evidence: `cwcli axi rm` and the surface gaps from the same probe

Real-instance validation for `add-axi-rm-verb`, plus the `axi build`, `run-tests --module`, and `init --reuse-bench` port-check items reported alongside it.

The repo's standing rule for delete paths applies here and was followed literally: a real full lifecycle against the worktree's **own editable install**, never mocks and never a published build. That rule exists because mocks previously hid a real bug in this exact area, and a passing suite over a mocked delete proves nothing about a delete.

## Environment (isolation)

- `CWCLI_HOME=/tmp/cwrm-e2e`, so the captain's real `~/.cwcli` was never read or written.
- Binary under test: `uv run cwcli` from this worktree, self-identifying as `1.1.0 (editable source build, git 5d44d0e, dirty)` - the code under review, not an installed build.
- Throwaway instances `cwrm-a` then `cwrm-b`, ports 12100-12105 / 13100-13105, Frappe **version-16** (`frappe 16.27.1`).
- **One instance alive at a time.** Two unrelated instances (`cwe2e-cebf62-main`, `frappe15`) were running on this box throughout and were never touched; no Docker prune was run. Both throwaways were torn down afterwards, including the volume that `--no-volumes` deliberately left behind.

## 1. The destructive path (the part that is not negotiable)

### Setup and seed

```
$ cwcli axi init cwrm-a --port 12100 --site dev.localhost     # CWCLI_ADMIN_PASSWORD in env
project: cwrm-a
bench_path: /workspace/frappe-bench
site_name: dev.localhost
bench_created: true
site_created: true

$ cwcli run cwrm-a --site dev.localhost execute frappe.client.insert \
    --kwargs '{"doc":{"doctype":"ToDo","description":"CWRM-SEED-MARKER-9f3a1c","status":"Open"}}'
{"name": "bm8lnvbtgd", ..., "description": "CWRM-SEED-MARKER-9f3a1c", ...}

$ cwcli run cwrm-a --site dev.localhost execute frappe.client.get_count \
    --kwargs '{"doctype":"ToDo","filters":{"description":"CWRM-SEED-MARKER-9f3a1c"}}'
1
```

Record `bm8lnvbtgd` is the marker the rest of this document follows. A second site, `second.localhost`, was added during §3, so the removal below had two sites across one bench to back up.

### Both refusals, against the live instance

| Command | Result |
|---|---|
| `cwcli axi rm cwrm-a` | `error: "Refusing to remove 'cwrm-a' without explicit consent."` + `help:` naming `--yes` and what it deletes. **Exit 2.** |
| `cwcli axi stop cwrm-a` then `cwcli axi rm cwrm-a --yes` | `error: "... it is not running, so the verified database backup that gates volume deletion cannot be taken."` + `help: "start it with 'cwcli start cwrm-a' and re-run, or pass --no-volumes to keep the data, or use 'cwcli rm cwrm-a --no-backup' to delete without a backup"`. **Exit 1.** |

After both refusals the instance was fully intact: 4 containers, 1 named volume, project directory present. Nothing was touched, and each refusal named the way out rather than dead-ending.

### The removal

```
$ cwcli start cwrm-a && cwcli axi status cwrm-a        # overall: running
$ cwcli axi rm cwrm-a --yes
project: cwrm-a
found: true
orphan: false
containers_removed: 4
volumes_removed: 1
dir_removed: true
backup_ok: true
failures[0]:
                                                       # EXIT 0
```

### Confirmation 1: the deletion is honest

```
containers matching cwrm-a : 0
volumes matching cwrm-a    : 0
project dir                : gone
cwcli axi ls               : no cwrm-a row (cache entry cleared)
```

### Confirmation 2: the backup is genuinely restorable

The archive holds a verified per-site backup for **both** sites on the bench:

```
/tmp/cwrm-e2e/archive/cwrm-a_20260721_123027/
  archive_metadata.json
  project_files/conf/docker-compose.yml
  dev.localhost/site_config.json
  second.localhost/site_config.json
  backups/dev.localhost/20260721_123029-dev_localhost-{database.sql.gz,files.tar,private-files.tar,site_config_backup.json}
  backups/second.localhost/20260721_220030-second_localhost-{...}
```

Restoring it into a **fresh** instance, which is the recovery path a user actually walks:

```
$ cwcli axi init cwrm-b --port 12100 --site dev.localhost
$ cwcli run cwrm-b --site dev.localhost execute frappe.client.get_count \
    --kwargs '{"doctype":"ToDo","filters":{"description":"CWRM-SEED-MARKER-9f3a1c"}}'
                                                       # no output: 0 rows (bench execute prints nothing for a falsy return)

$ cp <archive>/backups/dev.localhost/* \
     /tmp/cwrm-e2e/projects/cwrm-b/data/frappe-bench/sites/dev.localhost/private/backups/
$ cwcli restore cwrm-b --site dev.localhost --latest --yes \
    --mariadb-root-username root --mariadb-root-password 123
✓ Successfully restored site 'dev.localhost'
Updated encryption_key from backup site_config
✓ Migrated site 'dev.localhost'
✓ Instance restarted

$ cwcli run cwrm-b --site dev.localhost execute frappe.client.get_list \
    --kwargs '{"doctype":"ToDo","filters":{"description":"CWRM-SEED-MARKER-9f3a1c"},"fields":["name","description","status"]}'
[{"name": "bm8lnvbtgd", "description": "CWRM-SEED-MARKER-9f3a1c", "status": "Open"}]
```

Same primary key as the record seeded into the instance that was deleted. The data survived the destruction, in a different instance, read back through Frappe itself rather than by grepping the dump.

### The third exit the refusal names

`--no-volumes` destroys nothing data-bearing, so the backup gate does not apply and the not-running refusal must not fire. Against the stopped `cwrm-b`:

```
$ cwcli axi rm cwrm-b --yes --no-volumes
Warning: No container was running for 'cwrm-b', so a fresh database backup could not be taken before cleanup.
...
containers_removed: 4
volumes_removed: 0
dir_removed: true
                                                       # EXIT 0

containers: 0    volumes: cwrm-b_mariadb-data (SURVIVED)    dir: gone
```

The advice the stopped-project refusal gives is therefore advice that works.

## 2. `axi build` and `run-tests --module`

```
$ cwcli axi build cwrm-a --app frappe
project: cwrm-a
site: null                                             # a build acts on no site, and says so
app: frappe
results[1]{action,ok,message}:
  build,true,null
ok: true
                                                       # EXIT 0; asset build output on stderr
```

```
$ cwcli axi run-tests cwrm-a --site dev.localhost --app frappe --module frappe.tests.test_naming
$ bench --site dev.localhost run-tests --app frappe --module frappe.tests.test_naming
...
Ran 22 tests in 2.639s
OK
                                                       # EXIT 0
```

Two things this proves that could not be assumed: `bench` **accepts `--app` and `--module` together** (a real risk, since the flag was added on the reading of frappe's runner), and the narrowing is genuine - 22 tests ran where the frappe app's full suite runs thousands. The first attempt returned `Testing is disabled for the site!`, so `allow_tests` was set on the site and the run repeated; cwcli forwarded bench's own exit code honestly in both cases.

## 3. `axi init --reuse-bench` port self-conflict

Reproduced first against the **pre-fix** code (the fix stashed), using the exact invocation from the report:

```
$ cwcli axi init cwrm-a --port 12100 --reuse-bench --site second.localhost
error: "The following ports are already in use: 12100-12105, 13100-13105"
help: Use the --port flag to select a different starting port.
                                                       # EXIT 1
```

The ports it named were held by `cwrm-a` itself - the very instance being reused - and the advice could not be followed, because an existing instance's ports are frozen in its compose file.

With the fix (`core/init.py` skips the port check when the project already has a running container of its own, the same self-conflict the `auto_start=True` retry already skipped):

```
$ cwcli axi init cwrm-a --port 12100 --reuse-bench --site second.localhost
project: cwrm-a
bench_name: frappe-bench
site_name: second.localhost
bench_created: false                                   # bench reused
site_created: true                                     # new site created
                                                       # EXIT 0
```

The check still runs for a stopped or absent project, where a bound port genuinely belongs to somebody else.

## Teardown

Both throwaway instances removed, `cwrm-b_mariadb-data` (deliberately preserved by `--no-volumes`) removed by hand afterwards. No `cwrm*` container or volume remains; the two unrelated instances on the box were never touched.
