# Tasks: report status per bench

**Phase A (the proposal) is APPROVED and phase B (the implementation) is what this branch carries.**

Proposed as its own phase so the document-shape argument got read before an implementation biased the review - the sequencing `add-axi-apps-checkout-verb` and `add-axi-bench-exec-verbs` used, and the captain approved all three times.

## 0. Captain approval (blocks everything else) - RULED 2026-07-22

The captain ruled on the Lavish review surface, which he ended himself after submitting; every answer below is an explicit selection, not an inferred default.

- [x] 0.1 **Document shape: A.** Uniform, `benches[N]` always. `--bench` narrows to one entry; a single-bench project has one entry. He accepted that every `axi status` consumer breaks once, including single-bench ones, because `processes` moves to `benches[0].processes`.
- [x] 0.2 **Shipped-contract break acknowledged** on `cwcli axi status`: multi-bench with no `--bench` goes from exit 2 to exit 0, and `supervisor_up`/`web_http_code`/`processes`/`not_cwcli_supervised` move inside `benches[i]`. Two pinning tests are replaced (proposal §"What the pinning tests become").
- [x] 0.3 **No `--flat` compatibility flag.** Confirms the proposal's recommendation.
- [x] 0.4 **Instance-aggregate fold confirmed**, including "any bench running beats a never-started bench" (`design.md` Decision 3). `degraded` still dominates.
- [x] 0.5 **`core.start` is in scope**, bringing the audit's F5 false 60-second warning into this change. `core/start.py` does NOT hardcode 8000 into the newly-explicit `port` parameter.
- [x] 0.6 **TIGHTEN the defaulted-port hole** (the ruling that changed the proposal; see §1.1). `_read_assigned_ports` returns a defaulted `(8000, 9000)` for a bench whose config parses as a dict but omits `webserver_port`, with no signal distinguishing an explicit 8000 from a filled-in one (`core/scale.py:232` as shipped). The captain chose to CLOSE it rather than record it as a knowingly-retained defect, because the change's own headline promise is that 8000 is never guessed.
- [x] 0.7 Promote-don't-copy acknowledged: `_read_assigned_ports` gains callers and is PROMOTED to `resolvers.resolve_assigned_ports`, not copied (`design.md` Decision 2, the `set_maintenance` precedent).

## 1. The shared port reader

- [x] 1.1 Promote `core/scale.py:_read_assigned_ports` to `resolvers.resolve_assigned_ports(container, bench_paths, *, fill_defaults: bool) -> dict[str, tuple[int, int]]`. Its skip-on-unreadable/unparseable/non-dict/non-numeric is preserved verbatim - that skip is the fail-honest half, not an oversight.

      **The reader DISTINGUISHES explicit from defaulted** (the §0.6 TIGHTEN ruling). This task originally read "behavior byte-identical", and that instruction was WRONG: it is precisely what would carry the defaulted `(8000, 9000)` across into a PROBE TARGET and re-create the exact bug inside the change whose headline promise is that 8000 is never guessed. On a bench past the first that means `web_port: 8000`, `web_port_verified: true`, and a probe against bench 0's server - F2 and F4 reported as verified fact.

      `fill_defaults` is keyword-only with NO default, because the two callers need OPPOSITE answers and neither is the obvious one:
      - `core.scale` passes `True` and keeps today's behaviour exactly. A bench whose config omits a port key serves Frappe's default, and scale is computing which host ports to PUBLISH, so that bench must be covered by the range. The default is correct and deliberate there, and the docstring already said so.
      - `core.status` and `core.start` pass `False`. The SAME answer becomes a probe target, so an omitted key is UNRESOLVED and takes the honest path this change already designed: `web_port: null`, `web_port_verified: false`, no probe, `_overall(..., web_probed=False)`, and a `status.web_port_unknown` warning.

      The asymmetry is what made the hole easy to miss: promoting a correct default into a different risk profile is the failure mode, not the default itself.
