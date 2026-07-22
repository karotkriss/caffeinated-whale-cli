# Tasks: report status per bench

**Phase A only. This change is a PROPOSAL. Nothing below §1 is implemented, and §0 blocks everything.**

Proposed as its own phase so the document-shape argument gets read before an implementation biases the review - the sequencing `add-axi-apps-checkout-verb` and `add-axi-bench-exec-verbs` used, and the captain approved both times.

## 0. Captain approval (blocks everything else)

- [ ] 0.1 **Document shape** (`design.md` Decision 1, proposal §"The decision this proposal asks the captain to rule on"). Rule between:
      **A** - uniform, `benches[N]` always, single-bench consumers break too (recommended);
      **B** - additive only, flat fields kept for the single-bench and `--bench` cases, `benches` only for the bare multi-bench form.
      A ruling for B changes §2, §4 and §5 substantially and is cheaper to take now than after.
- [ ] 0.2 **Acknowledge the shipped-contract break** on `cwcli axi status`: multi-bench with no `--bench` goes from exit 2 to exit 0, and (under A) `supervisor_up`/`web_http_code`/`processes`/`not_cwcli_supervised` move inside `benches[i]`. Two pinning tests are replaced (proposal §"What the pinning tests become").
- [ ] 0.3 **Confirm no `--flat` compatibility flag** (proposal). Recommended NO: a flag that reproduces the old document reproduces an unattributed `web_http_code`.
- [ ] 0.4 **Confirm the instance-aggregate fold**, specifically "any bench running beats a never-started bench" (`design.md` Decision 3). This is the rule that makes the audit's headline scenario read `running`; the plain worst-wins alternative reports `online` while a bench serves.
- [ ] 0.5 **Confirm `core.start` is in scope** (`design.md` Decision 8), which brings the audit's F5 false 60-second warning into this change. Declining it means `core/start.py` hardcodes 8000 into the newly-explicit `port` parameter, which must then be recorded as a knowingly-retained defect.
- [ ] 0.6 Acknowledge the reported promote-don't-copy item: `core/scale.py:211` `_read_assigned_ports` gains two callers and is PROMOTED to `resolvers.resolve_assigned_ports`, not copied (`design.md` Decision 2, the `set_maintenance` precedent).

## 1. The shared port reader

- [ ] 1.1 Promote `core/scale.py:_read_assigned_ports` to `resolvers.resolve_assigned_ports(container, bench_paths) -> dict[str, tuple[int, int]]`, behavior byte-identical INCLUDING its skip-on-unreadable (that skip is the fail-honest half, not an oversight). Move the `_WEB_CONTAINER_BASE`/`_SOCKETIO_CONTAINER_BASE` defaults with it.
- [ ] 1.2 Re-point `core/scale.py`'s single call site and DELETE the private helper. Do not leave a shim: one caller does not justify indirection.
- [ ] 1.3 Confirm `tests/test_core_scale.py` stays green unchanged in substance, and that the core purity gate (`tests/test_core_envelope.py`) still passes for `resolvers`.

## 2. The probe

- [ ] 2.1 Re-signature `supervision.web_http_code(container, *, port: int)`, `web_is_serving(container, *, port: int)`, `wait_web_ready(container, *, port: int, timeout=..., interval=...)`. Keyword-only, NO default (`design.md` Decision 2).
- [ ] 2.2 Update the three docstrings so none of them names `:8000` as a fact.
- [ ] 2.3 Add a test asserting no default is declared for `port` on any of the three, so a later "convenience" default is a test failure rather than a silent re-arming.

## 3. The core

- [ ] 3.1 Add `core.status.BenchStatus` (`index`, `bench_path`, `label`, `overall`, `supervisor_up`, `web_port`, `web_port_verified`, `web_http_code`, `processes`, `not_cwcli_supervised`) and restructure `StatusReport` to `overall` / `project` / `container_running` / `benches`. `overall` stays the FIRST field.
- [ ] 3.2 Enumerate benches from `resolvers.cached_benches` when no selector is given; narrow to one when `--bench`/`--path` is. `resolve_bench`'s existing errors and warnings are unchanged (`design.md` Decision 6).
- [ ] 3.3 Resolve each bench's port via `resolvers.resolve_assigned_ports` and pass it explicitly to the probe. Report the port that was actually probed.
- [ ] 3.4 Unresolvable port: `web_port=None`, `web_port_verified=False`, no probe, `_overall(..., web_probed=probe_web and port_known)`, plus a `status.web_port_unknown` warning naming the bench (`design.md` Decision 4). NEVER fall back to 8000.
- [ ] 3.5 Add the instance fold per `design.md` Decision 3. Four tokens, no fifth.
- [ ] 3.6 `_offline` returns `benches: []` (`design.md` Decision 5).
- [ ] 3.7 Remove the `select_bench` return path entirely. `core.status.status` must never return `NEEDS_CHOICE`.
- [ ] 3.8 Preserve the not-cwcli-supervised (honcho) fallback per bench, unchanged in substance - it is a per-bench property and moves onto `BenchStatus` with no behavior change.

## 4. `core.start` (per the §0.5 ruling)

