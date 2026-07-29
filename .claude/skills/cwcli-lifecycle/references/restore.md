# `restore` command internals (sharp edges)

Each note guards a real shipped bug. Keep the root-cause "why" so a later change does not silently re-break the fix.
`restore --receive` is the most destructive path in the codebase. This file also holds the shared site-detection / default-site helpers (`utils/bench_sites.py`), which `inspect` and `rm` also use.

> **MIGRATED onto the core (batch 11, `migrate-restore-core`, 2026-07-17).** Every logic/I-O note below now lives in **`core/restore.py`** as a plan/apply split (`restore_plan`/`receive_plan -> restore_apply`, a read/destroy safety separation); **`commands/restore.py` is a renderer**. The root-cause "why"s are unchanged - only the addresses moved. Seam map: `restore_receive_mode` -> frontend `_run_receive` over `core.receive_plan` + `core.restore_apply`; the normal path -> frontend `_run_normal` over `core.restore_plan` + gates + `core.restore_apply`; `_resolve_default_site`, `scan_backups_for_all_sites`, `check_missing_apps`/`_read_backup_installed_apps`, `select_backup_set`, `parse_backup_filename` -> `core/restore.py` (the frontend re-exports the public ones); `_post_restore_migrate_and_restart` -> `core.restore_apply`'s buffered migrate + `core.start(restart=True)` (a failed migrate is now a `WARNING` carrying `migrate_ok=False`, was a `False` return); the destructive/missing-apps confirms -> the shared frontend `_gate` + the core's `confirm_restore` consent parameter; the sendme subprocess + ticket prompt stay frontend (the `commands/logs.py` `-it` precedent). The secret env-transport (M5), the streamed copies (M4), the both-modes credential prompts, and the six restore+inspect bugs are all preserved byte-exactly - see the `restore` entry in the always-loaded `AGENTS.md` and the batch-11 section of `cwcli-core-axi` for the migration's design decisions. When editing, touch `core/restore.py` for logic and `commands/restore.py` for rendering; the notes below tell you WHY each piece behaves as it does.

### `restore` command: receive-mode data-safety semantics

`restore --receive` (`restore_receive_mode` in `commands/restore.py`) downloads a peer's backup over sendme and then runs `bench restore --force`, which drops and recreates the live default site's database.
That is the most destructive path in the codebase, and it used to run the instant the download finished with no "are you sure" gate (the only interactive prompt was the *conditional* missing-apps confirm, which fires only when apps are missing).

The receive path carries the same destructive-restore gate the normal path has, placed right before the `bench ... restore ... --force` command is built:

- It prints the `⚠ This will replace all data in site '{site}'` warning + the backup filename, then compares the backup's origin site (parsed from the database filename via `parse_backup_filename(...)["site_name"]`, which is already dots-to-underscores) against `transform_site_name_to_backup_format(site)` and prints an `⚠ Origin mismatch` line when they differ.
  A failed parse yields `origin_parsed is None`, so the mismatch check is skipped (fail-safe, no false alarm); this is the same parser that must already have matched to set `database_file`, so a hyphen-bearing site that the parser can't represent would have errored earlier.
