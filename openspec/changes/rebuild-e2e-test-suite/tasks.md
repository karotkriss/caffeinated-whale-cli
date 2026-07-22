This is a multi-PR rebuild; checked boxes record shipped work or validation evidence, while unchecked boxes are deferred to follow-up PRs.
Items 4.3, 4.7, 4.8, 4.9, 5.4, and 8.5 remain deferred.
The per-command checkboxes in §4 track the E2E coverage, not mock-suite retirement.
The retirement contract and its sequencing are owned by the [E2E test-suite specification](specs/e2e-test-suite/spec.md#requirement-parallel-run-transition-off-the-mock-suite); this checklist reconciliation does not retire any mock suite.

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
- [x] 3.5 Run version-agnostic E2E only on the v16 leg; version-sensitive E2E on all three legs.
- [x] 3.6 Gating triggers: required on PRs into protected branches, plus on-demand via a PR label. Note in the workflow/PR that branch protection must be enabled on `develop`/`master` for "required" to bite.

## 4. Per-command E2E + mock retirement (parallel-run cadence)

Each checked subgroup has shipped real-Docker coverage for the command.
The specification linked above owns the separate mock-retirement follow-up; pure-logic tests are kept.

- [x] 4.1 **init** - E2E: real `cwcli init` builds a genuine bench + site (already the harness fixture); assert both interactive and non-interactive (`--yes`, `--admin-password`, `--mariadb-root-password`) paths and the generated-admin-password print-once behavior. (init has no pure "mock behavior" suite to retire beyond resolver logic, which stays in unit.)
- [x] 4.2 **backup** - E2E: assert a real, non-empty DB dump lands on the host; both modes; multi-bench `--bench`. Retire the mock backup assertions once green (`backup` has no dedicated mock suite today - this is net-new real coverage).
- [ ] 4.3 **rm** - E2E: assert named volumes + `~/.cwcli/projects/<name>` are gone AND a verified non-empty DB dump existed first (C1 gate); the early-abort-before-container-removal on a failed backup; both modes. THEN retire the container-mock behavior tests in `tests/test_rm_safety.py` / `tests/bench_fakes_mb.py` (keep `test_rm_truth.py`'s pure-logic name/path validators in unit).
- [x] 4.4 **restore** - `tests/e2e/test_restore_e2e.py` proves genuine data replacement through `--latest` and the interactive selector, then verifies the site boots after migrate + restart; both modes. The v14-only `--receive` bare-filename path is covered separately by §5.3. Selector and origin-mismatch branches remain covered in the unit tier.
- [x] 4.5 **update / apps update** - `tests/e2e/test_apps_update_e2e.py` drives the real binary against a real instance in both modes: deterministic `bench update --reset` output and exit-code paths use a bench shim, while the missing-app refusal and auto-start prompts use the genuine bench. It also pins structured-output purity and the deprecated `cwcli update` warning/delegation contract. Pull/migrate, maintenance-mode, and `--site` branching remain covered in the unit tier.
- [x] 4.6 **unlock** - `tests/e2e/test_unlock_e2e.py` creates real site locks and proves their removal through non-interactive, interactive, auto-start, and `axi` paths. Multi-bench `--bench` resolution remains covered in the unit tier.
- [ ] 4.7 **inspect** - E2E: assert the 3-tier freshness behavior against a real bench (T1 serve, T2 read-only drift detect, T3 re-cache on real drift); a freshly installed app becomes visible without `--update`. THEN retire the tier-assertion mock tests in `tests/test_inspect_partial_refresh.py` (keep any pure-logic in unit).
- [ ] 4.8 **apps (list/install/uninstall)** - E2E: real `bench get-app`/`install-app`/`uninstall-app`, multi-site fan-out with honest aggregated exit codes, `--json` purity, post-mutation cache refresh reflected in `where`/`inspect`; both modes. Retire the remaining `tests/test_apps.py` container-mock tests once green.
- [ ] 4.9 **yes-flag / auto-start contract** - fold the `ensure_containers_running` / `confirm_or_exit` non-TTY-refusal assertions into the per-command E2E (each command asserts non-TTY-without-flag refuses non-zero). Retire the container-mock parts of `tests/test_yes_flag.py`; keep any pure-logic there in unit.

## 5. Version-sensitive matrix coverage (runs on all three legs)

- [x] 5.1 MariaDB flag: assert `new-site` uses `--no-mariadb-socket` on v14 and `--mariadb-user-host-login-scope=%` on v15/v16, and that the site is actually created (the flag divergence is real - the 15+ flag breaks bench 14).
- [x] 5.2 pyenv/nvm branches: assert v14 provisions python3.10 + node16 + yarn, v15 provisions python3.12, v16 uses image defaults (no pyenv/nvm step).
- [x] 5.3 `--receive` bare-filename: assert the v14 leg reproduces-then-passes the full-container-path fix (v15/v16 mask it via alternative-directory fallback).
- [ ] 5.4 apps behaviors on each version leg (per §4.8), plus the v16-first-ever real E2E coverage (no v16 manual evidence exists yet).

## 6. P2P (gated, single loopback)

- [x] 6.1 Ensure `sendme` is installed on the P2P leg (or gate the test off cleanly when absent).
- [x] 6.2 Single `e2e_p2p` loopback: `restore --send` produces a ticket, `restore --receive` consumes it on the same runner, and the receive path's real side effect (data replaced + migrate + restart) is asserted. NOT on the per-version matrix legs.

## 7. Docs

- [x] 7.1 Rewrite `tests/README.md` and `docs/testing/README.md` for the two-tier model (fast unit vs real-Docker E2E), the marker split, and the isolation/backstop contract.
- [x] 7.2 Add an E2E-harness note to `AGENTS.md` (how the automated harness relates to the manual `docs/e2e/` recipe it was built from; the `cwe2e-` prefix + `CWCLI_HOME` + safety rails).
- [x] 7.3 `CHANGELOG.md` entries for the test-suite rebuild (user-facing surface only) and the `CWCLI_HOME` feature.

## 8. Validation (definition of done for the implementation phase)

- [x] 8.1 Fast unit tier stays green with a broken Docker endpoint (proves it is still mock-free).
- [x] 8.2 The full E2E matrix goes green on all three legs (v14/v15/v16) at least once; the P2P leg goes green.
- [x] 8.3 Deliberately leak a container mid-test and confirm the `cwe2e-` backstop sweeps it; deliberately point `HOME` at the real home and confirm the rail refuses.
- [x] 8.4 Confirm each destructive command's E2E asserts the REAL side effect (not a string match) and that both modes are exercised.
- [ ] 8.5 Ship the tests + the `CWCLI_HOME` change + the `openspec/` specs through /no-mistakes per the gate rules (respond to gates, escalate ask-user findings, no `--yes`).
