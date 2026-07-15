## ADDED Requirements

### Requirement: core.label exposes a read and two mutations, built on the existing core primitives

The system SHALL provide `core.list_benches(project) -> Result[BenchList]`, `core.set_label(project, *, bench=None, label) -> Result[LabelOutcome]`, and `core.clear_label(project, *, bench=None) -> Result[LabelOutcome]`, together owning everything `commands/label.py` performs between its cache read and its printed output.
These SHALL be built from the primitives the foundation already provides (`core.docker.get_frappe_container`, `resolvers.resolve_container_state`, `resolvers.resolve_bench`, `resolvers.cached_benches`, `utils.bench_labels`, `db_utils.set_bench_label`, the `Result`/`Choice`/`CwcliError` envelope) WITHOUT adding new primitives.
Listing SHALL be a distinct function rather than a mode of the mutation functions, because a missing bench selector is a legitimate read request and not a decision the core cannot make.
Setting and clearing SHALL be distinct functions rather than one function whose `label=None` means clear, so the core does not expose the `None` sentinel that `db_utils.set_bench_label` uses at the storage layer.
`core.label` SHALL NOT print, prompt, or call `typer.Exit`.

#### Scenario: Listing benches needs no container

- **WHEN** `core.list_benches` runs against a project with cached benches
- **THEN** it returns `Result(OK, BenchList)` with one `BenchInfo(index, path, label)` per bench in stable sorted order, touching no container and printing nothing

#### Scenario: A project with no cached benches is a typed error naming the remedy

- **WHEN** `core.list_benches` runs against a project that has never been inspected
- **THEN** it raises `CwcliError(kind=NOT_FOUND)` carrying a hint directing the caller to run `cwcli inspect <project>` first, rather than returning an empty list

#### Scenario: Setting a label writes the marker and the cache

- **WHEN** `core.set_label` runs against a running project with a valid, unused label
- **THEN** it writes the marker file inside the container, writes the label to the cache, and returns `Result(OK, LabelOutcome(label=<new>, previous_label=<old>, cleared=False))`

#### Scenario: An invalid or duplicate label is rejected before anything is written

- **WHEN** `core.set_label` is given a purely numeric label, a label violating the charset rules, or a label already used by another bench in the same project
- **THEN** it raises `CwcliError(kind=USAGE)` and writes neither the marker nor the cache

#### Scenario: An unknown selector raises NOT_FOUND without presentation data

- **WHEN** `core.set_label` or `core.clear_label` is given a bench selector matching no bench
- **THEN** it raises `CwcliError(kind=NOT_FOUND)` naming the selector, and the error does NOT carry a rendered bench list (the frontend obtains one from `core.list_benches`)

#### Scenario: Multi-bench with no selector is a select_bench choice

- **WHEN** `core.set_label` or `core.clear_label` runs on a multi-bench project with no `bench`
- **THEN** it returns a `select_bench` `NEEDS_CHOICE` result and writes nothing

#### Scenario: A single-bench project resolves without a selector

- **WHEN** `core.set_label` runs on a single-bench project with no `bench`
- **THEN** it operates on that bench and carries a `bench.sole` warning in the envelope rather than printing it

### Requirement: label requires a running container and never auto-starts it

`core.set_label` and `core.clear_label` SHALL resolve the container run-state through `resolvers.resolve_container_state` with `offer_choice=False` and `auto_start=False`, so a stopped container raises `CwcliError(kind=NOT_RUNNING)` rather than offering a `confirm_start` choice or starting anything.
A label change SHALL NOT start a stopped project, because the marker is stored inside the bench and a label change is not a reason to spin up an instance.

#### Scenario: A stopped container is a typed error, not a choice and not a start

- **WHEN** `core.set_label` runs against a project whose frappe container is not running
- **THEN** it raises `CwcliError(kind=NOT_RUNNING)`, starts nothing, writes nothing, and does NOT return a `confirm_start` choice

#### Scenario: The not-running hint names a remedy the command actually has

- **WHEN** the `NOT_RUNNING` error from a `label` operation is rendered by any frontend
- **THEN** its hint directs the caller to start the project first, and does NOT suggest a `--yes` flag, which `cwcli label` does not have

