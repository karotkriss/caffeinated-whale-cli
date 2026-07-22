## ADDED Requirements

### Requirement: Status reports every bench of an instance

WHEN no bench selector is given, `cwcli status <project>` and `cwcli axi status <project>` SHALL report EVERY bench of the instance in one answer.

Neither surface SHALL refuse a multi-bench instance, prompt for a bench, or return a `select_bench` choice.
`core.status.status` SHALL NOT return `Status.NEEDS_CHOICE` on any path.

The bench list SHALL come from `resolvers.cached_benches`, in the same order `--bench <index>` indexes into, so an index means the same thing in both forms and agrees with `cwcli axi benches`.

WHEN the project has no cached benches, status SHALL report a single bench at `resolvers.DEFAULT_BENCH_PATH` carrying the existing `bench.default_used` warning, preserving today's behavior for a never-inspected project.

#### Scenario: A two-bench instance reports both benches

- **WHEN** `cwcli axi status mbstat` runs against an instance holding `/workspace/bench1` and `/workspace/bench2`
- **THEN** stdout is ONE TOON document whose `benches` collection has two entries, each carrying its own `bench_path`, `overall`, `supervisor_up`, `web_port`, `web_http_code` and `processes`, and the process exits 0

#### Scenario: The refusal is gone and cannot return

- **WHEN** `core.status.status` is called for a multi-bench project with no selector
- **THEN** the result status is never `NEEDS_CHOICE`, and no code path in `commands/status.py` or `commands/axi.py` handles a `select_bench` choice from status

### Requirement: Every reported bench is named

`StatusReport` SHALL carry `benches: list[BenchStatus]`, and each `BenchStatus` SHALL carry the bench's `index`, `bench_path` and `label`, bringing status into line with `StartOutcome.bench_path`, `LogsRead.bench_path` and `ProcessRestartOutcome.bench_path`.

`supervisor_up`, `web_http_code`, `processes` and `not_cwcli_supervised` SHALL live on `BenchStatus`, NOT on `StatusReport`.
`StatusReport` SHALL carry `overall` (FIRST, so the axi contract holds), `project`, `container_running` and `benches`, and nothing else.

The document shape SHALL be uniform: a single-bench project and an explicit `--bench` selection SHALL emit the same `benches` collection with exactly one entry, so no consumer branches on bench count.

#### Scenario: A single-bench project emits the same shape

- **WHEN** `cwcli axi status singlebench` runs
- **THEN** the document carries `benches` with exactly one entry, and the per-bench fields are inside that entry rather than at the top level

#### Scenario: Two benches are distinguishable in one document

- **WHEN** two benches of the same instance are reported
- **THEN** each entry carries a distinct `bench_path`, so no reader has to infer which bench a process row or HTTP code belongs to

### Requirement: The web probe measures the named bench's own port

`supervision.web_http_code`, `supervision.web_is_serving` and `supervision.wait_web_ready` SHALL each take an explicit keyword-only `port: int` parameter with NO default value, so a caller cannot reach the probe without naming a port.

The port SHALL be resolved from that bench's OWN `sites/common_site_config.json` through a single shared reader promoted from `core/scale.py`'s `_read_assigned_ports` to `resolvers.resolve_assigned_ports`, with behavior byte-identical to the original.
`core/scale.py` SHALL be re-pointed at the promoted reader and SHALL NOT keep a private copy.

`core.status` SHALL report, per bench, the port it actually probed.

#### Scenario: A second bench is measured on its own port

- **WHEN** bench 1 is assigned `webserver_port 8001` in its `common_site_config.json` and status reports it
- **THEN** the probe is issued against `http://localhost:8001`, and the bench entry carries `web_port: 8001`

#### Scenario: A healthy second bench is not reported degraded

- **WHEN** only bench 1 is started, every one of its programs is `RUNNING`, and it answers on its own port
- **THEN** that bench's `overall` is `running`, and no bench entry pairs a `RUNNING` web process row with a null `web_http_code`

#### Scenario: A bench serving nothing does not borrow a live code

- **WHEN** bench 1's web program is `STOPPED` while bench 0 serves
- **THEN** bench 1's entry carries `web_http_code: null` or `"000"`, never a code produced by bench 0's server

#### Scenario: The probe cannot be called without a port

- **WHEN** the three supervision probe helpers are inspected
- **THEN** none of them declares a default value for `port`, so omitting it is a call-time error rather than a silent 8000

### Requirement: An unresolvable port is reported unknown and never guessed

WHEN a bench's `sites/common_site_config.json` cannot be read or parsed, status SHALL report `web_port: null` and `web_port_verified: false` for that bench, SHALL NOT issue any probe, and SHALL NOT fall back to port 8000.

That bench SHALL NOT be degraded on account of the missing web signal: the aggregate SHALL be computed with `_overall`'s existing `web_probed=False` path, so supervisor-up plus every program healthy still reads `running`.

A `status.web_port_unknown` warning SHALL name the affected bench and point at `cwcli inspect <project>`.

#### Scenario: An unreadable config does not manufacture a false degraded