- The confirmation honors the `--yes/-y` flag on `restore`: `--yes` proceeds without prompting, an interactive TTY asks `questionary.confirm("Are you sure you want to restore?")`, and a **non-TTY without `--yes` refuses and exits non-zero** rather than silently proceeding or exiting 0.
  This confirm exits **non-zero on any refusal** (declined confirm, non-TTY-no-yes, or Ctrl-C), as does the normal path (issue #40 closed the prior exit-0-on-cancel finding).
- `--yes` is threaded through both `restore_receive_mode(...)` call sites and BOTH confirm blocks on the normal path (the missing-apps "continue anyway?" and the destructive "are you sure?"). It bypasses every confirmation prompt on both paths; it does NOT remove the sendme-ticket prompt (receive path) or the `--mariadb-root-password` / `--mariadb-root-username` credential prompts, which already meet the standard via `_prompt_mariadb_credentials`.
- **The sendme ticket meets both-modes via `--ticket` (`_run_receive`):** `--ticket <sendme-ticket>` supplies the ticket non-interactively (prompt skipped); an interactive TTY without it prompts; a **non-TTY without `--ticket` refuses with exit 1 naming `--ticket`** (mirrors the `_prompt_mariadb_credentials` non-TTY refusal). This closed a prior silent no-op: `questionary.text(...).ask()` returned `None`/empty on a non-TTY, the code printed "Receive cancelled." and `return`ed -> exit 0 with no restore, so a caller believed the restore ran. An interactive cancel (empty ticket) now also exits 1, never 0. The prompt/refuse stays frontend because the sendme subprocess does. Coverage: `tests/test_restore_safety.py::TestReceiveTicket` (non-TTY refuse, `--ticket`-supplied, interactive-cancel non-zero) + the E2E `test_receive_non_tty_without_ticket_refuses`.

Two supporting fixes travel with it, and BOTH the receive path and the normal restore path share the same shapes:

- **Secrets off the argv (M5):** the MariaDB root password (and admin password) are no longer interpolated into the command string. They are put in a `restore_env` dict and referenced as `"$CWCLI_MARIADB_ROOT_PASSWORD"` / `"$CWCLI_ADMIN_PASSWORD"`, and the command is executed as `frappe_container.exec_run(["sh", "-c", cmd], workdir=..., environment=restore_env)`.
  Passing the list form `["sh", "-c", cmd]` (rather than a bare string, which docker-py would `shlex.split` and exec directly) is REQUIRED so the shell actually expands the `$VAR`; the secret then never appears in the command the container process list shows.
  cwcli's own verbose masking (`cmd.replace(password, "***")`) only hid it from cwcli output, not from `ps`/`docker top`/exec-inspect - that masking is gone because the secret is not in `cmd` at all.
- **Streamed tar copy (M4):** the file-copy loop passes the open tar file handle straight to `frappe_container.put_archive(backup_dir, tar_file)` (docker-py streams it) instead of `tar_file.read()` (which slurped a multi-GB `--with-files` backup into host RAM), and it checks `put_archive`'s bool return and `raise typer.Exit(1)` on failure instead of ignoring it and surfacing a confusing "backup not found" later.

Regression coverage is in `tests/test_restore_safety.py`: it drives `restore_receive_mode` with a `FakeReceiveContainer` that records every `exec_run` (command, workdir, environment) and asserts a declined/non-TTY confirm does NOT run `bench restore --force` and exits non-zero, `--yes` proceeds, an origin mismatch is surfaced, and the DB password rides in `environment=` (never in the recorded argv).
Testing note: `restore_receive_mode` is a plain function (not the Typer command), so tests call it directly with all args explicit; stub `TipSpinner` to a no-op (it starts a Rich spinner even with `enabled=False`) and fake `subprocess.run` to write the "downloaded" backup into its `cwd`.

**`_run_receive`/`_run_send` run `sendme` from a managed directory under `cwcli_home()`, never the system temp dir.**
sendme creates its on-disk blob store in whatever directory it is invoked from (`.sendme-recv-<hash>/` in the CWD; verified against sendme 0.36.0's own source and README).
`_run_receive` previously used `subprocess.run(..., cwd=temp_dir)`, with `temp_dir` created by a bare `tempfile.TemporaryDirectory()` in the system temp dir.
`_run_send` also staged files in a bare `TemporaryDirectory()`, but its `subprocess.Popen(...)` had no `cwd`, so sendme inherited the caller's working directory instead.
On a host where either filesystem is tmpfs (RAM-backed) or otherwise small, a multi-GiB backup can fill it mid-transfer; sendme's store write then fails (ENOSPC-class), which iroh-blobs surfaces not as the real I/O error but as the cryptic `error sending over irpc: Receiver closed`.
This failure was reproduced byte-identically by capping the writable file size during a real receive.
`commands/restore.py:_sendme_download_root()` now anchors the store at `cwcli_home() / "tmp"` (honors `CWCLI_HOME`, same disk as the rest of cwcli's footprint and follows the `rm` archive-dir precedent), and `_check_receive_free_space` refuses BEFORE starting any download when that location has under a fixed 2 GiB floor free (sendme reveals a collection's real size only after connecting to the sender, so a floor is the best pre-transfer guard available).
On a sendme failure, `_explain_sendme_failure` matches the `Receiver closed`/`No space left` signature and swaps the generic `Failed to download files via sendme` headline for one naming the download directory and current free space, while still surfacing sendme's raw stderr underneath (unrelated failures, e.g. a malformed ticket, keep the old generic message unchanged).
Partial-download cleanup needs no new code: the per-attempt `tempfile.TemporaryDirectory(dir=...)` already removes its tree on any exit path (including a raised `typer.Exit`), so a failed receive leaves nothing behind under the new root.
Coverage: `tests/test_restore_sendme_download_location.py` (download-root location, preflight refusal/pass, error-signature translation vs. the generic-message fallback, and post-failure cleanup).

### `restore` command (normal path): non-interactive selectors + honest exit codes (issue #40)

The normal (non `--send`/`--receive`) `cwcli restore` path is fully drivable by an agent or script. Every prompt has a flag, and a non-TTY without the needed flag refuses with a NON-ZERO exit instead of silently exiting 0 (the prior "exit-0-on-cancel" finding is closed).

- **Backup selection.** `--latest` non-interactively selects the newest backup set for the target site; `--backup-file <filename-or-full-path>` selects one by its database file. The selector bypasses the `questionary.select` menu. `--latest` does NOT fall through to other-site backups (that would silently restore a different site's data); `--backup-file` searches both target and other backups (the caller named a specific file). The two flags are mutually exclusive, and neither applies to `--send`/`--receive` (validated up front). The resolution happens in `select_backup_set(target_backups, other_backups, *, latest, backup_file)` - a pure helper near `display_backup_selection_menu` - so it is unit-testable with no TTY or container.
- **Non-TTY guard.** A non-TTY with neither selector exits 1 (message names `--latest`/`--backup-file`) rather than reaching `questionary.select().ask() -> None` on a non-TTY and exiting `0` having done nothing.
- **`--yes` widened to the normal path.** Both normal-path `questionary.confirm` prompts honor `--yes`: `--yes` proceeds without prompting, a non-TTY without `--yes` refuses with exit 1, an interactive TTY asks. A declined confirm or Ctrl-C exits 1 (was `Exit(0)`) on BOTH paths.
- **The sentinel** `{"_restore_from_ticket": True}` (remote restore via sendme, reached from the menu) stays INSIDE the interactive branch only; the selector branch can never produce it (correct: remote restore is reached via `--receive`).
- Both confirms pass `auto_enter=False` (load-bearing - they must consume their own trailing Enter so the keystroke does not leak into the following credential prompt). `commands/utils.py:confirm_or_exit` omits that setting, so it is NOT used here; the local prompt code is kept.
- Credentials fire AFTER the confirms via `_prompt_mariadb_credentials` (unchanged, already meets the standard); do not reorder.

Regression coverage is in `tests/test_restore_safety.py` (`TestSelectBackupSet` for the pure helper, `TestNormalPathSelectorsAndExitCodes` for the Typer command). The Typer-command tests call `restore_mod.restore(...)` with ALL params explicit (omit any `Option` and its truthy default object leaks through - the same trap flagged for `inspect`); the `@handle_docker_errors` decorator is bypassed by patching `docker_utils`'s `shutil`/`docker` (the decorator reads them from its own namespace; `restore_mod` does not import them directly).
The authoritative repros and the real-instance E2E evidence (both selector happy paths, the non-interactive refusals, the interactive menu decline, and the mutual-exclusion guards) are in `docs/e2e/restore-noninteractive-h2.md`.

### `restore` + `inspect`: currentsite, default site, receive path, credential prompts, missing-apps, migrate+restart

Six bugs the captain hit on a live 0.33.0 session (Frappe `version-14`, site `development.localhost`) were fixed together.
The authoritative repros and the real-instance E2E evidence are in `docs/e2e/restore-inspect-e2e-r6.md`.

#### Site detection is shared (`utils/bench_sites.py`)

`bench_sites.list_sites(container, bench_path)` is the single canonical "what are the real sites" implementation, used by BOTH `core/rm.py:_list_sites` (which delegates; moved off `commands/rm.py` by `migrate-rm-core`) and `core/inspect.py:_get_sites` (moved off `commands/inspect.py` by `migrate-inspect-core`).
A real Frappe site is a DIRECTORY containing `site_config.json`; detection probes for that per entry rather than denylisting known non-site names.
It is fail-safe: an entry is excluded only on a positive NOTASITE (a non-directory, or a readable dir with no `site_config.json`); anything ambiguous (probe error, unreadable dir) is treated as a site.
This replaced `inspect._get_sites`' old denylist, which did NOT list `currentsite.txt` (a plain file written by `bench use`), so inspect reported it as a site and then errored `bench --site currentsite.txt list-apps -> "Site currentsite.txt does not exist!"`.
`bench_sites.read_current_site(container, bench_path)` reads `sites/currentsite.txt` (the default-site pointer), failing safe to `None`.
The probe string in `list_sites` is byte-identical to the one rm's tests already assert, so rm's fakes stay green; `tests/test_inspect_partial_refresh.py`'s fake gained the same SITE/NOTASITE probe handling.

#### Default site resolves from EITHER source (`currentsite.txt` OR `common_site_config.json`)

A bench records its default site in `common_site_config.json`'s `default_site` OR `sites/currentsite.txt`; a plain `bench use`d dev bench only has the latter (the captain's `ners` had no `default_site` key at all).
`Bench.current_site` is a nullable column (idempotent `_migrate_bench_current_site_column`, mirroring the `label` migration) populated by a full inspect from `currentsite.txt`, carried forward by the T2 partial pass, and surfaced by `get_cached_project_data`.
`db_utils.get_default_site` returns `common_site_config.default_site` if set, else the cached `current_site` (via `get_current_site`) - so `restore`/`backup`/`unlock` all resolve the default even with no `default_site` key.
`inspect`'s tree marks `(default)` from either source too.
`restore.py:_resolve_default_site` adds a LIVE `currentsite.txt` read as a final fallback for the destructive restore path (a cold/stale cache still resolves correctly); it is used by both the normal and receive paths in place of the bare `get_default_site`.

#### `--receive` restore: full container path, not the bare filename

Receive-mode built `bench restore <bare-filename>` with `workdir=backup_dir`; on Frappe `version-14` bench rejects that with `Invalid path <db filename>` (data restore broken).
The fix passes the FULL container path `{backup_dir}/{database_file.name}` and reorders the args to exactly match the normal path (db path, credentials, `--force`, then `--with-public-files`/`--with-private-files`).
On Frappe `version-15` bench has a "trying alternative directories" fallback that masks the bare-filename bug, so this only reproduces on v14 - which is why the E2E built a v14 bench (`docs/e2e/`).

#### Credential prompting works in BOTH modes (`_prompt_mariadb_credentials`)

Shared by the normal and receive paths.
Interactive (a TTY): prompt for the username (default `root`, blank keeps `root`) AND the password (blank is an error).
Non-interactive (flags / non-TTY): the username defaults to `root`; the password is a required secret, so a non-TTY WITHOUT `--mariadb-root-password` refuses with a non-zero exit rather than hanging or proceeding empty.

The interactive "empty password" bug and its ROOT fix (sharp edge, worth remembering for ANY questionary confirm followed by another prompt):
`questionary.confirm(...)` with the default `auto_enter=True` submits on the `y`/`n` keypress and leaves the user's habitual trailing Enter in prompt_toolkit's INTERNAL input buffer.
That stray Enter is then read by the immediately-following prompt as an empty submit, so the password prompt returned empty (`Error: Password cannot be empty.`).
A `termios.tcflush` stdin drain was tried and proven INEFFECTIVE: `FIONREAD` shows the OS tty queue is empty (the Enter is inside prompt_toolkit, not the OS queue), so a tcflush reaches nothing - it was removed.
The fix is `auto_enter=False` on EVERY restore confirm (both the destructive `Are you sure you want to restore?` and the missing-apps `continue anyway?`, on both paths): with `auto_enter=False` the confirm REQUIRES Enter to submit and thus CONSUMES its own trailing Enter, so nothing leaks into the next prompt - robust regardless of which credential flags are set.
(An earlier partial mechanism - the username prompt absorbing the stray Enter - only worked in the default flow where the username prompt runs; it silently failed for `--mariadb-root-username <flag>` + habitual `y`+Enter, which is why a realistic pty E2E must press `y` THEN Enter.)

#### Missing-apps warning reads the BACKUP's apps (`check_missing_apps` + `_read_backup_installed_apps`)

The warning regressed to silence because `check_missing_apps` read `sites/{site}/apps.json`, which Frappe does NOT write per site (it lives at the bench level, `sites/apps.json`), so the `cat` always failed and it returned `[]`.
The correct question is "does the BACKUP need apps this bench lacks" (the captain's words), not "what does the about-to-be-overwritten site have" - and a site whose app code is already missing cannot even be listed by `bench list-apps` (the import crashes).
`_read_backup_installed_apps` `zcat -f`s the backup's DB dump and greps the `installed_apps` global (`...,'["frappe","widgets"]','installed_apps',...` - exactly what `frappe.get_installed_apps()` reads), extracting the app-name tokens.
`check_missing_apps(frappe_container, project_name, bench_path, backup_db_path, ...)` (the `site` arg became `backup_db_path`) compares those against the bench's apps read LIVE from `ls {bench_path}/apps`, and returns `backup_apps - available_apps`.
It fails safe: an unreadable dump / absent marker yields `None` -> no warning (never a false positive).
Both call sites pass the backup's container path (`selected_backup["database"]["full_path"]` on the normal path; `{backup_dir}/{database_file.name}` on the receive path).
Because both sides are read LIVE (the backup's dump and `ls apps`), the check no longer touches cwcli's cache, so `check_missing_apps` dropped its `cache.recache_project` call and `--no-recache` became a DEPRECATED no-op - the flag (and its `no_recache` param) is kept only for backward compatibility with existing callers.

#### Post-restore: migrate then restart (`_post_restore_migrate_and_restart`)

After a successful `bench restore`, both paths run `bench --site <site> migrate` then restart the instance (via `_start_project`, which kills the old `bench start` and relaunches it - the same app restart `cwcli restart` does).
The restart targets the SAME bench that was just restored via `_start_project`'s `bench_path_override` (used verbatim, bypassing the `resolve_bench_path` guessing), so a multi-bench restore into a non-first bench restarts the right dev server, never bench 0.
A failed migrate does NOT undo the restore; it is surfaced and the restart still runs, but the function returns False so the caller exits non-zero.
A Docker/API exception raised by the migrate `exec_run` is treated as a failed-but-reported migrate (`exit_code=1`) that STILL continues to the restart, so an exec error can never skip bringing the instance back up.
`--no-migrate` skips the whole post-restore step.
The restart now goes through `core.start`'s web-readiness wait (see `start-status.md`); a `start.web_not_ready`
timeout warning is forwarded onto the result rather than swallowed - a restored site that never begins serving
is exactly the kind of thing this safety-critical path must not hide.

Regression coverage: `tests/test_restore_inspect_fixes.py` (all six) and `tests/test_bench_labels`-style DB tests; `tests/test_inspect_partial_refresh.py` and `tests/test_restore_safety.py` fakes were updated for the shared site probe and the `no_migrate` param.
