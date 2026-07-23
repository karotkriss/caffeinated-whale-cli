# E2E: multibench `status` / `start` against a genuinely serving second bench

Real-instance validation of the per-bench status report (`report-status-per-bench`)
against an instance holding **two benches that both genuinely serve**, plus the
task 5.5 latency measurement.

Everything here was produced by the repository's own CI Docker matrix on
GitHub-hosted runners, not by hand on a developer's machine. The tests are
`tests/e2e/test_multibench_serving_e2e.py` and `tests/e2e/test_multibench_latency_e2e.py`.

## Why a new fixture existed at all

The multibench E2E that shipped with the change built a bench **skeleton**: a
directory holding `apps/`, `sites/` and a `sites/common_site_config.json`
containing `{}`. `inspect`'s detector recognises that as a bench, so the document
shape could be asserted over it - and nothing else could, because a skeleton has
no virtualenv, no Procfile, no supervisord and nothing bound to a port.

The three defects the change exists to remove only exist when a second bench is
genuinely answering on a port of its own:

| | Defect |
| --- | --- |
| **F3** | A fully healthy bench past the first reported `degraded`, because the probe was hardcoded to `:8000` and therefore measured bench 0's web server. |
| **F4** | A bench serving nothing reported its neighbour's live HTTP code, by the same mechanism in the other direction. |
| **F5** | `cwcli start --bench N` for N>0 waited on `:8000` for the full 60-second timeout, then warned that a bench which had been serving all along had failed to start. |

So the matrix ran green over an artifact that could not have failed. The skeleton
test survives, unweakened, renamed
`test_multibench_document_structure_over_a_bench_skeleton`, and now says in its own
docstring that it covers structure only.

## The fixture

One instance, two real benches, built by two real `cwcli init` runs - the second
adds a bench to the already-running instance, which is how a developer really
grows one:

```
cwcli init <project> --port <base> --frappe-branch version-16 --admin-password ... --auto-start
cwcli init <project> --port <base> --frappe-branch version-16 --admin-password ... --auto-start \
  --bench frappe-bench-2 --site second.localhost
cwcli inspect <project> --update
```

Each bench gets its own `bench init` virtualenv, apps, site, Procfile and
supervisord, and serves the port bench's own `make_ports` assigned it
(`/workspace/frappe-bench` -> 8000, `/workspace/frappe-bench-2` -> 8001). The
fixture asserts those two ports **differ** before yielding: if bench ever handed
both benches the same port, every assertion in the module would pass while proving
nothing.

## The standard each test holds to

- **Positive before negative.** Before asserting a bench is not misreported, the
  test proves that bench is genuinely serving - its own site, on its own port,
  answering 200. A "no wrong answer" check passes just as happily against a bench
  that was never up.
- **Assert the discriminator.** Each test also asserts the value the removed
  bench-blind probe *would* have read, shown to differ from what `status` reports.
  That is what makes the assertion fail under the defect rather than merely
  describe it.

## Proof that the assertions bite

<!--EVIDENCE:MUTATION-->

## Task 5.5: instance-wide `status` latency

<!--EVIDENCE:LATENCY-->

## Safety

Both proof runs executed on ephemeral GitHub-hosted runners against
`cwe2e-`-prefixed throwaway instances under an isolated `HOME`/`CWCLI_HOME`, torn
down by exact name (`cwcli rm --yes --volumes --no-backup`) with the harness's
unconditional `sweep_cwe2e` backstop behind it. No instance on any developer
machine was created, started, stopped or removed for this work, and no broad
prune ran outside the ephemeral runner.