### Requirement: resolve_container_state accepts a caller-supplied not-running hint

`resolvers.resolve_container_state` SHALL accept an optional caller-supplied hint used on its `NOT_RUNNING` error, defaulting to the string it hardcodes today so that every existing caller is unchanged.
The hardcoded default SHALL remain correct for callers that have a `--yes` flag.

#### Scenario: Existing callers are unaffected by the widening

- **WHEN** `core.backup`, `core.unlock`, or the `commands/utils.py` CLI wrapper resolves a container state without supplying a hint
- **THEN** the `NOT_RUNNING` error carries the same hint text it carried before this change

### Requirement: The cached-benches read is shared from the core layer

The system SHALL provide `resolvers.cached_benches(project)` returning the cached bench list for a project, and SHALL use it everywhere that read is performed.
The private `_cached_benches` helper in `commands/utils.py` SHALL be removed and its callers re-pointed at the core helper, because `core/` cannot import `commands/` and the helper must live in the layer both can reach.
The `(cached_data or {}).get("bench_instances") or []` idiom SHALL NOT remain duplicated across the core and command layers.

#### Scenario: One implementation serves both layers

- **WHEN** the core resolvers, the CLI wrappers, and the label command each need a project's cached benches
- **THEN** all of them call `resolvers.cached_benches`, and no module re-implements the idiom inline

### Requirement: The clear path writes the marker before the cache and fails closed

`core.clear_label` SHALL remove the marker file FIRST and SHALL update the cache only if the marker removal succeeded.
When the marker removal fails, `core.clear_label` SHALL raise and SHALL leave the cache label intact, because a cleared cache with a surviving marker would let a later full `inspect` resurrect the cleared label.
When the marker removal succeeds but the cache update fails, `core.clear_label` SHALL raise so the inconsistency is surfaced rather than reported as success.

#### Scenario: A failed marker removal leaves the cache label intact

- **WHEN** `core.clear_label` cannot remove the marker file inside the container
- **THEN** it raises a typed error, the cache label is unchanged, and the marker is still present

#### Scenario: A failed cache update after a successful marker removal is surfaced

- **WHEN** `core.clear_label` removes the marker but the cache update reports no row updated
- **THEN** it raises a typed error explaining the marker was removed but the cache could not be cleared, rather than returning success

#### Scenario: The clear ordering survives the migration

- **WHEN** the `label` migration is complete
- **THEN** the existing unit tests pinning both clear-path failure modes pass unchanged against the core

### Requirement: cwcli label is a thin frontend over the core

`commands/label.py` SHALL call `core.list_benches` / `core.set_label` / `core.clear_label` and SHALL retain only presentation and interaction: the typer signature, `_print_bench_list`, the `rich` markup, the available-benches list rendered on an unknown selector, and the exit codes.
The command's arguments, flags, messages, and exit codes SHALL be preserved.

#### Scenario: List mode is preserved

- **WHEN** `cwcli label <project>` runs with no bench selector
- **THEN** it prints the bench list exactly as before, touching no container

#### Scenario: The available-benches list on an unknown selector is preserved

- **WHEN** `cwcli label <project> <unknown-selector> <label>` runs
- **THEN** the frontend renders the error and the available-benches list as before, obtaining the list from `core.list_benches` rather than from data carried on the error

#### Scenario: Preserved exit codes

- **WHEN** any `label` operation fails
- **THEN** the frontend renders the failure in the historical message form and exits with the code the command used before the migration

### Requirement: The dead verbose parameter is removed and cwcli label --verbose is made honest

The `verbose` parameter SHALL be removed from `bench_labels.read_label_marker`, `write_label_marker`, and `clear_label_marker`, where it is accepted and never read, and the call sites in `commands/inspect.py` SHALL be updated for the signature change only.
`cwcli inspect`'s own `--verbose` behavior SHALL be unchanged.
`cwcli label --verbose`, which today feeds only those dead parameters and therefore does nothing, SHALL be retained and SHALL emit the operation's envelope warnings and the resolved marker path.
The flag SHALL NOT be removed, because removing it would break an existing CLI surface for no gain.

#### Scenario: Verbose reports something true

- **WHEN** `cwcli label <project> <selector> <label> --verbose` succeeds
- **THEN** it reports the envelope's warnings and the resolved marker path, where before it reported nothing

