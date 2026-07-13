## 1. Logic-core return contract (dataclasses)

- [x] 1.1 Create `src/caffeinated_whale_cli/core/` package with a `types` (or `envelope`) module defining `Status` (`OK`/`WARNING`/`NEEDS_CHOICE`), `Message` (`code`/`text`/`detail`), `Choice` (`kind`/`param`/`prompt`/`options`/`default`), and `Result[T]` (`status`/`data`/`warnings`/`choice`) as stdlib `@dataclass(frozen=True, slots=True)` (no `errors` field). Model the shape on `apps._report_and_exit`'s `results` payload.
- [x] 1.2 Define `CwcliError(Exception)` with `kind: ErrorKind`, `code`, `message`, `hint`, and the closed `ErrorKind` enum (`USAGE`/`NOT_FOUND`/`NOT_RUNNING`/`CONFLICT`/`PRECONDITION`/`DOCKER`/`INTERNAL`).
- [x] 1.3 Enforce UI-purity: no `rich`/`questionary`/`typer.Exit` in `core/`. Add a unit test that imports every `core/` module and asserts the ban (import-scan or an assertion on the module source).
- [x] 1.4 Unit-test the envelope/error/choice DTOs: `dataclasses.asdict(Result(...).data)` yields plain data with no live object; `Status`/`ErrorKind` round-trip their string values; `Choice` carries the fillable `param`.

## 2. Core I/O resolvers + thin CLI wrappers

- [x] 2.1 Add a core frappe-container accessor that returns an exec-usable handle and raises `CwcliError(NOT_FOUND/DOCKER)`; re-express `docker_utils.get_frappe_container` as a thin CLI wrapper defined in terms of it (call core, map the typed error to the existing print + `typer.Exit`), preserving current messages/exit codes.
- [x] 2.2 Split `ensure_containers_running` into a pure core resolver (returns running / `confirm_start` `NEEDS_CHOICE` / `NOT_RUNNING`, never prompts or prints) plus a thin CLI wrapper in `commands/utils.py` that resolves the `confirm_start` choice via `questionary` and preserves today's decline/non-TTY exit codes and messages.
- [x] 2.3 Split `resolve_bench_path` into a pure core resolver (returns path / `select_bench` `NEEDS_CHOICE` / `CwcliError(USAGE)` on `--bench`+`--path` / `None` on no-cache) plus a thin CLI wrapper that prints the bench list and resolves the choice (prompt, or first-with-note for the `on_ambiguous="first"` callers).
- [x] 2.4 Keep `confirm_or_exit` CLI-only; assert (test) that no `core/` module imports it, and that no live Docker object appears in any DTO returned across a core boundary.
- [x] 2.5 Unit-test each split resolver against faked I/O: running/stopped/auto-start; single/multi/no-cache benches; `--bench`+`--path` usage error; wrapper-preserves-exit-code cases.

## 3. Reference slice: core.backup + re-seated cwcli backup

- [x] 3.1 Implement `core.backup(project, *, site, bench, bench_path, with_files, ...) -> Result[BackupOutcome]` owning `commands/backup.py`'s container/bench/default-site resolution, site+path metachar validation, backup-dir ensure, and the `bench --site <site> backup` exec; return `NEEDS_CHOICE` for the two forks, raise `CwcliError` for hard failures, return `OK` + `BackupOutcome` on success. No print/prompt/`typer.Exit`.
- [x] 3.2 Define the `BackupOutcome` dataclass (`site`, `bench_path`, `artifact_path`, `included_files`); resolve `artifact_path` to the created dump.
- [x] 3.3 Re-seat `commands/backup.py` as a thin frontend over `core.backup`: keep the typer signature, `TipSpinner`, and every `console.print`; resolve returned `confirm_start`/`select_bench` choices via the CLI wrappers (prompt then re-invoke); map `CwcliError` to the current messages/exit codes; keep the exact success banner.
- [x] 3.4 Unit-test `core.backup` against faked I/O for every branch (success outcome; multi-bench choice; stopped-container choice; missing site `NOT_FOUND`; failed backup `PRECONDITION`; shell-unsafe site/path `USAGE`).
- [x] 3.5 Confirm the re-seated command keeps `tests/e2e/test_backup_e2e.py` green UNCHANGED (refactor-under-green), both non-interactive and interactive.

## 4. cwcli axi surface (namespace + serializer + home + backup verb)

- [x] 4.1 Add the `cwcli axi` Typer sub-app and register it in `main.py` alongside the existing `add_typer` calls.
- [x] 4.2 Add the shared, dependency-free TOON serializer (walk `dataclasses.asdict` output; `default` for `Enum`/`Path`) plus the shared `emit_axi_error`, `emit_axi_choice_as_usage_error`, and `exit_for(kind)` helpers; stdout carries only TOON, stderr carries progress. Include a runnable self-check asserting the encoded structure matches the `--json` shape the codebase already emits.
- [x] 4.3 Implement the content-first `cwcli axi` home: `bin:` (abs path, `~`-collapsed), one-line `description:`, live instances via the existing `_list_instances` producer, a `help[N:]` next-steps block, and a definitive empty state.
- [x] 4.4 Implement `cwcli axi backup` (~15-25 lines): call `core.backup`, serialize `BackupOutcome` to TOON on success (exit 0 for `OK`/`WARNING`, else 1), render a raised `CwcliError` as a structured stdout error (exit 2 for `USAGE`, else 1), and render a `NEEDS_CHOICE` result as a usage error naming the exact flag (exit 2).
- [x] 4.5 Unit-test the axi verb + serializer: TOON on stdout for success; no progress text on stdout; typed-error rendering and exit codes; needs-choice -> flag-naming usage error exit 2; the `ls`-DTO home (populated and empty).

## 5. Docs + validation

- [x] 5.1 Add a `README.md` note introducing the `cwcli axi` surface (namespace, content-first home, `backup` verb) and a `CHANGELOG.md` entry (Keep a Changelog format, user-facing only) - landing with the implementation PR.
- [x] 5.2 Keep `uv run mypy src/` at zero errors and `black`/`ruff` clean over `src/`; the new `core/` package and `axi` surface are fully typed with no `# type: ignore`.
- [x] 5.3 Re-run the real-instance backup E2E after the no-mistakes run and after any review fixes (captain standard), confirming both modes still behave end to end on a throwaway instance.

## 6. Deferred - NOT in this change (tracked here so the boundary is explicit)

- [ ] 6.1 DEFERRED: streaming typed event iterators (`Iterator[BackupEvent]` with `ProgressLine`/`Done`/`Failed`) - arrives with the first streaming command (`logs`/`update`; `unlock`'s verbose `exec_start(stream=True)` path).
- [ ] 6.2 DEFERRED: the plan/apply split - arrives per-command with `restore`/`rm`, reusing this change's envelope + needs-choice + typed-error primitives.
- [ ] 6.3 DEFERRED: the AXI cross-cutting shell (SessionStart hook, installable skill, `skills-lock.json`, CI `--check` staleness build) - the narrowed `cwcli-axi-pass-x9` task.
- [ ] 6.4 DEFERRED: migrating every command other than `backup` (including the `restore`/`rm`/`init`/`update` monsters) - one later OpenSpec change and PR per command or small batch, E2E staying green, each gaining AXI conformance as it migrates.
