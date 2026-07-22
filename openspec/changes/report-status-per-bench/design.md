# Design - per-bench status

## Decision 1: One report type carrying `benches`, not a second wrapper DTO

Two shapes were available for the captain's instance-wide ruling.

1. Keep `StatusReport` as the per-bench report and add an `InstanceStatusReport { benches: list[StatusReport] }` above it.
2. Make `StatusReport` itself the instance report, with `benches: list[BenchStatus]` inside it.

**Chosen: 2.**

Option 1 is tempting because it is additive: today's document survives untouched for `--bench` and single-bench callers, and only the bare multi-bench form gets a new type.
That is exactly what makes it wrong.
It produces two documents from one verb, selected by a fact the caller may not know (how many benches the project has), so every consumer must branch on bench count before it can parse.
An agent that only ever tested against a single-bench project would ship code that breaks the first time the project grows a second bench - the same class of failure as the defect being fixed.

Option 2 matches `core.inspect`, which is the model the captain's framing named: `InspectReport.benches: list[BenchInfo]` (`core/inspect.py:117`) is uniform whether the instance holds one bench or six, and nobody branches to read it.

The cost is real and is stated in the proposal rather than buried: single-bench `axi status` consumers break too, because `processes` moves to `benches[0].processes`.

## Decision 2: The probe takes an explicit non-defaulted PORT, not a bench path

`supervision.web_http_code(container)` becomes `web_http_code(container, *, port: int)`, not `web_http_code(container, bench_path: str)`.

Three properties follow from that choice, and the third is the reason for it.

**It keeps the port resolution in one place.** If the probe resolved the port itself it would need to read `common_site_config.json`, which is `resolvers.resolve_assigned_ports`'s job, and `core.status` would end up unable to report `web_port` without reading the file a second time (it must report the port it asked, or `web_http_code` is unattributed all over again).

**It keeps `core/supervision.py` doing one thing.** The module's own docstring describes it as process discovery and supervisor control. Giving it a config reader widens it for no gain.

**It makes the bug unrepresentable.** With no default, there is no way to call the probe without naming a port. A future caller cannot inherit 8000 by omission, which is precisely how the current defect arrived: `web_http_code` was written for a single-bench world, and every later caller correctly passed nothing.

A `port: int = 8000` default would fix today's two call sites and leave the trap armed for the third.

`web_is_serving` and `wait_web_ready` take the same treatment because they wrap the same probe. `web_is_serving` has no caller outside the module today, so its signature is free; changing it anyway keeps the three consistent, and a future caller finding an inconsistent trio is how one of them ends up bench-blind again.

## Decision 3: The instance `overall` is a fold over the four existing tokens

No fifth token.
The four (`offline`/`online`/`running`/`degraded`) are a settled contract (`add-per-process-supervisor` kept them explicitly when supervisord introduced partial stacks), they are what `README.md:1617` documents, and they are what the human stdout token has always been.

The fold:

| Instance state | Rule |
| --- | --- |
| container not running | `offline` |
| any bench `degraded` | `degraded` |
| else, any bench `running` | `running` |
| else | `online` |

The one non-obvious rule is **"any bench running wins over a bench that was never started"**, and it is the rule the audit's headline scenario turns on.

A developer stops the instance and runs `cwcli start mbstat --bench 1`.
Bench 0 was never started, so its own aggregate is `online` (`_overall` returns `ONLINE` when `started` is False - a never-started bench is not a fault).
Bench 1 is genuinely healthy, so its aggregate is `running`.

A plain worst-wins fold ordered `degraded > online > running` would report the instance `online`, which reads as "nothing is started" while a bench serves real traffic.
That is F3 in a new costume: a correct-looking token that is wrong about a healthy bench.

`degraded` still dominates everything, so a real fault is never masked by a healthy sibling.
The per-bench rows carry the detail either way, which is the whole point of the restructure: the instance token is a summary, and the summary must not be more alarming *or* more reassuring than the rows beneath it.

## Decision 4: An unresolved port reuses `web_probed=False` rather than degrading

`_overall` already takes `web_probed: bool = True`, added for `--watch` so a suppressed probe does not degrade the aggregate (`core/status.py:310-335`).

An unresolvable port takes that same path: `_overall(..., web_probed=probe_web and port_known)`.

This is reuse rather than a new axis, and the semantics line up exactly.
`web_probed=False` means "cwcli has no web signal for this bench", and "cwcli could not determine which port to ask" is one of the two ways to have no web signal. Both are gaps in cwcli's knowledge, neither is a fault in the bench.

