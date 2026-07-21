# Tasks - `cwcli axi rm`

## 1. The verb

- [x] 1.1 `commands/axi.py`: `axi_rm(project, --yes, --volumes/--no-volumes)` over the UNCHANGED `core.remove`.
- [x] 1.2 `_rm_narrate`: `RmStep`/`RmNotice`/`RmWarning`/`RmError` to stderr; `RmTrace` dropped (no `--verbose` here).
- [x] 1.3 Consent guard: no `--yes` -> `USAGE` exit 2, naming the flag and what it deletes.
- [x] 1.4 Stopped-project guard: `NOT_RUNNING` exit 1 BEFORE the core call, naming all three ways out. Scoped to STOPPED; an orphan goes to the core.
- [x] 1.5 Blocked-gate warning naming `cwcli rm <project> --no-backup`, since the core's hint names a flag this surface lacks.
- [x] 1.6 Exit code from `outcome.failures`.

## 2. Tests

- [x] 2.1 Reverse `TestNoAxiRmVerb` -> `TestAxiRmVerbShipped` in `tests/test_core_rm.py`, carrying the reversal's reasoning.
- [x] 2.2 New `tests/test_axi_rm.py`: consent, no-auto-start, gate-always-on, the three refusals' actionability, exit codes, stdout/stderr split.
- [x] 2.3 Regenerate `skills/cwcli/SKILL.md` (`tests/test_axi_skill.py` blocks a stale one).

## 3. Real-instance validation (the standing rule for delete paths)

Run against the worktree's own editable install (`uv run cwcli`), isolated `CWCLI_HOME`, one throwaway instance at a time. Never mocks, never a published build.

- [x] 3.1 Real `cwcli axi init` of a throwaway instance.
- [x] 3.2 Seed a real record and confirm it reads back.
- [x] 3.3 Both refusals on the real instance, confirming nothing was touched.
- [x] 3.4 `cwcli axi rm --yes`: exit 0, one TOON document.
- [x] 3.5 Deletion is HONEST: containers, named volumes, project dir and cache entry all actually gone.
- [x] 3.6 Backup is genuinely RESTORABLE: restored into a fresh instance, seeded record present.
- [x] 3.7 Evidence written to `docs/e2e/axi-rm-destructive-lifecycle.md`.

## 4. Docs

- [x] 4.1 `CLAUDE.md`: the `rm` entry's "NO `axi rm` verb" assertion replaced with the shipped verb and its two decided properties.
- [x] 4.2 `.claude/skills/cwcli-core-axi/SKILL.md` and `cwcli-lifecycle`: same reversal where they assert the absence.
- [x] 4.3 `README.md`: the verb in the `axi` surface list.
