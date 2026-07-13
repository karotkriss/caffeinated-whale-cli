## 1. start E2E (both modes, real side effects)

- [x] 1.1 Add `tests/e2e/test_start_status_e2e.py` marked `pytest.mark.e2e`, importing the existing `tests/e2e/harness.py` and using the `session_instance` / `running_instance` fixtures; no `src/` or CI change.
- [x] 1.2 Non-interactive: `cwcli start <project> --yes` on a stopped instance -> exit 0, containers + bench up, site reachable (real outcome, not a string match). NOTE: asserted via a direct web-port probe (`_web_reachable`), not `harness.wait_for_site_ready`, because the latter only proves DB readiness (`bench list-apps`) while "genuinely serves" means honcho is answering :8000; the from-stopped path is what actually runs `bench start` (`cwcli init` never does).
- [x] 1.3 Re-run invariant: with the instance already serving, run `cwcli start` again -> site still reachable afterward; assert ONLY health. Exit code deliberately NOT pinned: today a re-run against a container-up instance self-conflicts on its own host ports (exit non-zero, no second bench) - that is exactly the idempotency mechanic PR 2 fixes, so it is migration-transient.
- [x] 1.4 Honest failure: `cwcli start <nonexistent>` -> non-zero exit, never reports "started".
- [x] 1.5 Interactive: drive `cwcli start` through `harness.spawn_cwcli` + `pexpect` into its port-conflict confirmation prompt, awaiting the `ESC[?2004h` marker (`expect_prompt_ready`), answer it, and confirm the run completes - proving the prompt is genuinely shown and answered. Uses the live session instance as the port HOLDER + a `docker create`d stopped stub on the same host port; answers NO so the shared holder is preserved (isolation), exercising the prompt via the deterministic decline path.

## 2. status E2E (lifecycle-state discrimination)

- [x] 2.1 `offline` when the project's containers are absent or stopped -> stdout `offline`, exit 0. (Absent: a never-created project. Stopped: asserted inside 1.2 after `cwcli stop`.)
- [x] 2.2 Running state when the site answers -> reports the running state, exit 0.
- [x] 2.3 Not-started state distinct from running: containers up but `bench start` not run -> assert the state by its INVARIANT (containers up, site not answering), NOT by string-matching the transitional `online` token PR 2 may enrich.

## 3. logs + restart E2E

- [x] 3.1 `cwcli logs <project> --no-follow` on a started instance -> shows the captured bench output, exit 0; assert by content/exit, NOT by the `/tmp/bench-<project>.log` path (PR 2 relocates it). Kept in `tests/e2e/test_start_status_e2e.py` (one module reads cleaner than a split); driven through a pty because `logs` uses `docker exec -it`.
- [x] 3.2 `cwcli restart <project>` on a running instance -> exit 0, site reachable again afterward (via the real web probe).
- [x] 3.3 `cwcli restart <nonexistent>` -> non-zero exit.

## 4. Multi-bench + isolation discipline

- [x] 4.1 No multi-bench fixture exists in the harness (standing one up is an extra expensive `cwcli init`, out of scope); the module documents that multi-bench start/status behavior is covered by PR 2's new-behavior tests, not here, and that any multi-bench case would pass an explicit `--bench <index|label>` (never the ambiguous default PR 2 changes).
- [x] 4.2 Lifecycle-mutating cases (stop->start, restart, honcho-kill) restore the running state via `_ensure_serving` / the `running_instance` fixture so they don't disturb sibling tests sharing `session_instance` (mirrors the existing `running_instance` guarantee); the module also runs last alphabetically, after `init`/`backup`.

## 5. Validation + docs

- [x] 5.1 Every assertion is engineered to hold on TODAY's (pre-migration) `cwcli` (see the module docstring's per-behavior notes); the module is collected-but-deselected under the fast `-m unit` tier (verified locally: `10 deselected / 0 selected`). The real-instance green across the v14/v15/v16 legs is produced by the `e2e.yml` matrix (Docker is unavailable in the authoring env), i.e. the ship gate.
- [x] 5.2 Added a `tests/README.md` note that lifecycle-command (`start`/`status`/`logs`/`restart`) E2E now exists (test-file list, Done/Partial coverage entries); no `CHANGELOG.md` entry (test-only, not user-facing).

## 6. Deferred - NOT in this change (boundary made explicit)

- [ ] 6.1 DEFERRED to `migrate-start-status-core` (PR 2): all `src/` changes - `core.start`/`core.status`, the reseated `cwcli start`/`cwcli status`, the `cwcli axi start`/`status` verbs, the log relocation + bound, `cwcli logs` path update, the idempotency fix.
- [ ] 6.2 DEFERRED to PR 2: asserting the NEW behavior - per-process health fields, the `degraded` aggregate, the idempotent single-supervisor no-op, the relocated bounded log, the multi-bench prompt/error. These land as PR 2's own new-behavior tests on top of this preserved net.
