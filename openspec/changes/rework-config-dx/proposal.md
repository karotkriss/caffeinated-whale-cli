## Why

The captain reports `cwcli config` "feels clunky to use".
This change pins that feel to eleven concrete findings, each verified by driving the real command (isolated `CWCLI_HOME`, worktree editable install, 2026-07-16) or by reading `commands/config.py`, and reworks the surface to fix them.
It also carries `config`'s migration onto the UI-pure logic core, because the standing intent is that the DX rework lands with that migration rather than reworking the old monolith twice (position argued in `design.md` Decision 1).

The findings, ranked by real-use pain:

**F1. The config command cannot answer "what is my config?" (worst offender).**
There is no `config show`, no `config get`, and - despite `add-path`/`remove-path` existing - NO way to list the configured search paths.
Every read a user actually wants requires `cat $(...)` on the TOML file, and even the file's path is delivered as wrapped prose (F8).
Evidence: the full `--help` tree has no read verb for settings; `add-path`/`remove-path` write blind.

**F2. `auto-inspect` is ten subcommands over three state stores with no signal which is which.**
`enable`/`disable`/`set-interval` mutate the TOML, `start`/`stop`/`restart` mutate a live daemon, `install-startup`/`uninstall-startup` mutate OS boot units, `status`/`logs` read - all flat siblings.
The enable/start split forces a two-step dance the tool itself apologizes for: `enable` prints "Run 'cwcli config auto-inspect start' to start the background process now", and `start` when disabled refuses with a hint pointing back at `enable` (both driven, exit 1 on the refusal).
Three ways exist to install the boot hook (`enable --startup`, `start --startup`, `install-startup`) and two to set the interval (`enable --interval`, `set-interval`).

**F3. `enable --interval 30` half-commits: it persists `enabled = true`, THEN fails interval validation with exit 1.**
Driven: after the failing command, `config.toml` reads `enabled = true`.
A failed command must not leave the config mutated (`commands/config.py:144` writes before `:148` validates).

**F4. `cache clear <project> --all --yes` silently ignores the project argument and wipes the ENTIRE cache.**
Driven: "Entire cache has been cleared", exit 0.
The most destructive subcommand in the group resolves contradictory arguments toward the more destructive reading without a word (`commands/config.py:71` checks `--all` first).

**F5. No `--json` on any read.**
`cache list`, `auto-inspect status`, `tips status`, and both `path` verbs are human-only, while every comparable read in cwcli (`ls`, `inspect`, `apps list`, `where`) has `--json`.

**F6. No agent surface at all.**
Zero `cwcli axi config` verbs: an agent cannot read the search paths, cache inventory, or auto-inspect state without scraping rich tables.

**F7. The word `path` means three unrelated things in one namespace.**
`config path` = where the config file lives; `config add-path`/`remove-path` = mutate bench search paths; `config cache path` = where the cache DB lives.

**F8. The path verbs are script-hostile.**
`config path` prints "Config file is located at: <path>" through rich, which wraps the path across lines at narrow widths (driven; the path broke mid-line at width 80).
`$(cwcli config path)` is unusable, defeating the one job a path verb has (clig.dev: output should be simple to parse).

**F9. `add-path` validates nothing.**
Driven: it accepted `not/absolute/../weird` (help says "absolute path"), and `/a/b` plus `/a/b/` were stored as two distinct entries (exact-string dedup, `config_utils.py:122`).

**F10. Exit-code and channel inconsistencies.**
`cache clear` with no target exits 1 for a usage error (Typer usage errors exit 2 everywhere else, driven); `auto-inspect logs` on a read failure prints a red error and exits 0 (`commands/config.py:318` has no raise).

**F11. `tips` is a three-verb group flipping one boolean**, whose `status` duplicates what an effective-config read would show.

## What Changes

