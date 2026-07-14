# E2E evidence: stopped `rm` starts, backs up, then deletes (start -> back up -> delete)

Real-instance, full-lifecycle validation of the dangerous-delete change on an
**isolated** throwaway environment: a dedicated `CWCLI_HOME` on the roomy host
disk (so the captain's real `~/.cwcli` was never touched), a unique
`cwe2e-rmstop-*` docker-compose project, and dedicated volumes/network. The
captain's real instances (`ners`, `visa`, ...) and the other lane's
`cwe2e-bf6650-main` were confirmed untouched, and every throwaway was torn down
afterwards. The bench was a genuine Frappe `version-16` instance
(`cwcli init ... --version 16`) with the default `development.localhost` site.

Every run seeded a distinctive marker row before the test - a `ToDo` whose
`description` is `CWE2E_SEED_MARKER_*` - so the backup's database dump can be
grepped to prove it captured real, restorable data. Each test was run against
the worktree's **editable** install (`uv run cwcli`), not a PyPI build.

## Pre-fix reproduction (the bug)

With the fix stashed (`git stash`), a `cwcli rm cwe2e-rmstop-a --volumes --yes`
on the STOPPED, seeded project:

```
Warning: No container was running for 'cwe2e-rmstop-a', so a fresh database
backup could not be taken before cleanup.
  Removed 1 named volume(s) for 'cwe2e-rmstop-a'
  Removed project directory .../projects/cwe2e-rmstop-a
✓ Successfully removed 4 container(s)      # exit 0
```

The archive held ONLY `project_files/conf/docker-compose.yml` - **no database
dump**. The `mariadb-data` volume and the whole bench (with the seeded row) were
destroyed with no backup, exit 0. Bug reproduced.

## What the fix proves

- **(a) non-interactive** (`rm --volumes --yes`, stopped): prints
  `'cwe2e-rmstop-a' is stopped; starting it to take a backup before removal...`,
  runs a live `bench backup --with-files` on the transiently-started project
  (`Backed up 1 site(s)`), then deletes. The host archive contains the full
  `--with-files` set (`database.sql.gz`, `files.tar`, `private-files.tar`,
  `site_config_backup.json`) and the DB dump **contains the seed marker**
  (`gunzip -c ...-database.sql.gz | grep CWE2E_SEED_MARKER_A1` -> 1 hit). Volume,
  containers, and project dir are all gone. Exit 0.
- **(a) interactive** (real pty, `pexpect`, answering `y`): the confirmation
  prompt discloses the plan BEFORE the confirm -
  *"The following project(s) are stopped and will be started to take a backup
  first, then deleted (start -> back up -> delete)"* - then on `y` runs the same
  start -> back up -> delete. The backup again contains the seed marker; the
  deletion is honest.
- **interactive decline** (pty answering `n`): the same disclosure is shown, and
  declining prints `Operation cancelled.` and deletes nothing (volume + dir
  intact), exit 0.
- **(b) start succeeds but backup fails -> abort, keep everything, return to
  stopped.** The site's `db_password` was corrupted so the container starts
  (the MariaDB TCP readiness probe passes) but `bench backup` fails to
  authenticate:

  ```
  'cwe2e-rmstop-a' is stopped; starting it to take a backup before removal...
  Warning: Could not backup site 'development.localhost'
  Warning: Refusing to remove 'cwe2e-rmstop-a': a verified database backup
  could not be created.
  Returning 'cwe2e-rmstop-a' to its stopped state...
  ✗ Project 'cwe2e-rmstop-a' was not fully removed      # exit 1
  ```

  Nothing was deleted: the `mariadb-data` volume, the containers, and the project
  dir were all still present, and the containers were left `Exited` (returned to
  their stopped state). No database dump was written.
- **(c) `--no-backup` escape hatch** (`rm --no-backup --yes`, stopped): NO
  transient start (the "starting it to take a backup" line is absent), deletes the
  volume + containers + dir with no backup taken, exit 0. This is the documented
  way to delete a stopped project without a backup.

## Both modes

Interactive (pty-driven confirm) and non-interactive (`--yes`) were both
exercised end-to-end on a real instance, per the captain "both modes" standard.
The interactive prompt genuinely collected input (raw-mode marker `ESC[?2004h`
awaited before each keystroke); the non-interactive path ran to completion with
no prompt and honest exit codes (0 on success, non-zero on the abort).
