# E2E: multibench `status` / `start` against a genuinely serving second bench

Real-instance validation of the per-bench status report (`report-status-per-bench`) against an instance holding **two benches that both genuinely serve**, plus the task 5.5 latency measurement.

Everything here was produced by the repository's own CI Docker matrix on GitHub-hosted runners, not by hand on a developer's machine.
The tests are `tests/e2e/test_multibench_serving_e2e.py` and `tests/e2e/test_multibench_latency_e2e.py`.

Three CI runs produced the evidence below, each on a throwaway branch since deleted:

| Run | What it establishes |
| --- | --- |
| Serving suite with the defect reintroduced | Every assertion that guards a defect goes red when that defect returns |
| Serving suite + six-bench latency, unmodified | The suite is green, and the 1-to-6-bench cost curve |
| The same, driven through the `latency_benches` dispatch input | The opt-in path works end to end, and the curve reproduces |

## Why a new fixture existed at all

The multibench E2E that shipped with the change built a bench **skeleton**: a directory holding `apps/`, `sites/` and a `sites/common_site_config.json` containing `{}`.
`inspect`'s detector recognises that as a bench, so the document shape could be asserted over it - and nothing else could, because a skeleton has no virtualenv, no Procfile, no supervisord and nothing bound to a port.

The three defects the change exists to remove only exist when a second bench is genuinely answering on a port of its own:

| | Defect |
| --- | --- |
| **F3** | A fully healthy bench past the first reported `degraded`, because the probe was hardcoded to `:8000` and therefore measured bench 0's web server. |
| **F4** | A bench serving nothing reported its neighbour's live HTTP code, by the same mechanism in the other direction. |
| **F5** | `cwcli start --bench N` for N>0 waited on `:8000` for the full 60-second timeout, then warned that a bench which had been serving all along had failed to start. |

So the matrix ran green over an artifact that could not have failed.
The skeleton test survives, unweakened, renamed `test_multibench_document_structure_over_a_bench_skeleton`, and now says in its own docstring that it covers structure only.

## The fixture

One instance, two real benches, built by two real `cwcli init` runs - the second adds a bench to the already-running instance, which is how a developer really grows one:

```
cwcli init <project> --port <base> --frappe-branch version-16 --admin-password ... --auto-start
cwcli init <project> --port <base> --frappe-branch version-16 --admin-password ... --auto-start \
  --bench frappe-bench-2 --site second.localhost
cwcli inspect <project> --update
```

Each bench gets its own `bench init` virtualenv, apps, site, Procfile and supervisord, and serves the port bench's own `make_ports` assigned it (`/workspace/frappe-bench` -> 8000, `/workspace/frappe-bench-2` -> 8001).
The fixture asserts those two ports **differ** before yielding: if bench ever handed both benches the same port, every assertion in the module would pass while proving nothing.

## The standard each test holds to

- **Positive before negative.**
  Before asserting a bench is not misreported, the test proves that bench is genuinely serving - its own site, on its own port, answering 200.
  A "no wrong answer" check passes just as happily against a bench that was never up.
- **Assert the discriminator.**
  F3, F4, and F5 assert the exact value the removed bench-blind probe would have read, shown to differ from the correct target.
  For F4, the test captures the first bench's response to the second site's Host header at runtime, proves that response is live, and asserts that neither the bench-narrowed nor instance-wide status reports it for the stopped bench.

## Proof that the assertions bite

A test that cannot fail is not a test, so this was demonstrated rather than claimed.

**One** mutation was run, not one per defect, because the three defects share a single root cause - that is the audit's own central finding, and it is one line.
`supervision.web_http_code`'s URL was pinned back to `http://localhost:8000`, ignoring the port it is handed, and the suite re-run on the same CI leg.

Per test, against that one mutation:

| Test | Defect it guards | Under the mutation |
| --- | --- | --- |
| `test_f3_a_healthy_second_bench_is_not_reported_degraded` | F3 | **red** |
| `test_f4_a_stopped_bench_never_borrows_its_neighbours_http_code` | F4 | **red** |
| `test_f5_starting_a_non_first_bench_waits_on_its_own_port` | F5 | **red** |
| `test_both_benches_genuinely_serve_on_their_own_ports` | the fixture's own serving precondition | **red** |
| `test_instance_wide_status_latency_on_two_serving_benches` | none - it is a measurement | green, correctly |

**No assertion fails to catch its defect.**
Every test that guards a defect went red.
The one that stayed green guards nothing: it times `cwcli axi status` and asserts a loose ceiling, and the mutation changes what `status` reports, not what it costs.
Green is the right answer for it, and it is neither an uncaught defect nor an unreachable mutation.
State it that way rather than as a ratio of the module's five tests, which reads as a score and hides which coverage is which.

What this run does **not** establish: it mutates the one shared root cause, so it says nothing about regressions of a different shape - dropping the probe's `Host` header, say, or breaking the instance fold.
Those carry unit coverage (`TestPerBenchWebProbe` and the fold tests) and were not separately mutation-tested here.

Each failure below is the audit's original defect, reproduced verbatim against real benches.

**F3 - a healthy bench reported `degraded`.**
The second bench, every one of its five processes `RUNNING`, its port resolved correctly, and a null web code beside them:

```
overall: degraded
  benches[1]:
    - index: 1
      bench_path: /workspace/frappe-bench-2
      overall: degraded
      supervisor_up: true
      web_port: 8001
      web_port_verified: true
      web_site: second.localhost
      web_http_code: null
      processes[5]{label,up,pid,uptime_s,cpu_pct,rss_kb,state}:
        web,true,6615,15,18.4,136644,RUNNING
        socketio,true,6498,17,3.4,102492,RUNNING
        watch,true,6499,17,5.1,69752,RUNNING
        schedule,true,6497,17,4.5,60960,RUNNING
        worker,true,6517,17,4.6,69276,RUNNING
```
```
assert 'null' == '200'
```

