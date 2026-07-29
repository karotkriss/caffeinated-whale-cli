This is a multi-PR rebuild.
For behavioral E2E coverage claims, a `[x]` marks a checklist item only when every clause it states is independently backed by real E2E evidence.
For source, harness, CI, unit-test, documentation, and historical-validation items, a `[x]` means the clause has its appropriate direct implementation or validation evidence.
A historical behavioral requirement that bundled several clauses, where some are proven and some are not, is split into a parent line plus per-clause sub-items, so an unproven clause is never presented as complete by riding a checked parent.

The per-command items in §4 track E2E coverage, not mock-suite retirement.
The retirement contract and its sequencing are owned by the [E2E test-suite specification](specs/e2e-test-suite/spec.md#requirement-parallel-run-transition-off-the-mock-suite); this checklist reconciliation does not retire any mock suite.
As of this reconciliation, none of the container-mock suites `proposal.md`'s Impact section names for retirement (`tests/test_rm_safety.py`, `tests/test_restore_safety.py`, `tests/test_restore_inspect_fixes.py`, `tests/test_inspect_partial_refresh.py`, `tests/test_apps.py`, `tests/test_yes_flag.py`, plus the shared `tests/bench_fakes.py`/`tests/bench_fakes_mb.py` fakes) have been retired, even though `backup`, `init`, `restore`, `update`/`apps update`, and `unlock` each have real E2E landed below - retirement has not started.

For the current, authoritative per-file test coverage, read [`tests/README.md`](../../../tests/README.md) and the [`tests/e2e/`](../../../tests/e2e/) tree directly rather than this file: restating which test file or test function proves which clause here would be a second, decaying copy of that ledger. This file instead records what was decided, why, and - per clause - whether real E2E proof exists yet.

## 1. CWCLI_HOME source hardening (decision #9 - lands first, its own PR)

- [x] 1.1 Add a single shared base-directory helper (e.g. `cwcli_home()` in `utils/config_utils.py`) that returns `Path(os.environ["CWCLI_HOME"])` when `CWCLI_HOME` is set and non-empty, else `Path.home() / ".cwcli"`.
- [x] 1.2 Route `CONFIG_DIR`, `PROJECTS_DIR` (`config_utils.py`) and `CACHE_DIR` / `DB_PATH` (`db_utils.py`) through the helper so config, projects, and cache all relocate together; keep the existing `0700` dir / `0600` file permissions.
- [x] 1.3 Confirm `CWCLI_HOME` only moves cwcli's own paths and never mutates the process `HOME` (host-side git/ssh unaffected).
- [x] 1.4 Unit tests (mock-free): `CWCLI_HOME` set redirects all three locations; unset keeps `~/.cwcli`; relocated cache keeps secure permissions; single-helper means `config_utils` and `db_utils` agree.
- [x] 1.5 Document `CWCLI_HOME` in `README.md`; add a `CHANGELOG.md` entry.

## 2. Shared E2E harness (the reusable seam every command E2E builds on)

- [x] 2.1 Create the `tests/e2e/` package and repurpose the markers in `pyproject.toml`: `unit` / `e2e` / `e2e_p2p` (retire the dead `slow` / `integration`); set default `addopts` to `-m "not e2e and not e2e_p2p"` so a bare `pytest` stays fast.
- [x] 2.2 Add the E2E-only dependency group with `pexpect` (NOT `testcontainers`).
- [x] 2.3 Docker-availability gate: detect `docker info` / `client.ping()` and SKIP LOUDLY (obvious infra failure, never silent green) when no daemon is reachable.
- [x] 2.4 Hard isolation rails (fail-closed before any Docker work): refuse if `HOME` is (or nests under) the real home, if `CWCLI_HOME` is unset or does not resolve to a location inside that isolated `HOME`, or if any project name lacks the `cwe2e-` prefix.
- [x] 2.5 Isolation fixtures: per-session temp `HOME` + `CWCLI_HOME` override + unique `cwe2e-<runid>-<n>` project/site names.
- [x] 2.6 Port allocator: non-overlapping bases ≥1006 apart (web `{port}..{port+5}`, socketio `{port+1000}..{port+1005}`).
- [x] 2.7 Session/instance fixture: stand up a real instance via `cwcli init` (full `bench init` + `new-site`), wait on REAL readiness (never fixed sleeps), yield, tear down via `cwcli rm --yes --volumes`.
- [x] 2.8 pexpect helper for interactive flows: await the prompt_toolkit `ESC[?2004h` raw-mode marker before each keystroke; a non-interactive helper drives flags.
- [x] 2.9 Unconditional teardown backstop: sweep every `com.docker.compose.project` matching `cwe2e-` (`compose down -v` + volume prune by label + drop temp `HOME`), plus between-phase Docker prune for the 14 GB disk ceiling.

## 3. CI: host-runner E2E matrix + two-tier gating

- [x] 3.1 `.github/workflows/test.yml`: scope the existing `Pytest` job to the fast unit tier (`-m unit`), still inside the uv container, on every push/PR (always-required gate).
- [x] 3.2 New `.github/workflows/e2e.yml`: `strategy.matrix.frappe: [14, 15, 16]` of `ubuntu-latest` jobs with NO `container:`; install uv via `astral-sh/setup-uv`.
- [x] 3.3 Authenticated Docker Hub login step (dodge anonymous pull rate limits); a distinct "upstream pull/compose fetch failed" annotation separable from an assertion failure.
- [x] 3.4 Per-job `timeout-minutes` (start ~45, provisional); the `cwe2e-` teardown backstop in an `always()` step.
- [x] 3.5 Run version-agnostic E2E only on the v16 leg and version-sensitive E2E on all three legs.
- [x] 3.6 Gating triggers: required on PRs into protected branches, plus on-demand via a PR label. Note in the workflow/PR that branch protection must be enabled on `develop`/`master` for "required" to bite.

## 4. Per-command E2E + mock retirement (parallel-run cadence)

A parent item below is checked only when every clause it once bundled has real E2E proof; where a clause is unproven or deliberately deferred to the unit tier, it is broken out as its own sub-item so the parent cannot read as fully done.

- [ ] 4.1 **init**
  - [x] 4.1a The non-interactive path creates a real bench and site.
  - [x] 4.1b The non-interactive fixture exercises `--auto-start` and `--admin-password`.
  - [x] 4.1c The interactive PTY path reaches the success output, and omitting `--admin-password` generates and prints the administrator password exactly once.
  - [ ] 4.1d Interactive bench and site creation are not independently read back from the real instance.
  - [ ] 4.1e An explicit `--db-root-password` value is not E2E-proven.
  - `init` has no pure mock-behavior suite to retire beyond resolver logic, which stays in unit.
- [ ] 4.2 **backup**
  - [x] 4.2a A real, non-empty DB dump lands on the host, in both interactive and non-interactive modes.
  - [ ] 4.2b Multi-bench `--bench` backup against a real instance - NOT E2E; only unit-tier (mocked container) coverage exists. `backup` has no dedicated mock suite to retire (net-new coverage).
- [ ] 4.3 **rm**
  - [x] 4.3a `cwcli axi rm --yes` produces a verified, non-empty database archive that genuinely restores into a fresh instance.
  - [x] 4.3b `cwcli axi rm --yes` removes the named volumes and isolated `$CWCLI_HOME/projects/<name>`, along with the containers and project network.
  - [ ] 4.3c Removal of the default `~/.cwcli/projects/<name>` path is not directly E2E-proven because the isolation rails require `CWCLI_HOME`.
  - [ ] 4.3d The human `cwcli rm` interactive path is not E2E-proven.
  - [ ] 4.3e The human `cwcli rm` non-interactive refusal without `--yes` is not E2E-proven.
  - [ ] 4.3f Early abort before container removal when backup fails is not E2E-proven.
  - [ ] 4.3g The container-mock behavior tests in `tests/test_rm_safety.py` and `tests/bench_fakes_mb.py` have not been retired.
  - Keep `test_rm_truth.py`'s pure-logic name and path validators in unit.
- [ ] 4.4 **restore**
  - [x] 4.4a Genuine data replacement through `--latest`, and through the interactive backup-selection menu, each followed by a site that becomes ready.
  - [ ] 4.4b Explicit `--backup-file <name>` selection (as opposed to `--latest` or the interactive menu) - NOT E2E; unit-tier only.
  - [ ] 4.4c Origin-mismatch handling - NOT E2E; unit-tier only.
  - The v14-only `--receive` bare-filename path is covered separately by §5.3.
- [ ] 4.5 **update / apps update**
  - [x] 4.5a Both modes drive the real binary against a real instance for output-purity, exit-code, missing-app-refusal, and auto-start-prompt behavior, plus the deprecated `cwcli update` warning/delegation contract (`bench update --reset` itself is shimmed on the deterministic-output legs, matching `test_run_e2e.py`'s precedent, so what's proven there is cwcli's output routing and exit code, not bench's own reset).
  - [ ] 4.5b Real pull + migrate of an app against a live instance - NOT E2E; unit-tier only.
  - [ ] 4.5c Maintenance mode genuinely toggled on then off around the update - NOT E2E; unit-tier only.
  - [ ] 4.5d `--site` narrowing - NOT E2E; unit-tier only.
- [ ] 4.6 **unlock**
  - [x] 4.6a Real site locks are created and their removal is proven through the non-interactive, interactive, auto-start, and `axi` paths.
  - [ ] 4.6b Multi-bench `--bench` resolution against a real instance - NOT E2E; unit-tier only.
- [ ] 4.7 **inspect** - E2E: assert the 3-tier freshness behavior against a real bench (T1 serve, T2 read-only drift detect, T3 re-cache on real drift); a freshly installed app becomes visible without `--update`. THEN retire the tier-assertion mock tests in `tests/test_inspect_partial_refresh.py` (keep any pure-logic in unit).
- [ ] 4.8 **apps (list/install/uninstall)**
  - [x] 4.8a The scoped `cwcli axi apps install` verb genuinely fetches and installs an absent app onto the named real site.
  - [x] 4.8b The scoped agent verb refuses an already-installed app by plain name and git URL, fails closed on an unreadable site, and requires `--site`.
  - [ ] 4.8c The human `cwcli apps install` every-site fan-out is not E2E-proven.
  - [ ] 4.8d The human install verb's non-interactive `--yes` path is E2E-proven while establishing the uninstall fixture; its interactive confirmation path is not.
  - [x] 4.8e The human `cwcli apps list --site <site> --json` and agent `cwcli axi apps list --site <site> --installed` paths both report a genuinely installed app from a real instance.
  - [x] 4.8f The dedicated human `cwcli apps uninstall --site <site> --yes` E2E establishes the app through both `apps list --site` and `frappe.get_installed_apps`, removes it, proves absence through both reads, confirms the site still serves HTTP 200, and observes the `restart-processes` report.
  - [ ] 4.8g The interactive human uninstall confirmation path is not E2E-proven.
  - [ ] 4.8h Multi-site fan-out and honest aggregated exit codes across list, install, and uninstall are not E2E-proven.
  - [ ] 4.8i Broader `--json` purity and post-mutation cache refresh in `where` and `inspect` are not E2E-proven.
  - [ ] 4.8j The remaining `tests/test_apps.py` container-mock tests have not been retired.
- [ ] 4.9 **yes-flag / auto-start contract**
  - [x] 4.9a `apps update` refuses non-interactively on a stopped instance without `--yes`.
  - [ ] 4.9b `init` non-TTY refusal without its required `--admin-password` is not E2E-proven.
  - [ ] 4.9c `backup` non-TTY refusal on a stopped instance without `--yes` is not E2E-proven.
  - [ ] 4.9d `restore` non-TTY refusal at the destructive confirmation without `--yes` is not E2E-proven.
  - [ ] 4.9e `unlock` non-TTY refusal on a stopped instance without `--yes` is not E2E-proven.
  - [ ] 4.9f `axi rm` refusal without its required `--yes` is not E2E-proven.
  - The cross-cutting retirement of `tests/test_yes_flag.py` has not happened.
  - Keep any pure logic in `tests/test_yes_flag.py` in unit.

## 5. Version-sensitive matrix coverage (runs on all three legs)

- [x] 5.1 MariaDB flag: assert `new-site` uses `--no-mariadb-socket` on v14 and `--mariadb-user-host-login-scope=%` on v15/v16, and that the site is actually created (the flag divergence is real - the 15+ flag breaks bench 14).
- [x] 5.2 pyenv/nvm branches: assert v14 provisions python3.10 + node16 + yarn, v15 provisions python3.12, v16 uses image defaults (no pyenv/nvm step).
- [x] 5.3 `--receive` bare-filename: assert the v14 leg reproduces-then-passes the full-container-path fix (v15/v16 mask it via alternative-directory fallback).
- [ ] 5.4 Apps behaviors on each version leg
  - [x] 5.4a The scoped `axi apps install` slice in §4.8 runs on the v14, v15, and v16 matrix legs.
  - [ ] 5.4b The full `apps list`/`install`/`uninstall` behaviors in §4.8 are not E2E-proven on every version leg.

## 6. P2P (gated, single loopback)

- [x] 6.1 Ensure `sendme` is installed for the P2P tests, or fail clearly when installation fails.
- [x] 6.2 Single-runner `e2e_p2p` loopback: `restore --send` produces a ticket, `restore --receive` consumes it, the transferred data replaces the live data, and the site subsequently becomes ready.
  The generic transport proof runs on the v16 matrix leg, while the separate bare-filename regression in §5.3 runs on the v14 matrix leg.

## 7. Docs

- [x] 7.1 Rewrite `tests/README.md` and `docs/testing/README.md` for the two-tier model (fast unit vs real-Docker E2E), the marker split, and the isolation/backstop contract.
- [x] 7.2 Add an E2E-harness note to `AGENTS.md` (how the automated harness relates to the manual `docs/e2e/` recipe it was built from; the `cwe2e-` prefix + `CWCLI_HOME` + safety rails).
- [x] 7.3 `CHANGELOG.md` entries for the test-suite rebuild (user-facing surface only) and the `CWCLI_HOME` feature.

## 8. Validation (definition of done for the implementation phase)

- [x] 8.1 Fast unit tier stays green with a broken Docker endpoint (proves it is still mock-free).
- [x] 8.2 The full E2E matrix goes green on all three legs (v14/v15/v16) at least once; the P2P leg goes green. (A one-time historical validation event from when the harness landed, not an ongoing coverage claim about the current test tree.)
- [x] 8.3 Deliberately leak a container mid-test and confirm the `cwe2e-` backstop sweeps it; deliberately point `HOME` at the real home and confirm the rail refuses.
- [ ] 8.4 Confirm each destructive command's E2E asserts the REAL side effect (not a string match) and that both modes are exercised.
  - [x] 8.4a `backup` - real dump on the host, both modes.
  - [x] 8.4b `restore` - genuine data replacement followed by site readiness, both modes.
  - [x] 8.4c `unlock` - real lock removal, both modes.
  - [ ] 8.4d `update` - both modes are exercised, but the real side effect (pull/migrate/maintenance toggle) is not E2E-asserted; see 4.5b/4.5c.
  - [ ] 8.4e `rm` - the real side effect is E2E-proven, but only one mode (`cwcli axi rm --yes`) is exercised; see 4.3.
- [ ] 8.5 Ship the tests + the `CWCLI_HOME` change + the `openspec/` specs through /no-mistakes per the gate rules (respond to gates, escalate ask-user findings, no `--yes`).
