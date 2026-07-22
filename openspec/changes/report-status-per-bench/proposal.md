# Report status per bench, and probe the bench's own port

## Why

**On a multi-bench instance, `cwcli status` reports a bench it cannot name, using a measurement taken from a different bench.**

This was proven at runtime on a real two-bench instance, not read off the source: `/home/cmckay/projects/firstmate/data/cwcli-status-multibench-audit/report.md`.
That audit is the evidence base for this proposal and its findings are not re-argued here.

The headline result is that a **fully healthy bench reports `degraded`, at exit 0, on both surfaces**, in a document that contradicts itself on its own face:

```
$ cwcli axi status mbstat --bench 1
overall: degraded
web_http_code: null
processes[5]{label,up,pid,uptime_s,cpu_pct,rss_kb,state}:
  web,true,250,75,2.6,102284,RUNNING
```

`web,true,...,RUNNING` and `web_http_code: null` cannot both describe the same web server, and they do not.
The process row describes bench 1's web (correctly: process discovery *is* bench-keyed).
The HTTP code describes bench 0's absent one.

That fires on the ordinary "I only started the bench I am working on" flow, which is what `cwcli start <project> --bench N` exists for.

### The four findings, and why they are one change

| # | Finding | Where |
| --- | --- | --- |
| F1 | `StatusReport` carries **no bench field**, so neither surface can name the bench it just described | `core/status.py:78-95` |
| F2 | The web probe hardcodes `localhost:8000` and takes no bench argument | `core/supervision.py:574-582` |
| F3 | A healthy bench therefore reports `degraded` | `core/status.py:310-335` folding F2's answer |
| F4 | A bench serving nothing reports a live `web_http_code` belonging to another bench | same |

F3 and F4 are F2's two output directions.
F1 is why neither is visible to a reader: with no bench in the document, there is nothing to notice the mismatch against.

So there are exactly **two defects**, and each is a missing parameter:

- **The probe is missing a port.** `web_http_code(container)` cannot ask about a bench because it never receives one.
- **The report is missing a subject.** `StatusReport` cannot name a bench because it has no field for one.

Fixing either alone leaves a broken verb.
A bench-aware probe whose answer lands in an unattributed document is still unreadable; a named bench carrying another bench's HTTP code is a *better-labelled* lie.
This is why the change is one coherent shape rather than four patches.

### Status is the only member of its own family that cannot name its subject

- `StartOutcome.bench_path` (`core/start.py:57`)
- `LogsRead.bench_path` (`core/logs.py:103`)
- `ProcessRestartOutcome.bench_path` (`core/restart.py:34`)
- `StatusReport` - **absent**

And `core.inspect` already models an instance the right way: `InspectReport.benches: list[BenchInfo]` (`core/inspect.py:117`).
The per-bench model exists in this codebase. `status` simply does not use it.

### The test that should have caught this cannot

`tests/test_core_status.py:229-235`:

```python
def test_start_and_status_share_the_bench_selector(self, wire):
    # --bench 1 resolves to the same path in both verbs (one shared resolver).
    ...
    report = core_status.status("proj", bench="1").data
    assert report.overall in ("running", "degraded", "online")
```

The assertion admits every non-`offline` value, so it passes whichever bench was resolved.
It is a tautology **because the DTO has no bench field to assert on**.
The test's own name claims a property the type system makes unverifiable, which is the clearest possible signal that F1 is structural rather than cosmetic.

## What multibench actually is (the mechanism the fix depends on)

One instance is one `frappe` container. Benches are sibling directories under `/workspace`, each with its own `env/`, `apps/`, `sites/`, `Procfile`.

**Per-bench ports are assigned by bench's own `make_ports`, not by cwcli.**
cwcli writes zero port config; each bench records its assignment in its own `sites/common_site_config.json`.
The audit confirmed this on a live instance:

```
/workspace/bench1/sites/common_site_config.json -> webserver_port 8000, socketio_port 9000
/workspace/bench2/sites/common_site_config.json -> webserver_port 8001, socketio_port 9001
```

That file is therefore the **only** authority on which port a bench serves, and cwcli already reads it: `core/scale.py:211` `_read_assigned_ports`.
`axi scale` already emits the resulting per-bench map, already shaped for TOON.

The data exists, is already computed correctly, and is already tested.
Status does not consume it.

## The captain has already ruled on instance-wide mode

Settled 2026-07-22: the bare `cwcli status <project>` form must report **all** benches instead of today's exit-2 refusal (`axi`) / interactive prompt (human CLI).
This proposal designs that ruling; it does not reopen it.

## The decision this proposal asks the captain to rule on

**Is the new document shape uniform (recommended), or does the single-bench case keep today's flat shape?**

The instance-wide ruling forces a `benches` collection into the report.
It does not by itself say what a **single-bench** project, or an explicit `--bench 1`, should emit.