The full before -> after mapping.
"Frozen alias" = kept as a hidden deprecated alias with byte-identical behavior plus a one-line stderr deprecation warning (policy in `design.md` Decision 4; `start`'s argv is load-bearing for already-installed boot units, which exec `cwcli config auto-inspect start` verbatim - `utils/startup.py:185,282`).

| Today | After | Class |
| --- | --- | --- |
| (none) | `config show [--json]` - one-shot effective config: search paths, auto-inspect settings + live daemon state + boot-hook state, tips, config-file and cache-DB locations | NEW (fixes F1) |
| (none) | `config paths [--json]` - list the search paths | NEW (F1) |
| (none) | `config edit` - open the TOML in `$EDITOR` (optional rider, `design.md` D3) | NEW |
| `config path` | kept; output becomes the bare path, nothing else | fix (F7, F8) |
| `config add-path P` | `config paths add P` - refuses non-absolute (exit 2), normalizes before dedup | moved + fix (F9); frozen-ish alias (validation applies, disclosed) |
| `config remove-path P` | `config paths remove P` - normalizes before match; absent stays exit 0 | moved; alias as above |
| `config cache clear [NAME] [--all] [--yes]` | kept; NAME plus `--all` becomes a usage error (exit 2, nothing cleared); no target becomes exit 2 | fix (F4, F10) |
| `config cache path` | kept; bare-path output | fix (F8) |
| `config cache list` | `config cache list [--json]` | +json (F5) |
| `config auto-inspect enable [-i N] [--startup/--no-startup]` | validates BEFORE writing (atomic), enables AND starts the daemon; idempotent re-run applies changes (restarts on interval change) | widened (F2, F3) |
| `config auto-inspect disable` | stops daemon AND disables AND removes the boot hook, printing each action taken | widened (F2) |
| `config auto-inspect stop` | kept: daemon-only stop, stays enabled (returns at boot if hooked) | unchanged |
| `config auto-inspect status` | `status [--json]`; separates config / daemon / boot-hook state explicitly | +json (F5) |
| `config auto-inspect logs [-n]` | kept; a read failure exits 1 | fix (F10) |
| `config auto-inspect start [--startup]` | frozen alias (boot units depend on the argv AND on its refuse-when-disabled guard) | deprecated |
| `config auto-inspect restart` | frozen alias; new home: `enable` (idempotent) | deprecated |
| `config auto-inspect set-interval N` | frozen alias; new home: `enable --interval N` | deprecated |
| `config auto-inspect install-startup` | frozen alias; new home: `enable --startup` | deprecated |
| `config auto-inspect uninstall-startup` | frozen alias; new home: `enable --no-startup` (keep running, drop hook) or `disable` | deprecated |
| `config tips enable` / `disable` | kept | unchanged |
| `config tips status` | frozen alias; subsumed by `config show` | deprecated (F11) |
| (none) | `cwcli axi config` - the effective config as one TOON document, read-only | NEW (F6) |

Visible surface: 19 verbs today -> 15 after (aliases hidden from `--help`).

Carried migration (Decision 1): the logic lands in `core/config.py` (settings, search paths, cache inventory/clear) and `core/auto_inspect.py` (daemon + boot-hook orchestration over the untouched `utils/auto_inspect.py` / `utils/startup.py` mechanics), returning `Result[...]` envelopes per the core conventions; `commands/config.py` thins to a renderer.
The only `NEEDS_CHOICE` surface is `confirm_clear` (cache clear `--all` without consent); the CLI renders it as today's prompt/refusal.
Frozen aliases deliberately bypass the core and keep calling the utils the monolith called, because building core API for verbs scheduled for deletion is waste (Decision 4).

## Impact

- **New:** `core/config.py`, `core/auto_inspect.py`, `commands/config.py` rework, `axi config` verb, `tests/test_core_config.py`, `tests/test_core_auto_inspect.py`, `tests/test_axi_config.py`.
- **Changed:** `commands/config.py` (renderer + aliases), `commands/axi.py` (+1 verb), `skills/cwcli/SKILL.md` (regenerated; the verb table is generated, never hand-edited), README `config` sections, `tests/test_config_validation.py` / `test_tips.py` / `test_auto_inspect.py` (re-pointed; changes BY DESIGN named in tasks), CLAUDE.md ledger + `cwcli-core-axi` skill.
- **Unchanged:** `utils/config_utils.py` / `utils/auto_inspect.py` / `utils/startup.py` mechanics (storage and process layers; the two auto-inspect entries on the Known-hazards board - PID reuse, SIGTERM re-entry - are deliberately NOT absorbed, per the board's own discipline), `db_utils` cache mechanics, every non-config command, the TOML schema (no config-file migration needed).
- **Behavior deltas are the product, disclosed per-row above**; everything not in the table is byte-preserved and characterization-tested first (tasks §1).
- **Zero new core primitives expected, as the falsifiable claim**: the envelope, `CwcliError`, and the DTO discipline cover every need; config touches no Docker, no benches, no resolvers. If implementation finds a bend, it is reported, per the standing rule.
