## Context

This is the rework's Step 3: the first per-batch migration after the foundation (`core-logic-foundation`, the `backup` reference slice) and `migrate-start-status-core`.
It follows the established pattern with no new architecture: a UI-pure core function returning a typed `Result[T]` or raising `CwcliError`, a reseated thin CLI frontend, and a `cwcli axi` verb over the same core.

OpenSpec reads no code, so every `file:line` below comes from a first-hand audit of the real worktree done before this proposal was written, not from the backlog and not from the prior recon alone.
Where the audit and the recon disagree, the audit wins and the disagreement is called out (see Decision 2).

### Audit: what is genuinely migrated

Verified per command, not by the presence of a file in `core/`.
The test applied to each: is the logic in `core/`, is the CLI a thin frontend over it, and does a conforming `cwcli axi` verb exist?

| Command | `core/` module | CLI reseated | `axi` verb | Verdict |
|---|---|---|---|---|
| `backup` | `core/backup.py` | yes (`commands/backup.py:129`) | `axi backup` (`axi.py:223`) | **done** |
| `ls`/`list` | `core/list.py` | yes (`commands/list.py:7`) | `axi ls` (`axi.py:180`) | **done** |
| `where` | `core/where.py` | yes (`commands/where.py:15`) | `axi where` (`axi.py:197`) | **done** |
| `start` | `core/start.py` | yes (`commands/start.py:7`) | `axi start` (`axi.py:254`) | **done** |
| `status` | `core/status.py` | yes (`commands/status.py:29`) | `axi status` (`axi.py:358`) | **done** |
| `restart` | `core/restart.py` | yes (`commands/restart.py:7`) | `axi restart` (`axi.py:383`) | **done** |

Two `core/` modules are NOT commands and correctly have no verb: `core/supervision.py` (the substrate `start`/`status`/`restart` share) and `core/docker.py`/`core/resolvers.py`/`core/envelope.py`/`core/errors.py` (the foundation).
One is a genuine half-state worth recording: `core/version.py` holds `self-update`'s logic UI-pure and is shared with the passive update notice, but `commands/self_update.py` has **no** `axi` verb.
That is a deliberate, documented deferral (CLAUDE.md: "Human CLI only - no `cwcli axi` verb (deferred)"), not an oversight, so `self_update` counts as migrated-without-a-verb rather than done.

**Remaining un-migrated** (confirmed by import inspection: no `..core` import of a verb module): `unlock`, `stop`, `open`, `inspect`, `label`, `restore`, `rm`, `apps`, `config`, `init`, `run`.
`logs` is a partial: it imports `core.supervision` and `core.resolvers` (`logs.py:7-8`) to read the supervision substrate, but has no `core.logs` and no verb.
This matches the dispatch's believed list, with `logs` more accurately described as partial and `self_update` as logic-migrated-verb-deferred.

### Audit: the batch

`unlock` is confirmed as `backup`'s near-twin, quantitatively.
`commands/unlock.py:54-158` and `core/backup.py:55-136` perform the same eight steps in the same order, diverging only at the operation.
The shell-metacharacter list is duplicated character-for-character between `unlock.py:119` and `backup.py:24`, which is the clearest possible signal the two share a substrate that wants extracting.
`unlock` therefore requires **no new core primitives**, which is exactly the property that makes it the cheap generality proof the dispatch asked for.

`stop` proves thin (118 lines, no bench or site resolution, `_stop_project` already returning a proto-DTO at `stop.py:13-49`) and carries a concrete defect the migration fixes (Decision 3).

`open` does NOT prove thin, and is excluded on structural grounds recorded in Decision 4.

## Goals / Non-Goals

**Goals:** prove the foundation's primitives generalize by migrating `unlock` with zero new primitives; extract the bench-op validation substrate on its second caller; migrate `stop` and close the `axi` stdout-purity hole its `rich`-printing helper opens; add the two axi verbs; give `unlock` the both-modes E2E that standing policy already requires.

**Non-Goals** (the proposal holds the full list): `open`, streaming machinery, plan/apply, the monsters, the AXI cross-cutting shell.

## Decisions

### 1. `unlock` adds no new core primitives (the generality test, stated as a falsifiable claim)

`core.unlock` is built from `core_docker.get_frappe_container`, `resolvers.resolve_container_state`, `resolvers.resolve_bench` + `DEFAULT_BENCH_PATH`, `db_utils.get_default_site`, and the envelope, in the same order `core/backup.py:55-136` uses them.

- **Why this is the right first batch.** The foundation extracted its primitives while migrating exactly one command, so "these primitives are general" is currently an untested claim. `unlock` tests it at the lowest possible cost, and the test has a real failure mode: if `core.unlock` needs a primitive bent, widened, or special-cased, the foundation was overfitted and we learn it on 194 lines instead of on `restore`'s 2118.
- **The claim is falsifiable, and that is deliberate.** Implementation MUST report if any existing primitive needed changing to fit `unlock`. Adding a shared validation helper (Decision 5) is expected and does not count: that is extraction of duplicated code, not a primitive bending to a caller.

### 2. `unlock`'s `rm -rfv` is buffered, not streamed (this change overrides the recon)

