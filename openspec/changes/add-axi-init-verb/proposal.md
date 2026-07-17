## Why

`cwcli init` is fully migrated onto the UI-pure core (`core.init_instance` + `core.init_bench`, batch 9 `migrate-init-core`), but there is deliberately NO `cwcli axi init` verb.
That absence was a DEFERRAL, not a refusal: `migrate-init-core` design Decision 9 explicitly recorded that the two-call core shape makes the verb thin whenever it is decided, and pinned the deferral with `tests/test_core_init.py:test_axi_registry_has_no_init_command` so "deferred" could never read as "forgotten".
The captain approved the verb in principle on 2026-07-16 and gave the go on 2026-07-17.

This is the PROPOSAL phase.
It defines `cwcli axi init` as a thin TOON-rendering frontend over the EXISTING core, changing NO `core/` logic, and it resolves the two design questions the captain owns before anyone writes the verb: how a 10-20 minute provisioning run reports progress on a surface that emits ONE terminal document, and how the site's admin password reaches the agent-driven, always-non-interactive verb without a generated-password path.

The un-built state has one concrete cost: an agent that can already `cwcli axi inspect`, `cwcli axi start`, `cwcli axi apps update`, and back up a site cannot create the instance those verbs operate on.
Every provisioning run must be initiated by a human at a TTY, so an agent cannot stand up a throwaway instance to work in and tear down.
The core is ready; only the frontend and its two product decisions are missing.

## What Changes

- **A new `axi init` verb in `commands/axi.py`**, a renderer over the unchanged `core.init_instance` then `core.init_bench`, following the established `cwcli axi` pattern: parse flags, call the SAME core functions the human `cwcli init` calls, serialize the returned `InitReport` DTO to TOON on stdout, map status/`CwcliError` to an exit code, and NEVER prompt.
  No business logic is added and no `core/` file is touched.

- **Progress (design question 1): block until done, emit ONE terminal TOON document on stdout, narrate coarse phase progress to stderr.**
  The verb blocks for the full 10-20 minute provisioning run and emits a single `InitReport` TOON document at the end, exactly as `axi apps update` blocks on a minutes-long `bench update` and `axi backup` on a long `bench backup`.
  Streaming N documents would break the one-TOON-document contract.
  Because the AXI contract already reserves stderr for progress/diagnostics (stdout carries only TOON), the verb wires `core.init`'s `on_event` callback to write coarse phase-level lines (`InitStepStart.message`, `InitNotice.text`) to stderr, so the long block is not silent, and points the agent at `cwcli logs <project>` / `cwcli status <project>` (run from a second shell) for live and deeper progress.
  The stderr narrator emits NO secret and NO raw exec output (that noise is what `cwcli logs` is for); the core already guarantees no event carries a secret value.

- **Secret UX (design question 2): accept the admin password via the `CWCLI_ADMIN_PASSWORD` environment variable as the recommended transport, and via `--admin-password` on argv for parity with a documented caveat; omitted is a usage error.**
  A generated-and-printed password path cannot exist on a surface with no TTY (it would leak a secret into a captured transcript), so when neither the env var nor the flag is supplied the verb refuses with a USAGE error (exit 2) naming both - it NEVER generates and NEVER prompts.
  The env var reduces host-side exposure (`ps aux` / `/proc/<pid>/cmdline` and the persistent shell-history file) that the argv flag has; the flag wins if both are given.
  The same transport applies to the MariaDB root password (`CWCLI_DB_ROOT_PASSWORD`, default `123`).
  The security tradeoff is stated plainly in `design.md` Decision 3, including the exposures no host-side transport removes.

