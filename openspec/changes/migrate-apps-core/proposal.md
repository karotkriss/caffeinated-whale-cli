## Why

`apps update` was migrated in batch 4 (`migrate-update-core`), but it was only ever a 57-line shim over `update.py`.
The other three subcommands - `list`, `install`, `uninstall` - are the real `commands/apps.py`, and they are still four concerns welded together: typer parsing, the fan-out logic, the container I/O, and `rich` rendering.

The seam runs through the middle of the file, which is why this batch exists as its own unit rather than riding along with batch 4.
`apps update` now returns a typed report from `core/update.py` while its three siblings in the same module still hand-roll `results = [{"app", "site", "action", "ok"}]` and print it themselves (`apps.py:195-220`).
One module, two contracts.

Three facts shape the work:

- **`_report_and_exit` is the hand-rolled envelope the foundation was modeled on.**
  `core-logic-foundation/design.md:8` cites `apps.py:167-192` explicitly as the shape `Result` was derived from.
  This batch retires the original now that the typed form exists; it is not inventing a shape, it is finishing a substitution the foundation started.
- **The three verbs are one fan-out with three payloads.**
  `list` reads (available apps, then installed-per-site); `install` fetches then installs per site; `uninstall` removes per site.
  All three resolve container + bench identically, all three iterate sites, and two of them recache and aggregate an exit code the same way.
- **They already have `--json` and are already re-pointed at `core/exec_stream.py`** (batch 3).
  So this batch blocks nothing and unblocks nothing; it is the migration itself, plus the one read verb the agent surface is missing.

## What Changes

- **New `core/apps.py`**: `list_apps(...) -> Result[AppsListing]`, `install_apps(...) -> Result[AppsReport]`, `uninstall_apps(...) -> Result[AppsReport]`.
  Each owns its resolution, container I/O, and fan-out, and RETURNS a typed report rather than printing one.
  Progress rides an optional typed-event callback (`on_event`), following `core.update` (batch 4), not a generator.
- **`commands/apps.py` becomes a renderer** over those three functions.
  Its CLI contract - every flag, message, and exit code - is preserved byte-for-byte.
  `_report_and_exit`, `_run_bench`, `_capture_bench`, `_stream_bench`, `_list_available_apps`, `_list_installed_apps`, `_resolve_target_sites`, and `_derive_app_name` move to the core or die.
- **New `cwcli axi apps list` verb** (read-only): one TOON document, exit 0/1/2.
  It answers a question the agent surface currently cannot: which apps exist on a bench, and which are installed on which site.
- **`axi apps install` / `axi apps uninstall` are NOT shipped** (captain-locked; see design Decision 1 and `tasks.md` §7).
  They are recorded as explicit deferred tasks, not dropped.

## Capabilities

### New Capabilities

- `apps-core` - the three app operations as UI-pure core functions returning typed reports.
- `axi-apps-list` - the agent-facing read verb over `core.list_apps`.

### Modified Capabilities

- `app-management` - unchanged in behaviour; re-seated onto the core. Every flag, message, and exit code is preserved.

## Impact

- **Behaviour: none intended.** This is refactor-under-green. `tests/test_apps.py` is the net and must pass unchanged (see design Decision 6 for the one honest exception).
- **Risk concentration is `install`**, whose `apps/` before/after diff derives the app name to install (`apps.py:337-348`). That diff is subtle and its fallback (`_derive_app_name`) is load-bearing.
- **`uninstall` is destructive**, so its confirm gate is the one place a mistake destroys site data. It becomes a `NEEDS_CHOICE`, never a prompt in the core.
- No new dependency, no schema change, no config change.

## Non-Goals

- **`axi apps install` / `axi apps uninstall`.** A destructive agent verb is its own decision, not a slot in a migration batch (Decision 1). Tracked in `tasks.md` §7.
- **plan/apply.** `uninstall` is destructive, but its confirm is a yes/no over a known site list - that is `NEEDS_CHOICE`, not a non-trivial preview. plan/apply still arrives with `restore`/`rm` (foundation Decision 5).
- **Fixing anything `apps` does today.** Not the `install` name-derivation fallback, not `list`'s partial-failure exit code. A migration does not get to change behaviour (batch 4, Decision 4).
- **Migrating `inspect`.** `cache.recache_project` still routes through the `inspect` command; this batch deliberately does NOT add a second core-to-CLI reach (Decision 3).
- **A docstring/test-coverage campaign** beyond the characterization tests this batch needs.

## Corrections to the record

- **`AGENTS.md` says `apps`'s other three subcommands "already have `--json`, block nothing, and the seam runs through the middle of `apps.py`".**
  All three are accurate and re-verified first-hand at `3ff07ca`.
- **The recon framing that this batch would add a second core-to-CLI reach is WRONG, and the difference is load-bearing.**
  `core/update.py` reaches into the `inspect` command mid-fan-out, between the pull and the discovery that depends on it, which is why it could not be hoisted.
  `install`/`uninstall` recache AFTER every mutation (`apps.py:377-378`, `:465-466`), as an epilogue.
  An epilogue hoists to the frontend for free. See Decision 3.
