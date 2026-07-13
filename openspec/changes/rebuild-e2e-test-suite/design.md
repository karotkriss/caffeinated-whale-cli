## Context

The recon audit (`data/cwcli-e2e-recon-r4/report.md`) established the ground truth this design rests on; it is the authoritative input and is not re-litigated here.
Key facts it settled, with the consequences that shape the design:

- **The whole suite is mock-only.** 409 tests pass against a broken Docker endpoint in ~9s (§1). There is no "already integration-ish" layer to preserve - only three genuinely mock-free files test real string/model logic and survive untouched.
- **The real side-effect surface is small and thin.** cwcli touches Docker four ways (§2): the docker-py SDK (discovery by `com.docker.compose.project` label, `exec_into_container`), `docker compose` via subprocess (only in `init`), `container.exec_run(...)` for every bench operation, and the `sendme` binary for P2P. Everything Frappe is a shell string exec'd in the frappe container - that `exec_run` boundary is exactly where the mocks sit and where they go behavior-blind.
- **Isolation is a single seam: `HOME`.** All of `~/.cwcli` (config, projects, cache DB) derives from `Path.home()` (`db_utils.py:10`, `config_utils.py:5-8`). A temp `HOME` + unique `cwe2e-*` names + unique ports is the entire isolation story, and it is exactly what the manual `docs/e2e/` recipe already practices.
- **CI cannot run Docker today.** Every job runs inside `container: ghcr.io/astral-sh/uv:python3.12-bookworm`, which has no Docker daemon (§3). A Docker-backed job must run on the host `ubuntu-latest` (Docker + compose v2 preinstalled).
- **The dominant risk is runtime, not correctness.** A genuine `cwcli init` pulls a multi-GB `frappe/bench` image, git-clones Frappe + pip/npm build (`bench init`), then `bench new-site`. That is minutes-to-tens-of-minutes per instance; the matrix design lives or dies on how that cost is handled (§5, §6).
- **The code's own version-gating is the version-sensitive test set** (§4): `_select_mariadb_flag` (`≤14` vs `15+`), `branch_python`/`branch_node` (v14 → pyenv 3.10 + nvm 16 + yarn; v15 → pyenv 3.12; v16 → image defaults), and the v14-only `--receive` bare-filename bug that v15's alternative-directory fallback masks.

## Goals / Non-Goals

**Goals**
- Enforce the destructive-path guarantees (rm/restore/backup/update/unlock) by machine, on every PR, as **real side effects** - not shell-string matches against a fake container.
- Automate and CI-ify the existing manual `docs/e2e/` recipe (temp HOME, `cwe2e-*` names, genuine `cwcli init` benches, pexpect for TTY, flags for non-TTY, teardown) across the Frappe versions that actually diverge.
- Make isolation a guaranteed property (hard safety rails + a leaked-resource backstop), never a hope - the captain's real instances must never be touched.
- Keep a fast local/PR feedback loop (the mock-free unit tier) permanently.