- **The three interactive choice surfaces the human `cwcli init` resolves become non-prompting errors naming the exact flag:**
  - `confirm_reuse_bench` (the bench directory already exists and neither `--reuse-bench` nor `--no-reuse-bench` was passed): a USAGE error (exit 2) naming both flags.
    A new branch in `axi.py`'s `emit_axi_choice_as_usage_error` renders the `confirm_reuse_bench` choice kind; the core already returns it.
  - port conflict: the core RAISES `CwcliError(CONFLICT, "ports.in_use")` carrying the `--port` hint, which flows through the generic `emit_axi_error` to an operational error (exit 1) with a `help:` line naming `--port`, exactly as `axi start` reports its port conflict.
  - `confirm_start` (stage-1 readiness poll timeout, or a stage-2 race where the container stopped between stages): an operational error (exit 1) with a message tailored to init (the containers are init's own and did not come up), pointing at `cwcli status` / `cwcli logs`, never the generic "start it first" line.
  There is deliberately no `--auto-start` on the agent surface: an agent composes `cwcli axi start` then re-runs, matching how `axi inspect`/`axi apps update` refuse to open a start-from-axi path.

- **The full flag set** the verb takes: the `project` argument plus `--port`, `--bench` (the new bench NAME, not the `--bench <index|label>` selector other verbs use - init creates a bench, it does not select one; the divergence is documented), `--site`, `--bench-parent`, `--frappe-branch` / `--version` (mutually exclusive; the mutual-exclusion error stays a frontend USAGE error), `--db-root-password`, `--admin-password`, `--reuse-bench/--no-reuse-bench`, `--install-erpnext`, and `--erpnext-branch`.
  No `--verbose` (stdout is always TOON) and no `--auto-start`.

- **The exit-code contract**: 0 on success (`OK` or `WARNING`; soft provisioning failures such as a yarn/setuptools/pyenv install hiccup are `WARNING` and still exit 0, as everywhere else, because the bench and site were still created); 1 on any operational `CwcliError` (`PRECONDITION`, `DOCKER`, `CONFLICT`, `NOT_RUNNING`) and the two `confirm_start` cases; 2 on any USAGE error (missing admin password, malformed `--version`, `--frappe-branch` + `--version` together, and the unresolved `confirm_reuse_bench`).

- **At implementation time (NOT in this proposal phase), the registry-absence assertion flips from deferred to shipped.**
  `tests/test_core_init.py:test_axi_registry_has_no_init_command` (and its explanatory docstring) is replaced by a presence assertion; `skills/cwcli/SKILL.md` regenerates from the live registry via `scripts/build_skill.py` (the `tests/test_axi_skill.py --check` gate blocks a stale skill); and the CLAUDE.md `init` ledger entry flips from "deliberately NO `axi init`" to documenting the shipped verb.

## Impact

- **New:** the `axi_init` command in `src/caffeinated_whale_cli/commands/axi.py`, its unit tests in a new `tests/test_axi_init.py` (both modes: env-var and argv transport, each choice-surface-to-error mapping, exit codes), and a real-instance E2E leg proving a full agent-driven `cwcli axi init` provisions a genuine bench + site non-interactively.
- **Changed (implementation phase only):** `commands/axi.py` (the new verb + the `confirm_reuse_bench` branch in `emit_axi_choice_as_usage_error`), `tests/test_core_init.py` (registry assertion flips to presence), `skills/cwcli/SKILL.md` (regenerated), CLAUDE.md ledger, the `cwcli-lifecycle` + `cwcli-core-axi` skills, `README.md` ("Making agents aware of the surface" / the axi verb list), and `tests/README.md` coverage map.
- **Unchanged:** every `core/` file (`core/init.py`, `core/exec_stream.py`, the envelope, resolvers) - the verb adds zero core logic; the human `cwcli init` and all its behavior; the cache schema; the secret env-transport into `bench new-site` (inherited byte-exactly from the core).
- **Zero new core primitives, as a falsifiable claim:** the verb reuses `core.init_instance`/`core.init_bench`, the `Result` envelope, `emit_result`/`emit_axi_error`/`emit_axi_choice_as_usage_error`, and the `OnEvent` idiom.
  The only additions are a new branch in the axi choice-renderer for the `confirm_reuse_bench` kind (a value already in `Choice.kind`'s open token set, the `select_editor` precedent) and a small stderr narrator - neither is a primitive change.
