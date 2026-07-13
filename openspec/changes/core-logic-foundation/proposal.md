## Why

cwcli's long-range aim is a GUI, and the enabling rework is a logic core that is pure of UI (no `rich`, no `questionary`) but not of I/O, feeding three frontends: the human CLI, the agent AXI surface, and a future GUI.
Today every file under `commands/` welds four concerns into one function body - argument parsing (typer), business logic, side effects (Docker SDK, subprocess, filesystem, the peewee registry), and presentation (`rich` output plus `questionary` prompts).
`commands/backup.py` is representative: one function interleaves the typer signature, an `ensure_containers_running` gate that both starts containers and prompts/prints/`raise typer.Exit`, five `exec_run` probes, default-site and validation logic, `console.print` at every step, and - critically - it returns nothing structured (`backup.py:1-224`).
Success is a printed banner (`backup.py:208-213`); to serialize `backup` today an alternate frontend would have to scrape stdout.

This change is the foundation step of that rework (backlog `cwcli-core-foundation-c3`, blocked-by the now-landed real-Docker E2E net).
It builds the shared return contract, splits the interaction-coupled substrate, and takes ONE reference command - `backup` - end to end through the core, the re-seated CLI, and a new `cwcli axi` surface, proving the whole pattern on a real command while the green `tests/e2e/test_backup_e2e.py` pins the behavior the refactor must preserve.

The seven architecture forks this foundation settles were locked with the captain and are recorded, with the losing alternative for each, in `design.md`.

## What Changes

- **Add a UI-pure logic-core return contract** (stdlib `dataclasses`, `@dataclass(frozen=True, slots=True)`): a light common envelope `Result[T]` (`status`, `data`, `warnings`, `choice`), a `Status` enum (`OK`, `WARNING`, `NEEDS_CHOICE`), a `Message` DTO (`code`, `text`, `detail`), a needs-choice `Choice` DTO (`kind`, `param`, `prompt`, `options`, `default`), and a typed error hierarchy `CwcliError` carrying an `ErrorKind` (`USAGE`, `NOT_FOUND`, `NOT_RUNNING`, `CONFLICT`, `PRECONDITION`, `DOCKER`, `INTERNAL`).
  The envelope is modeled on the hand-rolled `results` list + `_report_and_exit` aggregator that `commands/apps.py` already grew independently (`apps.py:167-192`), lifted into typed, shared form.
- **Non-interactive core, needs-choice by return, typed error by raise.** The core never prompts and never calls `typer.Exit`.
  When it cannot resolve a fork from its explicit params it returns `Result(status=NEEDS_CHOICE, choice=...)`; when it genuinely cannot proceed it raises `CwcliError`.
  Soft/partial/expected outcomes ride in `data` + `warnings` + a `WARNING`/`OK` status, never raised.
- **Split the interaction-coupled substrate into pure core resolvers plus thin CLI wrappers:**
  - A core Docker accessor that returns a container handle usable for exec and raises a typed `CwcliError` (`NOT_FOUND`/`DOCKER`) instead of printing and `raise typer.Exit` the way `get_frappe_container` does today (`docker_utils.py:104-134`); no live Docker object ever appears in a returned DTO.
  - A pure `ensure_containers_running` core resolver that returns "running" / a `confirm_start` needs-choice / a typed `NOT_RUNNING` error, with the `questionary.confirm` + `stderr_console.print` + `raise typer.Exit` behavior (`utils.py:89-129`) moved into a thin CLI wrapper.
  - A pure `resolve_bench_path` core resolver that returns the path, a `select_bench` needs-choice on multi-bench ambiguity (today `raise typer.Exit`, `utils.py:206-221`), or a typed `USAGE` error on `--bench`+`--path` conflict, with the list-printing left to the CLI wrapper.
  - `confirm_or_exit` (`utils.py:224-259`) stays CLI-only and never enters the core.
- **Add `core.backup(...) -> Result[BackupOutcome]`** owning `backup.py`'s I/O and logic (returning a `BackupOutcome` DTO: `site`, `bench_path`, `artifact_path`, `included_files`), and **re-seat the interactive `cwcli backup`** on top of it - the typer signature, the `TipSpinner`, every `console.print`, and the CLI-side resolution of `confirm_start`/`select_bench` choices stay in the frontend.
- **Add the `cwcli axi` namespace** (a Typer sub-app registered in `main.py` exactly like `add_typer(apps_cmd.app, name="apps")`, `main.py:67`) plus a shared, dependency-free serializer that emits TOON on stdout (JSON stays internal via `dataclasses.asdict`), a content-first `cwcli axi` home built on the existing `ls` DTO (`_list_instances`, `list.py:73-93`), and one verb `cwcli axi backup` calling the same `core.backup`.
  Each axi verb is ~15-25 lines: parse flags, call the core fn, serialize the DTO to TOON, map `status`/`CwcliError.kind` to exit code (`0`/`1`/`2`); a `NEEDS_CHOICE` result becomes a structured usage error naming the exact flag, exit `2`.