The alternative - treat an unreadable config as `degraded` - would manufacture a fresh instance of F3 in the act of fixing the old one: a bench with every program `RUNNING` reported broken because cwcli could not read a JSON file.

The gap is not silent. `web_port_verified: false` sits in the document (the pair `BenchPortMap` already uses, `core/scale.py:104-107`) and a `status.web_port_unknown` warning names the bench and points at `cwcli inspect`.

## Decision 5: A stopped instance reports `benches: []`

`_offline` returns `overall: offline`, `container_running: false`, `benches: []`.

Populating the list from the cache with every bench marked down was considered.
It is more uniform for a consumer iterating `benches`, and it is arguably knowable (the container is down, so everything in it is down).

Rejected on the smaller-diff rule: `overall: offline` plus `container_running: false` is a complete answer to "how is this instance", and manufacturing per-bench rows nothing was probed for adds a claim-shaped structure backed by no observation.
`inspect` is the verb for "what benches does this instance have"; `status` is the verb for "is it healthy".

## Decision 6: Bench enumeration comes from the cache, and that needs no new honesty token

The bare form enumerates `resolvers.cached_benches(project_name)` - the same list `--bench <index>` indexes into, so an index means the same thing in both forms and both agree with `cwcli axi benches`.

That list is remembered, not verified: a bench created since the last `cwcli inspect` is absent from it.

**This does not get a `where`-style verification token**, and the reasoning is the repo's own settled read-surface audit (`CLAUDE.md`, the `inspect` entry):

> `benches` serves remembered addressing (paths/labels) but those are validated on USE by the next live op rather than consumed as correctness evidence - the lower-severity, self-correcting case left deliberately untokened.

That applies here exactly.
Each enumerated bench's **health is read live** (`ps`, `supervisorctl`, the probe), so a stale path self-corrects into a visible no-processes bench rather than a confident wrong answer.
A missing bench is a real gap, but it is the *same* gap `--bench` already has, and it is `cwcli inspect`'s job to close it.

Today's no-cached-benches behavior is preserved unchanged: one synthetic entry at `resolvers.DEFAULT_BENCH_PATH` with the existing `bench.default_used` warning.
A never-inspected project keeps working; no new refusal is introduced.

## Decision 7: The per-bench probe cost is measured, not pre-optimized

The instance-wide form runs the full probe set once per bench: `read_marker`, `discover_stack` (one `ps`), `expected_labels` (one `Procfile` read), `supervisorctl_states`, and one `curl`.
On a two-bench instance that is twice today's work.

`discover_stack`'s `ps` is container-wide and identical for every bench - only the keying differs - so hoisting it to one `ps` per invocation is an available saving.

**It is not specified here.** The bench count is bounded in practice by the published-port range (six, before `cwcli scale` is needed), `--watch` already suppresses the curls via `probe_web=False`, and optimizing a cost nobody has measured is how a small change acquires a second mechanism.

Task 5.4 measures the real 2-bench and 6-bench latency and reports it. If it is material, the hoist is a follow-up with a number attached.

## Decision 8: `core.start` is in scope; the human prompt is removed

**`core.start` is in scope** because `wait_web_ready` is the same bench-blind probe reached by the other caller (audit F5: a false 60-second "web did not start" warning on `--bench 1`, which then routes the user to `cwcli status`, which under F3 confirms the phantom fault - the two bugs corroborate each other).
Once the probe requires an explicit port, `core/start.py:187` must supply one or hardcode 8000, and hardcoding it would leave the defect at the last remaining call site while the change claims to have removed it.

`StartOutcome` gains no field: `web_ready: bool | None` already means "not probed" for None, which is what an unresolvable port yields (skip the wait; do not spend 60 seconds on a guess).

**The human multi-bench prompt is removed** rather than retained for the bare form.
`commands/status.py:_fetch` currently resolves a `select_bench` `NEEDS_CHOICE` by prompting and re-invoking; with the core no longer returning that choice, the loop is dead and is deleted along with `axi_status`'s `emit_axi_choice_as_usage_error` branch.

This is a strict reduction in prompting, so the both-modes captain standard is satisfied trivially: `status` becomes non-prompting on every path, and `--bench` remains the non-interactive selector it already was.
Deleting the dead branches is part of the change, not a follow-up - a retained-but-unreachable prompt is how the refusal comes back.