#### Scenario: Non-verbose output is unchanged

- **WHEN** `cwcli label` runs without `--verbose`
- **THEN** its output and exit code are byte-for-byte what they were before the migration

#### Scenario: inspect's marker recovery is undisturbed

- **WHEN** a full `inspect` runs against benches carrying marker files after the signature change
- **THEN** labels are recovered from the markers exactly as before

### Requirement: cwcli axi benches answers what --bench accepts

The system SHALL provide `cwcli axi benches <project>` emitting a `BenchList` as exactly one TOON document on stdout, carrying each bench's numeric index, path, and user label.
This SHALL be the structured answer to the question every bench-scoped axi verb asks when it reports "multiple benches; pass --bench <index|label>", which no existing axi verb can answer.
A project with no cached benches SHALL be a structured error naming `cwcli inspect` as the remedy, rather than an empty list, because "not inspected yet" and "zero benches" are different facts.

#### Scenario: An agent can discover valid --bench values

- **WHEN** an agent receives "multiple benches; pass --bench" from any bench-scoped axi verb and then runs `cwcli axi benches <project>`
- **THEN** it receives one TOON document listing every bench's index and label, from which a valid `--bench` value can be read directly

#### Scenario: An uninspected project is a definitive error, not an empty result

- **WHEN** `cwcli axi benches <project>` runs against a project that has never been inspected
- **THEN** it emits a structured error whose help line names `cwcli inspect <project>`, and exits non-zero

### Requirement: cwcli axi label is a non-interactive, structured mutation

The system SHALL provide `cwcli axi label <project> [--bench <sel>] --set <label> | --clear` on the existing `cwcli axi` serializer and exit-code mapper.
`--set` and `--clear` SHALL be mutually exclusive, and exactly one SHALL be required; supplying neither SHALL be a usage error rather than an implicit list, because `cwcli axi benches` is the discovery verb.
It SHALL NOT prompt and SHALL NOT auto-start a stopped container: a `NEEDS_CHOICE` result SHALL be rendered as a structured usage error naming the flag that resolves it, with exit code 2.
It SHALL emit exactly one TOON document on stdout.
Listing SHALL NOT be a mode of this verb selected by the absence of an argument.

#### Scenario: Multi-bench without a selector is a usage error naming the flag

- **WHEN** `cwcli axi label <project> --set staging` runs on a multi-bench project with no `--bench`
- **THEN** it emits a structured error naming `--bench`, lists the valid benches, exits 2, and writes nothing

#### Scenario: A stopped container is a structured error with a usable remedy

- **WHEN** `cwcli axi label` runs against a project whose container is stopped
- **THEN** it emits a structured error whose help line directs the agent to start the project, does not mention a `--yes` flag, exits non-zero, and starts nothing

#### Scenario: Neither --set nor --clear is a usage error

- **WHEN** `cwcli axi label <project> --bench 0` runs with neither `--set` nor `--clear`
- **THEN** it emits a structured usage error and exits 2, rather than listing benches

### Requirement: label is E2E-verified non-interactively against a real instance

The system SHALL provide a real-Docker E2E for `label` covering the DB-and-marker consistency invariant end to end: set a label, verify BOTH the cache and the in-container marker carry it, clear it, verify BOTH are gone, and verify a subsequent `inspect` does not resurrect the cleared label.
The E2E SHALL assert real outcomes against a real instance and a real container, not string matches on output.
The E2E SHALL cover ONE mode, non-interactive, because `label` has no prompt: it contains no `questionary`, no `isatty`, and no confirmation, and it deliberately does not auto-start.
An interactive leg SHALL NOT be added, because there is no prompt to drive.

#### Scenario: A set label is present in both stores

- **WHEN** `cwcli label <project> <selector> <label>` runs non-interactively against a real running instance
- **THEN** it exits 0, the cache carries the label, and the marker file inside the container carries it

#### Scenario: A cleared label is gone from both stores and stays gone

- **WHEN** `cwcli label <project> <selector> --clear` runs and a full `cwcli inspect` follows
- **THEN** both the cache label and the in-container marker are genuinely gone, and the inspect does not resurrect the label