### Option A - uniform, always `benches[N]` (recommended)

`StatusReport` always carries `benches: list[BenchStatus]`.
`--bench 1` narrows that list to one entry; a single-bench project has one entry; the bare multi-bench form has N.
There is exactly one document shape and one parse path.

Top-level keeps `overall` (now an instance-level fold, still the FIRST field so the axi contract holds), `project`, and `container_running`.
`supervisor_up`, `web_http_code`, `processes`, and `not_cwcli_supervised` move **into** each bench entry, where they have always belonged.

- **Cost, stated plainly:** every `axi status` consumer breaks, **including single-bench ones**, because `processes` moves from the top level to `benches[0].processes`. It is a one-line path edit, but it is a break, and it is not confined to the multi-bench case that is already wrong.
- **Benefit:** an agent parses one shape. `inspect` is uniform for the same reason and nobody has to branch on bench count to read it.

### Option B - additive only, two shapes

Keep the flat fields for the single-bench and `--bench` cases (purely additive: add `bench_path`/`index`/`label`/`web_port`), and emit `benches[N]` **only** for the bare multi-bench form.

- **Cost:** `axi status` emits two different documents depending on invocation and on a fact the caller may not know (how many benches the project has). Every consumer must branch. This is precisely the shape AXI's one-document contract exists to prevent, and an agent that only ever tested against a single-bench project would ship code that breaks the first time the project grows a second bench - the same class of failure as the bug being fixed.
- **Benefit:** no break for existing single-bench consumers.

**Recommended: Option A.**
The field this change is fixing, `web_http_code`, is the top-level field that has been *wrong*.
Preserving its position to preserve compatibility means preserving the exact shape that taught readers to look for a whole-instance answer in a single-bench-shaped document.
One break, once, uniform, is cheaper than a permanent shape branch.

**There is deliberately no `--flat` compatibility flag.** A flag that reproduces the old document reproduces a document whose `web_http_code` is unattributed. It has no named beneficiary that `benches[0]` does not serve (the `axi apps install --force` / `axi migrate --skip-maintenance` rulings).

## The shipped-contract break, stated plainly

### What changes on `cwcli axi status`

| Today | After |
| --- | --- |
| multi-bench, no `--bench` -> `error: multiple benches; pass --bench <index\|label>`, **exit 2** | reports every bench, **exit 0** |
| top-level `supervisor_up`, `web_http_code`, `processes`, `not_cwcli_supervised` | inside each `benches[i]` |
| top-level `overall`, `project`, `container_running` | unchanged, `overall` still first |
| always exits 0 on a successful read | unchanged |
| flags `--bench` | unchanged, no flag added or removed |

The verb list and every flag are untouched, so `scripts/build_skill.py` regenerates `skills/cwcli/SKILL.md` with no verb-table change.

### What the pinning tests become

| Test | Today | After |
| --- | --- | --- |
| `tests/test_axi_start_status.py:264` `test_multi_bench_names_the_flag_exit_2` | asserts exit 2 and `--bench` in stdout | **replaced** by `test_multi_bench_reports_every_bench_exit_0`, asserting one TOON document carrying both benches, each named |
| `tests/test_core_status.py:223` `test_multi_bench_returns_select_bench` | asserts `NEEDS_CHOICE` / `select_bench` | **replaced** by an assertion that `core.status` **never** returns `NEEDS_CHOICE` on any path, so the refusal cannot return by accident |
| `tests/test_core_status.py:229` `test_start_and_status_share_the_bench_selector` | tautological (`overall in (...)`) | **becomes real**: asserts `report.benches[0].bench_path == "/w/b1"` |

`select_bench` disappears from `status` entirely.
That is a deliberate simplification worth naming: `commands/status.py:_fetch`'s prompt-and-retry loop and `commands/axi.py:axi_status`'s `emit_axi_choice_as_usage_error` branch both become dead and are deleted.

### What an agent relying on the refusal should do instead

The exit-2 refusal was a signal to go read `cwcli axi benches` and then poll `cwcli axi status --bench <i>` once per bench, reassembling the instance view by hand from documents that did not say which bench they described.

**Nothing that was possible before becomes impossible.**
`--bench <index|label>` still answers for exactly one bench, unchanged (§3 below).
The bare form now answers the question the refusal used to send the agent away to reconstruct, in one call, with each answer attributed.

An agent that branches on `exit == 2` to mean "multi-bench, go enumerate" will simply stop taking that branch and receive the enumeration inline.
An agent that reads `processes` repoints to `benches[0].processes`, or iterates `benches`, which is what it wanted.

## Fail honest: the port is never guessed

Per `core.where`'s rule, and stated as the load-bearing constraint of this change:

**If a bench's assigned port cannot be read, the HTTP code is reported unknown and said to be unknown. It is never probed against 8000. That fallback IS the current bug.**

