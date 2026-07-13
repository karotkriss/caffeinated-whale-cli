## 1. start E2E (both modes, real side effects)

- [ ] 1.1 Add `tests/e2e/test_start_status_e2e.py` marked `pytest.mark.e2e`, importing the existing `tests/e2e/harness.py` and using the `session_instance` / `running_instance` fixtures; no `src/` or CI change.
- [ ] 1.2 Non-interactive: `cwcli start <project> --yes` on a stopped instance -> exit 0, containers + bench up, site reachable via `harness.wait_for_site_ready` (real outcome, not a string match).
- [ ] 1.3 Re-run invariant: run `cwcli start` twice -> exit 0, site still reachable after the second run; assert ONLY health (no in-container process-count assertion - that mechanic is PR 2's).
- [ ] 1.4 Honest failure: `cwcli start <nonexistent>` -> non-zero exit, never reports "started".
- [ ] 1.5 Interactive: drive `cwcli start` through `harness.spawn_cwcli` + `pexpect` into its port-conflict confirmation prompt, awaiting the `ESC[?2004h` marker (`expect_prompt_ready`), answer it, and confirm the run completes - proving the prompt is genuinely shown and answered.

## 2. status E2E (lifecycle-state discrimination)

- [ ] 2.1 `offline` when the project's containers are absent or stopped -> stdout `offline`, exit 0.
- [ ] 2.2 Running state when the site answers -> reports the running state, exit 0.
- [ ] 2.3 Not-started state distinct from running: containers up but `bench start` not run -> assert the state by its INVARIANT (containers up, site not answering), NOT by string-matching the transitional `online` token PR 2 may enrich.

## 3. logs + restart E2E

- [ ] 3.1 `cwcli logs <project> --no-follow` on a started instance -> shows the captured bench output, exit 0; assert by content/exit, NOT by the `/tmp/bench-<project>.log` path (PR 2 relocates it). Split into `tests/e2e/test_logs_e2e.py` only if it reads cleaner.
- [ ] 3.2 `cwcli restart <project>` on a running instance -> exit 0, site reachable again afterward via `harness.wait_for_site_ready`.
- [ ] 3.3 `cwcli restart <nonexistent>` -> non-zero exit.

## 4. Multi-bench + isolation discipline

- [ ] 4.1 Any multi-bench case passes an explicit `--bench <index|label>` (never the ambiguous default, which PR 2 changes); if no multi-bench fixture exists, note that multi-bench start/status behavior is covered by PR 2's new-behavior tests, not here.
- [ ] 4.2 Lifecycle-mutating cases (stop->start, restart) use a throwaway instance or restore the running state so they don't disturb sibling tests sharing `session_instance` (mirror the existing `running_instance` guarantee).

## 5. Validation + docs

- [ ] 5.1 Confirm the new module is green on TODAY's (pre-migration) `cwcli` on a real instance across at least one version leg, and is collected-but-deselected under the fast `-m unit` tier.
- [ ] 5.2 Add a short `tests/README.md` / `docs/testing/README.md` note that lifecycle-command (`start`/`status`/`logs`/`restart`) E2E now exists; `CHANGELOG.md` entry only if user-facing (test-only change may warrant none).

## 6. Deferred - NOT in this change (boundary made explicit)

- [ ] 6.1 DEFERRED to `migrate-start-status-core` (PR 2): all `src/` changes - `core.start`/`core.status`, the reseated `cwcli start`/`cwcli status`, the `cwcli axi start`/`status` verbs, the log relocation + bound, `cwcli logs` path update, the idempotency fix.
- [ ] 6.2 DEFERRED to PR 2: asserting the NEW behavior - per-process health fields, the `degraded` aggregate, the idempotent single-supervisor no-op, the relocated bounded log, the multi-bench prompt/error. These land as PR 2's own new-behavior tests on top of this preserved net.