- [x] 1.2 Re-point **BOTH** of `core/scale.py`'s call sites (`:463` and `:556` as shipped - the proposal said "single call site" and was wrong; re-pointing one and deleting the helper breaks the other) and DELETE the private helper. No shim: the call is one line, so two callers still do not justify indirection. `_WEB_CONTAINER_BASE`/`_SOCKETIO_CONTAINER_BASE` stay in `core/scale.py` (its compose regexes are built from them); the resolver carries its own `WEB_CONTAINER_BASE`/`SOCKETIO_CONTAINER_BASE`.
- [x] 1.3 `tests/test_core_scale.py` stays green unchanged, and the core purity gate (`tests/test_core_envelope.py`) still passes for `resolvers`.

## 2. The probe

- [x] 2.1 Re-signature `supervision.web_http_code(container, *, port: int)`, `web_is_serving(container, *, port: int)`, `wait_web_ready(container, *, port: int, timeout=..., interval=...)`. Keyword-only, NO default (`design.md` Decision 2). This also breaks 7 tests in `tests/test_core_supervision.py::TestWebProbe` (missing from the proposal's Impact list; 4 of them call `web_is_serving` directly, which is why Decision 2's "no caller outside the module" is true of PRODUCTION code only).
- [x] 2.2 Update the three docstrings so none of them names `:8000` as a fact.
- [x] 2.3 Add a test asserting no default is declared for `port` on any of the three, so a later "convenience" default is a test failure rather than a silent re-arming - plus one asserting the probe URL carries the CALLER's port.

## 3. The core

- [x] 3.1 Add `core.status.BenchStatus` (`index`, `bench_path`, `label`, `overall`, `supervisor_up`, `web_port`, `web_port_verified`, `web_http_code`, `processes`, `not_cwcli_supervised`). `index` is `int | None`: it is the position in the stable CACHED order, and a `--path` override (or the synthetic default on a never-inspected project) has none - reporting 0 there would name a bench `--bench 0` resolves somewhere else, the attributed-lie class this change removes and restructure `StatusReport` to `overall` / `project` / `container_running` / `benches`. `overall` stays the FIRST field.
- [x] 3.2 Enumerate benches from `resolvers.cached_benches` when no selector is given; narrow to one when `--bench`/`--path` is. `resolve_bench`'s existing errors and warnings are unchanged (`design.md` Decision 6).
- [x] 3.3 Resolve each bench's port via `resolvers.resolve_assigned_ports` and pass it explicitly to the probe. Report the port that was actually probed.
- [x] 3.4 Unresolvable port (which now includes a config that PARSES but names no `webserver_port` - see §1.1): `web_port=None`, `web_port_verified=False`, no probe, `_overall(..., web_probed=probe_web and port_known)`, plus a `status.web_port_unknown` warning naming the bench (`design.md` Decision 4). NEVER fall back to 8000.
- [x] 3.5 Add the instance fold per `design.md` Decision 3. Four tokens, no fifth.
- [x] 3.6 `_offline` returns `benches: []` (`design.md` Decision 5).
- [x] 3.7 Remove the `select_bench` return path entirely. `core.status.status` must never return `NEEDS_CHOICE`.
- [x] 3.8 Preserve the not-cwcli-supervised (honcho) fallback per bench, unchanged in substance - it is a per-bench property and moves onto `BenchStatus` with no behavior change.

## 4. `core.start` (per the §0.5 ruling)

- [x] 4.1 Resolve the resolved bench's web port and pass it to `wait_web_ready`.
- [x] 4.2 Unresolvable port: SKIP the wait, `web_ready=None`. No 60-second spend on a guess. `StartOutcome` gains no field.
- [x] 4.3 `start.web_not_ready` names the bench's real port, not `:8000`.

## 5. Both surfaces

