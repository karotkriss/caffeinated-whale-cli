## Context

`cwcli init` was migrated onto the UI-pure core in batch 9 (`migrate-init-core`) as two sequential plain functions:

- `core.init_instance(project_name, *, port=8000, auto_start=False, stream_output=False, on_event=None) -> Result[InstanceUp]` - project dir, compose download, port/image customization, `compose pull` + `up -d`, and the bounded readiness poll.
- `core.init_bench(project_name, *, bench_name, site_name, bench_parent, frappe_ref, db_root_password, admin_password, reuse_bench=None, install_erpnext=False, erpnext_branch, auto_start=False, stream_output=False, on_event=None) -> Result[InitReport]` - bench resolve, the tri-state `reuse_bench`, version gating, `bench init`, bench configs, `bench new-site` (secrets via `exec_stream(environment=)`), the optional ERPNext pair, and the cache clear.

Both are UI-pure: they import no `rich`/`questionary`/`typer`, never print or prompt, return a typed `Result[T]` or raise `CwcliError`, and surface a decision they cannot make from parameters as a `NEEDS_CHOICE` carrying a `Choice`.
`commands/init.py` is already a pure renderer over them, and `commands/axi.py` already renders eight-plus other core verbs to TOON.
So `axi init` is a frontend-only addition: it calls the same two functions the human command calls and renders their result the axi way.

The two hard questions are not "how to call the core" - that pattern is settled - but two product decisions the core deliberately left to each frontend, and which the human frontend answers with TTY-coupled UX that the agent surface cannot reuse:

1. **Progress.** `init` runs 10-20 minutes. The human frontend drives spinners and streams raw exec output. The axi contract is ONE terminal TOON document, stdout-pure, no interactive spinner.
2. **The admin password.** The human frontend generates a strong password and prints it once when `--admin-password` is omitted at a TTY. The axi surface has no TTY and no safe place to print a generated secret, so that path cannot exist, and the secret must arrive by some transport.

The rest of the design is mechanical: the flag set, the exit codes, and turning the three interactive choice surfaces (`confirm_reuse_bench`, port conflict, `confirm_start`) into non-prompting errors.

## Goals / Non-Goals

**Goals.**
- Ship `cwcli axi init` as a thin renderer over the existing `core.init_instance` + `core.init_bench`, with zero `core/` changes.
- Resolve the progress and secret-UX questions with a clear recommendation and a stated tradeoff each.
- Preserve every core behavior byte-for-byte: version gating, the secret env-transport into `bench new-site`, the existing-bench flow, idempotent re-runs, and soft-failure `WARNING` semantics.
- Turn every interactive choice surface into a non-prompting, flag-naming error, honoring "a non-TTY without the needed flag must refuse with a non-zero exit, never silently proceed, default, or hang."

**Non-Goals.**
- No change to `core/init.py` or any other core module.
- No `--verbose` and no human-style spinners on the agent surface.
- No `--auto-start` convenience on the agent surface (composability over a start-from-axi path).
- No removal of the container-side `bench new-site` argv exposure or the `docker top` residual exposure; both are inherited from the core unchanged and out of scope (they are identical regardless of the host-side transport chosen here).
- Not the implementation itself: this proposal defines the verb; the registry-assertion flip, SKILL.md regeneration, and CLAUDE.md ledger update happen in the implementation phase.

## Decisions

### 1. A thin TOON frontend over the unchanged core, zero core changes

`axi init` parses its flags, calls `core.init_instance` then `core.init_bench`, and renders the returned `Result` with the existing `emit_result` / `emit_axi_error` / `emit_axi_choice_as_usage_error` helpers.
It adds no business logic.
This is the same shape as `axi backup`, `axi start`, `axi inspect`, and `axi apps update`; the only new mechanics are the stderr narrator (Decision 2), the env-var read (Decision 3), and one new branch in the choice renderer for `confirm_reuse_bench` (Decision 4).

The verb calls the two stages in sequence exactly as `commands/init.py` does: `init_instance` first, then `init_bench`, threading the resolved project name.
Where the human renderer loops to re-invoke a stage after resolving a choice interactively, the axi verb does NOT loop: an unresolved choice is rendered as an error and the process exits, because an agent resolves it by re-running the command with the missing flag.

### 2. Progress: block until done, ONE terminal TOON document, coarse stderr narration (design question 1)

