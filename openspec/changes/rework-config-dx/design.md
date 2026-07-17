# Design: rework-config-dx

## Decision 1: ONE change carrying DX rework + core migration together

**Position: one change.** The alternatives were argued, not assumed:

- **Stacked, migrate-first (rejected).** The house pattern (batches 1-8) is refactor-under-green: migrate byte-identical, prove nothing changed. That pattern is meaningless here because the green is the thing being replaced - the captain has already ruled today's behavior wrong. Migrating first means building core functions for ten auto-inspect verbs (bugs included: the F3 partial mutation would be faithfully ported) and then deleting or reshaping most of them days later. Throwaway core surface, double review.
- **Stacked, rework-first (rejected).** Reworking DX on the monolith writes a new UX into `commands/config.py` that the migration batch immediately rewrites - exactly the "reworking the old monolith twice" the standing intent forbids.
- **One change (taken).** The migration discipline transplants cleanly: characterization tests pin only the behavior that SURVIVES (frozen aliases, cache clear semantics, logs tail, tips enable/disable), and the proposal's before -> after table is the review contract for everything that changes. Precedent for a batch carrying disclosed behavior deltas: batch 8's `offer_choice=False` hardening, and `add-per-process-supervisor` reversing a locked decision with captain approval. The skills' warning against "a behaviour change wearing a migration's clothes" is about UNDISCLOSED deltas; here the deltas are the headline and the migration rides along.

The risk of combining is review size.
Mitigation: config is host-side only (no Docker, no benches, no resolvers), so the core slice is small, and the tasks are ordered so the characterization net lands green before anything moves.

## Decision 2 (CONTESTED - captain picks): the auto-inspect verb model

The friction: ten verbs over three state stores (TOML flag, live daemon, OS boot unit), the enable/start two-step, and three spellings for the boot hook.
Three shapes, sketched for a pick rather than accept-or-reject:

**A1 - fused desired-state verbs (recommended, and what the proposal table shows).**
`enable [--interval N] [--startup/--no-startup]` = write config + start daemon + sync boot hook; `disable` = stop + disable + remove hook; `stop` stays as the one daemon-only verb (pause until boot); `status`, `logs` unchanged.
10 verbs -> 5 visible.
Rationale: "enabled but not running" is the state users never want (the current `enable` hint text proves the tool knows it), and an idempotent `enable` subsumes `restart` and `set-interval` for free.
Cost: `enable` and `disable` widen semantics under their existing names (disclosed in the proposal table); a script calling today's `enable` now also gets a daemon. The follow-up `start` such a script would issue becomes a no-op-equivalent (already-running, exit 0), so the observable end state is identical.

**A2 - the systemctl model.**
`enable`/`disable` = persistence only (config flag + boot hook), `start`/`stop` = now, `enable --now` = both; `start` no longer refuses when disabled.
Rationale: a familiar, standard idiom; keeps "run once now without persisting" expressible.
Cost: keeps 7 verbs and keeps the two-store split as something the user must understand; `start`-no-longer-refusing changes the semantics the installed boot units rely on (`ExecStart=... auto-inspect start` honoring `enabled=false` is what makes a stale hook inert), so the boot unit would need to move to a new `start --if-enabled` spelling across a deprecation window.

**A3 - minimal bugfix-only.**
Keep all ten verbs; fix only F3 (validate-before-write), F4, F9, F10, add `show`/`--json`/`axi config`.
Rationale: zero script or boot-unit risk, smallest diff.
Cost: the clunk the captain named - the two-step, the triplicated boot-hook spelling, the ten-verb sprawl - all survive.

A1 is recommended because the fused pair is the only shape that deletes the two-step rather than documenting it.
If the captain picks A2 or A3, the proposal table rows for `enable`/`disable`/aliases change accordingly; the rest of the change is shape-independent.

## Decision 3 (CONTESTED - captain picks): settings access model

**B1 - bespoke verbs + `config show` (recommended, and what the proposal shows).**
Every setting is set through the verb that owns its feature (`tips enable/disable`, `auto-inspect enable --interval`, `paths add/remove`), and `config show` is the single read.
Rationale: the config file has FIVE leaf keys, two of them side-effectful (`auto_inspect.enabled` starts/stops a daemon, `startup_enabled` touches OS boot units).
A git-style key engine for that is machinery without a payload, and a generic `set auto_inspect.enabled true` would either silently skip the side effects or surprise by performing them.

**B2 - git-style `config get/set/unset <dotted.key>` + `show`.**
Uniform, scriptable, scales with new keys; side-effectful keys refuse `set` and point at their verb.
Cost: a key-path parser, a type-coercion/validation table, and a refusal list, all for two purely-scalar keys today (`interval`, `show_tips`).
Revisit trigger, recorded: if the config grows past roughly eight scalar keys, B2 becomes the right shape and `show` already establishes the dotted names it would use.

**B3 - `config edit` (rider, compatible with either).**
Open the TOML in `$EDITOR` (`click.edit`); the power-user escape hatch, a few lines.
Included in the proposal as an optional row; drop it costlessly if unwanted.

## Decision 4: deprecation and back-compat policy

- **Frozen aliases, byte-identical behavior.** Every moved or subsumed verb (`add-path`, `remove-path`, `start`, `restart`, `set-interval`, `install-startup`, `uninstall-startup`, `tips status`) stays registered with EXACTLY today's semantics, including today's refusals and exit codes. New semantics live only under the surviving names, so no script silently changes behavior.
  The one disclosed exception: `add-path`/`remove-path` gain the same input validation as their new home, because storing `not/absolute/../weird` was never a behavior worth preserving (F9) - a script feeding garbage now gets exit 2 instead of a silently useless config entry.
