## Why

`open` is the batch the inspect migration (batch 7, `migrate-inspect-core`) just unblocked, and its shape is already settled, not re-decided here.
The `cwcli-open-handover-design-o9` recon (captain-endorsed, recorded in the CLAUDE.md ledger) measured that `open` was never a handover command: only ONE of its four editor branches hands over (`--docker` -> `exec_into_container` -> `os.execvp`), while `--code`/`--code-insiders`/`--cursor` call `open_in_vscode` and return normally.
It is 285 lines of ordinary, returning, envelope-shaped logic behind 3 lines of handover, and the `cwcli-inspect-recon-i7` recon Q8 confirmed its only two logic edges (`open.py:135` fallback populate, `open.py:217` in-memory `--app` freshness) are served by batch 7's surface - both already call `core.inspect` / `core.partial_refresh` today, so nothing blocks it.

Three costs of its un-migrated state, all measured:

**1. `open`'s logic is the least-tested resolve chain in the codebase.**
There is no `test_open.py`; its 285 lines are covered incidentally by a file named after `inspect` (`test_inspect_partial_refresh.py`) plus one regression file, and those tests already mock the handover and assert on the arguments it would have received - the `LaunchTarget` seam exists in test code by necessity (o9 Finding 4).
The migration replaces a `MagicMock` call-args assertion with a typed return value.

**2. The editor prompt is a hand-rolled `NEEDS_CHOICE`.**
`open.py:279-346` hand-rolls a questionary select plus its own non-TTY refusal, next to an architecture whose envelope carries exactly that decision as `Choice` (o9's evidence index names this pairing).
The core returns `select_editor`; the CLI keeps rendering it as today's prompt and refusal.

**3. `open` re-implements container lookup the core already owns.**
`open.py:93-114` hand-rolls `get_project_containers` + frappe-service filtering + two error prints that `core/docker.py:get_frappe_container` provides as typed errors with the same messages.

## What Changes

- **`core/open.py`**, the settled shape: `core.open_plan(project_name, *, bench=None, bench_path=None, app=None, editor=None, auto_start=False, on_event=None) -> Result[LaunchTarget]` - `RunPlan` minus `run_stream` (the shipped `core/run.py` seam with the second call deleted).
  A plain function: nothing streams, there is no laziness to work around, and `NEEDS_CHOICE` must be returnable at call time.
- **`LaunchTarget` stays declarative** - `project`, `container_name`, `working_dir`, `editor` - never an argv, so a GUI can perform its own handover.
  It carries a container NAME (not an ID): both handover mechanisms consume the name, and unlike `run` there is no phase-2 core call to bridge an ID back into a handle.
- **Three choice surfaces**: `confirm_start` (the stopped-race backstop, `run_plan`'s exact pattern), `select_bench` (multi-bench with no selector; the CLI keeps rendering it as today's error-with-bench-list, exit 1), and the NEW `select_editor` kind (no editor flag while at least one editor is installed; the CLI resolves it as today's questionary prompt on a TTY and today's refusal naming the flags on a non-TTY).
- **The fallback populate becomes core-to-core**, the coupling batch 7's design predicted: `resolvers.resolve_bench` returning `None` -> `core.inspect(refresh="auto", offer_choice=False)` -> re-resolve -> only then `DEFAULT_BENCH_PATH` with a warning.
  The interaction with the merged fallback-abort decision (PR #93) is specified deliberately: a hard `CwcliError` from the fallback inspect ABORTS (propagates; `tests/test_open_inspect_fallback.py` pins it), while the `/workspace/frappe-bench` default survives only for a non-`CwcliError` exception and for a populate that succeeds yet still resolves nothing - exactly current HEAD behavior.
  One disclosed hardening rides this edge: the fallback inspect runs `offer_choice=False`, so a container stopped in the race window becomes a typed `NOT_RUNNING` abort instead of today's silently-discarded choice followed by opening the guessed default path.
- **The `--app` pass moves verbatim**: cached-bench match by PATH (never `[0]`), the in-memory `core.partial_refresh` freshness pass (degrade-on-error, never persists), the membership check, and the `{bench}/apps/{app}` working-dir assembly - all pinned by the existing suites.
- **Editor detection lands in the core** via stdlib `shutil.which` (the `core/version.py` host-side-detection precedent); a requested-but-not-installed editor is `CwcliError(NOT_FOUND)` carrying today's install-URL hint; no flag and nothing installed auto-picks `docker` as today.
  `vscode_utils.select_vscode_editor` and the three `is_*_installed` one-liners are deleted once grep proves the migration left them no caller (a named task, not a silent sweep).
- **`commands/open.py` thins to a renderer plus the handover**: the four-boolean-flag fusion and its mutual-exclusion error stay frontend (the `apps` fused-`--yes` precedent: flag UX is one frontend's choice), the `ensure_containers_running` prologue and capped `confirm_start` retry mirror `run.py`, prompts stay outside the spinner, and the four-way switch performs the handover - `docker` -> `exec_into_container` (`execvp`), editors -> `vscode_utils.open_in_vscode`.
  `exec_into_container` and `open_in_vscode` are untouched.
- **There is deliberately NO `axi open` verb, and its absence becomes asserted**: not because the plan will not serialize (it will - four strings), but because `execvp` destroys the process that owes `axi` its TOON document.
  A test pins the verb's absence (the `axi apps install`/`uninstall` non-verb precedent) so it cannot slip in on the wrong premise.

## Impact

- **New:** `src/caffeinated_whale_cli/core/open.py`, `tests/test_core_open.py`, the no-`axi open` assertion.
- **Changed:** `commands/open.py` (renderer + handover), `utils/vscode_utils.py` (dead helpers removed if caller-free), existing open coverage (`tests/test_open_inspect_fallback.py`, `test_inspect_partial_refresh.py`'s open classes: patch targets move with their subject, assertions unchanged unless named by design), CLAUDE.md ledger + `cwcli-core-axi` skill, `docs/technical/README.md` module list, `tests/README.md` coverage map.
- **Unchanged:** `exec_into_container` (`docker_utils.py`), `open_in_vscode` and the extension machinery (`vscode_utils.py`), `core/inspect.py`, `core/resolvers.py`, `core/docker.py`, the cache schema, every exit code, the `axi` surface (no new verb).
- **Zero new primitives, as a falsifiable claim**: `get_frappe_container`, `resolve_container_state`, `resolve_bench`, `DEFAULT_BENCH_PATH`, `core.inspect`, `core.partial_refresh`, and the `OnEvent` idiom cover every need; `select_editor` is a new value in `Choice.kind`'s open token set, not a primitive change.
  If implementation finds a bend, it is reported, per the standing rule.