- **WHEN** a bench's config cannot be read while its supervisord is up and every program is `RUNNING`
- **THEN** that bench's `overall` is `running`, `web_port` is null, `web_port_verified` is false, and a `status.web_port_unknown` warning names the bench

#### Scenario: No probe is issued against a guessed port

- **WHEN** the port cannot be resolved for a bench
- **THEN** no `curl` is executed for that bench on any port

### Requirement: The bench selector keeps working unchanged

`--bench <index|label>` and `--path` SHALL resolve through `resolvers.resolve_bench` exactly as today, including the `--bench`/`--path` conflict `USAGE` error, the `bench.not_found` `NOT_FOUND` raise for an unknown selector, and the `bench.sole` warning on a single-bench project.

WHEN a selector is given, `benches` SHALL contain exactly that one bench, and the instance `overall` SHALL equal that bench's `overall`.

#### Scenario: A selector narrows the report to one bench

- **WHEN** `cwcli axi status mbstat --bench 1` runs
- **THEN** `benches` has exactly one entry whose `bench_path` is bench 1's, and the top-level `overall` equals that entry's `overall`

#### Scenario: An unknown selector still errors

- **WHEN** `cwcli axi status mbstat --bench 9` runs
- **THEN** a `NOT_FOUND` `bench.not_found` error is raised and no bench is reported

### Requirement: The instance aggregate folds the per-bench aggregates

`StatusReport.overall` SHALL be derived from the reported benches using the SAME four tokens (`offline`, `online`, `running`, `degraded`).
No fifth token SHALL be introduced.

The fold SHALL be: `offline` when the container is not running; otherwise `degraded` when any bench is `degraded`; otherwise `running` when any bench is `running`; otherwise `online`.

#### Scenario: One started healthy bench and one never started reads running

- **WHEN** bench 0 was never started (`online`) and bench 1 is healthy (`running`)
- **THEN** the instance `overall` is `running`, and the two bench entries carry `online` and `running` respectively

#### Scenario: A real fault is never masked by a healthy sibling

- **WHEN** bench 0 is `running` and bench 1 is `degraded`
- **THEN** the instance `overall` is `degraded`

#### Scenario: A stopped instance reports no benches

- **WHEN** the frappe container is not running
- **THEN** `overall` is `offline`, `container_running` is false, and `benches` is empty

### Requirement: The human surface keeps one token on stdout

`cwcli status` SHALL print exactly ONE token on stdout - the instance `overall` - on every path, unchanged.
All per-bench detail SHALL go to stderr.

The stderr detail SHALL carry one sub-block per bench headed by its index, path and label, and its web line SHALL name the port that was probed rather than implying a universal one.

`--watch` SHALL render a `bench` column only when more than one bench is reported, leaving the single-bench live view unchanged, and SHALL keep `probe_web=False`.

`cwcli status` SHALL NOT prompt on any path.

#### Scenario: Stdout stays one token on a multi-bench instance

- **WHEN** `cwcli status mbstat` runs against a two-bench instance
- **THEN** stdout is exactly one line carrying one of `offline`/`online`/`running`/`degraded`, and both benches appear only on stderr

#### Scenario: The multi-bench prompt is gone

- **WHEN** `cwcli status mbstat` runs on a TTY against a two-bench instance
- **THEN** no bench prompt is shown and both benches are reported

### Requirement: The agent surface emits one TOON document and still always exits 0

`cwcli axi status` SHALL emit ONE TOON document on stdout with `overall` first, and SHALL exit 0 on every successful read regardless of the aggregate.

The nested `benches` collection SHALL be rendered by the EXISTING `utils/toon.py` encoder using its records-carrying-nested-collections list form; no encoder change SHALL be required.
Each bench's `processes` SHALL remain a compact tabular block.

The verb SHALL gain and lose no flags, so the generated `skills/cwcli/SKILL.md` verb table is unchanged.

#### Scenario: A degraded instance is still a successful read

- **WHEN** `cwcli axi status mbstat` reports `overall: degraded`
- **THEN** the process exits 0, because a successful read is not an error on the agent surface

#### Scenario: Stdout parses as exactly one document

- **WHEN** any `cwcli axi status` invocation succeeds
- **THEN** stdout is one TOON document and nothing else, with every warning rendered inside its `warnings` block

### Requirement: Start waits on its own bench's web port

`core.start` SHALL resolve the resolved bench's assigned web port and pass it to `supervision.wait_web_ready`, so a launch of any bench past the first is not measured against another bench's server.

WHEN the port cannot be resolved, start SHALL SKIP the wait entirely and report `web_ready: null` (the field's existing "not probed" value) rather than waiting on a guessed port.

The `start.web_not_ready` warning text SHALL name the bench's real port instead of `:8000`.
`StartOutcome` SHALL gain no new field.

#### Scenario: Starting a second bench emits no false warning

- **WHEN** `cwcli start mbstat --bench 1` launches bench 1, which begins serving on its own assigned port
- **THEN** the start reports `web_ready: true` promptly and emits no `start.web_not_ready` warning

#### Scenario: An unresolvable port skips the wait rather than stalling

- **WHEN** the bench's assigned port cannot be read
- **THEN** no web wait is performed, `web_ready` is null, and no timeout is spent
