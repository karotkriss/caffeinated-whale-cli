# E2E evidence: `restore` on the UI-pure logic core (batch 11, `migrate-restore-core`)

Full-lifecycle real-instance proof that the plan/apply migration of cwcli's most
destructive path (`restore_plan`/`receive_plan -> restore_apply`) preserves genuine
restore behavior end to end, in BOTH modes, per the captain's dangerous-delete
standard.

Unlike the earlier manual `restore-*-r6`/`-h2` runs, this batch's E2E is
**codified as a permanent, CI-run harness test**: `tests/e2e/test_restore_e2e.py`
(the `e2e` marker tier, driven against a genuine throwaway Frappe instance by
`tests/e2e/harness.py`). It runs against the worktree's OWN editable install
(`uv run pytest tests/e2e -m e2e`), never mocks and never a published build.

## Isolation

All isolation comes from `conftest.py`'s session fixtures: a temporary `HOME` +
`CWCLI_HOME` (so `~/.cwcli` is never touched), the mandatory `cwe2e-` project-name
prefix enforced by `harness.enforce_isolation()`, and the unconditional
`sweep_cwe2e` teardown backstop. The captain's real projects and real `~/.cwcli`
are never touched; the instance is torn down with `cwcli rm` (+ the sweep) at the
end. Memory-serialized against the sibling `rm` migration crew (only one real
bench init at a time on the 11 GB box).

## The genuine-restore assertion (cache-free)

A throwaway DB table `cwe2e_marker` is the marker, read via raw `frappe.db.sql`
so no Redis/defaults cache can mask a stale read:

1. seed `cwe2e_marker = "ORIGINAL"`, 2. `cwcli backup` (the dump captures
`ORIGINAL`), 3. mutate the live table to `"MUTATED"`, 4. sanity-assert the live DB
holds `MUTATED`, 5. `cwcli restore --latest`, 6. assert the table reads `ORIGINAL`
again (never `MUTATED`). Because `bench restore --force` DROPS and recreates the
DB from the dump, a genuine restore returns the backup's state; a no-op would
leave `MUTATED`.

## What it covers

| Test | Path | Asserts |
| --- | --- | --- |
| `test_restore_noninteractive_is_genuine` | normal, non-interactive (`--latest --yes --mariadb-root-password`) | the marker round-trips ORIGINAL->MUTATED->ORIGINAL; "Successfully restored"; the site boots after migrate + restart |
| `test_restore_no_migrate_still_restores` | normal, `--no-migrate` | the restore is genuine but the migrate + restart are skipped |
| `test_restore_interactive_is_genuine` | normal, **real pty** | the `select_backup` menu (Enter selects the newest), the destructive confirm (`y`+Enter, `auto_enter=False`), and the MariaDB username/password prompts all drive a genuine restore; `ESC[?2004h` awaited before each keystroke |
| `test_non_tty_without_selector_refuses` | normal, non-TTY | no `--latest`/`--backup-file` on a non-TTY refuses (exit != 0), naming the flags |
| `test_non_tty_without_password_refuses` | normal, non-TTY | a selector + `--yes` but no `--mariadb-root-password` refuses rather than hanging |
| `test_mutually_exclusive_flags` | flag guards | `--send`+`--receive`, `--latest`+`--backup-file`, and selector+`--send` each exit non-zero |

Both the interactive and non-interactive destructive-confirm paths, the
non-interactive selectors, secrets off the argv (the `--mariadb-root-password`
rides `exec_run(environment=)`), and post-restore migrate + restart are exercised
against real Docker, confirming the migration is behavior-preserving.

## Result

`uv run pytest tests/e2e/test_restore_e2e.py -m e2e` - Frappe **version-16**, site
`development.localhost`, all 6 tests PASSED (see the branch's E2E run log). Re-run
after the no-mistakes pipeline and any review fixes, per the captain standard.
