## Context

This is the rework's Step 3, batch 2: the migration after `core-logic-foundation` (the `backup` reference slice), `migrate-start-status-core`, and `migrate-unlock-stop-core` (batch 1, merged as PR #77).
It follows the established pattern with no new architecture: a UI-pure core function returning a typed `Result[T]` or raising `CwcliError`, a reseated thin CLI frontend, and `cwcli axi` verbs over the same core.

OpenSpec reads no code, so every `file:line` below comes from a first-hand audit of the real worktree done before this proposal was written, not from the backlog and not from the prior recon alone.
Where the audit and the recon disagree, the audit wins and the disagreement is called out (see Decisions 3 and 6).

### Audit: what is genuinely migrated

Verified per command by import inspection and by reading each frontend, not by the presence of a file in `core/`.
The test applied to each: is the logic in `core/`, is the CLI a thin frontend over it, and does a conforming `cwcli axi` verb exist?

| Command | `core/` module | CLI reseated | `axi` verb | Verdict |
|---|---|---|---|---|
| `backup` | `core/backup.py` | yes | `axi backup` (`axi.py:225`) | **done** |
| `ls`/`list` | `core/list.py` | yes | `axi ls` (`axi.py:182`) | **done** |
| `where` | `core/where.py` | yes | `axi where` (`axi.py:199`) | **done** |
| `start` | `core/start.py` | yes | `axi start` (`axi.py:313`) | **done** |
| `status` | `core/status.py` | yes | `axi status` (`axi.py:421`) | **done** |
| `restart` | `core/restart.py` | yes | `axi restart` (`axi.py:446`) | **done** |
| `unlock` | `core/unlock.py` | yes | `axi unlock` (`axi.py:256`) | **done** (batch 1) |
| `stop` | `core/stop.py` | yes | `axi stop` (`axi.py:290`) | **done** (batch 1) |
| `self_update` | `core/version.py` | yes (`self_update.py:54`) | **none, deferred** | **logic-migrated, verb-deferred** |

`self_update`'s row is the one this batch acts on, and it is the row `migrate-unlock-stop-core/proposal.md:56` got wrong in prose while `design.md:24-25` got right in the audit.
`core/version.py` was UI-pure from its first commit; `commands/self_update.py:54` is `result = core_version.check(use_cache=not no_cache)` followed by rendering and exit codes only, and its module docstring says exactly that.
There is no migration to do; there is only a verb to add or defer, which is a decision (Decision 5).

**Remaining genuinely un-migrated:** `inspect`, `restore`, `rm`, `apps`, `config`, `init`, `run`, `open`.
`logs` is a partial (imports `core.supervision`/`core.resolvers`, has no `core.logs` and no verb).
The proposal's Non-Goals carries this list; it is stated in both places on purpose, because the imprecise version of it in batch 1 is what mis-scoped this batch.

### Audit: the batch

`label` (150 lines) maps step for step onto the migrated references, needing no primitive that does not exist:

| # | `label` step | `label.py` | Primitive | Exists? |
|---|---|---|---|---|
| 1 | read cached benches | `:58-59` | `db_utils.get_cached_project_data` | yes |
| 2 | no benches -> "run inspect first" | `:61-66` | `CwcliError(NOT_FOUND)` + `hint` | yes |
| 3 | no selector -> list mode (read-only) | `:69-71` | a DTO, not a primitive | n/a (Decision 1) |
| 4 | resolve selector -> bench | `:73` | `resolvers.resolve_bench` | yes (Decision 2) |
| 5 | no match -> error + bench list | `:75-80` | `CwcliError(NOT_FOUND)` | yes (Decision 2) |
| 6 | validate label + duplicate check | `:88-98` | `bench_labels.validate_user_label` (pure) + a sibling scan | yes; the scan is label-specific logic belonging IN `core.label` |
| 7 | resolve frappe container | `:103` | `core_docker.get_frappe_container` | yes |
| 8 | require running, never auto-start | `:104-110` | `resolvers.resolve_container_state(offer_choice=False)` | yes - **the interesting one** (Decision 3) |
| 9 | write/clear the marker in-container | `:118,133` | `bench_labels.write_label_marker` / `clear_label_marker` | yes, already UI-pure |
| 10 | write the DB label | `:125,142` | `db_utils.set_bench_label` | yes |
| 11 | print result + refreshed list | `:131,143,148-150` | frontend | n/a |