- [x] 5.1 `commands/status.py`: stdout stays EXACTLY one token (the instance `overall`) on every path. Per-bench sub-blocks on stderr, each headed by index/path/label, web line naming the probed port.
- [x] 5.2 `commands/status.py`: `--watch` renders a `bench` column only when more than one bench is reported; single-bench live view unchanged; `probe_web=False` kept.
- [x] 5.3 `commands/status.py`: DELETE `_fetch`'s prompt-and-retry loop, now dead. `commands/axi.py:axi_status`: DELETE the `emit_axi_choice_as_usage_error` branch, now unreachable. A retained-but-unreachable prompt is how the refusal comes back (`design.md` Decision 8).
- [x] 5.4 **ANSWERED before implementation:** no encoder change is required. Proven by running the SHIPPED `utils/toon.py` on this proposal's own sample shape - it emits the `- ` item form with the nested compact `processes` table, matching the sample exactly (the `axi inspect` precedent, already pinned by `tests/test_axi_inspect.py:80-87`).
- [ ] 5.5 **OPEN GAP:** Measure the instance-wide latency on a real 2-bench and 6-bench instance (`design.md` Decision 7).
      The committed E2E records no latency measurement for either instance size.
      The `discover_stack` `ps` hoist is deliberately NOT done here because there is no measurement to justify it.
      The filed follow-up task `cwcli-multibench-serving-e2e` will provide the real serving fixture that enables these measurements.

## 6. Tests

- [x] 6.1 REPLACED `tests/test_core_status.py::TestChoicesAndErrors::test_multi_bench_returns_select_bench` with `test_status_never_returns_needs_choice`, asserted across every bench count and every selector shape so the refusal cannot return through a path that was not re-pointed.
- [x] 6.2 DE-TAUTOLOGIZED `tests/test_core_status.py:229` `test_start_and_status_share_the_bench_selector`: assert `report.benches[0].bench_path == "/w/b1"`. Its name has always claimed this; the DTO now makes it assertable.
- [x] 6.3 REPLACED `tests/test_axi_start_status.py::TestAxiStatus::test_multi_bench_names_the_flag_exit_2` with `test_multi_bench_reports_every_bench_exit_0`, asserting one TOON document carrying both benches, each named. **The class name is load-bearing:** an identically-named test lives in `TestAxiStart` and pins `axi start`'s refusal, which this change does not touch and which must NOT be replaced - `start` mutates one bench, so it must be told which.
- [x] 6.4 The F3 regression, as a unit test: bench 1 healthy on its own port, bench 0 not started -> bench 1 is `running`, and no bench entry pairs a `RUNNING` web process with a null `web_http_code`.
- [x] 6.5 The F4 regression: bench 1's web `STOPPED` while bench 0 serves -> bench 1's `web_http_code` is null/`000`, never bench 0's code.
- [x] 6.6 Fail-honest: an unreadable `common_site_config.json` yields `web_port: null`, `web_port_verified: false`, `overall: running` for an otherwise-healthy bench, a `status.web_port_unknown` warning, and ZERO probes issued.
- [x] 6.7 The fold: `online` + `running` -> `running`; `running` + `degraded` -> `degraded`; container down -> `offline` with empty `benches`.
- [x] 6.8 Stdout purity, human: exactly one token on stdout for single-bench, multi-bench, offline, and `--bench` invocations.
- [x] 6.9 Stdout purity, axi: stdout parses as exactly one TOON document on every path, and `axi status` still exits 0 for `degraded`.
- [x] 6.10 `core.start` unregressed: `tests/test_core_start.py` green, plus a test that an unresolvable port skips the wait rather than spending the timeout.

## 7. Real-instance E2E

Per the captain standard: a behavior change is not proven by unit tests plus a green pipeline, and the E2E is re-run after any review fixes.

**OPEN GAP:** The committed multibench E2E creates only a bench skeleton: a directory with `apps/`, `sites/`, and `sites/common_site_config.json` containing `{}`.
It has no venv, no Procfile, no web process, and no supervisord.
Because of that, the real-Docker matrix runs the suite green without ever exercising F3 (a healthy bench past the first reporting degraded), F4 (a dead bench borrowing a sibling's live HTTP code), or F5 (start's false 60-second `web_not_ready` warning).
It also measures no latency for either a 2-bench or a 6-bench instance.

