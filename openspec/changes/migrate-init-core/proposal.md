## Why

`init` is batch 9: the largest un-migrated command (1292 lines) and the exec-stream contract's ONE remaining non-consumer, deferred to "its own batch" precisely because both `bench new-site` secrets ride its execs (`core/exec_stream.py:175` already supports `environment=` so this migration is unblocked).
Three costs of its un-migrated state, all measured at current HEAD:

**1. init holds the contract's fail-open class in its own variant.**
`_exec_in_container` (`init.py:192-197`) reads `exec_inspect` ONCE with no poll, through the dead `.get("ExitCode", 1)` default the contract's docstring names.
`exec_inspect` returns `{"ExitCode": None, "Running": True}` while an exec runs, and `CancellableStream` swallows a dropped connection into a clean-looking end of stream, so a lost connection during a 10-minute `bench init` - or a clean EOF that races the daemon's code recording - reads `ExitCode: None` and reports `Command failed with exit code None`.
That is fail-closed by accident with a dishonest message: for the lost-stream case the command may still be running and succeed, and for the EOF race the command already succeeded.
init also still holds its own copy of the per-stream decode loop (`init.py:173-185`) that `exec_stream` superseded for every other consumer; after this batch `utf8_stream_decoder`'s only consumer is the contract itself.

**2. A live frontend-calling-frontend edge survived batch 7.**
`init.py:50` imports `add_path` - a Typer COMMAND - from `commands/config.py` and calls it mid-flow (`init.py:306,351`), printing another command's stdout inside init.
Batch 7 killed this class at all seven core-layer edges; this command-to-command edge is the same family and the last of it.
The real logic is one `config_utils.add_custom_path` call away.

**3. Every stage is written twice, and the decisions are welded to their rendering.**
Four dual verbose/non-verbose blocks duplicate the flow (`init.py:921-970`, `1061-1083`, `1154-1178`, `1201-1254`); the validators hand-roll print-and-exit (`init.py:68-106`) next to a typed-error architecture; the existing-bench decision is a prompt loop interleaved with container probes (`init.py:270-352`) next to an envelope that carries exactly that decision as a `Choice`.

## What Changes

- **`core/init.py`, TWO sequential plain functions** - the batch's first-class shape finding, disclosed up front rather than forced into a prior batch's mold (design Decision 1):
  `core.init_instance(project_name, *, port=8000, auto_start=False, on_event=None) -> Result[InstanceUp]` (project dir + compose download + port/image customization + pull + up + the bounded readiness poll), and
  `core.init_bench(project_name, *, bench_name, site_name, bench_parent, frappe_ref, db_root_password, admin_password, reuse_bench=None, install_erpnext=False, erpnext_branch, auto_start=False, on_event=None) -> Result[InitReport]` (bench resolve + version gating + `bench init` + configs + `bench new-site` + ERPNext + cache clear).
  The seam sits exactly where init's one mid-flow user decision lives: the existing-bench question needs a running container (stage 1's own product) and fires on the COMMON interactive path (the devcontainer image ships `/workspace/frappe-bench`), so a one-call shape would re-run host setup, a Docker Hub query, `compose pull`, and `compose up` on every interactive default init just to answer "reuse it?".
  Neither prior two-call motivation applies (no generator laziness, nothing destructive to preview); this is a third, disclosed motivation and it does NOT reopen plan/apply.
- **The secret env-transport moves byte-exactly onto the contract**: `exec_stream(container, ["bash", "-lc", cmd], environment={"CWCLI_DB_ROOT_PASSWORD": ..., "CWCLI_ADMIN_PASSWORD": ...})` with the command string referencing unexpanded `$CWCLI_*` exactly as today (`init.py:1133-1152`).
  No event, DTO, warning, or error detail may carry a secret the old path did not print: `ExecChunk` carries the same bench output today's stream writes raw, the command-echo trace carries the `$`-refs and never the values, and `InitReport` carries no password field at all.
  Admin-password generation, the interactive-session gate, and the print-once stay in the frontend (they are TTY-coupled UX); the `docker top` residual exposure is inherited unchanged and stays out of scope.