The recon (`data/cwcli-core-recon-f3/report.md` §1b) identified `unlock`'s verbose path as "the typed event iterator case" and the rework defers streaming machinery to `logs`/`update`.
Taken together those would put `unlock` out of scope for this batch. The audit finds the premise wrong.

- **What the code actually does.** `unlock.py:166-183` streams `rm -rfv <site>/locks` chunk by chunk to `sys.stdout` under `--verbose`. The payload is the removal log of a locks directory: a handful of small lock files, completing in milliseconds. The stream exists so verbose mode can echo which paths were removed, not because the output is large, slow, or unbounded.
- **Decision.** `core.unlock` runs the `rm -rfv` buffered through one `exec_run` (exactly as `core/backup.py:156` runs the backup), parses the `removed` paths out of the output, and returns them in `UnlockOutcome.removed: list[str]`. The CLI prints them under `--verbose`; `axi` serializes the list.
- **Why it wins.** It keeps the batch free of deferred machinery, and the structured `removed` list is strictly more useful to an agent than an opaque byte stream would be. Building an event iterator here would be scaffolding for a millisecond-scale `rm`, with `logs`/`update` (genuinely long-lived, genuinely unbounded) still the real first consumer.
- **The disclosed cost.** Verbose output appears at completion rather than incrementally. Same bytes, same order, same stream; only the timing differs, by well under a second. This is the change's one behavior nuance and is stated in the proposal rather than buried.
- **Rejected: build the event iterator now.** Pulls deferred machinery into the batch to serve a case that does not need it, and would let the iterator's shape be set by its least representative consumer.
- **Consequence.** The "unlock is the streaming case" note is retired. `logs`/`update` remain the streaming contract's first real consumers, and they should design it against their own needs.

### 3. `stop` migrates because a `rich` helper is load-bearing substrate (the concrete defect)

`_stop_project` (`commands/stop.py:13-49`) is imported by four modules: `restart.py:15`, `start.py:135`, `rm.py:1222`, and `axi.py:332`.
It prints to **stdout** (`utils/console.py:6`, `console = Console()`) on two branches: `stop.py:23` on project-not-found and `stop.py:39` on already-stopped.

- **The hole.** `commands/axi.py:332-335` calls `_stop_project` from `axi start --yes`'s port-conflict resolution. The comment claims "(running -> stdout-silent)", which is an assumption about which internal branch the callee takes: it is true only because `detect_port_conflicts` returns projects actively holding ports, which are therefore running. If a conflicting project stops or is removed between detection and the stop call, `_stop_project` takes the `stop.py:23` or `stop.py:39` branch and emits `rich` markup onto `axi`'s stdout, corrupting the single-TOON-document contract. The adjacent code at `axi.py:340-348` already guards against exactly this class of teardown race, so the race is acknowledged by the surrounding code, not hypothetical.
- **Decision.** `core.stop(project) -> Result[StopOutcome]` owns the lookup and the stop loop and prints nothing. All four callers re-point at it. The `axi` path then cannot print by construction, and the comment's fragile assumption is deleted along with the coupling.
- **Why not just fix the print.** Routing those two lines to stderr would patch the symptom while leaving cwcli's most destructive path (`rm`'s backup gate, `rm.py:1229`) and the agent surface depending on a `typer`/`rich`-coupled helper. The root fix is the migration this batch is doing anyway, and it is the smaller diff across the four callers.
- **`_stop_project`'s `None` sentinel becomes a typed error.** Its docstring explains `None` distinguishes not-found from a legitimate zero count. That is precisely `CwcliError(NOT_FOUND)` versus `StopOutcome(already_stopped=True)`, so the sentinel retires into the taxonomy it was approximating.

### 4. `open` is excluded on structure, not on size (the dispatch's conditional, answered)

The dispatch offered `open` "if it proves as thin". It does not, and the reason is not its 362 lines.