- **`start` is load-bearing beyond scripts.** Already-installed boot units on user machines exec `cwcli config auto-inspect start` verbatim (`utils/startup.py:185` systemd, `:282` schtasks, and the launchd plist), and its refuse-when-disabled guard is what keeps a stale hook inert after `disable`. The alias therefore keeps BOTH the argv and the guard until a release whose `enable --startup` has rewritten units in the wild for at least two minors.
- **Hidden, not `[DEPRECATED]`-labeled.** Aliases carry `hidden=True` (dropped from `--help`) plus a one-line stderr warning naming the replacement, mirroring `cwcli update`'s warning wording. This deliberately diverges from the visible `cwcli update` precedent: that is a top-level command where visibility aids discovery; these are eight group-internal renames whose visible presence would defeat the de-clutter that motivates the change. Warnings go to stderr only, so no parsed stdout changes shape.
- **Removal horizon:** not before 1.0, and no earlier than two minor releases after this change ships, whichever is later. Tracked as a follow-up task, not a silent TODO.
- **Aliases bypass the core.** They keep calling `config_utils`/`auto_inspect`/`startup` exactly as the monolith did. Building `Result`-shaped core API for verbs scheduled for deletion is waste, and when the aliases die nothing has to be unpicked from `core/`. The utils remain the storage/process layer the core itself uses, so there is still exactly one implementation of each underlying operation.

## Decision 5: the core surface

Per the `cwcli-core-axi` conventions (no `rich`/`questionary`/`typer` imports - the AST ban in `tests/test_core_envelope.py` polices new modules automatically; return `Result[T]` or raise `CwcliError`; DTOs are plain data; the core never prints, prompts, or exits):

- **`core/config.py`**: `show_config() -> Result[ConfigReport]`; `add_search_path(path)` / `remove_search_path(path) -> Result[SearchPathChange]` (validation and normalization live HERE, one implementation for verb, alias, and any future GUI); `set_tips(enabled) -> Result[TipsState]`; `cached_projects() -> Result[list[CachedProjectDTO]]`; `clear_cache(project=None, *, all_projects=False, consent=False) -> Result[CacheClearOutcome]`.
  `ConfigReport` carries `config_file: str`, `cache_db: str`, `search_paths: list[str]`, `auto_inspect: AutoInspectState`, `show_tips: bool` - all plain data, `asdict`-safe.
  A non-absolute path is `CwcliError(USAGE)`; an already-present add or already-absent remove is `OK` with a `changed=False` DTO field (idempotent per the AXI standard, exit 0 on both surfaces).
  `clear_cache` with both or neither target is `CwcliError(USAGE)`; `--all` without consent returns `NEEDS_CHOICE` (`confirm_clear`) - the fused-consent lesson from `apps uninstall` applied from day one: the core takes consent as a parameter, and `--yes` stays one frontend's UX spelling.
- **`core/auto_inspect.py`**: `enable(interval=None, at_boot=None) -> Result[AutoInspectOutcome]` (validate interval FIRST, then write once, then start, then sync the hook - the F3 fix is structural: nothing persists until every input is validated); `disable() -> Result[AutoInspectOutcome]`; `stop() -> Result[AutoInspectOutcome]`; `status() -> Result[AutoInspectState]`; `log_tail(lines) -> Result[LogTail]`.
  `AutoInspectState` separates the three stores explicitly: `enabled` (config), `daemon_running` + `daemon_pid` (process), `boot_installed` (OS) - the F2 legibility fix carried into the DTO so every frontend renders it honestly.
  The daemon spawn/kill and boot-unit mechanics stay in `utils/auto_inspect.py` / `utils/startup.py`, called by the core; a mechanics failure surfaces as `CwcliError(INTERNAL)` with the util's message as detail.
  The two Known-hazards entries (PID reuse, SIGTERM re-entry) live in those untouched utils and are NOT absorbed here, per the board's own discipline: a signal-path behavior change deserves its own review.
- **No new primitives.** No resolvers, no Docker, no exec-stream; the envelope and error types cover everything. This is the falsifiable claim, batch-N edition: a bend gets reported, not absorbed.

## Decision 6: the `axi` surface

**One read verb: `cwcli axi config`**, emitting `ConfigReport` as one TOON document (settings, daemon state, locations), exit 0/1.
This is the aggregate the AXI standard's §4 asks for: search paths, auto-inspect state, and file locations in one call, because the follow-up call is the expensive token cost.

Deliberately NOT built, mirroring the `axi apps install`/`uninstall` deferral discipline:

- **No mutating `axi config` verbs** (paths add/remove, cache clear, auto-inspect enable/disable). An agent rewriting the user's search paths or wiping the cache is a product decision on its own evidence, not a consequence of moving code. A test asserts the `axi` registry carries `config` and no `config`-prefixed mutations, so "deliberately not built" cannot be misread as "forgotten".
- **No `axi config cache list`**: cached-project inventory is adjacent to `axi ls`/`axi benches` territory; if an agent need materializes, it is its own small decision.

The `--json` flags land on the HUMAN reads only; `axi` stays TOON-only per the locked captain decision (no `--json` on any `axi` verb, ever).

## Decision 7: output contract

- `config path` and `config cache path` print the bare path and nothing else - no prose, no rich wrapping, safe for `$(...)`. The human niceties live in `config show`.
- `config show` renders a rich layout for humans and a stable JSON object under `--json` (the DTO via `asdict`, enums/paths stringified at the boundary).
- `--json` output is written with `print`, never through a width-wrapping console - the `ls --json` stdout-purity precedent.
- Deprecation warnings are stderr-only.
- Reads never prompt; the only prompt in the group remains cache clear `--all` (confirm), which keeps its `--yes` flag and its non-TTY refusal (exit 1), E2E-verified in both modes per the captain standard.