**New primitives required: zero.** Same result batch 1 got for `unlock`, by the same test.
One existing primitive needs a string widened (Decision 3), which is disclosed rather than absorbed.

`run` and `self_update`, the other two commands originally scoped into this batch, are excluded on evidence recorded in the proposal's Why and Non-Goals.
`run` IS the deferred streaming machinery (`run.py:69`, twinned with the deferred `apps.py:70` and `update.py:37`); `self_update` is already migrated.

## Goals / Non-Goals

**Goals:** close the agent-surface bench-discovery dead end with `cwcli axi benches`; migrate `label` with zero new primitives and disclose the one widening honestly; retire the `offer_choice=False` branch into its first production caller; move the `cached_benches` helper down into the layer that can share it; give `label` the E2E its consistency invariant deserves; add the free read-only `axi self-update --check`; and correct the un-migrated record batch 1's prose got wrong.

**Non-Goals** (the proposal holds the full list): `run`, the mutating `axi self-update`, `open`, streaming machinery, plan/apply, the monsters, the AXI cross-cutting shell, and the set-path DB-write asymmetry.

## Decisions

### 1. `label` is two operations, so `core.label` is three functions, not one with a mode flag

`commands/label.py:69-71` returns the bench list when `bench_selector is None`.
This is NOT a `NEEDS_CHOICE` fork, and modeling it as one would be a category error: a missing selector is a legitimate read request, not an ambiguity the core cannot resolve.
The `NEEDS_CHOICE` contract exists for decisions the core cannot make from its params (`core/envelope.py:38`); "list the benches" is a decision the caller already made.

- **Decision.** `core/label.py` exposes `list_benches(project) -> Result[BenchList]` (the read), plus `set_label(project, *, bench=None, label) -> Result[LabelOutcome]` and `clear_label(project, *, bench=None) -> Result[LabelOutcome]` (the mutations).
- **Why set/clear are two functions rather than `set_label(label=None)`.** A `None`-means-clear parameter is precisely the sentinel batch 1 retired from `_stop_project`, where `None` was standing in for a typed distinction (`design.md:77`). `db_utils.set_bench_label(project, path, None)` keeps that convention at the DB layer, where it is a storage detail; the core does not propagate it into a public API that a GUI and an agent surface both consume. Two functions also give each an honest signature: `clear_label` has no `label` param to pass `None` to.
- **Consequence for the frontends.** The human CLI dispatches on its existing positional arguments (`label.py:32-42`), which is why its surface is preserved exactly. The axi surface gets two verbs (Decision 7), which is what an agent should see anyway.

### 2. The selector resolves through `resolvers.resolve_bench`, and the duplicate check re-keys on path

Today `label.py:73` calls `bench_labels.resolve_bench(benches, selector)` directly, getting back the bench **dict**, and `label.py:91-93`'s duplicate check relies on that dict's **identity** (`other is not chosen`).
`resolvers.resolve_bench` (`core/resolvers.py:99`) returns a **path string** instead, and already wraps `bench_labels.resolve_bench` at `:131`.

- **Decision.** `core.set_label`/`core.clear_label` resolve the bench through `resolvers.resolve_bench(project, bench, None)`, the same primitive every other bench-scoped verb uses. The duplicate check compares by path (`other["path"] != chosen_path and other.get("label") == label`) instead of by dict identity.
- **Why this is a fix, not a workaround.** A bench IS its path: `db_utils.set_bench_label(project_name, bench_path, label)` (`db_utils.py:486`) keys on path, and the marker lives at a path. Path is the natural key; dict identity was an artifact of having the dict in hand. The two are equivalent within a single call, and path survives a re-read of the cache while identity does not.
- **What it buys.** `label` inherits the family contract for free: an explicit selector resolves or raises `NOT_FOUND`; no selector on a single-bench project uses that bench with a `bench.sole` warning; no selector on a multi-bench project returns `select_bench` `NEEDS_CHOICE`. That last two matter only to the axi verb and a future GUI, because the human CLI always passes a selector when mutating (no selector is list mode, Decision 1). It means `cwcli axi label proj --set staging` works on a single-bench project and is an honest usage error on a multi-bench one, exactly like `axi backup`.
- **The "available benches" list stays a frontend concern.** `label.py:78-79` prints the bench list when a selector does not match. `resolvers.resolve_bench`'s `NOT_FOUND` does not carry that list, and it is NOT widened to carry it: stuffing presentation data into an error's `detail` to preserve one rendering would be the exact primitive-bending this batch exists to detect. The frontend catches `NOT_FOUND` and calls `core.list_benches` to render the list. The error taxonomy stays clean and the output is preserved.