- **It never returns.** `open.py:359` calls `exec_into_container`, which uses `os.execvp()` to replace the process (`docker_utils.py:135-157`, documented in the function's own docstring: "DOES NOT RETURN ... No code after this function call will execute"). The core contract is `-> Result[DTO]`. A function that never returns cannot return an envelope, and this is not incidental: handing the terminal to the user IS `open`.
- **There is no coherent axi verb.** `cwcli axi open` would either hand an agent an interactive bash shell or launch a VS Code window on the host desktop. Neither is serializable TOON, so the axi half of the migration has nothing to produce.
- **It depends on a deferred monster.** `open.py:135` and `open.py:217` import `inspect` and `partial_inspect_known_benches`. Migrating `open` cleanly means confronting `inspect` (672 lines, explicitly deferred).
- **Decision.** Exclude. `open` first needs a design answer for how a process-handover command relates to a return-a-DTO core (plausibly: core resolves the target and returns a `LaunchTarget` DTO, and the frontend performs the handover, since the handover is inherently a frontend act). That is a design question deserving its own change, and answering it here would inflate the batch that is meant to prove the pattern is boring.

### 5. Extract the shared bench-op validation on its second caller

The metachar guard and the bench/site `test -d` probes exist once in `core/backup.py:100-136` and are about to exist twice.

- **Decision.** Extract them into one shared helper (a `validate_site_name`/`validate_bench_path` pair plus a `probe` for the two `test -d` checks), and have `core/backup.py` call it. Location settled in implementation between `core/resolvers.py` (where the sibling resolvers live) and a small `core/benchop.py`; the audit leans `resolvers.py`, since these are the same "resolve and validate a bench op's target" concern and a new module for two small functions is not obviously earning itself.
- **Why now and not in the foundation.** One caller is not evidence of a shared concern. Two identical callers is. Extracting at the second caller is the point where the abstraction is justified by fact rather than by anticipation.
- **Guard.** `tests/test_unlock.py:55,65,77` pin that every container command is an argv list rather than a shell string, which is what makes the commands immune to metacharacter interpolation regardless of validation. Those tests re-point at `core.unlock` and must stay green. The validation is defense in depth on top of the argv discipline, and the extraction must not weaken either.

### 6. `unlock` gets a both-modes E2E; `stop` does not need one

- **`unlock`.** It is a prompting command (the auto-start confirm at `unlock.py:55`) with a `--yes` flag, and the captain standard requires every prompting command to be E2E-verified in both interactive (pty-driven) and non-interactive modes. It has none today. That is a pre-existing gap against standing policy, and it means a migration would be refactoring blind. This change adds it, which also gives the migration the same refactor-under-green property that made `backup` the right reference slice.
- **`stop`.** Already driven and asserted by the green net (`test_start_status_e2e.py:103,140` asserts exit 0 and then `status` -> `offline`; `test_status_unsupervised_e2e.py:52`). Per the test discipline, logic refactored with behavior unchanged leaves its E2E untouched. No new `stop` E2E.
- **New behavior gets tests.** The two axi verbs are genuinely new behavior and get unit coverage; whether `axi unlock` also earns an E2E leg is settled in implementation, with the bias toward folding it into the `unlock` E2E's non-interactive mode rather than adding a separate instance-costing test.

## The DTOs, concretely

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class UnlockOutcome:
    site: str
    bench_path: str
    locks_path: str          # {bench_path}/sites/{site}/locks
    removed: list[str]       # paths rm -rfv reported removing; empty when nothing was locked
    already_unlocked: bool   # locks dir absent -> a clean OK, not an error

@dataclass(frozen=True, slots=True, kw_only=True)
class StopOutcome:
    project: str
    stopped: int             # containers this call stopped
    already_stopped: bool    # found, but nothing was running (today's return 0)
    containers: list[str]    # names of the containers stopped
```

`already_unlocked` is called out because today's `rm -rf` on a missing locks directory silently succeeds, and that must stay a success rather than becoming a `NOT_FOUND`; making it an explicit DTO field keeps the honesty the CLI's printed banner currently glosses over.

## Boundary discipline

No live Docker object crosses a `core.<verb>` return boundary: `StopOutcome.containers` carries names, not `Container` objects.
`core/unlock.py` and `core/stop.py` import no `rich`, no `questionary`, no `typer`; the ban is already enforced by the unit test in `tests/test_core_envelope.py`, which will cover the two new modules automatically.
The stopped-container and multi-bench forks return `NEEDS_CHOICE` (`confirm_start`, `select_bench`) exactly as `core/backup.py:58-73` does, and the CLI resolves them with the existing wrappers.

## Risks / Trade-offs

- **The generality claim could fail.** If `unlock` cannot be built on the existing primitives unchanged, this batch surfaces a foundation problem rather than shipping a migration. That is the test working, and it is cheaper here than anywhere later. Implementation reports it rather than quietly widening a primitive.
- **Verbose timing nuance** (Decision 2): accepted and disclosed.
- **Four callers re-pointed at once** (Decision 3): `rm`'s backup gate is among them, which is cwcli's most dangerous path. The re-point is mechanical (same inputs, typed error instead of a `None` sentinel), but `rm`'s caller MUST be checked against the sentinel's meaning: `rm.py:1229` currently treats `None` as not-found, and that branch has to keep failing closed. The captain standard for destructive-delete work applies to the `rm` caller change.
- **`stop`'s frontend parsing is fiddly** (the trailing-`-v` recovery at `stop.py:76-82`): it stays in the frontend verbatim rather than being cleaned up, so the migration does not smuggle in a behavior change.

## Migration Plan

1. Extract the shared validation helper; `core/backup.py` calls it; its tests stay green.
2. `core.unlock` + unit tests; re-point `tests/test_unlock.py`'s argv guards.
3. Reseat `commands/unlock.py`; add the both-modes `unlock` E2E.
4. `core.stop` + unit tests; reseat `commands/stop.py`; re-point the four callers (`rm` last and most carefully).
5. The two axi verbs.
6. Docs and skills.

## Open Questions

- The shared validation helper's home (`core/resolvers.py` versus a new `core/benchop.py`). Leaning `resolvers.py`; settled in implementation, and reversible either way.
- Whether `axi unlock` earns its own E2E leg or folds into the `unlock` E2E's non-interactive mode. Leaning fold-in, to avoid a second instance-costing test.
