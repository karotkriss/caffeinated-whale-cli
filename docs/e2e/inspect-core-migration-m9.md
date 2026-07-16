# E2E evidence: `inspect` on the logic core (migrate-inspect-core, m9)

Real-instance validation of the `inspect` migration (openspec `migrate-inspect-core`), per the captain standard: BOTH modes, one throwaway isolated instance, against the worktree's own editable install.

## Setup

- Instance: `cwe2e-inspm9-main`, genuine `cwcli init` (frappe `version-16`, port base 14100), `bench new-site development.localhost`.
- Isolation: temp `HOME` + `CWCLI_HOME` under the session scratchpad (never the real `~/.cwcli`); the `cwe2e-` name prefix; ports disjoint from every live instance.
- Binary: the worktree's `.venv/bin/cwcli` (editable install of the code under review), verified by process inspection.
- A concurrent `cwe2e-a5fc07-main` instance (another lane's E2E) was present and deliberately untouched throughout.

## Non-interactive legs (stdin closed, a real non-TTY)

| Leg | Command | Evidence | Result |
| --- | --- | --- | --- |
| T3 cold | `config cache clear` then `inspect -v` | `-v` trace `No cached data found, proceeding with inspect.`; full tree rendered | exit 0 |
| T2 cache hit | `inspect -v` | `Partial inspect found no drift; serving cached data unchanged.` | exit 0 |
| T1 | `inspect --no-refresh -v` | `Cache-only refresh; serving cached data as-is.`; stdout byte-identical to the T2 run | exit 0 |
| Forced T3 | `inspect --update -v` | `Full refresh requested, ignoring cache.` | exit 0 |
| `--json` shape | `inspect --json` | keys `project_name`/`bench_instances`, bench keys `[index, path, sites, available_apps, common_site_config]`, RAW `installed_apps` lines (`frappe 16.27.1 version-16`) | exit 0 |
| Drift escalation (issue #27's scenario) | real `bench new-app --no-git driftapp` + `bench install-app`, then plain `inspect -v` | `Partial inspect detected drift; escalating to a full inspect.`; `driftapp` in the tree AND persisted (available_apps + the deep per-site installed list) | exit 0 |
| Stopped + `--yes` | `stop`, then `inspect --update --yes` | containers auto-started, full inspect completed, 4 containers up after | exit 0 |
| Stopped, non-TTY, no `--yes` | `stop`, then `inspect --update` | refusal naming `--yes`, nothing started | exit 1 |
| `axi inspect --update` on stopped | | usage error `error: "Frappe container ... is not running. Start it?"` + `help: "start it first with 'cwcli start <project>'"`; nothing started | exit 2 |
| `axi inspect` (tiered) on stopped | | `served_from: cache`, full nested benches document | exit 0 |
| `axi inspect` on running | | `served_from: partial`, label/apps/sites all present | exit 0 |

Every `axi` output was validated against `tests/test_axi.py:assert_is_one_toon_document` (the recursive walker) - one TOON document, no prose, no Python reprs.

## Interactive legs (pexpect pty, awaiting `ESC[?2004h` before every keystroke)

| Leg | Drive | Result |
| --- | --- | --- |
| Start prompt DECLINED | stopped project, `inspect --update`, `n` + Enter at the questionary confirm | exit 1, nothing started |
| Start prompt ACCEPTED | stopped project, `inspect --update`, `y` + Enter | containers started, full inspect rendered, exit 0 |
| `-i` invalid label | `inspect -i`, answer `9999` | rejected (`cannot be purely numeric`), previous label kept (still `null` in the cache), exit 0 |
| `-i` set label | `inspect -i`, answer `e2elabel` | persisted to BOTH stores: `axi benches` shows `e2elabel` (cache) AND `/workspace/frappe-bench/.cwcli/.bench-label` holds `{"schema": 1, "label": "e2elabel"}` (marker) |
| `-i` blank keeps | `inspect -i`, bare Enter at the `[current: 'e2elabel']` prompt | label unchanged, exit 0 |
| Marker recovery (bonus) | `config cache clear` + `inspect --update` | the full inspect recovered `e2elabel` from the marker into the fresh cache |

One capture detail worth keeping: rich soft-wraps the `-i` rejection sentence at pty width, so a pexpect expect for the full phrase `Keeping the previous label` can straddle a line break - match a fragment (`cannot be purely numeric`) instead.

## Post-review-fix re-run (head 1ed5e06)

Re-run at head `1ed5e06` (includes review-fix commit `3b3cd57`, "abort fallback populate on `CwcliError`, not degrade to default"), on the same throwaway instance (`cwe2e-inspm9-main`), both modes again.

| Leg | Command | Evidence | Result |
| --- | --- | --- | --- |
| Fallback-populate abort (`open`) | config remove-path + cache clear, then `open <project> --docker` | `Error: No Bench Instances found for project '<project>'.`; zero `Using default` lines (no degrade to a default bench) | exit 1 |
| Fallback-populate abort (`apps update`) | same undiscoverable-bench setup, then `apps update <project> frappe` | same abort message; zero `Using default` lines | exit 1 |
| T3 cold (re-check after config add-path restore) | `config cache clear` then `inspect -v` | `No cached data found, proceeding with inspect.` trace unchanged | exit 0 |
| T2 no-drift | `inspect -v` | `Partial inspect found no drift; serving cached data unchanged.` unchanged | exit 0 |
| T1 | `inspect --no-refresh -v` | `Cache-only refresh; serving cached data as-is.` unchanged | exit 0 |
| `--json` shape + label recovery | `config cache clear` then `inspect --json` | user label recovered from the in-bench marker through the fresh full inspect; shape unchanged | exit 0 |
| `axi inspect` on running | | `served_from: partial`, exit 0 unchanged |
| `axi inspect --update` non-TTY on stopped | | refusal unchanged, naming `--yes` | exit 1 |
| `axi inspect --update` on stopped | | usage error naming `cwcli start` unchanged | exit 2 |
| Start prompt declined (pty) | stopped project, `inspect --update`, `n` + Enter | exit 1, nothing started (unchanged) |
| Start prompt accepted (pty) | stopped project, `inspect --update`, `y` + Enter | containers started, full inspect rendered (unchanged) | exit 0 |
| `-i` blank-keep (pty) | `inspect -i`, bare Enter | label unchanged in BOTH the cache and the marker | exit 0 |

The two changed fallback-populate call sites (`open`, `apps update`) now abort cleanly instead of silently degrading to a default bench when the target bench is undiscoverable; every other leg from the original run above is unaffected by the review fix and re-passed unchanged.

## Teardown

`cwcli rm cwe2e-inspm9-main --yes --volumes --no-backup` after the post-pipeline re-run (captain standard: the E2E is re-run after no-mistakes and after any review fixes before final teardown). No broad Docker cleanup; only this run's own `cwe2e-inspm9-*` resources.