**Non-Goals** (see the proposal's Non-Goals for the full list)
- `testcontainers`, a pre-built base image, self-hosted runners, upstream-ref pinning, Windows/macOS Docker E2E, and Frappe v13.

## Decisions

Each decision below was a real fork in the recon audit (§5 D1-D6, §7). The captain locked the answers; this section records **what** was chosen and **why the rejected alternative loses**, so a future reader does not silently reopen a settled fork.

### D1. Framework: pytest, two tiers, drive the real `cwcli` binary (recon D1)
**Chosen:** stay in pytest. A fast `unit` tier (mock-free pure logic) plus a real-Docker `e2e` tier whose session-scoped fixtures `cwcli init` a real instance, yield it, and `cwcli rm` on teardown. E2E drives the **console script** (`cwcli ...` via subprocess/pexpect), not in-process Typer functions.
**Why:** the manual recipe is already pytest-shaped; one runner keeps one CI reporting surface and lets the migration be gradual. Driving the real binary closes a genuine gap the mock suite skips - the unit suite calls Typer commands as plain functions with every param passed explicitly (an omitted param keeps its truthy `typer.Option(...)` default object and silently changes behavior), so it never exercises Typer parsing.
**Rejected:** `testcontainers-python` (recon D1-B) - cwcli *is* the thing that runs `docker compose`; wrapping that in testcontainers is double management that fights cwcli's own compose calls. A standalone bats/shell driver (D1-C) loses pytest reporting/parametrization for no gain over the manual scripts.

### D2. Isolation: temp HOME + explicit `CWCLI_HOME` + hard rails + label backstop (recon D2)
**Chosen:** per-session temp `HOME` **and** a new first-class `CWCLI_HOME` source override, plus unique `cwe2e-<runid>-<n>` names, a port allocator, and two independent safety layers.
- **Hard rail (fail-closed before any Docker work):** refuse to run if `HOME` is (or nests under) the real home directory, if `CWCLI_HOME` is unset or does not resolve to a location inside that isolated `HOME` (validating only that it is *set* is insufficient - a `CWCLI_HOME` at/under the real home would otherwise pass while cwcli wrote real state), or if any project name lacks the `cwe2e-` prefix.
- **Teardown backstop (unconditional):** an `always()`/session-teardown sweep of every `com.docker.compose.project` matching `cwe2e-` (`compose down -v` + `docker volume prune` by label + drop the temp `HOME`), so a crashed test cannot leak.

**Why `CWCLI_HOME` and not just temp `HOME`:** repointing the whole process `HOME` is a blunt instrument - it is the isolation seam only as a side effect of an env var that many host-side tools also read. An explicit `CWCLI_HOME` redirects exactly cwcli's own footprint and nothing else, which is both cleaner for the harness and a genuinely useful user feature (redirect cwcli's state without disturbing git/ssh/etc.). The harness sets both (temp `HOME` as the belt, `CWCLI_HOME` as the precise control) and the rail checks both.
**Rejected:** relying on `HOME` alone (recon D2-A) - works, but leaves isolation as an implicit side effect with no explicit, testable seam and larger blast radius.

### D3 / D4 (runner + amortization): GitHub-hosted, full `init` every run, no infra now (recon D3, §7 Q1/Q2)
**Chosen:** GitHub-hosted `ubuntu-latest`, drop the `container:`, install `uv` via `astral-sh/setup-uv`, run the full real `cwcli init` each run. No pre-built base image, no self-hosted runners in this change.
**Why:** zero infra to own or secure, and a full-`init`-every-run E2E is the honest end-to-end test - it is the only thing that actually covers `init` itself (image pull + compose customization + `bench init` build + `new-site`), which is where the version-gated pyenv/nvm branches live. Amortization is a real cost lever but premature until we have measured per-job runtimes.
**Rejected (deferred, not dismissed):** a pre-built "sited" base image per version and/or self-hosted runners (recon D3-B/C) - the highest-leverage speedups, but they add a build pipeline / infra to maintain and (self-hosted + fork PRs) a security footgun. Noted as the future optimizations to revisit once real runtimes exist.

