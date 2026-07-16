## 1. Characterization tests FIRST (green before anything moves)

- [x] 1.1 Baseline re-measured first-hand at `30cf57c` (current `develop`; PR #86 has since merged, so the `3ff07ca` this proposal was drafted against is stale - the numbers are unchanged by it):
  `commands/apps.py` **195 stmts, 17 miss, 91.28%**; `tests/test_apps.py` **49 tests, green**.
  Missing lines: `65, 99-100, 116, 128, 133, 147, 176-177, 189, 216, 278, 342-343, 353, 430-431`.
  Adjacent, measured in the same run because Decision 8 turns on it: `core/update.py` **268 stmts, 87.69%**, with `296-322` (`_sites_with_app`'s live fallback) **entirely uncovered**.
  Command: `uv run pytest tests/test_apps.py --cov=caffeinated_whale_cli.commands.apps --cov-report=term`.
- [ ] 1.2 Audit `tests/test_apps.py` for which of the three verbs' branches are actually covered, per verb, and write down the gaps BEFORE filling them. The claim "apps is well covered" is exactly the kind of inherited belief batch 4's tasks.md warns about.
- [ ] 1.3 Cover `install`'s name derivation on BOTH paths: the `apps/` before/after diff yielding exactly one new dir (`apps.py:346-347`), and every fallback into `_derive_app_name` (zero new dirs / more than one new dir). This is the batch's risk concentration (design Risks).
- [ ] 1.4 Cover `_derive_app_name`'s four input shapes (`apps.py:167-178`): plain name, git URL, `.git` suffix, `user@host:path`.
- [ ] 1.5 Cover `list`'s partial-failure exit: a site whose `list-apps` fails is `installed[site] = None` and exits 1, and in `--json` mode the document is emitted BEFORE the non-zero exit (`apps.py:265-272`).
- [ ] 1.6 Cover `uninstall`'s destructive gate in both modes: `--json` without `--yes` refuses (exit 1, no prompt), and the human path routes through `confirm_or_exit` (`apps.py:435-449`).
- [ ] 1.7 Cover the `install` banner honesty branch: "App(s) fetched." vs "App(s) installed." depends on an `install-app` step actually having run (`apps.py:381-388`), so `--fetch-only` and a bench with no sites both say "fetched".
- [ ] 1.8 These tests MUST pass **before** the migration and **after** it, **unchanged**. Per design Decision 6, no test edit is currently expected by design; if one becomes necessary, state which changed BY DESIGN and which merely moved with its subject.
- [ ] 1.9 Note for whoever runs the suite: `/tmp/pytest-of-cmckay` is owned by `root` on this host, so `tmp_path` tests error in setup unless `TMPDIR` is redirected. Environmental, not a real failure.

## 2. `core/apps.py` - `list_apps` first (the pure read)

- [ ] 2.1 `list_apps(project, *, bench=None, bench_path=None, sites=None, installed=False) -> Result[AppsListing]`. Resolve container + bench ONLY (design Decision 7 - no default-site, no site-name validation, no bench-dir probe).
  The draft signature omitted `sites`/`installed`; both are load-bearing, because the per-site read only happens `if installed or sites` (`apps.py:258`) and that same condition decides whether the human JSON carries an `installed` key at all (`apps.py:267-268`). The core returns `AppsListing.installed` (empty dict when not requested); the FRONTEND decides whether to emit the key, so the historical JSON shape is preserved without the core knowing about JSON.
- [ ] 2.2 Preserve `_resolve_bench`'s `or _DEFAULT_BENCH` fallback (`apps.py:47-54`) - it is behaviour that matches `run`, not an accident.
- [ ] 2.3 Move `_list_available_apps` (`apps.py:125-135`) into the core. Keep the `workdir=bench_path` form rather than interpolating the path into the command - that is a deliberate quoting-hazard guard.
- [ ] 2.4 Move `_list_installed_apps` (`apps.py:138-151`) into the core; keep `shlex.quote(site)` and the first-token-per-line parse.
- [ ] 2.5 `AppsListing.ok` is False iff ANY site read failed. The frontend's exit code reads `.ok`, NOT `result.status` (design Decision 5).
- [ ] 2.6 The core writes to stdout NEVER. `verbose` diagnostics become warnings or events; the frontend renders them to stderr.

## 3. `core/apps.py` - `install_apps` / `uninstall_apps` (the fan-outs)

- [ ] 3.1 Plain functions with an optional `on_event` callback (design Decision 2). NOT generators. Do not cite batch 4's maintenance-mode GC hazard as the reason - it does not apply here; shape-consistency with `core.update` does.
- [ ] 3.2 Move `_resolve_target_sites` (`apps.py:154-164`), `_derive_app_name` (`:167-178`), and the get-app/install-app fan-out (`:331-373`) into the core.
- [ ] 3.3 `uninstall_apps` returns `NEEDS_CHOICE`/`confirm_uninstall` for the destructive gate, taking `auto_start` and destructive-consent as SEPARATE params (design Decision 4). The core never prompts.
- [ ] 3.4 `AppsReport.ok` is False iff ANY result failed. Preserve `_report_and_exit`'s aggregation exactly (`apps.py:197`).
- [ ] 3.5 Do NOT call `cache.recache_project` from the core (design Decision 3). It stays a frontend epilogue gated on `any(r.ok)`.
- [ ] 3.6 Preserve the empty-site-set behaviours: `install` notes "app(s) fetched only" and continues; `uninstall` notes and exits **0** (`apps.py:352-355`, `:429-431`).

## 4. Re-seat `commands/apps.py` as a renderer

- [ ] 4.1 Every flag, message, and exit code preserved byte-for-byte. `--json` stdout purity holds: the document is the only thing on stdout, everything else is stderr.
- [ ] 4.2 Delete `_report_and_exit`, `_run_bench`, `_capture_bench`, `_stream_bench` once nothing calls them. Do not leave them behind "for later" - a replaced helper left lying around is what a later reader mistakes for live.
- [ ] 4.3 Keep the `ensure_containers_running(auto_start=yes)` prologue - it performs the real start the core only reports as `start_requested` (design Decision 4).
- [ ] 4.4 The recache epilogue and its failure warning live here (design Decision 3).
- [ ] 4.5 `apps update` (the batch-4 shim, `apps.py:476-534`) is untouched.

## 5. `cwcli axi apps list`

- [ ] 5.1 ~20-25 lines over `core.list_apps`: one TOON document, exit 0/1/2, no business logic.
- [ ] 5.2 Multi-bench with no `--bench` renders through `emit_axi_choice_as_usage_error` (exit 2), naming `cwcli axi benches` as the discovery verb.
- [ ] 5.3 A stopped container is `NEEDS_CHOICE`/`confirm_start` rendered as a usage error naming `cwcli start` - NOT an auto-start, and NO `--yes` on the verb (matches `axi backup`/`axi unlock`/`axi apps update`).
- [ ] 5.4 Exit 1 iff `AppsListing.ok` is False (a site read failed), NOT off `result.status`.
- [ ] 5.5 TOON only. No `--json` on any axi verb.

## 6. Docs + memory

- [ ] 6.1 `README.md` gains `cwcli axi apps list`.
- [ ] 6.2 `AGENTS.md`: update the un-migrated list (remove `apps`'s three subcommands), and record Decision 3's result - the core-to-CLI reach is STILL one place, and why an epilogue hoists where a mid-fan-out call cannot.
- [ ] 6.3 `AGENTS.md` + the `cwcli-core-axi` skill record the Decision 1 deferral and point at §7 below, so "deliberately not built" is never mistaken for "forgotten".
- [ ] 6.4 Report whether any primitive needed bending (the batch-1 falsifiable claim). A batch that quietly widens something and reports "zero primitives touched" destroys the signal the standard exists to produce.

## 7. DEFERRED - explicitly NOT this batch (captain-locked 2026-07-15)

These are recorded as tasks, not prose, so the next reader can tell "deliberately not built" from "forgotten".
They are NOT blockers for this change and must NOT be checked off by it.

- [ ] 7.1 **`cwcli axi apps uninstall`** - deferred. Needs its own decision, on its own evidence: it would let an agent destroy site data (`bench uninstall-app` drops the app's tables). Design Decision 1. Prerequisite already satisfied by this batch: `core.uninstall_apps` exposes destructive-consent separately from `auto_start` (Decision 4), so the verb wires the one it means rather than re-opening a fused flag.
- [ ] 7.2 **`cwcli axi apps install`** - deferred with 7.1. It mutates a real site but does not delete data, so it may well be decided differently; it was held only because there was no reason to settle half the question inside a refactor.
- [ ] 7.3 When 7.1/7.2 are taken up, the open question from design ("does `axi apps list` need `--fields`") should be revisited alongside them, since a mutation verb's report is larger than `AppsListing`.

## 8. REPORTED, not fixed - `core/update.py`'s dead cache branch (found while judging Decision 8)

Surfaced by this batch's assigned duplication question, verified first-hand, and deliberately left alone. Recorded as a task so it is not mistaken for forgotten. NOT a blocker for this change and must NOT be checked off by it.

- [ ] 8.1 **`core/update.py:291`'s cache branch never hits on a real bench.** It tests `if app in site.get("installed_apps", [])` (exact list membership), but `get_cached_project_data` returns the raw `bench list-apps` LINES (`db_utils.py:443`), which on v16 are `frappe 16.26.3` (`docs/e2e/init-admin-password-secrets-s5.md:59`). `"frappe" in ["frappe 16.26.3"]` is False, so `_sites_with_app` always falls through to its live query.
  **Fail-safe** (the live path returns the correct site set); the cost is a dead optimization plus a live fan-out on every `update`.
  Fixing it is a BEHAVIOUR change - it would start serving cached site sets as the input to a migration fan-out, which is a real staleness risk and wants its own evidence. Candidate fix if taken up: compare on the first token, or read the already-parsed `InstalledAppDetail.name` the cache writer produces (`db_utils.py:404-417`) instead of re-parsing `Site.installed_apps`.
- [ ] 8.2 **The suite exercises the opposite branch from production.** `tests/test_apps.py:658` seeds the cache with bare names, so tests take the cache branch and never reach the live fallback - which measures 0% covered (`core/update.py:296-322`, task 1.1). Whoever takes 8.1 should cover the live fallback FIRST; it is the only branch production actually runs.