Concretely, when `sites/common_site_config.json` is unreadable or unparseable for a bench (the case `_read_assigned_ports` already skips rather than defaults):

- `web_port: null` and `web_port_verified: false` - the same honest pair `BenchPortMap` already uses (`core/scale.py:104-107`), rather than a guessed 8000 reported as fact.
- `web_http_code: null`, because no probe was made.
- **The bench does not degrade on account of it.** `_overall` already has this exact behavior behind its `web_probed=False` parameter (built for `--watch`); an unresolved port takes the same path. A bench whose supervisor is up and whose every program is healthy stays `running` when cwcli could not determine where to look, because that is a gap in cwcli's knowledge, not a fault in the bench.
- A `status.web_port_unknown` warning names the bench and points at `cwcli inspect <project>`.

The alternative - degrade on an unreadable config - would manufacture a *new* instance of F3 while fixing the old one.

The same rule applies to `core.start`: with no resolvable port it **skips the wait entirely** and reports `web_ready: null` (the field's existing "not probed" value), rather than sitting on a guessed port for 60 seconds and then warning.

## `--bench` keeps working, unchanged

`--bench <index|label>` resolves through `resolvers.resolve_bench` exactly as today, including the `--path` conflict, the `bench.not_found` `NOT_FOUND` raise, and the `bench.sole` warning on a single-bench project.
The only change is what happens **after** resolution: instead of populating the report's top-level fields, the resolved bench populates the report's single `benches` entry.

With `--bench N` the instance `overall` equals that bench's `overall` by construction (a fold over one element), so the human stdout token is unchanged in meaning for every `--bench` invocation and for every single-bench project.

`cwcli status <project> --bench 1` therefore continues to answer "how is bench 1", exits 0, and prints one token on stdout.
What it gains is that the token is now *about bench 1*.

## Both surfaces

Both renderers are thin and both inherit the core defect, so both are corrected by the core change plus a rendering pass.

### Human (`commands/status.py`)

- **stdout stays exactly one token**, the instance `overall`. This is load-bearing and documented (`README.md:1589-1664`); it does not change.
- stderr gains one sub-block per bench, each headed by its index, path, label, and own aggregate, with the existing per-process detail nested beneath. The web line names the port it asked (`web :8001 -> 404`) instead of implying a universal one.
- `--watch` renders one table with a `bench` column, present only when more than one bench is reported, so the single-bench live view is unchanged. It keeps `probe_web=False`, which now suppresses N curls instead of one.
- The multi-bench **prompt is removed**. The bare form no longer asks which bench; it reports all of them. This is a strict reduction in prompting, so the both-modes captain standard is satisfied trivially: the verb becomes non-prompting on every path.

### Agent (`commands/axi.py:621` `axi_status`)

- Still `emit_result(report)` and still always exit 0. **Zero encoder change:** `utils/toon.py:_encode_value` already emits records-carrying-nested-collections as the `- `-marked list form, built for exactly this shape (a bench's nested `sites`), and proven by `axi inspect`. Each bench's `processes` stays a compact scalar table.
- The `NEEDS_CHOICE` branch is deleted as unreachable.

Sample multi-bench document:

```
overall: running
project: mbstat
container_running: true
benches[2]:
  - index: 0
    bench_path: /workspace/bench1
    label: null
    overall: running
    supervisor_up: true
    web_port: 8000
    web_port_verified: true
    web_http_code: "404"
    not_cwcli_supervised: false
    processes[5]{label,up,pid,uptime_s,cpu_pct,rss_kb,state}:
      web,true,3368,122,2.4,102720,RUNNING
      ...
  - index: 1
    bench_path: /workspace/bench2
    ...
```

## Core delta (the honest cost)

This is **not** a zero-core-delta change and does not claim to be.

| Change | Why it is not avoidable |
| --- | --- |
| **Promote** `core/scale.py:211` `_read_assigned_ports` to `resolvers.resolve_assigned_ports` | Status and start become its second and third callers. Copying it would put three copies of "which port does this bench serve" in the tree, and the day they drift is the day this bug comes back on one of them. The `set_maintenance` precedent (`add-axi-bench-exec-verbs` §0.7) is binding: promote, never copy. |
| **Re-signature** `supervision.web_http_code` / `web_is_serving` / `wait_web_ready` to take an explicit, non-defaulted `port: int` | This is the fix. See Decision 2: no default is what makes the bug unrepresentable rather than merely corrected. Two call sites (`core/status.py:191`, `core/start.py:187`); `web_is_serving` has no caller outside the module. |
| **Restructure** `StatusReport` and add `BenchStatus` | F1. |
| **Loop** `core.status` over benches instead of resolving one | The captain's instance-wide ruling. |
| `core/start.py`'s wait and its `start.web_not_ready` message text | Forced: the message names `:8000`, and once the probe requires a real port the message must name the real one. Covered below. |

### `core.start` is in scope, deliberately

The audit's F5 (`cwcli start --bench 1` emits a false 60-second "web did not start" warning, then routes the user to `cwcli status`, which then confirms the phantom fault) is the **same defect reached by the other caller**.
`wait_web_ready` is bench-blind for the identical reason.

Fixing it is forced rather than opportunistic: once the probe requires an explicit port, `core/start.py:187` must supply one, and it already has `resolved_path` in scope.
Leaving `start` passing a literal 8000 would re-create the bug at the one remaining call site while claiming to have removed it.

`StartOutcome` needs **no new field** - `web_ready: bool | None` already means "not probed" for None.
`core/start.py`'s `start.web_not_ready` warning text changes to name the bench's real port.

## What Changes

- **PROMOTE** `core/scale.py:_read_assigned_ports` to `resolvers.resolve_assigned_ports(container, bench_paths) -> dict[str, tuple[int, int]]`, behavior byte-identical (including its skip-on-unreadable, which is the fail-honest half). `core.scale`'s one call site re-points; the private helper is deleted, not left as a shim.
- **RE-SIGNATURE** `supervision.web_http_code(container, *, port: int)`, `web_is_serving(container, *, port: int)`, `wait_web_ready(container, *, port: int, ...)`. No default, keyword-only.
- **ADD** `core.status.BenchStatus` and restructure `StatusReport` to `overall` / `project` / `container_running` / `benches: list[BenchStatus]`.
- **CHANGE** `core.status.status(...)` to report every cached bench when no selector is given, and exactly one when `--bench`/`--path` is. It never returns `NEEDS_CHOICE`.
- **ADD** an instance-level fold for `overall` over the four existing tokens (no fifth token - Decision 3).
- **CHANGE** `core/start.py` to resolve its bench's port before waiting, skip the wait when the port is unknown, and name the real port in `start.web_not_ready`.
- **RENDER** both surfaces per bench, keeping stdout at one token (human) and one TOON document (axi).
- **REPLACE** the two tests that pin the refusal, and **de-tautologize** `test_start_and_status_share_the_bench_selector`.
- **UPDATE** `README.md` (`status` section, the `running` definition at :1617, the axi surface paragraph at :2052), `CLAUDE.md`'s supervisord entry, and the `cwcli-lifecycle` skill.

## Impact

- **New:** `resolvers.resolve_assigned_ports`, `core.status.BenchStatus`, per-bench rendering in both frontends, E2E coverage on a real two-bench instance.
- **Changed:** `core/status.py`, `core/supervision.py` (three signatures), `core/start.py`, `core/scale.py` (one call site), `core/resolvers.py`, `commands/status.py`, `commands/axi.py`, `tests/test_core_status.py`, `tests/test_axi_start_status.py`, `tests/test_core_start.py`, `README.md`, `CLAUDE.md`, `.claude/skills/cwcli-lifecycle/`.
- **Unchanged:** `utils/toon.py` (the nested shape is already supported), `scripts/build_skill.py`'s output verb table, every other core verb, the cache schema, `discover_stack`'s bench keying (which was already correct), the `axi status` always-exits-0 contract, and the one-token-on-stdout contract.
- **Measured, not assumed:** the instance-wide form performs the per-bench probe set N times. Task 5.4 measures it on a real 2-bench and 6-bench instance and reports; no optimization is specified in advance (Decision 6).

## Non-goals

Each of these is filed separately and is untouched here.

- **The Host-header 404 (audit F6).** The probe sends no `Host` header, so Frappe correctly answers 404 for a healthy multi-tenant site. **This change interacts with it and the interaction must be stated:** after this change the 404 is *correctly attributed* to the bench that produced it, but it is still a 404. `_overall` treats any code other than `None`/`"000"` as serving, so the aggregate is right either way; only the displayed number remains alarming. Fixing the number is a separate change.
- **`cwcli restart <project> --bench N` silently acting on the wrong bench (F7).** Different command, different cause (`--bench` threaded only into the `--process` branch).
- **The missing per-bench stop selector.** `cwcli stop` is project-wide.
- **init's hardcoded `:8000` "Open:" URL (F8).** That needs the host-side port, which requires the compose parse `core.scale` does, and it belongs with `cwcli open`.
- **No host-side port in `StatusReport`.** `web_port` is the container-side port the probe uses, and nothing else. Reporting "which host port do I browse" is F8's question and would pull the compose parse into a read verb for no benefit to the defect being fixed.
- **No fifth `overall` token.** The four are kept (Decision 3).
- **No new `Choice.kind`, no new `ErrorKind`.** If implementation finds one is needed, it is REPORTED rather than absorbed (the standing rule).
