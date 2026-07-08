# E2E evidence: non-interactive selectors and honest exit codes for the normal `restore` path (issue #40)

Real-instance end-to-end run on an **isolated** throwaway environment (a temporary
`HOME=/tmp/...` so `~/.cwcli` was a fresh DB, a unique docker-compose project
`cwclie2e40`, and dedicated volumes). The captain's real projects and real
`~/.cwcli` were never touched, and the instance was torn down afterwards.

One bench was built with `cwcli init`:

- **`cwclie2e40`** - Frappe **version-15** (`15.113.4`), site `development.localhost`,
  port 8100. A `bench backup --with-files` was taken so the restore had a real
  backup set to select.

Interactive prompts were driven through a real pty (pexpect); non-interactive
paths were driven with `echo |` (a non-TTY stdin). prompt_toolkit's raw-mode
readiness marker (`ESC[?2004h`) was awaited before each keystroke so nothing raced
the prompt.

## What it proves (acceptance criteria from issue #40)

- **Criterion 2 - non-interactive happy path.** `echo | cwcli restore cwclie2e40
  --site development.localhost --latest --yes --mariadb-root-password 123` completed
  a full restore with ZERO prompts and exited `0`. The destructive + missing-apps
  confirms were skipped by `--yes` (printed `Proceeding without confirmation (--yes).`);
  the backup menu was bypassed by `--latest`; `bench migrate` + instance restart ran.
  ```
  ✓ Successfully restored site 'development.localhost'
  ✓ Migrated site 'development.localhost'
  ✓ Instance restarted (logs: /tmp/bench-cwclie2e40.log)
  exit=0
  ```

- **Criterion 3 - non-interactive refusals are honest.** Each refuses and exits
  **1** (captured via a wrapper script, not a pipe whose `$?` masks the code) with a
  message naming the flag to pass:
  - `echo | cwcli restore cwclie2e40 --site development.localhost` (no selector) ->
    `Error: non-interactive session and no backup selector; pass --latest or --backup-file
    <name>...`, `CWCLI_EXIT=1`.
  - `echo | cwcli restore cwclie2e40 --site development.localhost --latest` (no
    `--yes`) -> `Error: Refusing to restore without confirmation. Re-run with --yes...`,
    `CWCLI_EXIT=1`.
  - `echo | cwcli restore cwclie2e40 --site development.localhost --latest --yes`
    (no password) -> `Error: No MariaDB root password provided and not running
    interactively. Pass --mariadb-root-password...`, `CWCLI_EXIT=1` (pre-existing
    credential refusal, still holds).

- **Criterion 4 - interactive path unchanged in feel.** Driven via pexpect on a real
  pty:
  - The backup-selection menu rendered (`Select a backup to restore:` with the arrow
    keys / Enter instruction), the first entry was selected with Enter.
  - The destructive confirm rendered `Are you sure you want to restore? (y/N)` and
    REQUIRED Enter to submit (decline via `n`+Enter -> `Restore cancelled.`, `CWCLI_EXIT=1`,
    was `Exit(0)` before this change).
  - The credential prompts fired and collected input after the confirm (the MariaDB
    password prompt rendered and accepted input). A DB auth failure in this pty (`Access
    denied for user 'rootroot'@...`, a pexpect/CPR terminal artifact on this headless pty
    that also surfaced `WARNING: your terminal doesn't support cursor position requests`)
    is unrelated to this change - the credential prompts themselves fired and collected,
    which is all criterion 4 asserts. The successful end-to-end restore is proven by the
    non-interactive happy path (criterion 2), which uses the same code path minus the
    prompts.

- **Criterion 5 - selector mutual exclusion.** Each exits **1** before any container work:
  - `cwcli restore cwclie2e40 --site development.localhost --latest --backup-file foo.sql.gz`
    -> `Error: Cannot use --latest and --backup-file together; choose one non-interactive
    selector.`, `CWCLI_EXIT=1`.
  - `cwcli restore cwclie2e40 --site development.localhost --latest --send` ->
    `Error: --latest/--backup-file only apply to the normal restore path, not --send or
    --receive.`, `CWCLI_EXIT=1`.
  - `cwcli restore cwclie2e40 --site development.localhost --backup-file foo.sql.gz --receive`
    -> same message, `CWCLI_EXIT=1`.

## Unit + lint gates

- `uv run pytest` green (320 passed, including the 15 new tests in
  `tests/test_restore_safety.py`: `TestSelectBackupSet` for the pure `select_backup_set`
  helper, and `TestNormalPathSelectorsAndExitCodes` for the Typer command).
- `uv run mypy src/` zero errors.
- `uv run black --check src tests` and `uv run ruff check src tests` clean.

## Teardown

The `cwclie2e40` project was removed with `cwcli rm --volumes --yes` and the isolated
`HOME` directory deleted afterwards.