**Recommendation.**
The verb BLOCKS for the full provisioning run and emits a single `InitReport` TOON document on stdout when it finishes, exactly as `axi apps update` documents ("Blocks until the update finishes and emits ONE terminal document, exactly as `axi backup` does for a minutes-long `bench backup`").
Streaming intermediate documents is off the table: the one-TOON-document contract is what lets an agent parse a single verdict rather than a stream, and N documents on stdout would break every downstream reader.

To keep a 10-20 minute block from being fully silent, the verb wires `core.init`'s `on_event` callback to a small narrator that writes COARSE phase-level lines to STDERR:
`InitStepStart.message` (for example, "Pulling Docker images", "Initializing bench", "Creating site") and `InitNotice.text`.
The AXI contract already reserves stderr for progress and diagnostics while stdout carries only TOON, so this adds liveness without touching the stdout document.
The narrator deliberately DROPS `InitOutput` (raw bench exec bytes - noisy, and an agent does not parse it) and `InitTrace` (verbose diagnostics).
For live or deeper progress, the verb's help text and the terminal document point the agent at `cwcli logs <project>` and `cwcli status <project>`, run from a second shell.

The narrator carries no secrets: the core guarantees no event field holds a secret value (`InitReport` has no password field, the command-echo trace carries only `$CWCLI_*` references), and the narrator emits only `InitStepStart.message` / `InitNotice.text`, neither of which is ever a secret.

**Tradeoff, stated.**
A single blocking shell call of 10-20 minutes risks tripping an agent-side tool-call timeout that is shorter than the provisioning time; if the agent's harness kills the call, `cwcli` dies but the container-side `bench init` / `bench new-site` may keep running, and the agent must reconcile via `cwcli axi inspect` / `cwcli status`.
The stderr narration mitigates the silence only insofar as the agent's harness surfaces stderr from a long-running call, which not all harnesses do incrementally.
The honest framing for the agent: `axi init` is a long, blocking, fire-then-verify operation; treat the terminal TOON document (or a non-zero exit) as the verdict, and use `cwcli status` / `cwcli logs` from a second shell for anything in between.

**Alternative considered.**
Match `axi apps update` exactly - block, no `on_event`, fully silent until the terminal document.
Rejected as the primary because init's 10-20 minutes is materially longer than a typical `bench update`, and the stderr narrator is a few lines that reuse the core's existing event stream at no core cost.
The captain may downgrade to the silent variant if stderr noise is unwanted; the sub-decision does not change the stdout contract.

### 3. Secret UX: env-var primary, argv with a caveat, omitted is a usage error (design question 2)

**The constraint.**
The human frontend GENERATES a strong admin password and PRINTS it once when `--admin-password` is omitted at a TTY (`commands/init.py:_generate_admin_password` + the print-once block), and REFUSES on a non-TTY because a generated secret would leak into a captured log.
The axi surface is always non-interactive: there is no TTY to print to and no human to read it.
So the generated-password path cannot exist here, and the secret must arrive by an explicit transport or the verb must refuse.

**Recommendation.**
Accept the admin password from the `CWCLI_ADMIN_PASSWORD` environment variable as the recommended transport, and from `--admin-password` on argv for parity with the human command, with a documented caveat.
The flag wins if both are supplied.
When NEITHER is supplied, the verb refuses with a USAGE error (exit 2) naming both - it NEVER generates a password and NEVER prompts.
The MariaDB root password takes the same shape: `CWCLI_DB_ROOT_PASSWORD` env var or `--db-root-password` argv, defaulting to `123` (the value hardcoded in the compose file the core itself downloads), so it is not required.

The env-var name reuses the exact name the core already uses inside `exec_stream(environment=)` for the leaf `bench new-site` exec, so the mental model is one name end to end.

**Security tradeoff, stated plainly.**
Where each transport is exposed on the HOST:

- `--admin-password` on argv: visible to any process via `ps aux` and `/proc/<pid>/cmdline`, and saved to the persistent shell-history file.
- `CWCLI_ADMIN_PASSWORD` env var: NOT visible in `ps aux`; readable only by same-user processes via `/proc/<pid>/environ`.
  An inline assignment (`CWCLI_ADMIN_PASSWORD=... cwcli axi init ...`) is still saved to the shell-history file, so the env var's clear win is over `ps aux` / multi-user process listing, not over shell history.

What NO host-side transport removes:

- The agent's own transcript: whatever command the agent runs - argv flag, inline env assignment, or a piped value - appears in the agent's conversation transcript verbatim.
  No host transport hides the secret from the transcript; only reading it from a pre-existing secret store or file descriptor would, which is beyond this verb's scope.
