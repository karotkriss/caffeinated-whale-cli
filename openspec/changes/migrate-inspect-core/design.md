## Context

This design implements the recon in `cwcli-inspect-recon-i7` (report at `data/cwcli-inspect-recon-i7/report.md`), which measured every claim against a clean tree at `4146c95`; the two commits since are doc-only, and every line reference below was re-verified against the current HEAD before being relied on.
The recon read `commands/inspect.py` (672 lines) and `utils/db_utils.py` (the cache layer) in full, all reference core slices, and every importer of the module, `partial_inspect_known_benches`, `_find_bench_instances`, and the cache readers/writers.

### Audit: what `inspect` actually is

The file splits cleanly in two:

| Lines | What | Migrates? |
| --- | --- | --- |
| `24-345` | Helpers: `_run_command`, `_is_bench_directory`, `_get_sites`, `_get_installed_apps`, `_get_available_apps`, `_find_bench_instances`, the two config readers, `_gather_bench_data`, `partial_inspect_known_benches` | **Yes.** All UI-light except `--verbose` stderr echoes; every one takes a live container as a parameter and returns data |
| `348-554` | The Typer body: the 3-tier state machine, container prologue, spinner, cache write | **Yes.** The tier logic and the write move to the core; the prologue/spinner/renderers stay |
| `562-618` | The `-i` interactive labeling loop | **No.** questionary prompts operating on returned data; stays frontend with today's write ordering |
| `620-672` | The `--json` and tree renderers | **No.** Pure rendering |

The tier state machine, measured:

- **T1** (`--no-refresh`, containers not running, or any T2 failure): serve `db_utils.get_cached_project_data` verbatim, zero container calls.
- **T2** (default on cache hit): `ensure_containers_running(prompt=False, auto_start=False)` - deliberately passive **even under `--yes`** (`inspect.py:431-437`) - then `partial_inspect_known_benches`. No drift -> serve cache unchanged, **no write**. Drift -> remember `drift_fallback_benches`, escalate to T3.
- **T3** (`--update`, cache miss, or drift escalation): `ensure_containers_running(prompt=prompt_to_start, auto_start=yes)` (the only tier that may prompt or auto-start), full discovery + gather, `db_utils.cache_project_data` on success (`inspect.py:554`). A drift escalation that rediscovers nothing degrades to the remembered cache **without persisting** (`:543-549`); `--update`/cache-miss keeps the hard error + exit 1 (`:551-552`).

Error handling, measured: per-probe failures never abort the fan-out (each helper absorbs them as `[]`/`None`); the T2 pass is wrapped in `except Exception` and degrades to cache; a raw docker exception mid-T3-fan-out is caught by NOTHING (`@handle_docker_errors` only wraps the `client.ping()` preflight - `docker_utils.py:51-65`), producing a raw traceback with no cache write (crash-without-corruption).

## Goals / Non-Goals