### D5. Matrix: per-version parallel jobs; agnostic-once, sensitive-per-version (recon D5)
**Chosen:** one parallel job PER Frappe version (`strategy.matrix.frappe: [14, 15, 16]`), each on its own runner. Version-agnostic E2E runs **once** (on the v16 leg). The version-sensitive set runs on all three: the MariaDB flag, the pyenv/nvm branches, the v14-only `--receive` bug, and apps behaviors.
**Why:** per-version *jobs* give isolation (one leaky instance can't poison another leg), parallelism, per-version resource headroom, and clean per-version logs - directly answering the 4 vCPU / 16 GB / 14 GB hosted-runner ceiling (§6): three heavy Frappe stacks must not run concurrently on one runner. Running truly version-agnostic tests three times is pure waste, hence agnostic-once. The per-version set is not a guess - it is exactly the code's own version-gating plus the one documented v14-only bug.
**Rejected:** one parametrized job (recon D5-B) - serial and slow, and three stacks contending on one 16 GB runner if concurrent; one failure can cascade.

### D6. Destructive-path coverage (absorbs t6) + P2P scope (recon D6)
**Chosen:** each destructive command asserts its **real** side effect (see the `e2e-test-suite` spec's destructive requirement), both modes. P2P is a single gated loopback send+receive on one runner (`e2e_p2p`), not on every version leg.
**Why:** these paths are the primary justification for the rebuild - mock coverage there is line-high but behavior-blind, and `backup`/`unlock` have no suite at all. P2P needs two endpoints + the `sendme` binary + iroh networking (the flakiest surface); it is orthogonal to the restore-path logic, so one gated loopback run covers the receive path without multiplying flakiness across every leg.
**Rejected:** keeping P2P fully stubbed/out (loses real receive-path coverage) or running it on every version leg (multiplies the flakiest surface for no version-sensitivity gain).

### Upstream refs: track latest, make failures diagnosable, authenticate pulls (recon §6, §7 Q6)
**Chosen:** track latest (compose `@main` + latest `frappe/bench` tag), matching real user behavior so the E2E catches upstream breakage. Make upstream-caused failures a **distinct signal** (compose fetch / image pull / bench-tag lookup failing is annotated separately from an assertion failure) so a red gate is never ambiguous. Mitigate Docker Hub anonymous rate limits with authenticated pulls.
**Why:** pinning would trade away the single biggest reason to run real E2E - catching the day upstream frappe_docker/bench breaks a real `cwcli init`. The cost of tracking latest is non-determinism; the mitigation is diagnosability (tell a human "upstream moved" vs "our code regressed"), not pinning.
**Rejected:** pinning the compose ref + bench tag (recon §6) - reproducible but blind to upstream breakage, defeating a core purpose.

### CI gating: two tiers; E2E required on protected-branch PRs + on-demand label (recon §7 Q8)
**Chosen:** the fast `unit` job runs on every push/PR and is the always-required gate; the `e2e` matrix is required on PRs into protected branches and available on-demand via a PR label.
**Why:** the captain's "re-run E2E after every behavior change" standard implies E2E must gate merges into protected branches, but running the full matrix on every push to every feature branch is wasteful - the label gives an escape hatch to run it early on demand. **Caveat, called out for the captain:** `develop`/`master` have no branch protection today, so "required" does not actually block a merge until a repo admin enables branch protection and ticks these checks required.

### Runtime budget: explicit `timeout-minutes`, provisional (recon §7 Q9)
**Chosen:** every E2E job sets `timeout-minutes` (start ~45), tuned once real numbers exist.
**Why:** there is no timeout today, so a hung `bench init` burns toward GitHub's 6h cap. An explicit, generous-but-bounded ceiling fails a hang fast. ~45 min is a starting guess (a full `init` is the dominant cost and unmeasured in CI); it is provisional by design.

## Risks / Trade-offs

- **Runtime is the make-or-break.** Full `init` every run on a hosted runner may be slow; if per-job minutes become intolerable, the escape hatch is the deferred amortization (pre-built sited base image, then self-hosted). Mitigation now: the `timeout-minutes` ceiling and per-version parallel jobs.
- **Upstream non-determinism.** Tracking latest means an upstream break turns the gate red. Mitigation: the distinct upstream-failure signal so it's obviously "upstream moved", plus authenticated pulls for the rate-limit class of failure.
- **Docker Hub rate limits** (100 anon pulls / 6h / IP) on a multi-GB image. Mitigation: authenticated pulls; layer caching is a later lever.
- **P2P/iroh flakiness.** Mitigation: single gated loopback leg, isolated behind `e2e_p2p`, off the per-version matrix.
- **Disk ceiling** (14 GB SSD): images + volumes + node_modules can approach it. Mitigation: prune between phases within a leg.
- **Interactive-mode-in-CI races.** Mitigation: await the prompt_toolkit `ESC[?2004h` raw-mode marker before each pexpect keystroke (a documented cwcli hazard), and wait on **real readiness** (health / `bench` responds), never fixed sleeps.
- **Migration window.** During the parallel-run transition both suites exist; the mock tests can still drift. Mitigation: retire each command's mock behavior tests promptly as its E2E proves out; keep the pure-logic tests permanently.

## Open questions (for review, not blocking the proposal)

These are calibration values the captain locked as provisional; the implementation phase confirms them against real numbers:
- The `timeout-minutes` starting value (~45) and whether the full matrix or a subset is the required gate day one.
- Whether the very first E2E green justifies immediately standing up the deferred amortization (pre-built base image) or waiting for measured pain.