- The container-side exposure: `core.init_bench` passes the expanded value to `bench new-site`'s own argv inside the container (bench offers only a flag interface), and `docker top` can observe it there.
  This is inherited from the core unchanged and is identical for every host-side transport, so it does not distinguish the options.

Net: the env var is recommended because it removes the two host-side exposures (multi-user `ps` and, for the flag form, shell history) that the argv flag has, at zero added complexity; the argv flag stays for human-command parity and simple one-off agent use, with its caveat documented.

**Alternative considered: stdin.**
Reading the password from piped stdin protects `/proc/<pid>/environ` too and, if fed from a file rather than an inline `printf`, the shell history.
Rejected as the primary transport because its marginal gain over the env var is narrow (it still does not beat the agent-transcript exposure), and it adds a real edge: stdin at a TTY would hang, so the verb would need a "stdin must be piped" guard that contradicts the simple agent flow.
Offered here as the max-host-side-protection option if the captain wants it.

### 4. The three interactive choice surfaces become non-prompting errors

The human `cwcli init` resolves three things interactively; the agent surface must turn each into a non-prompting error naming the exact flag, never a prompt, default, or hang.

- **`confirm_reuse_bench`** - `core.init_bench` returns `NEEDS_CHOICE` with `Choice(kind="confirm_reuse_bench")` when the bench directory already exists and `reuse_bench is None`.
  On axi this is a USAGE error (exit 2): a new branch in `emit_axi_choice_as_usage_error` renders it as `error: bench '<path>' already exists; pass --reuse-bench to reuse it or --no-reuse-bench with a different --bench name` plus a `help:` line.
  Passing `--no-reuse-bench` against an existing bench makes the core RAISE `CwcliError(CONFLICT, "bench.exists")`, which flows through `emit_axi_error` to exit 1 - a genuine conflict, distinct from the "you did not say what to do" usage error above.

- **Port conflict** - `core.init_instance` RAISES `CwcliError(CONFLICT, "ports.in_use")` with `hint` naming `--port`.
  This needs no special handling: it flows through the generic `emit_axi_error` to an operational error (exit 1) with the `--port` hint on a `help:` line, exactly as `axi start` renders its own port conflict.
  `exit_for(CONFLICT)` is 1, consistent with the surface.

- **`confirm_start`** - `core.init_instance` (stage-1 readiness poll timeout) and `core.init_bench` (stage-2 race: the container stopped between stages) return `NEEDS_CHOICE` with `Choice(kind="confirm_start")`.
  On axi this is an operational error (exit 1), NOT the generic "start it first with 'cwcli start <project>'" line, because for init the containers are init's OWN and it just tried to bring them up: the tailored message is `error: containers for '<project>' did not become ready; check 'cwcli status <project>' and 'cwcli logs <project>'`.
  The verb does NOT re-invoke with `auto_start=True` and does NOT expose `--auto-start`: an agent that wants to retry composes `cwcli axi start` then re-runs `cwcli axi init`, matching how `axi inspect` / `axi apps update` refuse to open a start-from-axi path.

### 5. Flag set, and the `--bench` naming divergence

The verb takes the union of the two core functions' user-facing parameters:
`project` (argument), `--port`, `--bench`, `--site`, `--bench-parent`, `--frappe-branch`, `--version`, `--db-root-password`, `--admin-password`, `--reuse-bench/--no-reuse-bench`, `--install-erpnext`, `--erpnext-branch`.
It does NOT take `--verbose` (stdout is always TOON) or `--auto-start` (Decision 4).
`--frappe-branch` and `--version` stay mutually exclusive with the mutual-exclusion error rendered as a frontend USAGE error (exit 2), and `--version` is resolved by `core.resolve_frappe_ref` whose malformed-shape error is already `CwcliError(USAGE)`.

`--bench` on this verb is the new bench NAME to create (matching the human `cwcli init --bench`), NOT the `--bench <index|label>` selector that every other axi verb uses to pick an existing bench.
This divergence is intrinsic - init creates a bench, it does not select one, so there is no existing bench to index - and it is documented in the verb's help text so an agent is not misled by the shared flag name.

### 6. Exit-code contract

- **0** - success.
  `Result.status` is `OK` or `WARNING`.
  `WARNING` maps to 0 (as everywhere on the surface): a soft provisioning failure (a pyenv/nvm/yarn install hiccup or the setuptools pin) is non-fatal, the bench and site were still created, and the warnings ride the TOON document's warnings block.
