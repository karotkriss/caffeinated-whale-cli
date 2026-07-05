# E2E evidence: restore + inspect fixes (currentsite, default site, receive path, credential prompts, missing-apps, migrate+restart)

Real-instance end-to-end run on **isolated** throwaway environments (a temporary
`HOME` so `~/.cwcli` was a fresh DB, unique docker-compose projects, and dedicated
volumes). The captain's real projects and real `~/.cwcli` were never touched, and
the instances were torn down afterwards. Two genuine benches were built with
`cwcli init` + `bench init`:

- **`cwe2e6`** - Frappe **version-15** (`15.113.4`).
- **`cwe2e6v14`** - Frappe **version-14** (`14.101.1`), the captain's exact version,
  where the `--receive` "Invalid path" bug actually manifests.

Both were put into the captain's reported state: `sites/currentsite.txt` present
and holding `development.localhost`, with **no** `default_site` in
`common_site_config.json` (the default site lives only in `currentsite.txt`).

Interactive prompts were driven through a real pty (pexpect); non-interactive
paths were driven with flags. prompt_toolkit's raw-mode readiness marker
(`ESC[?2004h`) was awaited before each keystroke so nothing raced the prompt.

## What it proves

- **#1 `--receive` uses the full DB path (Frappe v14).** With cwd = the site's
  `private/backups`, `bench restore <bare-filename>` fails with the captain's exact
  error `Invalid path <db>`; `bench restore <full-path>` succeeds. The receive path
  now builds the full path, and a real receive-mode restore completes on v14.
- **#2 credential prompts in BOTH modes.** Interactive: the MariaDB **username**
  prompt (previously skipped) appears and the **password** is actually collected.
  Non-interactive: `--mariadb-root-username/--mariadb-root-password` run the restore
  with no prompt; a non-TTY without a password refuses (unit-pinned).
- **#3 `currentsite.txt` is not a site.** `inspect` went from `Sites (2)`
  (`currentsite.txt` + the real site, the stray one erroring on `bench list-apps`)
  to `Sites (1)` (the real site only).
- **#4 default site from `currentsite.txt`.** With no `default_site` in
  `common_site_config.json`, `inspect` marks `development.localhost (default)` from
  `currentsite.txt`, `get_default_site` resolves it through the cache, and restore
  (normal + receive) uses it with no `--site`.
- **#5 missing-apps warning fires again.** A backup taken with `widgets` installed,
  restored into a bench with `widgets` removed from `apps/`, warns that the backup
  needs `widgets` (read from the backup's own DB dump, not the about-to-be-
  overwritten site).
- **#6 post-restore migrate + restart.** A successful restore runs
  `bench migrate` then restarts the instance.

## #1 - `--receive` full DB path, on Frappe v14 (the captain's version)

```bash
# BUGGY (bare filename, cwd = private/backups) - the captain's exact error:
$ cd .../private/backups && bench --site development.localhost restore 20260705_092753-...-database.sql.gz --force ...
Invalid path 20260705_092753-development_localhost-database.sql.gz

# FIXED (full path, cwd = private/backups) - what restore.py now builds:
$ cd .../private/backups && bench --site development.localhost restore /workspace/frappe-bench/sites/development.localhost/private/backups/20260705_092753-...-database.sql.gz --force ...
Site development.localhost has been restored
```

Real receive-mode restore through the receive code path on v14 (default site
resolved from `currentsite.txt`, then restore + migrate + restart):

```bash
[info] driving real receive-mode restore on cwe2e6v14 (Frappe v14)
Using default site: development.localhost
✓ Files copied to container
✓ Successfully restored backup to site 'development.localhost'
✓ Migrated site 'development.localhost'
Restarting instance...
[OK] receive-mode restore + migrate + restart completed on real v14 Frappe
```

A full **real sendme loopback** through the CLI (`restore --send` serving a ticket,
`restore --receive` pasting it) also completed on v14 (exit 0); the deterministic
harness above stubs only the P2P transfer, which is orthogonal to the path bug.