**Goals.**
Move `inspect`'s tier logic, fan-out, and cache write onto the core behind `Result[InspectReport]`.
Kill the frontend-calling-frontend class at all seven consumer edges, including `core/update.py`'s reach-back (via the untouched `cache.recache_project` seam).
Ship `axi inspect` and close the `axi benches` agent dead end.
Unblock `open` (recon Q8: its two inspect edges are this batch's surface; nothing else blocks it).

**Non-Goals.**
The `open` migration itself (next batch).
`--show-apps` disposition (captain hold `cwcli-inspect-recon-i7-decision-show-apps-dead-flag`; the batch keeps it byte-identical).
`core/update.py:291`'s dead cache branch (captain-tracked in `migrate-apps-core/tasks.md` §8; this batch must merely not change the cached `installed_apps` line rawness it depends on).
`cache_project_data` transactionality (a write-path behavior change deserving its own review; reported below).
The auto-inspect daemon's PID hazards (known-hazards board; only its `_inspect_project` call is re-pointed).
Unifying the `-i` loop onto `core.label.set_label` (would change write ordering and per-bench cache round-trips - a behavior change wearing a migration's clothes).

## Decisions

### 1. A plain `Result[InspectReport]` with an optional `on_event` callback - NOT a generator, NOT two-phase

In the order the reference slices settle it:

1. **No consumer consumes intermediate results.** The spinner is content-free; the only per-step output is `--verbose` stderr diagnostics. Nothing renders per-bench progress, and `--json` needs the terminal aggregate only.
2. **`NEEDS_CHOICE` must be returnable at call time.** T3 has the `confirm_start` fork on a stopped project. A generator body does not run until first iteration, so it cannot return `NEEDS_CHOICE` at all and cannot raise `CwcliError` at call time (`core/run.py:3-11`, the laziness argument, verbatim).
3. **There is no laziness to work around and nothing streams.** Every probe is a short buffered `exec_run`. The two-phase `run_plan`/`run_stream` split exists ONLY because generators are lazy; `update` (batch 4) is the standing counter-example proving "emits progress" does not imply "returns an iterator". `inspect` has no `try/finally` cleanup at all (unlike `update`'s maintenance mode), so even that pressure is absent; the degrade-to-cache paths are ordinary control flow inside a plain function.
4. **`exec_stream` is deliberately NOT consumed.** Same reasoning class as `core/logs.py`'s measured refusal: there is no streaming consumer, so adopting the contract adds machinery with no reader. The honest-exit-code concern that motivated `exec_stream` does not bite: every probe treats a non-zero/absent result as absence and fails safe; no destructive operation's success is inferred from an exit code.
5. **The verbose trace rides the event channel, not `warnings`.** Batch 5's amended decision (`core/apps.py`'s `AppsCommand`): `warnings` is for actionable notes, and a `$ cmd` debug echo inside an `axi` document would be a contract violation. `core.inspect` takes `on_event: OnEvent | None` (the `core/update.py:162` / `core/apps.py:138` idiom); the CLI renders events as today's `VERBOSE:` lines (small wording drift acceptable if disclosed, per the `core.backup -v` precedent); `axi` passes nothing.

Rejected: a generator (point 2 kills it), the two-phase plan/stream split (point 3: its motivation does not exist here; this is also NOT plan/apply, which is reserved for destructive previews per foundation Decision 5 - the cache is cwcli's own self-healing state, not user data).

### 2. The core owns the cache write

`core.inspect` owns the tier logic AND calls the unchanged `db_utils.cache_project_data`.

1. **Purity is not violated.** The purity test bans `rich`/`questionary`/`typer` only (`tests/test_core_envelope.py:21`); `db_utils` is already imported by `core/resolvers.py`, `core/where.py`, `core/update.py`, and `core/label.py`.
2. **The precedent exists.** `core/label.py` writes BOTH stores (marker + cache) from inside the core. The foundation's own words: "A UI-pure logic core (owns I/O, carries no rich/questionary)". Side effects are the core's job; rendering is the frontend's.
3. **The write IS the product for most consumers.** `recache_project`, `auto_inspect`, `update`'s mid-fan-out recache, `rm`'s pre-backup recache, and the open/update/restore no-cache fallbacks all invoke inspect solely to populate the cache. A frontend-owned write means a core-level recache cannot exist and the `core/update.py` reach-back becomes permanent.
4. **The freshness semantics are inseparable from the write decision.** T2's never-write, T3's write-on-success, and drift-degrade's serve-without-persist are one contract (pinned by `tests/test_inspect_partial_refresh.py`). Splitting decision (core) from write (frontend) lets each frontend diverge on exactly the invariant the tests exist to hold.

Rejected: **frontend writes after core returns** (kills point 3; every frontend re-implements point 4; the reach-back survives). **A distinct persist step** (nothing needs the gap between gather and write; the two-call shapes in this codebase exist for reasons that do not apply here - laziness for `run`, frontend-performed handover for `logs`/`open`).

The redaction whitelist stays inside `cache_project_data`, never in the inspect layer: redacting at the inspect layer would strip in-memory dicts the same run uses (the `cwcli-inspect-benches` skill's explicit warning).

### 3. `partial_refresh` keeps cache-shaped dicts; `discover_benches` is exposed

- **`partial_refresh(container, cached_benches) -> tuple[list[dict], bool]`** is the moved T2 pass. The dicts ARE the cache shape and feed straight back into cache/consumer comparisons; DTO-ifying an internal pass would force a convert-back at the cache boundary for zero consumer benefit. Its pinned behaviors move unchanged: carry-forward of `installed_apps`/configs/`label`/`current_site` (T2 never re-reads the marker or currentsite.txt), drop-vanished-bench, never-write.
- **`discover_benches(container) -> list[str]`** is the moved `_find_bench_instances`, killing `rm.py:804`'s import of an underscore-private name across a module boundary. Its `config_utils.load_config()` read is core-safe (config_utils imports only `os`/`pathlib`/`toml` - verified). The `sorted(set(...))` return IS the numeric-label order and moves byte-identical.

### 4. All seven consumer edges re-point; `recache_project` keeps its signature

| # | Edge | Replacement |
| --- | --- | --- |
| 1 | `utils/cache.py:38-50` `recache_project` (callers: `rm.py:1425`, `apps.py:130`, `core/update.py:389`) | body re-points at `core.inspect(refresh="full", offer_choice=False)`, catching `CwcliError(NOT_RUNNING)` into `False`; the signature and the never-prompt / False-when-stopped contract survive, so all three callers are untouched. This kills the core-imports-CLI reach-back at its root with zero churn in `core/update.py` |
| 2 | `utils/auto_inspect.py:174-187` | `core.inspect(refresh="full", offer_choice=False)` directly |
| 3 | `commands/open.py:135-147` (no-cache fallback populate) | `core.inspect(refresh="auto")` + the same `resolve_bench_path` re-resolve; when `open` migrates this coupling becomes core-to-core |
| 4 | `commands/open.py:217-231` (in-memory `--app` freshness) | `core.partial_refresh`; match-by-path against the refreshed list stays (index-shift safety), degrade-on-error stays, never persists |
| 5 | `commands/update.py:444-508` `_resolve_bench_path` fallback | inner call re-points at `core.inspect`; the flow (prints, `--json` skip for stdout purity) stays frontend |
| 6 | `commands/restore.py:723, :1000, :1675` (three fallback copies) | mechanical re-point at `core.inspect`; restore's own migration comes later, but removing the command-call now also removes the Typer-default trap |
| 7 | `commands/rm.py:804` (live discovery when the cache read raises) | `core.inspect.discover_benches`; the multi-bench backup gate's behavior (`test_rm_safety.py:692`) unchanged |

The trap all of #1-#6 defuse by hand today: an omitted Typer param is a truthy `typer.Option(...)` default OBJECT, so every caller must pass all nine params explicitly - and `open.py:135-147` already omits `prompt_to_start`.
A keyword-only core function with real defaults eliminates the class.

Cache READERS are shape-sensitive and must not change: `core/resolvers.py:45`, `core/update.py:284` (`_sites_with_app`, which depends on the RAW `bench list-apps` line format), `commands/open.py:187`, `commands/rm.py:795`, `commands/restore.py:706/983/1658`, `utils/completion_utils.py:148/196`, `core/where.py:65`, and `get_default_site`'s three callers.

### 5. One `axi inspect` verb, no split, no `--yes`

`cwcli axi inspect <project> [--update] [--no-refresh]`.

- The tier machinery IS the read-with-freshness contract; a read/refresh verb pair would make agents orchestrate T1/T2/T3 by hand, which is the complexity the tiers exist to hide. The two flags map directly onto `refresh="full"` / `"cache_only"`; the default is the tiered read.
- **TOON document:** the `InspectReport` DTO through the standard `emit_result` - `project`, `served_from`, benches with index/path/label/current_site and nested sites/app lists. A degrade-to-cache serves the data with a warning (`WARNING` -> exit 0, per the standing rule that WARNING is a completed operation).
- **NEEDS_CHOICE surface:** the only fork is `confirm_start` on the refresh path against a stopped project -> `emit_axi_choice_as_usage_error`, exit 2, help naming `cwcli start <project>`. Deliberately NO `--yes`: identical reasoning to `axi unlock` and the `axi apps update` incident - an axi verb must never open a start-from-axi path. `select_bench` does not arise: inspect is whole-project by construction.
- Writing the cache from an `axi` verb is established (`axi apps update` recaches mid-run); the cache is cwcli's own self-healing state, not user data, so this is not in the captain-locked destructive class (`axi apps install/uninstall`).
- The `axi benches` not-inspected hint (`axi.py:508-510`) re-points at `cwcli axi inspect`; the generated skill picks the verb up automatically (`build_skill.py` walks `registered_commands`; `tests/test_axi_skill.py --check` gates staleness).
- Ships in-batch: read verbs have shipped freely on this surface (`axi benches`, `axi apps list`, `axi self-update --check`), and this proposal is the normal approval point.

### 6. Two hardenings, disclosed

1. **Strict decode -> `errors="replace"`.** `inspect.py:34` decodes every probe's output strictly; one non-UTF-8 byte anywhere crashes the whole inspect. The core adopts the standing idiom (`core/backup.py:_decode`). Disclosed as deliberate hardening in the PR, not a silent change.
2. **Raw docker escape -> `CwcliError(DOCKER)`.** A dropped daemon connection mid-T3-fan-out is caught by nothing today (raw traceback, non-zero exit, no cache write). The core wraps it - the same wrap `resolvers.resolve_container_state` got for `reload()` per the latent-hardening precedent. The outcome contract is unchanged: T3 mid-fan-out connection loss stays crash-without-corruption (typed now); T2 stays degrade-to-cache.

### 7. The frontend keeps the backup pattern, the `-i` loop, and byte-identical output

- **Prologue outside the spinner:** the CLI pre-resolves the `confirm_start` fork interactively BEFORE entering the `TipSpinner` (the backup pattern), with a capped retry, preserving `prompt_to_start=False`-style spinner-borne safety for programmatic callers via `offer_choice=False`.
- **The `-i` labeling loop stays frontend**, operating on the returned bench data exactly as today: questionary per bench, `bench_labels.validate_user_label`, in-memory duplicate check, `write_label_marker` (warn-and-continue when the container is unavailable), then ONE bulk `cache_project_data` at the end (`inspect.py:618`). `bench_labels` and `db_utils` are utils, legal from the frontend. It is a second label-writing implementation next to `core/label.py`; unifying them is a later cleanup, NOT this batch (it would change the write ordering and per-bench cache round-trips).
- **Human `--json` byte-identical** (the index-augmented shape, precedent: `where --json`). Tree renderer unchanged, including the "(default)" resolution order: `common_site_config.default_site` first, `current_site` fallback.
- **`--show-apps` stays byte-identical** - a dead flag (declared at `:370-371`, never read; the tree always shows apps; the `--help` text lies). Its disposition (keep-as-dead vs make-honest-by-gating vs remove) is the registered captain hold, and two of those three options are visible behavior changes, so the batch does not touch it.

### 8. Zero new primitives, as a falsifiable claim

| Need | Existing primitive |
| --- | --- |
| container run-state, prompting caller | `resolvers.resolve_container_state(offer_choice=True)` -> `confirm_start` |
| container run-state, spinner-borne caller | `resolvers.resolve_container_state(offer_choice=False)` -> raises `NOT_RUNNING` |
| frappe container | `core/docker.py:get_frappe_container` |
| the probes | the moved helpers, taking containers as parameters (allowed: `core/run.py:18-22`) |
| the write | `db_utils.cache_project_data`, unchanged |
| the events | the `OnEvent` callback idiom (`core/update.py:162`, `core/apps.py:138`) |

`resolve_bench` is NOT needed: inspect is whole-project by construction.
The one novelty - the first core verb whose PRIMARY side effect is the cache write - is covered by the `core/label.py` precedent (a core function already writes marker + cache), not a primitive change.
If implementation finds a bend, it is reported, per the standing rule.

## The DTOs, concretely

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class SiteInfo:
    name: str
    installed_apps: list[str]
    has_site_config: bool

@dataclass(frozen=True, slots=True, kw_only=True)
class BenchInfo:
    index: int
    path: str
    label: str | None
    current_site: str | None
    default_site: str | None      # resolved: common_site_config.default_site, else current_site
    available_apps: list[str]
    sites: list[SiteInfo]

@dataclass(frozen=True, slots=True, kw_only=True)
class InspectReport:
    project: str
    served_from: str              # "cache" | "partial" | "full"
    degraded: bool                # drift escalation fell back to cache without persisting
    benches: list[BenchInfo]


def inspect(
    project_name: str,
    *,
    refresh: str = "auto",        # "auto"=tiered | "cache_only"=--no-refresh | "full"=--update
    auto_start: bool = False,     # T3 only; T2 stays passive by construction
    offer_choice: bool = True,    # False for spinner-borne callers: NOT_RUNNING raise, never confirm_start
    on_event: OnEvent | None = None,
) -> Result[InspectReport]: ...
```

Exact field names are the implementer's; the semantics above are the contract.
Serializable throughout (`asdict` -> plain data); the live container never crosses the return boundary.
The human `--json` renderer is NOT fed from `asdict(InspectReport)` blindly - it keeps emitting today's exact cache-dict shape (byte-identical), which the core returns alongside or the frontend derives; the implementation picks the mechanism, the spec pins the bytes.

## Boundary discipline

`core/inspect.py` imports no `rich`, no `questionary`, no `typer`; `tests/test_core_envelope.py`'s existing import ban covers it automatically.
It never prints, prompts, or exits; the `--verbose` trace is typed events.
It never auto-starts on the T2 path, under any parameter combination.
No live Docker object crosses the return boundary; the moved helpers take containers as parameters.

## Risks / Trade-offs

- **The tier state machine is exactly the kind of logic that breaks silently in a refactor** -> characterization-first ordering (batch 4's discipline): the tier tests are confirmed green against the unmigrated code first, then the subject moves under them; patched import paths move with their subject, assertions untouched; any test changed BY DESIGN is named in tasks.
- **Seven edges re-pointed in one batch** -> each edge's contract is pinned by an existing suite (`test_rm_stopped.py` for #1's never-prompt, `test_rm_safety.py:692` for #7, `test_inspect_partial_refresh.py` for #4) and the re-points are mechanical (same params, same catches).
- **The core writing the cache makes a bad write more central** -> the writer itself is unchanged and stays the single redaction chokepoint; its pre-existing non-transactionality (clear-then-create row-by-row, `db_utils.py:340-417`) is REPORTED as a follow-up, deliberately not absorbed into this batch.
- **`served_from`/`degraded` are new surface** -> additive fields on a new DTO; the human renderers do not consume them, so no human-visible drift.

## Migration Plan

`tasks.md` order: characterization tests pinned green first, the helpers and T2 pass move with their tests, then the tier machine and the write, then the frontend is reseated, then the seven edges, then `axi inspect`, then docs.
The human `--json` output and the cache dict shape are byte-compared before and after.

## Open Questions

None blocking. Three recorded:

1. **`--show-apps` disposition** - the registered captain hold (`cwcli-inspect-recon-i7-decision-show-apps-dead-flag`); the batch keeps it byte-identical regardless of the eventual answer.
2. **`cache_project_data` transactionality** - a small follow-up (wrap in a peewee transaction), deliberately its own review.
3. **`open`'s migration** - unblocked by this batch (recon Q8); sequence it immediately after, no interleaving.