The test had already asserted, immediately before, that this bench answers 200 on `:8001` for `second.localhost` and that `:8000` answers `000`.

**F4 - a bench serving nothing reported a live code.**
Same bench, stopped - every process down, `supervisor_up: false` - carrying its neighbour's live HTTP response:

```
      overall: online
      supervisor_up: false
      web_port: 8001
      web_http_code: "404"
      processes[5]{label,up,pid,uptime_s,cpu_pct,rss_kb,state}:
        web,false,null,null,null,null,null
        ...
```
```
assert '404' in ('000', 'null')
```

**F5 - the false web-not-ready warning.**
With both benches down, starting the second one:

```
  bench_path: /workspace/frappe-bench-2
  already_running: false
  web_ready: false
  warnings[1]:
    Dev services launched, but the web server did not begin serving on :8001 in time.
    Check 'cwcli status' / 'cwcli logs'.
```
```
assert 'web_ready: true' in ...
```

Worth noting how that last one reads: the warning names `:8001`, the correct port, because the message interpolates the *resolved* port while the probe was watching `:8000`.
A message that names the right port while the measurement watches the wrong one is precisely why this class of defect survived review.

**The serving baseline failed too**, which matters: with the defect in place the fixture could not even establish its own precondition.
The second bench reported `web_http_code: "404"` - bench 0's answer for a site it does not host - while genuinely serving 200 on its own port.

## Task 5.5: instance-wide `status` latency

Measured on a real instance grown one genuinely serving bench at a time, each on its own port (8000 through 8005, the full published range), re-measuring after each addition.
Median of five `cwcli axi status` invocations per data point, on a `ubuntu-latest` GitHub-hosted runner, Frappe v16.

| Serving benches | `cwcli axi status` (median) | Marginal |
| ---: | ---: | ---: |
| 1 | 1.03s | - |
| 2 | 1.57s | +0.54s |
| 3 | 1.90s | +0.33s |
| 4 | 2.47s | +0.58s |
| 5 | 2.97s | +0.49s |
| 6 | 3.58s | +0.61s |

From the separate two-bench fixture, same run: `--bench` (one bench) 1.26s, instance-wide (two benches) 1.76s, marginal 0.49s.
And `cwcli axi start --bench 1` against a second bench with the first one down: 5.0s, against a 60-second wait timeout - worth a number, since F5 is precisely the absence of that timeout being spent.

**Reading it.**
The cost is linear at roughly **0.5s per additional bench**, on a base of about 1s that is process startup rather than probing.
Six benches - the ceiling before `cwcli scale` is needed - is 3.58s.

`design.md` Decision 7 left the `discover_stack` `ps` hoist unspecified pending this number, on the grounds that optimising an unmeasured cost is how a small change acquires a second mechanism.
The number is now available and the hoist is still not done here.
For whoever picks it up: the `ps` is one of roughly five in-container execs per bench, so hoisting it addresses something on the order of a fifth of that 0.5s, and this measurement is what a proposal to do it should cite.
This document reports the cost; it does not decide the optimisation.

**Reproduced.**
The curve was measured twice, on two separate runners, and the two agree: 1.03/1.57/1.90/2.47/2.97/**3.58**s and 1.00/1.58/1.84/2.32/2.76/**3.21**s.
The second run is the one that also exercised the `latency_benches` dispatch input, and it ran the full v16 `standalone` group alongside the six-bench instance: **27 passed, 1 skipped in 28m 46s**, finishing with 108 GB of disk free.
That is why the opt-in path raises the job timeout and does nothing about disk: the bench trees are not close to a constraint, though the wall clock is.

**What the measurement covers, and what it does not.**
Every bench was asserted to be genuinely serving 200 for its own site on its own distinct port before it counted toward a data point, so this is the real probe cost and not the cheaper path a stopped bench takes.
Two runs on one runner class at one Frappe major; it is a cost profile, not a benchmark with error bars.

## What runs where

| | Where it runs |
| --- | --- |
| The serving fixture, F3, F4, F5, and the two-bench latency point | Every v16 `standalone` E2E leg, on every pull request that touches logic, deps or the E2E suite. Cost: two real bench builds. |
| The 1-to-6-bench latency curve | Opt-in only: dispatch the E2E workflow with `latency_benches: 6`. Off on every PR, because each data point is a real bench build. |

The two-bench half is deliberately not opt-in: F3/F4/F5 are regression guards and must run unconditionally, and the fixture they need is the same one the two-bench measurement rides for free.

## Fidelity of the proof runs to the shipped code

Two differences, both disclosed rather than glossed:

- The first two runs carried an earlier revision of the helper that parses a bench out of an `axi status` document.
  It differed only in also collecting a nested process row as a spurious key that no assertion reads.
  The third run carries the shipped version, and it is green.
- The third run's workflow additionally dropped the runner's preinstalled toolchains before starting, on the assumption that six bench trees would not fit.
  That run measured 108 GB free and disproved the assumption, so the step was removed afterwards.
  Its absence leaves the run with *more* preinstalled software and the same headroom; it cannot turn a pass into a failure.

Everything else - the tests, the fixture, the workflow's input plumbing and timeout - is what ships.

## Safety

All three proof runs executed on ephemeral GitHub-hosted runners against `cwe2e-`-prefixed throwaway instances under an isolated `HOME`/`CWCLI_HOME`, torn down by exact name (`cwcli rm --yes --volumes --no-backup`) with the harness's unconditional `sweep_cwe2e` backstop behind it.
No instance on any developer machine was created, started, stopped or removed for this work, and no broad prune ran outside the ephemeral runner.