- **1** - operational error.
  Any non-USAGE `CwcliError` (`PRECONDITION` for exec/disk-full/compose-download failures, `DOCKER` for a compose failure, `CONFLICT` for the port or `bench.exists` conflict, `NOT_RUNNING`) and the two `confirm_start` cases from Decision 4.
- **2** - usage error.
  Any `USAGE` `CwcliError` (a missing admin password, a malformed `--version`, `--frappe-branch` + `--version` together, an invalid project/bench/site name from the core validators) and the unresolved `confirm_reuse_bench` choice.

This reuses `exit_for` (USAGE -> 2, else -> 1) plus the explicit `0 if status in (OK, WARNING) else 1` mapping every other mutating verb uses; there is no `report.ok` field on `InitReport`, so the status mapping is the whole story (unlike `apps update`, whose partial-failure `report.ok` overrides the status).

### 7. The registry-absence assertion flips at implementation time

`tests/test_core_init.py:test_axi_registry_has_no_init_command` asserts `"init" not in` the axi registry (top-level and sub-apps), with a docstring recording the deferral.
At implementation time this becomes a PRESENCE assertion (`"init" in` the top-level axi registry), and the docstring is updated to record that the verb shipped.
`skills/cwcli/SKILL.md` is regenerated by `scripts/build_skill.py`, which walks Typer's live `registered_commands`, so the new verb appears automatically; `tests/test_axi_skill.py` runs `build_skill.py --check` as a unit test, so a stale skill blocks the gate.
The CLAUDE.md `init` ledger entry flips from "There is deliberately NO `axi init` verb - DEFERRED ..." to a one-line description of the shipped verb and its two design-question resolutions.
None of this happens in the proposal phase.

### 8. Zero new core primitives, as a falsifiable claim

The verb reuses `core.init_instance`, `core.init_bench`, the `Result` envelope, `emit_result`, `emit_axi_error`, `emit_axi_choice_as_usage_error`, and the `OnEvent` idiom.
The only additions are a new branch in `emit_axi_choice_as_usage_error` for the `confirm_reuse_bench` kind (a value already present in `Choice.kind`'s open token set - the `select_editor` precedent) and a small stderr narrator function local to the verb.
Neither is a primitive change, and neither touches `core/`.

## Risks / Trade-offs

- **The long blocking call (Decision 2).** Mitigated by the stderr narrator and the `cwcli status` / `cwcli logs` pointer, but not eliminated; the agent must treat the verb as fire-then-verify.
- **Argv secret exposure (Decision 3).** Mitigated by recommending the env var and documenting the flag's caveat; the container-side and transcript exposures are inherited/out of scope and stated plainly.
- **`--bench` name collision (Decision 5).** Mitigated by help text; the divergence is intrinsic to a creation verb.
- **An agent creating instances at all.** This was the captain's product decision, now made (2026-07-16 in principle, 2026-07-17 go).
  The fail-closed protections that matter for init are around DESTRUCTION (`rm`, `restore`), not creation; creating a throwaway instance is low-blast-radius, and the verb changes no core safety behavior.

## Migration Plan

Implementation phase (NOT this proposal):
1. Add `axi_init` to `commands/axi.py` with the flag set (Decision 5), the env-var-then-flag admin/db password resolution (Decision 3), the sequential `init_instance` -> `init_bench` calls, the stderr narrator (Decision 2), the choice-to-error mapping (Decision 4), and the exit codes (Decision 6).
2. Add the `confirm_reuse_bench` branch to `emit_axi_choice_as_usage_error`.
3. Flip `test_axi_registry_has_no_init_command` to a presence assertion (Decision 7).
4. Add `tests/test_axi_init.py` covering both transports, each choice-to-error mapping, and the exit codes.
5. Regenerate `skills/cwcli/SKILL.md`; confirm `tests/test_axi_skill.py` passes.
6. Update CLAUDE.md, `README.md`, the two skills, and `tests/README.md`.
7. Run the real-instance E2E leg proving a full agent-driven non-interactive `cwcli axi init`, in both password transports.

## Open Questions

- **Stderr narrator granularity (Decision 2).** Coarse phase lines are proposed; the captain may prefer fully silent (the exact `axi apps update` shape). Does not change the stdout contract.
- **Whether to keep `--admin-password` on argv at all (Decision 3).** The env var alone would remove the insecure argv path entirely at the cost of human-command parity. Recommended to keep the flag with its caveat; the captain owns the final call.
- **stdin as the max-host-side-protection transport (Decision 3).** Available if the captain wants it; not the primary recommendation.