- **The 10 `_exec_in_container` execs** (bench init, 4 set-configs, new-site, 2 final configs, 2 ERPNext) become `exec_stream` consumers - verbose renders each `ExecChunk` raw (carriage returns preserved), non-verbose drains under the spinner, and the exit code is the contract's honest, polled one.
  The buffered probe/install helpers (pyenv, nvm, yarn, setuptools pin, `test -d`, `mkdir -p`) keep buffered `exec_run` argv calls, the `backup`/`unlock` precedent - streaming was never their behavior.
- **Version gating moves verbatim**: `resolve_frappe_ref` (its `ValueError` becomes `CwcliError(USAGE)` with the same message), `_frappe_major_version`, `_select_mariadb_flag`, the Python/Node/setuptools gates, and the pyenv/nvm/yarn provisioning.
  The `--frappe-branch`/`--version` mutual-exclusion error stays frontend (the `apps` fused-`--yes` / `open` four-flag precedent: flag UX belongs to one frontend).
- **Three choice surfaces**: `confirm_start` from stage 1's poll timeout and from stage 2's race backstop (both resolved by the frontend via `ensure_containers_running` exactly as today, capped fail-closed), and the NEW `confirm_reuse_bench` kind (bench exists, `reuse_bench=None`); the CLI renders it as today's reuse prompt plus the rename loop, re-invoking stage 2 (cheap: subsecond probes) per round, and on a non-TTY as today's refusal naming both flags, exit 1.
  `--no-reuse-bench` against an existing bench is `CwcliError(CONFLICT)` - the kind whose docstring already anticipated init ("port conflict, existing bench"), as is the port-conflict check.
- **The `add_path` edge dies**: the core calls `config_utils.add_custom_path` and emits the outcome as an event; `commands/init.py` no longer imports `commands/config.py`.
- **`commands/init.py` thins to a renderer**: the project-name prompt, password generate-or-refuse, flag fusion, spinners driven by typed events (collapsing the four dual blocks), choice resolution, elapsed time, and the success/password block.
- **There is deliberately NO `axi init` verb in this batch, and the absence is asserted** (design Decision 9): unlike `axi open` (structurally impossible) this is a deferral, not a refusal - whether an agent may create instances (gigabytes of images, host state, a required secret on the agent's argv, a 10-20 minute single-document wait) is a product decision the captain owns on its own evidence, decoupled from the rework's largest refactor PR.
  The core work makes the verb thin whenever it is decided; a test pins the registry absence so "deferred" can never read as "forgotten" (the `apps install`/`uninstall` precedent).

## Impact

- **New:** `src/caffeinated_whale_cli/core/init.py`, `tests/test_core_init.py`, the no-`axi init` assertion, `tests/test_init_characterization.py` (committed green against unmigrated HEAD first).
- **Changed:** `commands/init.py` (renderer), the four existing init suites (`test_init_reuse_bench.py`, `test_init_admin_password.py`, `test_init_frappe_version.py`, `test_init_mariadb_flag.py` - pure helpers re-point with their subject; the resolver-loop tests split by design into core-choice tests and frontend-prompt tests, named in tasks), CLAUDE.md ledger, `cwcli-lifecycle` (`references/init.md`) + `cwcli-core-axi` skills, `docs/technical/README.md` module list, `tests/README.md` coverage map.
- **Unchanged:** every user-visible behavior (version gating, secret transport, existing-bench flow, prompts, exit codes, idempotent re-run), `core/exec_stream.py`, `tests/e2e/test_init_e2e.py` (must stay green unchanged), the cache schema, the `axi` surface (no new verb), `skills/cwcli/SKILL.md` (no verb change, so no regeneration).
- **Zero new primitives, as a falsifiable claim**: `exec_stream(environment=)` was built for this batch; `get_frappe_container`, `resolve_container_state(offer_choice=True/False)`, the envelope, and the `OnEvent` idiom cover the rest; `confirm_reuse_bench` is a new value in `Choice.kind`'s open token set (the `select_editor` precedent), not a primitive change.
  Two flat spots are REPORTED rather than absorbed silently (design Decision 10): the closed `ErrorKind` set has no network kind, so the compose-download and Docker Hub failures map to `PRECONDITION` rather than widening the closed set; and init is the first core verb that shells out to a host CLI (`docker compose`) - `core/version.py`'s detached subprocess is the precedent, and the captured-vs-inherited verbose drift is disclosed in design Decision 6.
