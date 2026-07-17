## Context

Batch 9 of the logic-core rework, and the batch the exec-stream contract (`add-exec-stream-contract`) explicitly deferred: `init` is the one consumer still off the contract "(secrets ride in its exec `environment=`; its own batch)", and `exec_stream` grew its `environment=` passthrough specifically so this migration would be unblocked (`core/exec_stream.py:173-175`).
Unlike batches 7 and 8, no separate recon preceded this change: this design owns both the audit and the proposal, and every line reference below was read at current HEAD (`9506e5e`) rather than inherited from prior proposals' prose (batch 1's Non-Goals error is the named anti-pattern).

### Audit: what `init` actually is at HEAD (1292 lines)

| Lines | What | Migrates? |
| --- | --- | --- |
| `50`, `306`, `351` | Imports the `add_path` Typer COMMAND from `commands/config.py` and calls it mid-flow (prints another command's stdout inside init) | **Yes.** Becomes `config_utils.add_custom_path` in the core plus a typed event; the last frontend-calling-frontend edge dies |
| `61-65` | `InitInputs` mutable dataclass | **Deleted.** The frontend keeps plain locals; the core takes keyword params |
| `68-106` | `_validate_slug` / `_validate_site_name` (print + `Exit(1)`) | **Yes.** Public core validators raising `CwcliError(USAGE)` with the same messages (Decision 7) |
| `109-135` | `_prompt_for_inputs` (questionary project-name prompt, Ctrl-C exit 0) | **No.** Prompt UX stays frontend; validation moves (Decision 7) |
| `138-211` | `_exec_in_container`: hand-rolled demux/decode loop, single un-polled `exec_inspect`, `.get("ExitCode", 1)` dead default, ENOSPC hint on the buffered path only | **Yes.** Becomes `core.exec_stream` consumption (Decision 5) |
| `214-229` | `_generate_admin_password`, `_is_interactive_session` | **No.** TTY-coupled secret UX stays frontend (Decision 3) |
| `232-246` | `_directory_exists` / `_ensure_directory` (buffered argv `exec_run`) | **Yes.** Move as core-private helpers, buffered as today |
| `249-267` | `_bench_name_validation` (questionary inline validator) | **No.** Inline prompt validation is frontend UX; the core re-validates the final name anyway |
| `270-352` | `_resolve_bench_target`: existence probe, tri-state `reuse_bench`, the interactive decline-and-rename loop, non-TTY refusal, `add_path` | **Split.** The decision moves to the core (`confirm_reuse_bench` / `CONFLICT`); the prompt loop and refusal renderings stay frontend (Decision 4) |
| `355-462` | pyenv/nvm probe + install helpers, yarn install (buffered `exec_run`, soft-fail warnings) | **Yes.** Move verbatim, buffered; soft failures become `Result.warnings` + events |
| `465-466` | `_build_cd_command` | **Yes.** Moves as-is |
| `469-549` | `DEFAULT_FRAPPE_BRANCH`, `_SEMVER_RE`, `resolve_frappe_ref`, `_resolve_frappe_branch`, `_frappe_major_version`, `_select_mariadb_flag` | **Split.** The resolvers and gates move to the core; the `--frappe-branch`/`--version` mutual-exclusion error stays frontend (Decision 2) |
| `552-570` | `_run_host_command` (its `use_spinner=True` branch is DEAD: both callers pass `False`) | **Yes.** Core subprocess with captured output; the dead branch dies with the move (Decision 6) |
| `573-615` | `_get_latest_bench_tag` (Docker Hub, fail-open to `v5.29.1`), `_download_github_file` | **Yes.** Core-owned network I/O, `PRECONDITION` on hard failure (Decision 10) |
| `618-674` | compose pull/up (host `docker compose` subprocess), `_setup_project_directory` | **Yes.** Stage 1 (Decision 6) |
| `677-726` | `_customize_compose_ports` (port ranges, bench image pin) | **Yes.** Stage 1 |
| `729-757` | `_wait_for_containers_running` (bounded silent poll via `ensure_containers_running(prompt=False)`) | **Yes.** Stage 1's poll over `resolvers.resolve_container_state` (Decision 8) |
| `760-852` | The Typer signature (14 params) | **No.** Frontend |
| `873-916` | Fail-fast ordering: ref resolve, password resolve, name prompt + validation, port-conflict check | **Split.** Ordering preserved by the frontend calling core validators up front (Decision 7); the port check moves to stage 1 (`CONFLICT`) |
| `921-970` | Dual verbose/non-verbose infra block | **Collapsed.** One stage-1 call; rendering forks on events in the frontend |
| `972-1097` | Parent mkdir, bench resolve, version gating, `bench init`, setuptools pin | **Yes.** Stage 2 |
| `1099-1198` | 4 `set-config` execs, site probe, `bench new-site` (secrets), 2 final configs | **Yes.** Stage 2; secrets per Decision 3 |
| `1200-1254` | ERPNext get-app + install-app (dual blocks) | **Yes.** Stage 2 |
| `1256-1259` | `db_utils.clear_cache_for_project` | **Yes.** Stage 2's epilogue, as today |
| `1261-1292` | Elapsed time, success block, generated-password print-once | **No.** Frontend, driven by `InitReport` |

Exec inventory: 10 execs flow through `_exec_in_container` at runtime (bench init, 4 set-configs, new-site, 2 final configs, 2 ERPNext) - these adopt `exec_stream`.
The 9 buffered `exec_run` calls (2 directory helpers, 3 pyenv, 2 nvm, yarn, setuptools pin) stay buffered argv calls; they were never on the streaming loop.
(The contract docstring's "init 11" was an estimate; this table is the audited count.)

## Goals / Non-Goals

**Goals.**
Move init's logic onto the core behind typed envelopes; make init the exec-stream contract's final consumer with the honest exit code; preserve the secret env-transport byte-exactly; kill the `add_path` frontend-calling-frontend edge; preserve every observable behavior including the captain's both-modes contract and idempotent re-runs.

**Non-Goals.**
Any behavior change to version gating, the existing-bench flow, prompts, exit codes, or the compose file handling.
Fixing the ENOSPC hint being dead when streaming (a behavior improvement the batch-3 audit already scoped out of migrations; it stays dead on the streamed path and alive on the drained path, exactly as today).
An `axi init` verb (Decision 9: deferred as its own captain decision, absence asserted).
The `restore`/`rm`/`config` migrations and `self_update`'s mutating half (the un-migrated list, restated from the code).
Randomizing `db_root_password`'s `"123"` default (coupled to the downloaded compose's hardcoded `MYSQL_ROOT_PASSWORD: 123`; shielded, not changed).
Fixing `db_utils.cache_project_data`'s non-transactionality (the standing hazards-board item; init only calls `clear_cache_for_project`).

## Decisions

### 1. TWO sequential core calls, and why neither prior shape fits

`core.init_instance(project_name, *, port=8000, auto_start=False, on_event=None) -> Result[InstanceUp]` and `core.init_bench(project_name, *, bench_name, site_name, bench_parent, frappe_ref, db_root_password, admin_password, reuse_bench=None, install_erpnext=False, erpnext_branch, auto_start=False, on_event=None) -> Result[InitReport]`.
Both are plain functions with the `OnEvent` callback (the `core.update` shape); neither is a generator, for `update`'s settled maintenance-mode/GC reasons.

The two-call seam is this batch's first-class structural finding, and it is motivated by neither of the shipped two-call reasons:

- Not `run_plan`/`run_stream`'s generator laziness (nothing here returns an iterator).
- Not plan/apply (nothing is previewed; both calls mutate).

The forcing function is a MID-FLOW user decision on the COMMON path.
The existing-bench question can only be asked after the containers run (its input is a `test -d` inside the container stage 1 creates), and it fires on essentially every default interactive init because the devcontainer image ships `/workspace/frappe-bench` (the `cwcli-lifecycle` skill's own observation).
Under a one-call shape, resolving that `NEEDS_CHOICE` by the doctrine (frontend prompts, re-invokes) re-runs project-dir setup, the Docker Hub tag query (up to 10s), `docker compose pull` (a registry round-trip even when cached), `compose up -d`, and the readiness poll - roughly 5-15 seconds of redo plus doubled narration, paid even by the default "yes, reuse" answer.
With the seam, resolving `confirm_reuse_bench` re-invokes ONLY `init_bench`, whose pre-decision work is a container resolve plus two subsecond probes.

Rejected alternatives:
**one call with the redo cost** (penalizes the most common interactive flow to preserve a shape symmetry no locked decision requires);
**a choice-resolver callback passed into the core** (a prompt by proxy: it blocks the core on user input, which is the GUI-hostile shape `update`'s generator analysis already rejected, and `NEEDS_CHOICE` exists precisely so the core never waits on a human);
**frontend keeps the infra stages** (leaves compose orchestration, the download, and the port logic un-migrated, which fails the task).

The two calls also mirror what init already is: stage 1's product is an INSTANCE (host dirs, compose, containers) and stage 2's product is a provisioned BENCH+SITE inside it, which is exactly the wizard seam a GUI needs.

### 2. Parameters: the frontend fuses flags, the core takes resolved values

`init_bench` takes one `frappe_ref` (the resolved branch or tag).
The `--frappe-branch`/`--version` mutual-exclusion error stays in the frontend (the `apps` fused-`--yes` and `open` four-flag precedent: flag UX is one frontend's choice), but `resolve_frappe_ref` itself moves to the core as a public function with its `ValueError` retyped `CwcliError(USAGE)` (same message), because the shape resolution is logic every frontend needs, not flag UX.
`_frappe_major_version` and `_select_mariadb_flag` move as core helpers; the Python/Node/setuptools gates key on the major exactly as today (`branch_python` 15/14/13, `branch_node` 14/13, setuptools pin at 13, version-16 falling through to container defaults).
`db_root_password` keeps its `"123"` default IN THE CORE, because the coupling it mirrors (the compose's hardcoded `MYSQL_ROOT_PASSWORD: 123`) lives in the compose file the core itself downloads; the comment moves with it.
`bench_parent` keeps the `rstrip("/") or "/workspace"` normalization, now in the core.

### 3. Secrets: the env-transport contract, byte-exact, with the event surface audited

`init_bench` builds `new_site_cmd` with the same unexpanded `"$CWCLI_DB_ROOT_PASSWORD"` / `"$CWCLI_ADMIN_PASSWORD"` references and passes the values via `exec_stream(container, ["bash", "-lc", cmd], environment=new_site_env)` - the exact `exec_create(environment=)` transport as today (`init.py:163-167`), through the parameter the contract added for this batch.
What each surface may carry, audited:

- **`ExecChunk` / `InitOutput` events**: bench's own output bytes, the same bytes today's `-v` already writes raw to the terminal; bench does not echo the password (E2E-verified per the `cwcli-lifecycle` skill).
  The events widen NOTHING: a consumer that renders them sees what today's terminal sees.
- **The command-echo trace event**: the command STRING, which contains the `$CWCLI_*` references and never the values (pinned by test, as `test_init_admin_password.py` pins today's echo).
- **`InitReport`**: no password field at all. The frontend generated or received the password, so it needs nothing back.
- **`CwcliError.detail` on a failed new-site**: the exec's captured output, same exposure as today's failure path.

Admin-password generation (`secrets.token_urlsafe(18)`), the `_is_interactive_session` gate (stdin AND stdout TTYs), the non-interactive refusal, and the print-once-gated-on-creation stay in the frontend: they are TTY-coupled secret-handling UX, and a GUI would implement its own (a form field, not a generated print).
The print gate becomes `generated and report.site_created`, which is today's `admin_password_generated and not site_exists` expressed on the returned fact instead of a probed local.
The `docker top` residual exposure (bench's flag-only interface expands the env ref onto the leaf argv) is inherent, inherited, and out of scope, per the skill.

### 4. The existing-bench decision: `confirm_reuse_bench`, with the rename loop as repeated re-invokes

`init_bench` probes the bench path and honors the tri-state exactly as `_resolve_bench_target` documents (issue #41):

- `reuse_bench=True`: reuse, `bench init` skipped, `InitReport.bench_created=False`.
- `reuse_bench=False`: `CwcliError(CONFLICT, "bench.exists")` with today's message naming the path and advising a different `--bench`.
- `reuse_bench=None` and the bench exists: `NEEDS_CHOICE` with the NEW `Choice(kind="confirm_reuse_bench", param="reuse_bench", prompt=...)`, carrying the bench path so the frontend can render today's yellow notice plus confirm.

The CLI resolves it preserving issue #20's flow: on a TTY it renders the reuse confirm (`auto_enter=False`); Yes re-invokes with `reuse_bench=True`; No prompts for a replacement name (questionary inline `_bench_name_validation` stays frontend) and re-invokes with the new `bench_name` (still `reuse_bench=None`, so an also-existing name surfaces the choice again - the loop is the natural fixpoint of re-invocation, each round costing subsecond probes); a cancelled prompt or blank name exits 0 with "No changes made." exactly as today.
On a non-TTY it renders today's refusal naming `--reuse-bench` and `--no-reuse-bench`, exit 1 (the `select_editor` non-TTY precedent: the core returns the choice, TTY-ness is the frontend's fact).
`add_custom_path` runs in the core after the decision, on every outcome, as `add_path` does today; its added/already-present outcome rides an event the frontend renders as today's stdout line.

### 5. The 10 streaming execs adopt `exec_stream`; the probes stay buffered

Every `_exec_in_container` call becomes: build the command string (unchanged, `shlex.quote` on non-secret interpolations), emit the command-echo trace, consume `exec_stream(container, ["bash", "-lc", cmd], workdir=None, environment=...)`.
Consumption modes per the contract: forward each `ExecChunk` as an `InitOutput` event (the verbose renderer writes `stdout` chunks via raw `sys.stdout.write` and `stderr` chunks to `sys.stderr`, preserving carriage returns exactly as `init.py:175-185` does today), or drain-and-join when no renderer wants bytes (the non-verbose spinner path - and the joined text is what feeds the ENOSPC check, so the hint stays exactly as alive as today: present on the drained path, dead on the streamed path).
A non-zero `ExecDone.exit_code` raises `CwcliError(PRECONDITION, "init.exec_failed", f"Command failed with exit code {code}: {command}")` - today's message - or the ENOSPC variant (`"init.disk_full"`, today's message) when the drained output shows it.
A lost stream or unknowable code raises the contract's typed `DOCKER` errors instead of today's `Command failed with exit code None`; this is the batch's one disclosed error-path hardening, same class as batch 7's (an honesty improvement on a failure path, not an outcome change - both paths exited non-zero).
The pyenv/nvm/yarn/setuptools/probe helpers keep buffered `exec_run` argv calls (they block to completion and read real exit codes; adopting the streaming contract for them would be change without behavior).

### 6. Host-side `docker compose` stays in the core, captured, with one disclosed verbose drift

Stage 1 shells out to `docker compose -p ... pull/up -d` via `subprocess` from the core.
Precedent: the core owns I/O and `core/version.py` already spawns a host subprocess; the purity ban covers `rich`/`questionary`/`typer`, not `subprocess`.
Compose has no docker-py API, so a subprocess is the only mechanism; a non-zero exit raises `CwcliError(DOCKER, "compose.failed", ...)` with the captured stderr in `detail`, which the frontend renders as today's `Host command failed` + stderr.
**One disclosed drift**: today's `-v` mode runs pull/up with INHERITED stdio (`capture_output=False`), so docker renders its TTY progress bars directly; the core always captures and streams lines as events, and compose on a pipe emits plain progress lines instead.
Cosmetic, `-v`-only, in a subprocess's own output; the alternative (inherited stdio from the core) would write to a terminal the core must not assume exists and would corrupt any structured frontend.
`_run_host_command`'s `use_spinner=True` branch is dead at HEAD (both callers pass `False`) and dies with the move.

### 7. Fail-fast validation ordering is preserved by public validators

Today a bad site name fails before ANY work (`_prompt_for_inputs` validates all three names up front, `init.py:898`, before the port check at `:905`).
Under the two-call shape, naive placement would validate `site_name` only when `init_bench` runs - after containers were created.
So the name validators are PUBLIC core functions (`validate_project_slug`, `validate_bench_slug`, `validate_new_site_name` - deliberately distinct from `resolvers.validate_site_name`, which is the bench-op shell-metacharacter guard with different rules and purpose), raising `CwcliError(USAGE)` with today's exact messages.
The frontend calls them immediately after input collection (preserving today's error ordering byte-for-byte), and `init_instance`/`init_bench` call them again on their own params (a core function must not trust its caller's discipline for its own contract).
The port-conflict check moves into stage 1 ahead of any filesystem work, as today (`CwcliError(CONFLICT, "ports.in_use")`, hint carrying the `--port` tip; the frontend renders today's three lines).

### 8. `confirm_start` on both stages, with a structural cap

**Stage 1** ends with the bounded silent poll (today's `_wait_for_containers_running`, re-expressed over `resolvers.resolve_container_state(auto_start=False, offer_choice=False)` with the `NOT_RUNNING` raise caught per attempt - never prompting, exactly what `prompt=False` achieves today).
On timeout with `auto_start=False` it returns `NEEDS_CHOICE`/`confirm_start`; with `auto_start=True` it raises `CwcliError(NOT_RUNNING)` (fail closed - the caller claimed the start was handled and the containers are still down).
The frontend ALWAYS calls stage 1 with `auto_start=False` first; on `confirm_start` it runs `ensure_containers_running(require_running=True, auto_start=<the user's --auto-start>)` - today's exact prompt/refusal/start path, byte-identical messages and exit 1 - then re-invokes with `auto_start=True`.
The cap is therefore structural: a second failure is the typed `NOT_RUNNING`, not a loop (the `backup` `started`-flag pattern without the flag).
No core function performs the start; "the actual (UI-coupled) start is the caller's job" is the settled convention (`core/inspect.py:598-600`), and the frontend's `ensure_containers_running` is that caller.
The rare-redo cost (an idempotent stage-1 re-run on the wedge path) is accepted: the wedge path is the exceptional one, unlike the reuse question Decision 1 exists for.
**Stage 2** opens with `resolve_container_state(auto_start=auto_start, offer_choice=True)` - `run_plan`'s exact race backstop - and the frontend resolves it with the same capped once-retry.

### 9. No `axi init` verb this batch, deferred as its own decision, absence asserted

The position, from the evidence both ways:

- **For a verb**: init is non-destructive (it creates, and refuses rather than clobbers); `axi backup` proves minutes-long verbs with one terminal DTO are acceptable; agents already drive `cwcli init` non-interactively through the human CLI (issue #41 built that; the E2E harness is an agent doing exactly this), so no doctrinal barrier exists.
- **Against shipping it HERE**: every shipped `axi` mutation verb operates on an instance the USER already created; a verb that creates instances (pulls gigabytes of images, allocates ports, writes host state) is a resource-and-product decision of the `axi apps install`/`uninstall` class - the captain owns those on their own evidence, not as a rider on a refactor.
  It also forces agent-surface secret UX (a generated-password path cannot exist non-interactively, so `--admin-password` lands on the agent's argv) and a 10-20 minute blind wait whose progress-reporting story deserves design, not defaulting.
  And the batch is already the largest migration; batch discipline severs decisions.

So: no verb, a test asserts the `axi` registry has no `init` (the `apps`/`open` precedent, so the deferral is legible), and the question is surfaced to the captain as a follow-up decision rather than silently omitted.
Unlike `open` (structurally impossible) the assertion's docstring records this as DEFERRED: the two-call core shape means the verb is thin whenever it is decided.

### 10. Zero new primitives, as a falsifiable claim, with two flat spots reported

| Need | Existing primitive |
| --- | --- |
| the streaming execs + secrets | `core.exec_stream(..., environment=)` (built for this batch) |
| frappe container | `core/docker.py:get_frappe_container` |
| run-state, poll, race backstop | `resolvers.resolve_container_state` (both `offer_choice` modes, as `inspect` proved) |
| the events | the `OnEvent` idiom (`core/update.py`, `core/inspect.py`) |
| the envelope + choices | `Result`/`Choice`; `confirm_reuse_bench` is a new token in the open `Choice.kind` set |
| search-path registration | `utils/config_utils.py:add_custom_path` (already a util; only its Typer wrapper was mis-layered) |

Deliberately NOT reached for (the Decision-8-of-batch-3 trap, applied again): `resolvers.resolve_bench` (init creates benches, it does not resolve cached ones), `resolve_default_site`, `require_bench_dir` (init probes with its own semantics: absence is normal, not an error), `resolvers.validate_site_name` (an injection guard, not a naming policy - Decision 7).
Two flat spots, reported per the standing rule rather than absorbed:
(1) the closed `ErrorKind` set has no network kind, so the compose-download hard failure and the (fail-open) Docker Hub path use `PRECONDITION`; widening a closed enum is a primitive change this batch refuses to smuggle, and exit codes are identical either way.
(2) init is the first core verb shelling out to a host CLI; no primitive exists or is added (a `_compose()` helper stays private to `core/init.py` until a second caller exists - the extract-on-second-caller rule from batch 1).
If implementation finds a genuine bend, it is reported, per the standing rule.

## Boundary discipline

`core/init.py` imports no `rich`, no `questionary`, no `typer`; `tests/test_core_envelope.py`'s AST ban covers it automatically.
It never prints, prompts, exits, or hands over; all narration rides typed events; no live Docker object crosses either return boundary (`InstanceUp` and `InitReport` are plain strings and bools; `asdict` yields plain data, asserted by test).
No secret value appears in any event, warning, DTO field, or the command-echo trace (Decision 3, pinned by test).
`commands/init.py` no longer imports `commands/config.py`, `utf8_stream_decoder`, or `db_utils`; after this batch `utf8_stream_decoder`'s only consumer is `core/exec_stream.py`.

## Risks / Trade-offs

- **The reuse-decline rename loop becomes re-invocations** -> each round is a container resolve plus two subsecond probes; behavior (prompt texts, cancel-exit-0, continue-on-fresh-name) is pinned by the re-pointed `test_init_reuse_bench.py` assertions before and after.
- **`-v` compose output loses TTY progress bars** -> disclosed in Decision 6; plain streamed lines, `-v`-only, subprocess-cosmetic.
- **The lost-stream failure message changes** -> disclosed in Decision 5; both paths exit non-zero, the new one is the contract's typed honesty.
- **The four init suites are helper-bound** (`_resolve_bench_target`, `_wait_for_containers_running`, `_exec_in_container` internals) -> characterization-first (batch 4's discipline): command-level tests pinning exec order, command strings, skip-on-exists gating, message texts, and both-modes refusals are written through migration-surviving seams (subprocess, container fakes, questionary, isatty) and committed green against unmigrated HEAD; helper-bound tests then move with their subjects, and every test changed BY DESIGN is named in tasks.
- **A 10-20 minute E2E per leg** -> the existing `tests/e2e/test_init_e2e.py` already builds real benches and stays green unchanged; the new interactive legs reuse ONE throwaway instance per the captain standard.

## Migration Plan

`tasks.md` order: characterization pinned green first, then `core/init.py` under `tests/test_core_init.py`, then the frontend reseated and the four suites re-pointed, then the no-`axi init` assertion and dead-code deletion, then E2E in both modes on one throwaway instance, then docs, ledger, and skills.

## Open Questions

None blocking. Two recorded:

1. **`axi init`** is deferred as the captain's own product decision (Decision 9); the core shape makes it thin if approved.
2. **The ENOSPC tail-buffer improvement** (restoring the hint on streamed execs via a retained tail in the primitive) remains the batch-3 audit's recorded non-item; adopting it would be a contract change, not an init change.
