## 0. Gate: captain decisions before implementation

- [x] 0.1 Captain picks the auto-inspect verb model (design Decision 2: A1 fused / A2 systemctl / A3 minimal) and the settings access model (Decision 3: B1 bespoke + show / B2 git-style get-set / B3 edit rider in or out), and folds in his own pain points. Update the proposal table to the picked shapes before any code.
  NOTE (2026-07-16): captain approved with A1 (fused desired-state verbs), B1 (bespoke verbs + `config show` as the single read), and the `config edit` rider IN, with no additional notes. These are exactly the shapes the committed proposal table already shows, so no table rewrite was needed.

## 1. Characterization first: surviving behavior pinned green BEFORE anything moves

- [ ] 1.1 Characterization tests, green against UNMIGRATED code, committed separately (the batch 4 discipline), for everything the proposal table marks unchanged or frozen: cache clear single-project semantics, `--all --yes` and the non-TTY refusal (exit 1), `cache list` output, `auto-inspect status` fields, `logs -n` tail, `stop` idempotency, `tips enable/disable`, and each frozen alias's stdout + exit codes (`start` refuse-when-disabled above all, since installed boot units rely on it).
- [ ] 1.2 Record the driven evidence for the behavior deltas (F3 partial mutation, F4 conflicting clear, F9 garbage add-path, F10 exit codes) as failing-today assertions so the fixes flip them green.

## 2. The core slice

- [ ] 2.1 `core/config.py`: `ConfigReport`/`SearchPathChange`/`TipsState`/`CachedProjectDTO`/`CacheClearOutcome` DTOs (frozen, plain data) + `show_config`, `add_search_path` (expanduser, absolute check -> `CwcliError(USAGE)`, normalization before dedup), `remove_search_path`, `set_tips`, `cached_projects`, `clear_cache` (both/neither target -> `USAGE`; all without consent -> `NEEDS_CHOICE` `confirm_clear`).
- [ ] 2.2 `core/auto_inspect.py`: `AutoInspectState`/`AutoInspectOutcome`/`LogTail` DTOs + `enable` (validate-all-first, single write, start, hook sync - the F3 fix is structural), `disable` (stop + flag + hook removal, actions reported on the DTO), `stop`, `status`, `log_tail`; mechanics stay in `utils/auto_inspect.py`/`utils/startup.py`, failures surface as `CwcliError(INTERNAL)`. The two Known-hazards entries are NOT touched.
- [ ] 2.3 `tests/test_core_config.py` + `tests/test_core_auto_inspect.py`: every branch, the `NEEDS_CHOICE` fork, asdict-is-plain-data, "the core prints nothing at all"; all against a `tmp_path` `CWCLI_HOME`, never the real one.

## 3. The frontend rework

- [ ] 3.1 `commands/config.py` thins to renderers per the (captain-picked) proposal table: `show [--json]`, `paths`/`paths add`/`paths remove [--json]`, bare-path `path`/`cache path`, `cache clear` target validation (exit 2 rows), `cache list --json`, `auto-inspect enable/disable/stop/status [--json]/logs` (exit-1 fix), `tips enable/disable`, optional `edit`.
- [ ] 3.2 Frozen aliases registered `hidden=True` with the stderr deprecation line (wording mirrors `cwcli update`'s), byte-identical behavior via the utils they call today; the `add-path`/`remove-path` validation exception applied and tested.
- [ ] 3.3 Interactive + non-interactive both verified for the one prompting subcommand (cache clear `--all`): pty-driven confirm (accept and decline, awaiting the raw-mode marker) and the non-TTY `--yes`/refusal pair, per the captain standard.

## 4. The axi verb and the cross-cutting shell

- [ ] 4.1 `cwcli axi config` in `commands/axi.py`: one TOON document from `ConfigReport`, exit 0/1, no `--json`, no mutating flags; `tests/test_axi_config.py` covers the document shape (via `assert_is_one_toon_document`), the exit mapping, and the registry assertion that no config-mutating verb exists.
- [ ] 4.2 Regenerate `skills/cwcli/SKILL.md` via `scripts/build_skill.py` (never hand-edit); `tests/test_axi_skill.py --check` stays green.

## 5. Docs and ledger

- [ ] 5.1 README: rewrite the `config` sections to the new surface, with the deprecation table (old -> new -> removal horizon) and the boot-unit note; `config show --json` and `axi config` examples.
- [ ] 5.2 CLAUDE.md ledger entry + `cwcli-core-axi` skill note (new core modules, the aliases-bypass-core decision, the axi deferral); `tests/README.md` coverage map; `docs/technical/README.md` module list.

## 6. Suite health and E2E

- [ ] 6.1 §1 characterization green UNCHANGED against the reworked code (minus the deltas named in 1.2, which flip by design); full fast tier green; black, ruff, mypy at zero.
- [ ] 6.2 Host-side E2E under an isolated `CWCLI_HOME` (no Docker needed): the show/paths/cache/tips surface end to end, both modes for the prompting path, alias stdout byte-checks, `$(cwcli config path)` substitution-safety at width 40.
- [ ] 6.3 One throwaway real instance (`cwe2e-` prefix, per skill `cwcli-e2e-testing`) ONLY for the auto-inspect daemon cycle, since it genuinely touches instances: `enable` -> daemon inspects the running project -> cache populated -> `stop`/`disable` teardown honesty; against the worktree's editable install; torn down at the end.
- [ ] 6.4 Re-run the E2E after the no-mistakes pipeline and any review fixes (captain standard).

## 7. Explicitly deferred, so it cannot be misread as forgotten

- [ ] 7.1 Removal of the frozen aliases: not before 1.0 and no earlier than two minors after this ships; file the follow-up when this merges.
- [ ] 7.2 Mutating `axi config` verbs and `axi config cache list`: deliberately not built (design Decision 6); revisit only on agent-need evidence.
- [ ] 7.3 The Known-hazards auto-inspect entries (PID reuse, SIGTERM re-entry): stay on the board, their own review when picked up.
