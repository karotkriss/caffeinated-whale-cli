## Context

This design implements two prior measurements rather than re-deciding them:

- **`cwcli-open-handover-design-o9`** (captain-endorsed; its correction is the CLAUDE.md ledger's `open` entry): `open` is not wholly a handover command - the `--docker` branch consumes the calling process contract through `exec_into_container`, while the other three editor branches return normally.
  The settled shape is `core.open_plan(...) -> Result[LaunchTarget]`, the frontend performs the handover: `RunPlan` minus `run_stream`, no new pattern, no new locked decision.
- **`cwcli-inspect-recon-i7` Q8** (re-verified against this HEAD): `open`'s only two logic edges are the no-cache fallback populate and the in-memory `--app` freshness pass, both re-pointed at `core.inspect` / `core.partial_refresh` by batch 7.
  Nothing else blocks it; it was sequenced immediately after inspect, no interleaving.

Every line reference below was re-verified against the current HEAD (`7af4419`) before being relied on.

### Audit: what `open` actually is at HEAD (357 lines)

| Lines | What | Migrates? |
| --- | --- | --- |
| `71-76` | Mutual exclusion of the four editor flags | **No.** Flag-fusion UX; stays frontend (the `apps` fused-`--yes` precedent) |
| `79` | `ensure_containers_running(require_running=True, auto_start=yes)` | **No.** The interactive prologue; stays frontend exactly as `run.py:56` |
| `85` | `resolve_bench_path(...)` (the CLI wrapper) | **Yes.** Becomes `resolvers.resolve_bench` inside the plan |
| `93-114` | Hand-rolled container lookup + frappe filter + two error prints | **Yes.** Becomes `core/docker.py:get_frappe_container` (same messages, typed) |
| `120-127`, `170-177` | Editor detection via `vscode_utils.is_*` (run twice) | **Yes.** Stdlib `shutil.which` inside the core, run once at editor-resolution time |
| `130-177` | The no-cache fallback populate: announce, `core.inspect(refresh="auto")`, re-resolve, default-path degrade, `CwcliError` abort | **Yes.** Core-to-core; the abort/degrade contract is Decision 4 |
| `180-248` | `--app`: cache read, match-by-path, `core.partial_refresh` in-memory freshness, membership check, `{bench}/apps/{app}` | **Yes.** Moves verbatim |
| `250-346` | Editor flag validation (install-URL errors) + the hand-rolled questionary select + non-TTY refusal | **Split.** The decision moves to the core (`select_editor` / `NOT_FOUND`); the prompt and refusal renderings stay frontend |
| `351-357` | The four-way handover switch | **No.** The frontend performs the handover |

The current fallback contract at HEAD (post PR #93, commit `3b3cd57`, pinned by `tests/test_open_inspect_fallback.py`):
a hard `CwcliError` from the fallback inspect ABORTS via `commands/inspect.py:render_error_exit` (exit 1, never the guessed default path); a non-`CwcliError` exception degrades to `/workspace/frappe-bench` with a warning; a populate that succeeds but still resolves nothing degrades likewise.
The recon Q8 line reference (`open.py:159-173`, "trivially expressible over a raising `core.inspect`") predates that fix; this design specifies the post-fix interaction deliberately - see Decision 4.

## Goals / Non-Goals

**Goals.**
Move `open`'s resolve chain (container, run-state, bench, fallback populate, `--app`, editor) onto the core behind `Result[LaunchTarget]`.
Keep the handover in the frontend, where it is the correct mechanism.
Preserve every observable behavior at HEAD, including the merged fallback-abort semantics.
Assert the absence of `axi open` so the exclusion cannot be re-litigated on the stale "not serializable" premise.

**Non-Goals.**
`exec_into_container` and `open_in_vscode` internals (the mechanisms; untouched).
Any `axi open` verb (o9's structural reason: the interactive Docker branch consumes the process that owes `axi` its TOON document).
The `restore`/`rm`/`config`/`init` migrations and `self_update`'s mutating half (the un-migrated list, stated from the code).
Re-working the VS Code extension install flow or the dev-containers URI scheme.
A `--json` for `open` (it has none today; adding surface is not a migration's job).

## Decisions

### 1. The settled shape, implemented not re-decided

`core.open_plan(project_name, *, bench=None, bench_path=None, app=None, editor=None, auto_start=False, on_event=None) -> Result[LaunchTarget]`.

A plain function, per the reference slices' settled reasoning: nothing streams (every probe is a buffered `exec_run`; the launch itself is the FRONTEND's), there is no generator-laziness to split around (the `run_plan`/`run_stream` split exists only because `run`'s phase 2 is a stream - `open` has no phase 2 in the core at all), and `NEEDS_CHOICE` must be returnable at call time for all three forks.
This is NOT plan/apply: nothing here is destructive, and the two-call shapes elsewhere in the core exist for reasons (laziness, streaming) that do not apply.
The module docstring records the boundary the way `core/run.py:1-33` does: the frontend owns the mechanism, the core never execs, and no core function ever hands over a terminal.

### 2. `LaunchTarget` is declarative, and it carries a container NAME

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class LaunchTarget:
    project: str
    container_name: str   # a NAME string; both handover mechanisms consume the name
    working_dir: str      # the bench path, or {bench}/apps/{app} under --app
    editor: str           # "docker" | "code" | "code-insiders" | "cursor"
```

Never an argv: if `LaunchTarget` carried `["docker","exec","-it",...]`, the core would emit `docker` CLI command lines while the rest of the core speaks docker-py, and a GUI would inherit a mechanism it cannot use (a GUI must own its launch behavior rather than consume its own process).
A NAME, not an ID, because `exec_into_container(container_name, ...)` and `open_in_vscode(..., container_name, ...)` both take the name (the vscode-remote URI hex-encodes it), and unlike `run` there is no phase-2 core call needing `core.docker.get_container` to bridge an ID back to a handle.
Serializable throughout; the live container stays internal to the plan call.

### 3. The editor decision moves into the envelope; the flags and prompts stay frontend

- The four boolean flags fuse to one `editor: str | None` param IN THE FRONTEND, and the `sum(editor_flags) > 1` mutual-exclusion error stays there with them: the fusion is one frontend's UX choice (the `apps` fused-`--yes` precedent), and a GUI would never express the editor as four booleans.
- **Detection lands in the core** as stdlib `shutil.which("code"|"code-insiders"|"cursor")` - the `core/version.py` precedent for host-side detection; the purity ban covers `rich`/`questionary`/`typer` only, and the core owns I/O.
  It runs once, at editor-resolution time, which also retires today's pointless double detection (`open.py:120-127` then `:170-177`).
- `editor` given but not installed -> `CwcliError(NOT_FOUND, "editor.not_installed")` carrying today's install-URL hint (`docker` is always valid); an unrecognized value -> `CwcliError(USAGE)`.
- `editor=None` and nothing installed -> `"docker"`, silently, as today (`open.py:304-305`).
- `editor=None` and at least one editor installed -> `NEEDS_CHOICE` with the NEW `Choice(kind="select_editor", param="editor", options=[...])`, options listing the installed editors plus Docker with today's exact labels.
  `Choice.kind` is an open token set (`"select_bench" | "confirm_start" | ...` per `envelope.py`), so this is a new value, not a primitive change.
- The CLI resolves `select_editor` exactly as today: questionary select on a TTY (cancel -> "Operation cancelled.", exit 1), and on a non-TTY the refusal naming `--code`/`--code-insiders`/`--cursor`/`--docker` (exit 1, today's message).
  It then re-invokes `open_plan` with `editor` filled (the doctrine: CLI prompts + re-invokes).
  The second pass resolves from the now-populated cache, so the double resolution costs a handful of cached reads and only on the interactive path; the editor choice arrives AFTER bench/app resolution succeeded, preserving today's error ordering.

Rejected: **editor resolved wholly in the frontend, passed pre-decided** (keeps the hand-rolled prompt/refusal pair the envelope exists to replace, leaves `LaunchTarget.editor` a passthrough field, and leaves a GUI re-implementing not-installed validation); **detection passed in as a parameter** (every frontend must then agree on the probe; `shutil.which` is the probe, and it is stdlib).

### 4. The fallback populate is core-to-core, with the abort/degrade contract specified deliberately

When `resolvers.resolve_bench` returns `None` (nothing cached), `open_plan`:

1. emits an `OpenNotice` event (today's unconditional "No cached bench path found. Running inspect..." line - the frontend renders it un-gated),
2. calls `core.inspect(project_name, refresh="auto", auto_start=auto_start, offer_choice=False)` for its cache side effect, discarding the returned report exactly as the frontend does today,
3. re-resolves via `resolvers.resolve_bench(project_name, bench, None)` - so a freshly-inspected multi-bench project still surfaces `select_bench` rather than silently picking a bench,
4. only if still unresolvable: `DEFAULT_BENCH_PATH` with a `bench.default_used` warning (`run_plan`'s exact warning precedent).

The interaction with the merged fallback-abort decision (PR #93, `tests/test_open_inspect_fallback.py`), preserved:

- **A hard `CwcliError` from the fallback inspect PROPAGATES** - the plan call raises, the frontend renders exit 1, and the guessed default path is never opened.
  This is HEAD behavior, restored by that fix after the batch-7 re-point briefly let typed failures fall into the degrade branch.
- **A non-`CwcliError` exception degrades** to `DEFAULT_BENCH_PATH` with a warning (today's `except Exception` residue, `open.py:161-168`).
  Post batch 7 this branch is nearly dead - `core.inspect` wraps raw docker escapes into `CwcliError(DOCKER)` - so it survives strictly as the belt-and-braces residue it already is, disclosed as such.
- **A populate that succeeds but still resolves nothing degrades** likewise (today's `open.py:151-156`).

**One disclosed hardening**: the fallback inspect runs `offer_choice=False` where today's call leaves the default `offer_choice=True` and then DISCARDS a returned `confirm_start`.
That discard is reachable only when the container stops in the race window after the prologue; today it silently opens the guessed default path against a stopped container (which then fails at the docker level), while under this design it aborts with a typed `NOT_RUNNING`.
Same class as batch 7's disclosed hardenings: an error-path honesty improvement on an otherwise-unreachable window, disclosed in the PR rather than smuggled.

### 5. The `--app` pass moves verbatim

Cache read -> no cached benches is `CwcliError(NOT_FOUND)` naming `cwcli inspect` (today's message); the cached bench is matched BY PATH against the resolved `working_dir` base, never `bench_instances[0]` (`test_inspect_partial_refresh.py:544` pins the wrong-bench bug this guards); `core.partial_refresh` runs in-memory over the cached benches (degrade to the cached list on ANY exception with a trace event, NEVER persists - `:385` pins it); the vanished-bench index shift is re-matched by path; an app absent from `available_apps` is `CwcliError(NOT_FOUND)` listing the available apps; success assembles `working_dir = f"{bench_path}/apps/{app}"`.
No step is added or removed: `open_plan` deliberately resolves exactly what `open` resolves today (the `run_plan` lesson: reaching for adjacent primitives ADDS failures the command does not have).

### 6. No `axi open` verb, now asserted

The absence is deliberate and its reason is structural, not serializability: `LaunchTarget` is four strings and would serialize fine, but the interactive Docker branch consumes the process that owes `axi` its one-TOON-document contract, and the editor branches are meaningless to an agent with no desktop.
A test asserts the `axi` Typer registry has no `open` command (the `axi apps install`/`uninstall` non-verb precedent from `migrate-apps-core` tasks §7), and `core/open.py`'s docstring records the sharper reason so a future agent cannot reopen the question from the stale premise o9 corrected.

### 7. The frontend keeps the prologue, the prompts, the rendering, and the handover

- **Prologue outside the spinner**: `ensure_containers_running(require_running=True, auto_start=yes)` then the capped `confirm_start` once-retry loop, byte-for-byte `run.py:54-89`'s pattern.
- **`select_bench` rendered as `run.py:91-96` renders it**: the error with the bench list plus the "Pass --bench" hint, exit 1.
  Today `open` reaches the same outcome through `resolve_bench_path`'s wrapper rendering; the small wording alignment onto the `run` renderer is disclosed (the `core.backup -v` small-drift precedent).
- **Events**: `OpenEvent = OpenNotice | OpenTrace` mirroring inspect's family - `OpenNotice` renders unconditionally (the fallback-populate announcement), `OpenTrace` renders as `-v`'s `VERBOSE:` lines.
  Warnings render unconditionally (today's yellow "Using default" lines), a per-frontend rendering choice.
- **The handover switch stays in the frontend**: `editor == "docker"` -> `exec_into_container(target.container_name, working_dir=target.working_dir)`, whose docstring owns the POSIX process-replacement and Windows waited-child split; otherwise `vscode_utils.open_in_vscode(target.editor, target.container_name, target.working_dir, verbose=verbose)`, which returns.
- `handle_docker_errors` stays on the command for daemon-unreachable rendering, as on every migrated verb.

### 8. Zero new primitives, as a falsifiable claim

| Need | Existing primitive |
| --- | --- |
| frappe container + typed not-found | `core/docker.py:get_frappe_container` (same messages `open.py:95,104` prints today) |
| run-state + confirm_start race backstop | `resolvers.resolve_container_state(offer_choice=True)` |
| bench precedence / selector / multi-bench choice | `resolvers.resolve_bench` + `DEFAULT_BENCH_PATH` |
| the fallback populate | `core.inspect(refresh="auto", offer_choice=False)` (batch 7's surface) |
| the `--app` freshness pass | `core.partial_refresh` (batch 7's surface) |
| the events | the `OnEvent` callback idiom (`core/inspect.py:160`) |
| editor detection | stdlib `shutil.which` (the `core/version.py` host-side precedent) |

`select_editor` is a new `Choice.kind` token, which the envelope defines as an open set.
If implementation finds a bend, it is reported, per the standing rule.

## Boundary discipline

`core/open.py` imports no `rich`, no `questionary`, no `typer`; `tests/test_core_envelope.py`'s existing import ban covers it automatically.
It never prints, prompts, exits, or execs; the diagnostic trace is typed events.
No live Docker object crosses the return boundary; the container resolved inside the plan stays internal (it feeds `partial_refresh` as a parameter, which remains allowed).
The platform-specific `exec_into_container` handover stays in `utils/docker_utils.py`, called only by the frontend.

## Risks / Trade-offs

- **`open` has no dedicated test file, so there is no suite to move under the migration** -> characterization-first (batch 4's discipline): the existing incidental coverage is pinned green at base, and new characterization tests pin the currently-untested behaviors (editor flag validation, non-TTY refusal, default-path degrade, error ordering) through surfaces that survive the migration, committed before anything moves.
- **The real interactive shell cannot be exercised in-process** -> the frontend tests mock `exec_into_container` and assert its arguments alongside direct `LaunchTarget` assertions; the mechanism's platform split is unit-tested in `tests/test_docker_utils.py`, while a live shell remains E2E work.
- **Double resolution on the interactive editor path** -> the re-invoke reads a populated cache; bounded, cheap, interactive-only, and it preserves today's prompt-last ordering, which a prompt-first prologue would visibly change.
- **Message drift where hand-rolled prints become typed renders** -> messages pinned by existing tests stay byte-identical; the `select_bench` wording alignment onto the `run` renderer is the one named drift, disclosed.
- **Deleting the dead `vscode_utils` helpers could break an unseen caller** -> gated on grep proving zero callers, as a named task; `select_vscode_editor` already has none at HEAD.

## Migration Plan

`tasks.md` order: characterization pinned green first, then `core/open.py` under `tests/test_core_open.py`, then the frontend reseated and existing suites re-pointed (patch targets move with their subject, assertions untouched), then the no-`axi open` assertion and the dead-helper removal, then E2E in both modes on one throwaway instance, then docs and ledger.

## Open Questions

None blocking. Two recorded:

1. **A future GUI's handover** is the consumer this shape serves; nothing to build now.
2. **`axi run`'s absence** (noted in passing by o9) is a real agent-surface gap, unrelated to `open`; not this batch's.
