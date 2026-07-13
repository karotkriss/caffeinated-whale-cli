## Context

The rework separates four concerns welded into every `commands/*` function body today: **(P)arse** (typer), **(L)ogic** (business rules), **(S)ide-effects** (docker-py, subprocess, filesystem, the peewee registry), and **(R)ender** (`rich` + `questionary`).
The audit (`data/cwcli-core-recon-f3/report.md`) established the ground truth this design builds on; OpenSpec reads no code, so the `file:line` citations below come from that audit and the targeted reading done for this proposal.

Three facts from the audit shape the whole design:

- **`commands/apps.py` already hand-rolled the target pattern.** It has a stdout/stderr split (`utils/console.py:6-7`), stdout purity under `--json` (captures instead of streaming, `apps.py:80-94`), a `results` collection of `{app, site, action, ok}` accumulated across a fan-out (`apps.py:167-192`), and an aggregate reporter that picks the exit code and emits JSON or `rich` (`_report_and_exit`, `apps.py:167-192`).
  This is a hand-rolled `Result` envelope with a hand-rolled serializer.
  The foundation lifts it into typed, shared form rather than inventing a new shape.
- **The registry and site layers are already core-shaped.** `bench_sites.list_sites`/`read_current_site` take a container + path and return data, fail-safe, no `rich` (`bench_sites.py`); `db_utils.get_cached_project_data`/`get_default_site` return boundary-clean nested dicts (`db_utils.py`).
  These are reused as core substrate almost untouched.
- **The real surgery is three interaction-coupled seams.** `docker_utils.get_frappe_container` prints and `raise typer.Exit` on failure and leaks a live `Container` object (`docker_utils.py:104-134`); `ensure_containers_running` fuses starting containers with a `questionary.confirm` + prints + `raise typer.Exit` (`utils.py:19-129`); `resolve_bench_path` is pure resolution logic that nonetheless prints the bench list and `raise typer.Exit` on ambiguity instead of returning a choice (`utils.py:132-221`).
  `confirm_or_exit` (`utils.py:224-259`) is pure frontend and stays put.

`backup` is the reference command because it sits directly on this substrate, has real side effects (a `*.sql.gz` written into a volume and copied to the host), a naturally structured result, two genuine needs-choice forks (multi-bench ambiguity and the stopped-container auto-start confirm), and is already covered by `tests/e2e/test_backup_e2e.py` in both non-interactive (`--site ... -y`, stdin closed) and interactive (pty) modes - so the migration is refactor-under-green.

## Goals / Non-Goals

**Goals:**

- A UI-pure logic core (owns I/O, carries no `rich`/`questionary`) with a serializable, typed return contract feeding CLI, AXI, and a future GUI.
- Prove the contract end to end on ONE real command (`backup`), with its behavior pinned unchanged by the green E2E.
- Ship a thin `cwcli axi` surface (namespace + serializer + content-first home + `backup` verb) that is a pure serializer/exit-mapper over the same core.
- Establish the reusable primitives - envelope, needs-choice, typed error, split resolvers - that every later command migration and the plan/apply split will build on.