What is covered: the structural change, including the per-bench document shape, the fold, the removed refusal, the explicit non-defaulted port parameter, and the fail-honest unresolved-port path, is pinned by the 1798 passing unit tests.
This includes `TestPerBenchWebProbe`, whose two-bench fakes carry per-bench `ps`, per-bench `supervisorctl`, and per-port `curl` answers.
What is not covered: the runtime probe behaviour against two genuinely serving benches on real ports, and the per-bench latency numbers.
The follow-up task `cwcli-multibench-serving-e2e` is already filed and is where the real serving fixture lands.
That work will close 7.1 through 7.3 and enable 5.5.

- [ ] 7.1 **OPEN GAP:** Prove F3 against two genuinely serving benches on their real ports.
- [ ] 7.2 **OPEN GAP:** Prove F4 against two genuinely serving benches on their real ports.
- [ ] 7.3 **OPEN GAP:** Prove F5 by starting a selected non-first serving bench and confirming that the wait observes its own port without a false 60-second `web_not_ready` warning.

- [x] 7.4 `tests/e2e/test_start_status_new_behavior_e2e.py` - the multi-bench test is rewritten from `test_multibench_no_selector_refuses_noninteractively` to `test_multibench_start_refuses_but_status_reports_every_bench`: `axi start` still refuses with exit 2 naming `--bench` (it mutates one bench), while bare `axi status` reports `benches[2]:` at exit 0 with both benches named, human `status` prints exactly one token at exit 0 from a non-TTY, and `--bench 0` still narrows to `benches[1]:`.
- [x] 7.5 Both modes: the verb is now NON-PROMPTING on every path, so there is nothing to drive through a pty. That is a strict reduction in prompting and satisfies the both-modes standard trivially; `--bench` remains the non-interactive selector it already was. Asserted rather than assumed by `test_status_never_returns_needs_choice` (core) and the non-TTY E2E above.
- [x] Bonus, unplanned: the same E2E's second-bench skeleton writes `common_site_config.json` as `{}` - a config that PARSES but names no port, which is EXACTLY the §0.6 hole - so it now asserts `web_port: null` / `web_port_verified: false` for that bench against real Docker. The tightening has real-instance coverage without a new fixture.
- [x] Structural: `test_axi_status_reports_per_process_health` and `test_per_process_supervisor_e2e.py::test_axi_status_reports_supervisord_state` re-point their `processes[` assertions, which are now INDENTED under `benches`, and the former also asserts `benches[`, `bench_path:` and `web_port_verified: true`.

- [x] 7.6 Teardown by exact name; no broad prune; no protected instance touched - the `tests/e2e/harness.py` contract every leg of the matrix already follows.

## 8. Docs and memory

- [x] 8.1 `README.md`: the `status` section (1589-1668 as shipped), the `running` definition (:1617, which said "the web server answers on `:8000`"), the `start` web-wait bullet, and the axi surface paragraph (:2052). **That last line is being CORRECTED INTO TRUTH, not describing the refusal:** it claims `cwcli axi status` **always exits 0**, which has been FALSE since the multi-bench refusal shipped (exit 2) and becomes true under this change. A pre-existing defect this change fixes, and a point in its favour.
- [x] 8.2 `CLAUDE.md`: the supervisord entry's `status` contract, noting the per-bench report and the explicit-port probe.
- [x] 8.3 `.claude/skills/cwcli-lifecycle/`: the status/start incident detail, keeping the root-cause "why" (the probe was bench-blind, and the fix is an explicit non-defaulted port so it cannot be re-inherited).
- [x] 8.4 Confirm `skills/cwcli/SKILL.md` regenerates unchanged in its verb table (no flag added or removed) and that `tests/test_axi_skill.py --check` stays green.