- **BREAKING**: none.
  `cwcli backup` keeps its exact user-facing behavior (pinned by `tests/e2e/test_backup_e2e.py`, which drives the real binary in both non-interactive and interactive modes); the `cwcli axi` surface and the `core` package are purely additive.

## Capabilities

### New Capabilities

- `logic-core`: the UI-pure return contract - the `Result[T]` envelope, `Status`, `Message`, and `Choice` DTOs, and the `CwcliError`/`ErrorKind` typed-error hierarchy - all stdlib dataclasses, serializable, carrying no live Docker objects; the non-interactive input model (needs-choice by return, hard error by raise, soft/partial by envelope).
- `core-io-resolvers`: the split of the Docker container accessor and the `ensure_containers_running` / `resolve_bench_path` helpers into pure core resolvers that return data / needs-choice / typed-error, plus thin CLI prompt/print wrappers; `confirm_or_exit` stays CLI-only.
- `backup-core-slice`: `core.backup(...) -> Result[BackupOutcome]` and the re-seated interactive `cwcli backup` built on it, behavior-preserving under the green backup E2E - the reference vertical slice proving envelope + needs-choice + typed-error end to end on a real command.
- `axi-surface`: the `cwcli axi` sub-namespace, the shared TOON serializer / exit-code mapper, the content-first `cwcli axi` home on the `ls` DTO, and the `cwcli axi backup` verb.

### Modified Capabilities

- (none - there are no committed `openspec/specs/` capabilities yet, so no existing requirements change. The `cwcli backup` re-seat is behavior-preserving and captured as a requirement of the new `backup-core-slice` capability.)

## Impact

- **New**: a `src/caffeinated_whale_cli/core/` package - the envelope/error/choice DTOs, the core Docker accessor and `ensure_containers_running`/`resolve_bench_path` resolvers, and `core.backup`; a `src/caffeinated_whale_cli/commands/axi.py` (or `commands/axi/`) sub-app + the shared TOON serializer/exit-mapper; register the `axi` sub-app in `main.py` alongside the existing `add_typer` calls.
- **Modified**: `src/caffeinated_whale_cli/commands/backup.py` - re-seated as a thin frontend over `core.backup` (signature, spinner, prints, choice resolution kept; I/O and logic moved to core). `src/caffeinated_whale_cli/commands/utils.py` - `ensure_containers_running` and `resolve_bench_path` become thin CLI wrappers around the new core resolvers; `confirm_or_exit` unchanged. `src/caffeinated_whale_cli/utils/docker_utils.py` - the frappe-container accessor gains a core, typed-error-raising form; the print+`typer.Exit` form stays as the CLI wrapper.
- **Reuses (no changes)**: `utils/bench_sites.py` (`list_sites`, `read_current_site` - already core-shaped), `utils/db_utils.py` (`get_default_site`, `get_cached_project_data` - already return boundary-clean dicts), `utils/console.py` (the stdout/stderr split), `commands/list.py:_list_instances` (the axi home's live-state producer).
- **Left intact**: every non-`backup` command (`unlock`, `run`, `update`, `restore`, `rm`, `inspect`, `init`, `apps`, ...) keeps its current structure; they migrate in later per-command changes, not here.
- **Dependencies**: none added (stdlib `dataclasses`; the TOON encoder is a small hand-written module, no library).
- **Docs/tests**: unit tests for the envelope/error/choice DTOs, the split resolvers (return data / needs-choice / typed-error with faked I/O), `core.backup`, and the TOON serializer; the existing `tests/e2e/test_backup_e2e.py` must stay green unchanged (refactor-under-green); a `README.md` note introducing `cwcli axi` and a `CHANGELOG.md` entry land with the implementation PR.

## Non-Goals (explicitly deferred, not this change)

- **The streaming event-iterator machinery.** `backup` is one-shot/buffered; the typed event iterator (`Iterator[BackupEvent]` with `ProgressLine`/`Done`/`Failed`) arrives with the first streaming command (`logs`/`update`; `unlock`'s verbose `exec_start(stream=True)` path, `unlock.py:166-181`, is the near-term case). The foundation notes the shape but does not build it.
- **The plan/apply split.** `backup` is additive, so no destructive-preview boundary is built. `restore`/`rm` introduce plan/apply per-command later, reusing the envelope + needs-choice + typed-error primitives this change ships.
- **The AXI cross-cutting shell** - the SessionStart hook, the installable skill, `skills-lock.json`, and the CI `--check` staleness build step - is the narrowed `cwcli-axi-pass-x9` task, scheduled after the core and a few commands exist. This change ships only the `cwcli axi` namespace, serializer, home, and the `backup` verb.
- **Migrating the other commands.** Each command (and the `restore`/`rm`/`init`/`update` monsters) migrates in its own later OpenSpec change and PR, gaining AXI conformance as it moves, with the E2E net staying green throughout.