**Non-Goals** (see the proposal's Non-Goals for the full list): streaming event iterators, the plan/apply split, the AXI SessionStart hook / installable skill / `skills-lock.json` / CI staleness check (the narrowed `cwcli-axi-pass-x9`), and migrating any command other than `backup`.

## Decisions

The seven forks below were locked with the captain.
Each records the chosen option, the rejected alternative, and the audit evidence, so a later reader cannot silently reopen a settled fork.

### 1. Reference command = `backup`

`backup` is the first command taken end to end through core + re-seated CLI + `cwcli axi`.

- **Why it wins.** It is already covered by the real-Docker E2E net (`tests/e2e/test_backup_e2e.py:41,49`, both modes), so the migration is refactor-under-green from day one - the E2E is structure-agnostic (it drives the `cwcli backup` binary and asserts a genuine non-empty dump lands on the host), so it pins the behavior the refactor must preserve.
  It exercises the needs-choice input model on two genuine forks - multi-bench ambiguity (`resolve_bench_path`, `utils.py:206-221`) and the stopped-container auto-start confirm (`ensure_containers_running`, `utils.py:89-122`) - not a toy.
  It is non-destructive (additive artifact), so the first slice does not also have to settle the plan/apply boundary.
  And it sits on the shared bench-op substrate (`ensure_containers_running`, `resolve_bench_path`, `get_default_site`, `bench_sites`), so migrating it forces every core primitive into existence; `unlock` is its line-for-line near-twin (`unlock.py`) and `run`/`open`/`update`/`restore` resolve container+bench+site the same way, so the primitives immediately serve the next batch.
- **Rejected: `list`/`ls`.** Lowest risk and best isolates the envelope+serializer (`_list_instances` already returns a clean `list[dict]`, `list.py:73-93`, and `--json` already exists, `list.py:147-151`), but it has no needs-choice, no prompt, and no bench/site I/O, so it cannot prove the input model - the part of the rework most likely to be gotten wrong.
  It is reused here instead as the `cwcli axi` home's live-state producer.
- **Rejected: `unlock`** (backup's twin plus a streaming path) - not E2E-covered, so it loses the day-one green-tests property.
  **Rejected: `status`** - too thin (one enum token), under-exercises everything.
  **Rejected: `init`** - E2E-covered but a 1285-LOC monster, explicitly out of scope.

### 2. DTO library = stdlib `dataclasses`

`@dataclass(frozen=True, slots=True)`, `kw_only=True` where clarity helps.

- **Why it wins.** pydantic is absent from deps and lock (`grep -rn "pydantic" pyproject.toml uv.lock src/` returns nothing; runtime deps are `docker, typer, rich, questionary, toml, peewee`).
  Adding it would put a compiled Rust wheel (pydantic-core) into the runtime env of a tool distributed via `uv tool install`/`uvx`, cutting against the standing repo value of a lean runtime env (CLAUDE.md records that `black` was kept OUT of runtime deps for exactly this reason).
  Dataclasses are already the in-repo idiom (`commands/init.py`'s `InitInputs`), the pervasive `dict` payloads (`_list_instances`, `get_cached_project_data`, `apps`' `results`) are anonymous dataclasses waiting to be named, and serialization is a solved one-liner (`dataclasses.asdict` -> plain nested dicts; `list.py:149` and `apps.py:170` already `json.dumps` dicts).
  mypy is a blocking zero-error gate (CLAUDE.md; no `# type: ignore` in `src/`) and dataclasses are understood by mypy with zero config.
- **Rejected: pydantic.** Its headline feature - parse/validate untrusted external data into typed models - has no consumer in the first slice: the DTO envelope is a Python-to-Python return contract, input is already coerced by typer, and the core itself is the validation boundary (site/path metachar checks, existence probes).
  It would add a runtime dep and a mypy plugin now for no first-slice benefit.
  Because dataclasses serialize to the same JSON shape pydantic would, pydantic stays adoptable later at a future web-GUI deserialization seam without rewriting the core DTOs - so this is reversible, and the lean choice wins now.

### 3. Needs-choice mechanism = return `NEEDS_CHOICE`, frontend re-invokes

When the core hits a fork it cannot resolve from its explicit params, it returns `Result(status=NEEDS_CHOICE, choice=Choice(kind, param, prompt, options, default))` and does not act.
The frontend resolves the choice and re-invokes the core with the resolved param now explicit: the CLI prompts via `questionary` (exactly what `resolve_bench_path`/`ensure_containers_running` do inline today, now lifted out); `cwcli axi` never prompts (axi rule 6) and instead emits a structured usage error naming the exact flag (`error: multiple benches; pass --bench <index|label>`, exit 2); a future GUI renders a dialog.

- **Why it wins.** It is the purest read of the locked "prompting never lives in the core."
  Its only cost is re-running the cheap work before the fork, and cwcli's forks are all cheap reads (resolve bench from cache, pick default site, confirm start), so the recompute is negligible.
- **Rejected: injected resolver callback.** The core takes a resolver protocol it calls at the fork - single-pass, but it re-introduces control inversion that is prompting-by-proxy inside the core, arguably violating the "fully non-interactive core" lock.
- **Rejected: universal plan+apply for every command.** A cheap `resolve/preview` call gathers all choices, then `apply` runs with everything explicit - heaviest, and for cwcli it converges with the chosen option in practice (most choices resolve from cheap up-front reads).
  It is reserved for genuinely destructive commands where a preview is non-trivial (Decision 5), not made universal.

### 4. Error signaling = raise typed `CwcliError` for hard failures; envelope for soft/partial

The core raises `CwcliError` (carrying an `ErrorKind`) when it genuinely cannot proceed; each frontend has ONE handler mapping `kind -> (exit code, rendering)`.
Soft, partial, or expected outcomes (an apps-style per-site partial failure, an "already unlocked" no-op) ride in `Result.data` + `Result.warnings` with a `WARNING`/`OK` status - they are never raised.
`errors` is deliberately NOT a field on `Result`.

The `ErrorKind` set is closed and small, each mapping to an axi exit code, mirroring the `typer.Exit(1/2)` conventions already in the code:

| `ErrorKind` | Meaning (current site in code) | axi exit |
|---|---|---|
| `USAGE` | bad/missing/mutually-exclusive flag (`utils.py:167-169`, `--bench`+`--path`) | 2 |
| `NOT_FOUND` | project/site/bench absent (`backup.py:66,155`) | 1 |
| `NOT_RUNNING` | container down, no auto-start (`utils.py:95-101`) | 1 |
| `CONFLICT` | port conflict, existing bench (`start`/`init`) | 1 |
| `PRECONDITION` | gate failed (rm backup gate, disk, etc.) | 1 |
| `DOCKER` | daemon down / API error (`docker_utils.py:33-43`) | 1 |
| `INTERNAL` | unexpected | 1 |

- **Why it wins.** It matches how the code already behaves: `apps` collects per-site failures in `results` (not raised) and lets the aggregate pick the exit code (`apps.py:169`), while `backup`/`unlock` `raise typer.Exit` on a can't-proceed condition (`backup.py:67,148,156`).
  Raising for hard errors keeps every core call site from having to thread an error check, and the single per-frontend handler is the natural home for the `kind -> exit/rendering` map.
- **Rejected: envelope-only, never raise.** Every core fn returns a `Result` with an `errors` list and callers must inspect `status` - fully explicit, but verbose and drop-prone (a forgotten check silently swallows an error), and it reads further from today's code.

### 5. plan/apply split = NOT built in the foundation

`backup` is additive, so no destructive-preview boundary is built now.

- **Why it wins.** Building a generic plan/apply base with no first-slice consumer is speculative scaffolding (YAGNI).
  The envelope + needs-choice + typed-error primitives this foundation ships are exactly the substrate plan/apply will reuse: when `restore`/`rm` migrate, "plan" is just a `Result` whose `data` is a preview DTO (what will be dropped/backed-up/deleted) and "apply" is a second core call taking the confirmed plan - the same envelope and DTO machinery, no new mechanism.
- **Rejected: build a generic plan/apply base now.** Speculative; introduce it per-command when `restore`/`rm` actually need it.

### 6. `cwcli axi` scope for the foundation

Ship the `axi` namespace + the shared serializer/TOON encoder + a content-first `cwcli axi` home (on the `ls`/`_list_instances` DTO) + the `backup` verb.

- **Why it wins.** With typed DTOs from the core, an axi verb is a ~15-25 line serializer + exit-mapper with no business logic (recon 5b), so this stays a small, reviewable PR that still proves axi end to end on a real command.
  The home reuses the already-boundary-clean `_list_instances` producer (`list.py:73-93`), so the live-state view costs almost nothing.
- **Rejected: include the SessionStart hook + installable skill + `skills-lock.json` + CI `--check` now.** These are ambient-context cross-cutting pieces (axi rule 7) that only pay off once several commands are axi-conformant, and they enlarge the PR.
  They are the narrowed `cwcli-axi-pass-x9` task; this design names that boundary explicitly so the split is not re-litigated.

### 7. Output = a small hand-written, dependency-free TOON encoder

JSON stays internal (`dataclasses.asdict` -> plain dicts); TOON is emitted only at the stdout boundary (axi rule 1).

- **Why it wins.** The encoder is a thin recursive walk over `asdict` output with a tiny `default` for `Enum`/`Path` - `--json` in `apps.py:170-181` and `list.py:149` already prove the shape; TOON is the same walk with a different writer.
  No new dependency (keeps the `uvx` env lean), and it conforms to axi rule 1 from the first verb rather than shipping a JSON-only interim.
- **Rejected: ship JSON-only first, add TOON in x9** - defers axi rule 1 conformance for no real saving.
  **Rejected: vendor/add a TOON library** - a new runtime dependency for roughly one small module of code.

## The envelope, concretely

Modeled on `apps._report_and_exit`'s existing shape (`apps.py:167-192`), typed:

```python
class Status(Enum):
    OK = "ok"                        # completed
    WARNING = "warning"              # completed with non-fatal issues (partial fan-out)
    NEEDS_CHOICE = "needs_choice"    # cannot proceed without a decision the core won't make

@dataclass(frozen=True, slots=True)
class Message:
    code: str                        # stable token: "default_site.resolved", "bench.ambiguous"
    text: str
    detail: dict | None = None

@dataclass(frozen=True, slots=True, kw_only=True)
class Choice:
    kind: str                        # "select_bench" | "confirm_start" | ...
    param: str                       # the core param the frontend must fill to re-invoke: "bench"
    prompt: str
    options: list[dict] | None = None   # selects: [{"value": "0", "label": ".../frappe-bench"}]
    default: str | None = None

@dataclass(frozen=True, slots=True, kw_only=True)
class Result(Generic[T]):
    status: Status
    data: T | None = None            # the command's typed outcome DTO (e.g. BackupOutcome)
    warnings: list[Message] = field(default_factory=list)
    choice: Choice | None = None     # set iff status == NEEDS_CHOICE

class ErrorKind(Enum):
    USAGE = "usage"; NOT_FOUND = "not_found"; NOT_RUNNING = "not_running"
    CONFLICT = "conflict"; PRECONDITION = "precondition"; DOCKER = "docker"; INTERNAL = "internal"

class CwcliError(Exception):
    kind: ErrorKind
    code: str                        # stable token
    message: str
    hint: str | None = None          # axi-facing "pass --X" suggestion

@dataclass(frozen=True, slots=True, kw_only=True)
class BackupOutcome:
    site: str
    bench_path: str
    artifact_path: str
    included_files: bool
```

A `cwcli axi backup` verb in full is then approximately:

```python
@axi_app.command("backup")
def axi_backup(project: str, site: str = None, bench: str = None, with_files: bool = False):
    try:
        result = core.backup(project, site=site, bench=bench, with_files=with_files)
    except CwcliError as e:
        emit_axi_error(e); raise typer.Exit(exit_for(e.kind))          # stdout error, exit 1/2
    if result.status is Status.NEEDS_CHOICE:
        emit_axi_choice_as_usage_error(result.choice); raise typer.Exit(2)
    typer.echo(toon(asdict(result.data), warnings=result.warnings))
    raise typer.Exit(0 if result.status in (Status.OK, Status.WARNING) else 1)
```

`emit_axi_error`, `emit_axi_choice_as_usage_error`, `toon`, and `exit_for` are shared across every future verb, so per-command axi conformance is a handful of lines plus a schema choice.

## Boundary discipline

- **No live Docker object crosses a `core.<verb>` return boundary.** The core Docker accessor may keep a docker `Container` internally to run its exec calls, but every returned DTO carries only serializable data (`str`/`bool`/`Path`-as-str), keeping every GUI option open and the AXI layer a pure serializer.
  This is why `get_frappe_container` must gain a core form: today it both leaks the `Container` and prints + `raise typer.Exit` (`docker_utils.py:104-134`).
- **The core never imports `rich` or `questionary`.** Rendering and prompting live only in the frontends; `confirm_or_exit` stays in `commands/utils.py`.
- **Placement.** The core lives in a new `src/caffeinated_whale_cli/core/` package; `commands/*` frontends import from it. `commands/utils.py`'s `ensure_containers_running`/`resolve_bench_path` become thin wrappers that call the core resolver and translate a returned needs-choice into a `questionary` prompt + re-invoke (or `raise typer.Exit` on refusal, preserving today's exit codes).

## Risks / Trade-offs

- **Behavior drift during the refactor** -> the refactor is under the green `tests/e2e/test_backup_e2e.py`, which is structure-agnostic and asserts a real non-empty dump on the host in both modes; it must stay green unchanged.
  Unit tests additionally pin each split resolver's return (data / needs-choice / typed-error) against faked I/O.
- **The re-invoke model recomputes cheap work before a fork** -> accepted (Decision 3); cwcli's forks are cheap reads, and the purity of "core never prompts" is worth the negligible recompute.
- **A too-general envelope invites gold-plating** -> the envelope is deliberately minimal (four DTOs + one error class + two enums), modeled on what `apps` already needed, not a speculative superset; streaming and plan/apply are explicitly deferred until a real consumer arrives.
- **Two frappe-container accessors (core + CLI wrapper) risk drift** -> the CLI wrapper is defined in terms of the core one (call core, map the typed error to print + `typer.Exit`), so there is a single resolution implementation.
- **TOON hand-encoder correctness** -> it is a small module with a runnable self-check asserting round-trippable structure against the `--json` shape the codebase already emits; it only ever writes (no parsing), which bounds the surface.

## Migration Plan

Purely additive plus a behavior-preserving internal refactor of `backup`.
No data migration, no schema change, no config change, no dependency added.
`cwcli backup` keeps its exact CLI contract; `cwcli axi` and the `core` package are new.
Rollback is deleting the `core/` package and `commands/axi.py`, reverting `commands/backup.py`/`commands/utils.py`/`utils/docker_utils.py` to their pre-change form, and removing the one `add_typer` registration line in `main.py`.

## Open Questions

- **The exact placement of the core Docker accessor** (a new `core/docker.py` returning a thin handle, vs. keeping the docker `Container` internal to `core.backup`). Deferred to implementation - the load-bearing invariant (typed error, no live object in returned DTOs) is fixed here; the handle's concrete type is not.
- **Whether `Message.detail` should be a typed dataclass rather than `dict | None`.** Deferred - `dict` matches the current `apps` `results` shape and serializes cleanly; tighten it only if a consumer needs structure.
- **Default axi schema for `axi backup`.** The `BackupOutcome` is small enough to surface whole; `--fields` (axi rule 2) is deferred until a verb has a large DTO that needs it.