- [ ] 4.1 Resolve the resolved bench's web port and pass it to `wait_web_ready`.
- [ ] 4.2 Unresolvable port: SKIP the wait, `web_ready=None`. No 60-second spend on a guess. `StartOutcome` gains no field.
- [ ] 4.3 `start.web_not_ready` names the bench's real port, not `:8000`.

## 5. Both surfaces

- [ ] 5.1 `commands/status.py`: stdout stays EXACTLY one token (the instance `overall`) on every path. Per-bench sub-blocks on stderr, each headed by index/path/label, web line naming the probed port.
- [ ] 5.2 `commands/status.py`: `--watch` renders a `bench` column only when more than one bench is reported; single-bench live view unchanged; `probe_web=False` kept.
- [ ] 5.3 `commands/status.py`: DELETE `_fetch`'s prompt-and-retry loop, now dead. `commands/axi.py:axi_status`: DELETE the `emit_axi_choice_as_usage_error` branch, now unreachable. A retained-but-unreachable prompt is how the refusal comes back (`design.md` Decision 8).
- [ ] 5.4 Confirm `emit_result` needs no encoder change - `utils/toon.py:_encode_value` already emits records-carrying-nested-collections as the `- ` list form (the `axi inspect` precedent). If it does need one, REPORT it rather than absorbing it.
- [ ] 5.5 **Measure** the instance-wide latency on a real 2-bench and 6-bench instance and record the numbers (`design.md` Decision 7). Do NOT hoist `discover_stack`'s `ps` in this change; if the measurement says it matters, file the hoist as a follow-up with the number attached.

## 6. Tests

- [ ] 6.1 REPLACE `tests/test_core_status.py::TestChoicesAndErrors::test_multi_bench_returns_select_bench` with an assertion that `core.status.status` never returns `NEEDS_CHOICE` on any path.
- [ ] 6.2 DE-TAUTOLOGIZE `tests/test_core_status.py:229` `test_start_and_status_share_the_bench_selector`: assert `report.benches[0].bench_path == "/w/b1"`. Its name has always claimed this; the DTO now makes it assertable.
- [ ] 6.3 REPLACE `tests/test_axi_start_status.py::test_multi_bench_names_the_flag_exit_2` with `test_multi_bench_reports_every_bench_exit_0`, asserting one TOON document carrying both benches, each named.
- [ ] 6.4 The F3 regression, as a unit test: bench 1 healthy on its own port, bench 0 not started -> bench 1 is `running`, and no bench entry pairs a `RUNNING` web process with a null `web_http_code`.
- [ ] 6.5 The F4 regression: bench 1's web `STOPPED` while bench 0 serves -> bench 1's `web_http_code` is null/`000`, never bench 0's code.
- [ ] 6.6 Fail-honest: an unreadable `common_site_config.json` yields `web_port: null`, `web_port_verified: false`, `overall: running` for an otherwise-healthy bench, a `status.web_port_unknown` warning, and ZERO probes issued.
- [ ] 6.7 The fold: `online` + `running` -> `running`; `running` + `degraded` -> `degraded`; container down -> `offline` with empty `benches`.
- [ ] 6.8 Stdout purity, human: exactly one token on stdout for single-bench, multi-bench, offline, and `--bench` invocations.
- [ ] 6.9 Stdout purity, axi: stdout parses as exactly one TOON document on every path, and `axi status` still exits 0 for `degraded`.
- [ ] 6.10 `core.start` unregressed: `tests/test_core_start.py` green, plus a test that an unresolvable port skips the wait rather than spending the timeout.

## 7. Real-instance E2E

Per the captain standard: a behavior change is not proven by unit tests plus a green pipeline, and the E2E is re-run after any review fixes.

- [ ] 7.1 Two-bench throwaway instance on isolated state (`CWCLI_HOME`, a probed-free port base - `cwe2e-` prefix, `tests/e2e/harness.py` contract). The audit's own setup is the reference recipe.
- [ ] 7.2 Reproduce F3 against the FIXED build: stop, `cwcli start <p> --bench 1`, confirm `cwcli status --bench 1` and `cwcli axi status --bench 1` both report `running` and that the start emits no false `web_not_ready` warning (F5).
- [ ] 7.3 Reproduce F4 against the FIXED build: stop bench 1's web alone, confirm its `web_http_code` is not bench 0's live code.
- [ ] 7.4 Bare `cwcli axi status <p>` reports both benches in one document, each named, exit 0.
- [ ] 7.5 Both modes on a TTY and a non-TTY: no prompt appears on either, and `--bench` drives the single-bench answer non-interactively.
- [ ] 7.6 Teardown by exact name; no broad prune; no protected instance touched.

## 8. Docs and memory

- [ ] 8.1 `README.md`: the `status` section (:1589-1664), the `running` definition (:1617, which currently says "the web server answers on `:8000`"), and the axi surface paragraph (:2052, which currently documents the multi-bench refusal).
- [ ] 8.2 `CLAUDE.md`: the supervisord entry's `status` contract, noting the per-bench report and the explicit-port probe.
- [ ] 8.3 `.claude/skills/cwcli-lifecycle/`: the status/start incident detail, keeping the root-cause "why" (the probe was bench-blind, and the fix is an explicit non-defaulted port so it cannot be re-inherited).
- [ ] 8.4 Confirm `skills/cwcli/SKILL.md` regenerates unchanged in its verb table (no flag added or removed) and that `tests/test_axi_skill.py --check` stays green.