### 3. The `NOT_RUNNING` hint is widened, and this is reported, not absorbed (the disclosed finding)

`label` is the first production caller of `resolve_container_state(offer_choice=False)`.
Verified: the only three production call sites (`core/backup.py:54`, `core/unlock.py:75`, `commands/utils.py:66`) all pass `offer_choice=True`; the branch's only exercise is `tests/test_core_resolvers.py:59`.
`label.py:100-110` is exactly its intended shape, and the comment there states the intent: "Do NOT auto-start; a label change should not spin up a stopped project."

The branch fits `label` structurally and needs no change.
Its **hint string** does not:

```python
# core/resolvers.py:84-89
raise CwcliError(
    ErrorKind.NOT_RUNNING,
    "container.not_running",
    f"Frappe container for project '{project_name}' is not running.",
    hint=f"Pass --yes to auto-start it, or start it first with 'cwcli start {project_name}'.",
)
```

`label` has no `--yes` flag and never will, so this hint would tell a user (and an agent, via axi's `help:` line) to pass a flag that does not exist.

- **Decision.** Add a `not_running_hint: str | None = None` parameter defaulting to today's string, so existing callers are byte-for-byte unchanged and `label` supplies "Start the project first - the label marker is stored inside the bench." (preserving its current message at `label.py:107-108`).
- **Why report it.** Batch 1's Decision 1 set the standard: "Implementation MUST report if any existing primitive needed changing to fit." This is a string-parameterization, not a structural bend - the control flow, the signature semantics, and the return contract are unchanged, and no caller's behavior moves. But it is a genuine, if small, instance of a shipped primitive carrying an assumption baked in by its first callers ("every caller has a `--yes`"), and it was found by the first caller that violates the assumption. That is the falsifiable claim working as designed, and a batch that quietly widened the string and reported "zero primitives touched" would have destroyed the signal the exercise exists to produce.
- **The honest verdict to carry into the PR.** Zero new primitives; zero structural bends; ONE hardcoded caller-specific string parameterized. The foundation generalized to its third and fourth callers with one flat spot, in exactly the place a hardcoded English string would be expected to have one.
- **Rejected: let `label` catch the `NOT_RUNNING` error and re-raise with its own hint.** It hides the flat spot instead of fixing it, and leaves the next `offer_choice=False` caller to rediscover it.

### 4. The set/clear consistency asymmetry is PRESERVED verbatim, and here is why it is not a bug

`label`'s two-store write is the invariant CLAUDE.md's captain standard names by name ("`label --clear` DB/marker consistency").
The clear path is careful and comments its reasoning (`label.py:114-124`): clear the marker FIRST, touch the DB only on success, "because clearing the DB while the marker survives would let a later full inspect resurrect the old label."
Both of its failure modes are guarded and unit-pinned (`test_bench_label_db_and_command.py:171` and `:190`).

The set path is NOT symmetric: `label.py:142` calls `db_utils.set_bench_label(...)` and ignores its return value, though it returns `False` for an uncached bench (`db_utils.py:505`, pinned by `test_set_bench_label_unknown_returns_false:74`).

- **The asymmetry is correct, and the reason is the recovery direction.** On CLEAR, marker-survives-DB-cleared is a resurrection: a later full `inspect` reads the marker (`inspect.py:241`) and restores a label the user deleted. That is silent data resurrection, so it must fail closed. On SET, marker-written-DB-missed is self-healing in the same direction: the marker is the source of truth, so the next `inspect` recovers exactly the label the user asked for. The failure is also immediately visible, because `label.py:148-150` prints the refreshed list from the DB, which would show the label absent.
- **Decision.** Preserve both paths verbatim in `core.set_label` / `core.clear_label`, including the unchecked return on set. Record the reasoning here so a future reviewer does not "tidy" the asymmetry into symmetry and, in doing so, either turn a self-healing case into a hard failure or weaken the clear-path guard to match the set path.
- **Honesty about this rationale.** The reasoning above is INFERRED from the code's recovery semantics, not recorded anywhere in the repo. The clear path documents its own why; the set path documents nothing. So this is a defensible reading, not a retrieved decision, and the migration therefore preserves behavior and flags the question rather than acting on the inference. Changing it either way is a behavior change that does not belong in a migration whose contract is "not breaking".

### 5. `axi self-update --check` exits 0 when an update is available (a deliberate divergence)

`cwcli self-update --check` exits **1** when an update is available, deliberately: `self_update.py:13` records it as "so scripts/CI can gate", and `self_update.py:81` implements it.
The obvious move is to mirror that. It is wrong for the agent surface.

- **The axi contract reports state in the document, not the exit code.** Exit 1 means *error* to an agent (`core/errors.py:17-20`: every `ErrorKind` maps to 1, `USAGE` to 2). A read verb that successfully answers "you are outdated" has not failed; it has done its job. The established precedent is unambiguous: `axi status` (`axi.py:441`) exits 0 unconditionally after a successful read, so a fully offline project reports `overall: offline` with exit 0 rather than exit 1. `axi ls` on zero instances is a definitive empty state at exit 0, not an error.
- **Decision.** `cwcli axi self-update --check` exits 0 on any successful read and carries the answer in the document's `is_outdated` field, which `VersionInfo` (`core/version.py:52`) already has. The agent reads a field; it does not need the exit code as a second, lossier channel. Exit 1 is reserved for a genuine failure. A fail-open PyPI lookup (`latest: null` with the `pypi.unreachable` warning, `version.py:69`) is also exit 0, matching the human CLI's `--check` (`self_update.py:80`, "a read-only check must not punish a flaky network").
- **Why the divergence is safe.** These are different surfaces with different contracts, and the human CLI's exit code is untouched. A shell script gating CI keeps using `cwcli self-update --check`; an agent uses the field. Documenting the divergence is the whole cost.
- **Rejected: mirror exit 1.** It would make `axi self-update --check` the only axi read verb where a successful read exits non-zero, and would train an agent to treat exit 1 from an axi verb as sometimes-not-an-error, which corrodes the contract for every other verb.
- **Dev/uvx installs** (`version.py:41-45`): the DTO already carries `method` and `upgrade_command: None`, so the verb emits it and exits 0. No special case; the human CLI's early prose returns (`self_update.py:59-73`) are rendering, and rendering is what the frontend owns.

### 6. `cwcli label --verbose` is a dead flag; it is kept and made honest, not removed

The recon flagged the dead `verbose` param on `bench_labels.py:133,168,190` as a flag-only drive-by.
The audit finds one step further: `commands/label.py:43`'s `--verbose` CLI option feeds ONLY those three dead params (its only uses are `label.py:118` and `label.py:134`).
So `cwcli label --verbose` has been shipping as a public flag that does literally nothing.

- **Decision.** Delete the dead param from all three `bench_labels` functions (updating `inspect.py:241,610` for the signature only; `inspect`'s own `--verbose` is real and untouched), and KEEP `cwcli label --verbose`, wiring it to print the envelope's `warnings` and the resolved marker path from `LabelOutcome`.
- **Why not remove the flag.** Removal breaks a CLI surface: `cwcli label proj 1 staging --verbose` in someone's script starts exiting 2. The proposal's contract is "not breaking", and there is no gain to weigh against it.
- **Why not leave it a silent no-op.** The reseated frontend receives `Result.warnings` for free, exactly as `unlock --verbose` prints `UnlockOutcome.removed`. Wiring it is about two lines and converts a flag that lies into one that tells the truth. Leaving a knowingly-dead public flag in place after reading it is a choice, and the wrong one.
- **The disclosed nuance.** `--verbose` starts emitting output where it emitted none. That is additive and opt-in: no existing invocation changes its exit code, and no non-verbose output moves. This is the change's only behavior nuance.

### 7. Two axi verbs, and `benches` is the one that pays for the batch

- **`cwcli axi benches <project>`** emits `BenchList`. This is the batch's justification: it is the only structured way an agent can answer "what do I pass to `--bench`", a question every bench-scoped verb asks and none answers (`axi.py:79`: "multiple benches; pass --bench <index|label>"). Neither `InstanceDTO` (`core/list.py:22`) nor `WhereMatch` (`core/where.py:22`) can answer it. It is the same argument that justified `stop` in batch 1 ("the missing sibling of an otherwise complete family"), except the gap here is a dead end rather than an asymmetry.
- **`cwcli axi label <project> [--bench <sel>] --set <label> | --clear`** is the mutation. `--set` and `--clear` are mutually exclusive and one is required; neither given is a `USAGE` error rather than an implicit list, because the discovery verb exists for that.
- **Rejected: one `axi label` verb that lists when `--set`/`--clear` are absent.** Mode-switching on argument presence is the wrong shape for an agent surface: it makes the verb's output schema depend on argv, which defeats the "minimal, predictable default schema" the axi guidelines ask for, and it hides discovery inside a mutation verb's name where an agent looking for it will not think to look.
- **`--bench` is optional, not required.** It falls through to `resolvers.resolve_bench`'s family contract (Decision 2): sole bench resolves, multi-bench is a `select_bench` usage error naming the flag and listing the options via the existing `emit_axi_choice_as_usage_error` (`axi.py:87`). This matches `axi backup`/`axi unlock` rather than `axi restart`'s required `--process`.
- **No prompting, one TOON document, exit 0/1/2**, on the existing helpers. A stopped container becomes the `NOT_RUNNING` error carrying `label`'s own hint (Decision 3), which is the widening paying off immediately on the agent surface.

### 8. `cached_benches` moves down a layer; it is not a new abstraction

`(cached_data or {}).get("bench_instances") or []` appears four times: `core/resolvers.py:128`, `commands/utils.py:272`, `commands/label.py:59`, `commands/label.py:150`.

- **The sharper fact.** One of the four is ALREADY a named private helper: `commands/utils.py:269`'s `_cached_benches`, with two callers (`utils.py:183,211`). The core duplicates it not because nobody noticed, but because `core/` must never import `commands/` - verified, no such import exists, and that direction is the entire point of the layer. The helper is one layer too high to be shared.
- **Decision.** Move it down to `core/resolvers.py` as `cached_benches(project_name) -> list[dict]`, re-point `resolvers.py:128` and `commands/utils.py:183,211` at it, and have `core.label` use it. `commands/utils.py:269` is deleted.
- **Why this does not dent the zero-new-primitives claim.** Batch 1 ruled explicitly that extracting duplicated code "does not count: that is extraction of duplicated code, not a primitive bending to a caller" (`design.md:55`). This is the weaker version of even that: the abstraction already exists and already has a name; it only changes address. Batch 1 extracted its validation helper on the second caller on the principle "one caller is not evidence of a shared concern, two identical ones are" (`design.md:93`). This is at four, with one of them already named.

## The DTOs, concretely

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class BenchInfo:
    index: int               # position in stable sorted-by-path order; NOT durable
    path: str
    label: str | None        # the durable user label, or None

@dataclass(frozen=True, slots=True, kw_only=True)
class BenchList:
    project: str
    benches: list[BenchInfo]

@dataclass(frozen=True, slots=True, kw_only=True)
class LabelOutcome:
    project: str
    bench_path: str
    label: str | None        # the label now in effect; None after a clear
    previous_label: str | None
    marker_path: str         # {bench_path}/.cwcli/.bench-label
    cleared: bool
```

`BenchList` wraps its list in a dataclass rather than returning a bare list, following `WhereResult` (`core/where.py:35-39`), because `emit_result` serializes via `asdict(data)` (`axi.py:62`) and needs a dataclass at the top.
`BenchInfo.index` is included because it is the value `--bench` accepts and the thing an agent came to discover, but it carries the non-durability warning in its docstring: it shifts when a bench is added or removed, which is exactly why user labels exist.
`LabelOutcome.previous_label` is carried because a rename (`cwcli label proj staging prod`, `label.py:54`) is a real, documented use, and "what was it before" is the one fact the caller cannot recover afterward.
`BenchList` deliberately carries no sites/apps/status: the agent surface's default schema stays minimal, and `axi where`/`axi status` already answer those questions.

## Boundary discipline

No live Docker object crosses a `core.<verb>` return boundary: every DTO field above is a builtin or `None`.
`core/label.py` imports no `rich`, no `questionary`, no `typer`; the ban is already enforced by `tests/test_core_envelope.py`, which globs `core/*.py` and will cover the new module automatically.
The multi-bench fork returns `select_bench` `NEEDS_CHOICE` exactly as `core/backup.py` and `core/unlock.py` do; the stopped-container case raises `NOT_RUNNING` rather than offering `confirm_start`, which is the whole point of `offer_choice=False`.
`bench_labels.write_label_marker` (`bench_labels.py:168`) base64-encodes the payload on the host and decodes it in the container, so no label content is interpolated into a shell command; that discipline is preserved and must not be weakened by the reseat.

## Risks / Trade-offs

- **The zero-new-primitives claim is falsifiable, and it came back with one flat spot** (Decision 3). That is a smaller result than batch 1's clean "zero bent", and reporting it that way is the point: the value of the exercise is the signal, not the score. If implementation finds a SECOND primitive needing changes, that is a real foundation signal and belongs in the PR, not absorbed.
- **The two-store consistency invariant is the one genuinely dangerous thing here.** `label` is a mutation across a container marker and the SQLite cache, and cwcli has been burned on this exact line before (CLAUDE.md cites it by name). The ordering is load-bearing and must survive the reseat byte-for-byte. This is why the E2E exists (Decision: proposal's What Changes) and why `test_bench_label_db_and_command.py:171,190` must stay green rather than being rewritten to fit the new shape.
- **`inspect` is touched, and `inspect` is a deferred monster.** The `bench_labels` signature change reaches `inspect.py:241` and `inspect.py:610`. The edit is mechanical (drop one argument at the call site) and `inspect`'s own `--verbose` is untouched, but `tests/test_inspect_label_recovery.py` must stay green: `inspect`'s marker-recovery path is what makes labels survive a cache wipe, and it is a real shipped behavior this batch must not disturb while passing through.
- **The duplicate-check re-key** (Decision 2) changes an identity comparison to a path comparison. Equivalent within a call, but it is a real edit to a validation branch and needs its unit test (`test_reject_duplicate_label:217`) green unchanged.
- **A one-command batch spends a PR's overhead on one command.** Accepted: it closes an agent dead end, retires speculative scaffolding into a real caller, and keeps the pattern boring, which is what this batch is for.

## Migration Plan

1. Move `cached_benches` down into `core/resolvers.py`; re-point `resolvers.py:128` and `commands/utils.py:183,211`; delete `commands/utils.py:269`. Existing tests stay green.
2. Add the `not_running_hint` parameter to `resolve_container_state`, defaulting to today's string. Existing callers unchanged; `tests/test_core_resolvers.py:59` stays green and gains a case for the custom hint.
3. Drop the dead `verbose` param from the three `bench_labels` marker functions; update `inspect.py:241,610`. `tests/test_inspect_label_recovery.py` stays green.
4. `core/label.py`: `list_benches`, then `set_label`/`clear_label` + the DTOs + unit tests, preserving the marker-before-DB ordering on clear and the unchecked DB return on set (Decision 4).
5. Reseat `commands/label.py`; wire `--verbose` to the envelope warnings; re-point the existing 15 unit tests.
6. The `axi benches` and `axi label` verbs + unit tests.
7. `axi self-update --check` + unit tests (exit 0 on outdated, Decision 5).
8. The non-interactive `label` E2E.
9. Docs and skills, including the CLAUDE.md `self-update` line correction.

## Open Questions

- **The set-path unchecked DB write** (Decision 4). Preserved here on an inferred rationale. If the captain wants it symmetric with the clear path, that is a separate behavior change with its own test, not a migration item.
- **Whether `axi benches` should carry each bench's site count or default site.** Leaning no (minimal default schema; `axi status`/`axi where` answer those), but the first agent to use the verb is the real evidence, and adding a field later is additive and cheap.
- **Whether `cwcli label`'s list mode should also gain a `--json` flag** for parity with other human commands. Out of scope here; the axi verb is the structured surface and the rework's position is that agents use `cwcli axi`.