## #2 - credential prompts, interactive (pty) and non-interactive (flags)

Interactive `cwcli restore cwe2e6` (no `--site`, no creds):

```bash
Using default site: development.localhost
? Select a backup to restore: 2026-07-05 09:16:49  [DATABASE ONLY]
? Are you sure you want to restore? Yes
? MariaDB root username: root          <- previously SKIPPED; now shown + collected
? MariaDB root password:               <- input actually collected (123)
✓ Successfully restored site 'development.localhost'
[OK] MariaDB root username prompt appeared
[OK] MariaDB root password prompt collected input
```

Non-interactive (`--mariadb-root-username root --mariadb-root-password 123`): no
credential prompt fires; the restore runs straight through after the confirm
(`[OK] no credential prompt fired; restore ran non-interactively via flags`).

## #3 / #4 - inspect: currentsite.txt not a site; default from currentsite.txt

```bash
############ BUGGY (original code) ############
    └── Sites (2)
        ├── currentsite.txt
        │       └── Error fetching apps for site currentsite.txt
        └── development.localhost

############ FIXED (this change) ############
    └── Sites (1)
        └── development.localhost (default)
```

`development.localhost` is marked `(default)` even though `common_site_config.json`
has no `default_site` - it is resolved from `currentsite.txt`. Confirmed through
the cache too: `get_default_site('cwe2e6', ...) -> development.localhost`.

## #5 - missing-apps warning (through the CLI)

Backup taken with `widgets` installed; `widgets` then removed from the bench's
`apps/`. `cwcli restore cwe2e6 --site development.localhost`:

```bash
backup apps in dump: {'widgets', 'frappe'}     # read from the backup's own dump
MISSING APPS DETECTED: ['widgets']

⚠ Warning: The following apps are installed on the backup site but not available on this bench:
  • widgets
? Do you want to continue with the restore anyway? No
Restore cancelled.
[OK] MISSING-APPS WARNING PROVEN THROUGH THE CLI
```

## #6 - post-restore migrate + restart

Tail of the successful interactive restore:

```bash
✓ Successfully restored site 'development.localhost'
✓ Migrated site 'development.localhost'
Restarting instance...
✓ Instance restarted (logs: /tmp/bench-cwe2e6.log)
```

## Re-validation on the final code (after the no-mistakes run and CodeRabbit fixes)

Per the "re-run the real-instance E2E after no-mistakes and after any CodeRabbit
fixes" standard, the whole E2E was re-run against the final shipped code (the
no-mistakes review fixes - restart the restored bench, drop the unused recache -
the CodeRabbit fixes, and the `auto_enter=False` credential root fix). Crucially,
the confirm is driven the way a human types it: `y` THEN Enter. All green on real
Frappe v15 (`cwe2e6`) and v14 (`cwe2e6v14`):

```bash
# #3/#4 inspect (injection-safe positional-arg site probe):
    └── Sites (1)
        └── development.localhost (default)     # currentsite.txt excluded; default from it

# #2/#6 interactive normal restore (pty, y+Enter at the confirm): username prompt shown,
#        password collected, restore + bench migrate + instance restart all succeed.

# credential root fix (auto_enter=False) - interactive restore with --mariadb-root-username
#   as a FLAG and NO password flag (username prompt skipped) + habitual y+Enter: the
#   confirm consumes its own Enter, so the password prompt STILL collects input (this
#   case failed before the fix, when a tcflush drain could not reach the buffered Enter).

# non-interactive: --mariadb-root-username/--mariadb-root-password run with no prompt.

# #5 missing-apps (recache dropped): backup needing widgets, bench without widgets ->
MISSING (final code, no recache): ['widgets']

# #1 receive on v14: full-path bench restore succeeds, then migrate + restart.
✓ Successfully restored backup to site 'development.localhost'
✓ Migrated site 'development.localhost'
```
