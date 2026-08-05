# `init` command internals (sharp edges)

Each note guards a real shipped bug. Keep the root-cause "why" so a later change does not silently re-break the fix.

Since `migrate-init-core` (batch 9, 2026-07-16) the logic lives in `core/init.py` as TWO sequential plain functions - `init_instance` (project dir, compose, the workspace bind mount, containers up + the bounded readiness poll) then `init_bench` (bench resolve, version gating, the 10 provisioning execs on `core.exec_stream`, cache clear) - and `commands/init.py` is a renderer over them.
The seam sits at init's one mid-flow user decision (the existing-bench question, which needs a running container and fires on the common interactive path), so resolving `confirm_reuse_bench` re-invokes only stage 2's subsecond probes, never the compose orchestration.
Every note below survived the migration; its enforcement point is named per note.

### `init` command: the workspace bind mount (the ephemeral-bench fix)

`map-bench-workspace-volume` (2026-07-18): the upstream compose only ever bind-mounted the WHOLE project dir at the hardcoded `/workspace` (`- ..:/workspace:cached`), so `bench init` at `{--bench-parent}/{--bench}` persisted on the host only when `--bench-parent` was left at its default `/workspace`; any custom `--bench-parent` landed outside that mount, in the container's ephemeral layer, and evaporated on the next `docker compose up` (a fresh container has a fresh layer). `init_instance` (`core/init.py`) now rewrites the frappe service's workspace mount to `- ../data:{bench_parent}:cached` (host `{project}/data/`, created by `init_instance` so Docker never materializes it root-owned) and sets `working_dir` to `{bench_parent}`, so the mount covers any `--bench-parent`, not just the default, and `working_dir` always sits inside the mounted subtree instead of a root-owned dir outside it. That rewrite targets `working_dir: /workspace` - the string the unconditional root-owned-dir fix below has ALREADY normalized `/workspace/development` down to, moments earlier in the same function - not the raw upstream `/workspace/development`; an initial version matched the raw upstream string directly and was a silent no-op for every custom `--bench-parent` (the prior fix had already consumed it before this one ran), fixed same-day once `working_dir: /workspace` turned up still sitting outside a custom mount.

This is a BIND mount, not a named volume - a deliberate choice (captain-approved 2026-07-18) to keep bench files directly browsable/editable on the host; the accepted caveat is that the supervisor's unix socket then sits on the host filesystem backing `CWCLI_HOME`, solid on the default native-Linux `~/.cwcli` and only fragile if `CWCLI_HOME` is relocated onto a networked filesystem. `mariadb-data` and the compose `volumes:` block are untouched - no new named volume.

Two things guard real damage here: (1) **the rewrite is gated on `new_instance`** (the fresh-download branch) - an EXISTING instance's compose is frozen and never re-targeted, so a re-init against an already-running instance cannot silently change where its data lives out from under it; the ports/image rewrites stay unconditional as before (idempotent no-ops on an already-customized compose). (2) **a re-init mount MISMATCH is a `CwcliError(USAGE, "bench_parent.mismatch")`**, not a silent ephemeral-bench recreation: `_mounted_bench_parent` reads the frappe workspace mount's container target back out of the frozen compose (a regex matching `- ..<anything>:{target}:cached`, so it recognizes BOTH the old whole-project shape and the new `../data` shape) and, when a supplied `--bench-parent` disagrees with it, raises before touching any container, naming the mounted parent so the fix (drop the flag, or match it) is obvious; both frontends surface that hint on stderr (`commands/init.py:_render_error_exit` and the `axi` error renderer). `core.remove` needs no change: deleting the project directory already cleans `{project}/data/` along with `conf/`.

The mount rewrite alone is not enough to make a custom `--bench-parent` resolve correctly: `status`/`run`/`restart`/`restore` all resolve the bench path from cwcli's SQLite cache, and `init_bench` only CLEARS that cache (a stale entry is worse than none) - nothing else repopulates it, so an empty cache falls back to the hardcoded default bench path, which only happens to match a bench built under the DEFAULT `--bench-parent`. Both frontends recache immediately after a fresh bench is created - `commands/init.py:_refresh_cache` (human CLI) and the equivalent `cache.recache_project` call in `commands/axi.py:axi_init` - each degrading a recache failure to a stderr-only warning (never failing init, since the bench itself already succeeded, and never reaching `axi`'s stdout, preserving its one-TOON-document contract).

Regression coverage: `tests/test_core_init.py::TestWorkspaceMount` (default/custom `--bench-parent` rewrite, `working_dir` at the mount root, the unchanged `volumes:` block, host `data/` creation, the frozen-compose non-rewrite, the re-init mismatch/match cases); `tests/test_init_characterization.py::TestBenchParentMismatch` (the mismatch hint reaching the human CLI's stderr); `tests/test_axi_init.py::TestRecache` (the post-init recache success/failure-degrades-to-warning/never-reaches-stdout cases).

### `init`/`start`: host-uid alignment for the workspace bind mount (the CI `cwcli rm` permission-denied root cause)

Bench commands exec as the frappe/bench image's DEFAULT `frappe` user (uid 1000), so every file `bench init`/`bench build`/etc. write to the `../data` bind mount above land host-owned by uid 1000.
A dev box is usually also uid 1000, so this was invisible there; a CI runner is uid 1001, so the host user cannot recurse into those 755-dirs to delete them and `cwcli rm` failed with `[Errno 13] Permission denied` on `data/frappe-bench`.
The fix is at the SOURCE, not a reclaim/chown-at-`rm` band-aid: on hosts that expose `os.getuid`, `core.docker.align_container_user_to_host(container, *, chown_home=False)` remaps the container's `frappe` user to the HOST uid/gid as root (the frappe devcontainer's own `updateRemoteUserUID` trick), so every file the aligned user writes afterward is already host-owned on any host uid.
The gid change is `groupmod -o -g <gid> frappe`, which only rewrites the account databases and never walks the filesystem.
The uid change is a direct `sed` edit of `/etc/passwd`'s uid field, never `usermod -u`, because shadow-utils recursively changes ownership beneath the user's home as a side effect.
The direct edit preserves the account state while avoiding an unnecessary walk and overlayfs copy-up of the baked toolchain.
Platforms without `os.getuid`, such as Windows, return the existing clean no-op `(False, None)` before probing the container.
This capability check deliberately preserves remapping on macOS, while Docker Desktop's bind-mount ownership handling makes the remap unnecessary on Windows.

The two steady-state call sites have deliberately different costs.
`init_bench` calls it with `chown_home=True` BEFORE any provisioning exec (right after the reuse-bench decision, before `_ensure_directory`) so the remapped user's pyenv/nvm/pip installs can write their required home paths.
That repair is narrowed to the specific paths a first provision writes (`_CHOWN_HOME_RECURSIVE_DIRS`/`_CHOWN_HOME_SHALLOW_DIRS` in `core/docker.py`), and the baked pyenv/nvm toolchain beneath them is never recursively re-owned.
`core.start` calls it on every launch WITHOUT `chown_home`, so a matching identity stays a no-op while a container recreation performs the cheap account database edits and the same narrowed writable-path repair.
The writable-path repair is required when the uid changes because every later login shell runs `pyenv rehash` and must be able to rewrite the existing shims.
This re-alignment happens before supervisord writes its per-process logs to the host-owned bench.
A whole-stack restart already routes through `core.start`, and `run`/`apps` are out of scope by that same precedent.
`core.scale` has one additional conditional caller for old-major toolchain repair after a container recreation; `references/scale.md` owns that specialized path.
All callers share identity no-op fast paths: a platform without `os.getuid` skips the container probes entirely, while a capable host uses `_read_frappe_id` to read the container's current `id -u`/`id -g frappe` and skips the account remap when the ids match.
When `chown_home=True`, the narrowed provisioning-path ownership repair still runs even if the ids already match.
Neither case emits a warning.

Best-effort, never a hard failure: `align_container_user_to_host` returns `(remapped, failure)`, never raises.
On a capable host, an unreadable uid/gid or a failed remap exec is a soft warning surfaced as `InitNotice(code="init.uid_align_failed")` on init (`commands/init.py` renders it as an unconditional yellow warning, alongside `yarn.install_failed`/`setuptools.pin_failed`) and `Message("start.uid_align_failed", ...)` on start; the bench still builds/starts owned by the original uid - no worse than before this fix existed.
`commands/start.py` was fixed IN THE SAME BATCH to render `start.uid_align_failed` unconditionally rather than gated behind `--verbose` - a pre-existing sibling bug (`bench.default_used` had the identical gating mistake, unlike every other bench-op frontend's precedent) was fixed alongside it, since both signal the workspace may not behave as expected, not a verbose diagnostic.
`axi init`/`axi start` need no special-case code: both already fold every `result.warnings` entry into their one TOON document via the generic `emit_result(..., warnings=...)`.

Regression coverage: `tests/test_core_docker.py` (the no-op path for matching ids without home repair, matching ids with the narrowed home repair, the platform no-op without `os.getuid`, uid/gid remapping, root-user exec, and failures degrading to soft warnings); the `id -u`/`id -g frappe` probe fake plumbing added to `tests/test_core_init.py`/`tests/test_core_supervision.py`'s `FakeContainer` reports the host's own ids.

### `init` phases must announce themselves BEFORE they run, not just on completion (`fm/cwcli-init-silent-longphase`)

The alignment phase used to recursively re-own all of `/home/frappe`, but it now repairs only provisioning write paths and normally completes in a few seconds.
It remains a blocking Docker exec at the boundary between init's two stages, so it must announce itself before it starts.

The fix brackets the call in `core/init.py:init_bench` with the existing `InitStepStart` and `InitStepEnd` event family.
The start event uses phase `"align_uid"` and carries the user-facing progress message.
`commands/init.py` registers `"align_uid"` in `_SPINNER_GROUP` as its own group between stage 1 and `bench_init`, with its own `_group_label`, so the non-verbose spinner opens with the correct label as soon as the phase starts.
Verbose mode's `_on_start` also has a generic `elif event.message: stderr_console.print(...)` fallback.
Every phase that carries a message and is not specially formatted (`customize_ports`, `resolve_bench_tag`/`pull`, or the streamed `_LONG_PHASES`) is therefore announced without another phase-specific renderer branch.

The same completion-only shape existed in `_install_pyenv_python` (which can spend minutes compiling CPython from source) and `_install_nvm_node`.
Each already had an `InitNotice(code="python.installing"/"node.installing")` at the start, but nothing kept a spinner alive or updated during the install itself.
Both now use the same `InitStepStart`/`InitStepEnd` bracket, with phases `"python_install"` and `"node_install"`, and each has its own spinner group.

**The durable rule for any future blocking phase in `core/init.py`:** emit `InitStepStart(phase=..., message=...)` immediately before the blocking call, and register the phase in `commands/init.py:_SPINNER_GROUP`.
Add a `_group_label` entry when the phase deserves its own group instead of inheriting the current one.
An `InitTrace` or `InitNotice` emitted only after success is insufficient.
Keep completion output when it is useful for showing phase duration, but never make it the only progress signal.

Regression coverage: `tests/test_init_silent_phase_announcements.py` proves that the `align_uid` and `python_install` start events precede their faked blocking calls.
It also proves that non-verbose rendering opens the `align_uid` spinner on stage 2's first event, that `python_install` gets its own live spinner, and that verbose rendering prints the `align_uid` start message before its completion trace.

### `init` command: Frappe/bench version gating

The default Frappe branch is `version-16` (`DEFAULT_FRAPPE_BRANCH` in `core/init.py`; `--erpnext-branch` defaults to `version-16` to match). Two mutually-exclusive flags pick the ref, resolved BEFORE any port/Docker work so a bad value fails fast:

- `--frappe-branch <ref>` - a raw git branch/tag (e.g. `version-16`, `v16.26.3`, `develop`), passed to `bench init` unchanged.
- `--version <value>` - a shape-resolving alias (`core/init.py:resolve_frappe_ref`): a bare integer `N` -> branch `version-N`; a full SemVer 2.0.0 `X.Y.Z` (`_SEMVER_RE`) -> tag `vX.Y.Z`; anything else (e.g. `16.26`, `latest`) raises `CwcliError(USAGE)` with the same message the old `ValueError` carried. The flag fusion and the passing-both error stay in `commands/init.py:_resolve_frappe_branch` (flag UX belongs to one frontend; `Exit(1)`), and it defaults to `DEFAULT_FRAPPE_BRANCH` when neither is given.

Version gating keys on the MAJOR version parsed from the resolved ref via `core/init.py:_frappe_major_version` (handles BOTH `version-16`->16 and the `v16.26.3`->16 tag form; `develop`->`None`), NOT exact branch strings - so a SemVer tag is gated the same as its branch equivalent:

- `bench new-site` MariaDB flag (`_select_mariadb_flag`): major `<= 14` (or a `v14.x.x`/`v13.x.x` tag) -> `--no-mariadb-socket`; else -> `--mariadb-user-host-login-scope=%` (this flag only exists in bench/Frappe 15+; passing it to bench 14 makes `bench new-site` fail). `develop`/unknown -> the 15+ flag.
- Python version (`_BRANCH_PYTHON`, keyed by int: 15->3.12, 14->3.10, 13->3.9), Node major (`_BRANCH_NODE`, keyed by int: 14->16, 13->14), and `setuptools<82` pinned only when major == 13. version-16 uses the container-default Python/Node (no dict entry).
- The pyenv/nvm/yarn provisioning helpers stay BUFFERED `exec_run` calls (they block to completion and read real exit codes); their soft failures ride `Result.warnings` plus `InitNotice` events, never a hard stop - a failed pyenv install falls through to the container default exactly as before.

Regression coverage: `tests/test_init_frappe_version.py` (resolver shapes, mutual exclusion, default, major extraction, the resolved ref crossing the frontend->core seam), `tests/test_core_init.py:TestVersionGating` (the ref-to-command construction, tag-form gating, the v13 setuptools pin, soft-fail warnings), and the tag-form cases in `tests/test_init_mariadb_flag.py`.

#### `init` existing-bench flow: decline must continue, not dead-end

The devcontainer image ships a `/workspace/frappe-bench`, so a fresh `cwcli init` usually finds an already-existing bench at the default `bench_parent/bench_name`.
`core.init_bench` owns this branch: it probes the bench path (buffered `test -d`) and honors the tri-state `reuse_bench: bool | None` (`init`'s `--reuse-bench/--no-reuse-bench`, default `None`), which pre-answers the existing-bench question so the command is fully agent-drivable (issue #41).
When the bench exists, the core decides BEFORE any bench work or search-path registration:

- `reuse_bench is True` (`--reuse-bench`): reuse with no prompt, `InitReport.bench_created=False` (`bench init` skipped), exactly like an interactive Yes.
- `reuse_bench is False` (`--no-reuse-bench`): `CwcliError(CONFLICT, "bench.exists")` with the old message naming the path and advising a different `--bench`; the frontend renders it and exits 1. It NEVER enters the rename loop (a non-interactive loop would spin forever).
- `reuse_bench is None`: `NEEDS_CHOICE` with the `confirm_reuse_bench` kind (`param="reuse_bench"`, the bench path riding in `options`). The FRONTEND then resolves it (`commands/init.py:_resolve_reuse_choice`): on a TTY the unchanged interactive issue #20 loop below; on a non-TTY the honest exit-1 refusal naming both flags, BEFORE touching questionary - this replaced the old non-TTY hang (idle pipe) / `EOFError` crash (closed stdin); `.ask()` only catches `KeyboardInterrupt`, so the guard must be `sys.stdin.isatty()` checked up front.

When the bench does NOT exist the flag is irrelevant (`bench_created=True` after a real `bench init`); `--no-reuse-bench` with a fresh name is a useful no-op that just asserts freshness ("create a NEW bench or fail").

Interactive loop (only reached when `reuse_bench is None` AND a TTY): "Reuse the existing bench ...?" (`auto_enter=False`): Yes re-invokes `init_bench` with `reuse_bench=True` (`bench init` skipped); No prompts for a different bench name (`_bench_name_validation`, the questionary inline validator, stays frontend) and re-invokes with the new name and `reuse_bench=None` - an also-existing name surfaces the choice AGAIN, so the rename loop is the natural fixpoint of re-invocation, each round costing a container resolve plus two subsecond probes.
A blank replacement name or a cancelled prompt (`.ask()` returns `None`) exits cleanly with code 0 and "No changes made." - this exit-0 interactive-cancel is the DELIBERATE issue #20 behavior; only the NON-TTY path (where "no changes" is a failure to do the requested job) exits 1.
This replaced the old behavior where declining reuse just `raise typer.Exit(0)` and aborted the whole command (issue #20).
`add_custom_path` runs in the core AFTER the decision, on every proceeding outcome (never on cancel), and its added/already-present outcome rides an `InitNotice` the frontend renders as the old stdout line - the old `add_path` COMMAND import (the last frontend-calling-frontend edge) is dead.

Spinner-race fix (issue #41), now structural: the core cannot prompt at all, so the readiness poll (`core/init.py:_wait_for_running`, ~10 x 0.5s over `resolvers.resolve_container_state(auto_start=False, offer_choice=False)` with the `NOT_RUNNING` raise caught per attempt) is silent by construction - it absorbs normal startup latency (a container is often ~200ms from ready right after `compose up -d`, so a single check mis-reads it as down).
On timeout `init_instance` returns `confirm_start` (with `auto_start=False`) and the frontend runs `ensure_containers_running(..., auto_start=<--auto-start>)` - which prompts on a TTY or refuses on a non-TTY - only AFTER the renderer's spinner has closed, then re-invokes ONCE with `auto_start=True`; a second failure is the core's typed `NOT_RUNNING` raise (fail closed, the structural cap, never a loop). `--reuse-bench` and `--auto-start` are separate axes (bench reuse vs container start); do not merge them.
The `auto_start=True` re-invoke skips the port-conflict check (`core/init.py:init_instance`): by that point `ensure_containers_running` has already started THIS project's own containers, which bind exactly the ports being checked, so re-running the check would misreport a self-conflict and abort a start that just succeeded.
Regression coverage: `tests/test_core_init.py` (the tri-state matrix, the choice surfaces, the bounded poll, the auto_start=True fail-closed cap, the self-conflict skip), `tests/test_init_reuse_bench.py` (the frontend prompt loop, prompts-after-spinner-close, the non-TTY refusal), `tests/test_init_characterization.py` (both refusals end-to-end at the command level).

### `init` secret handling: env-transport + admin-password generation

`bench new-site` needs two secrets - the admin password and the MariaDB root password - and both used to sit inline on the command's argv AND be echoed verbatim in `-v` mode.
The fix mirrors `restore.py`'s M5 pattern exactly (do NOT reinvent, do NOT use `cmd.replace(secret, "***")` masking), and the migration moved it byte-exactly onto the exec-stream contract:

- `core.init_bench` builds `new_site_cmd` referencing the secrets as unexpanded `"$CWCLI_ADMIN_PASSWORD"` / `"$CWCLI_DB_ROOT_PASSWORD"` and supplies the values via `exec_stream(container, ["bash", "-lc", cmd], environment={...})` - the parameter the contract grew specifically to unblock this batch; the in-container `bash -lc` shell expands them at exec time. Non-secret interpolations (site name, path, mariadb flag) keep `shlex.quote`.
- **The event surface is audited and pinned by test** (`tests/test_core_init.py:TestExecOrderAndSecrets`): no `InitOutput`/`InitNotice`/`InitTrace` event, no `Result.warnings` entry, and no `InitReport` field carries either secret value; the command-echo trace (`InitTrace(code="exec.command")`) carries the `$`-refs and never the values; `InitReport` has NO password field at all - the frontend generated or received the password, so it needs nothing back.
- **Admin password lifecycle stays FRONTEND** (TTY-coupled secret UX): generated with `secrets.token_urlsafe(18)` (`commands/init.py:_generate_admin_password`) ONLY when `--admin-password` is omitted; used VERBATIM when supplied (no strength/non-empty check by captain decision - bench is the only backstop). A non-interactive session (`_is_interactive_session()` = both stdin AND stdout TTYs) with no `--admin-password` REFUSES up front (`Exit(1)`) rather than generate a secret into a captured log; an interactive run generates one and prints it ONCE.
- **The print is gated on `admin_password_generated and report.site_created`** - the old probed local (`not site_exists`) expressed on the returned fact. `bench new-site` is skipped when the site already exists, so an idempotent re-run sets NO password; printing a fresh one there would be a lie. Never echo a user-SUPPLIED password back.
- **`db_root_password` is NOT randomized** - its default `"123"` lives IN THE CORE (`init_bench`'s parameter default) because the coupling it mirrors, the downloaded compose's hardcoded `MYSQL_ROOT_PASSWORD: 123`, lives in the compose file the core itself downloads; it only gets the off-argv/off-echo env treatment.

Sharp edge (verified by real-instance E2E, do NOT re-assert a false guarantee): the env-transport keeps the secret off cwcli's `-v` echo and off the `bash -lc` wrapper argv / docker exec `Cmd` record, but it does NOT hide it from `docker top` of the LEAF process.
`bench new-site` accepts the password only as a flag, so the shell expands `$CWCLI_ADMIN_PASSWORD` into the child `frappe new-site --admin-password <plaintext>` python process's argv, which `docker top`/`/proc/<pid>/cmdline` show for the ~1-2 min the site is being created.
This residual exposure is inherent to bench's flag-only interface (bench reads no env/stdin password channel) and is identical to `restore`'s; it is out of scope to "fix" (would need a bench change). The real, testable win is the echo/log hygiene.
Regression coverage: `tests/test_init_admin_password.py` (env-ref not literal in the command, secrets ride in `environment=`, supplied-verbatim, generator non-empty/distinct/shell-safe, non-interactive refusal, print-once gated on `report.site_created`) and the Decision 3 audit in `tests/test_core_init.py`.

### `init` exec honesty: the exit-code fail-open is closed by the contract

The old `_exec_in_container` read `exec_inspect` ONCE with no poll through the dead `.get("ExitCode", 1)` default; a dropped connection during a 10-minute `bench init` (which `CancellableStream` swallows into a clean-looking EOF) or a clean EOF racing the daemon's code recording read `ExitCode: None` and printed the dishonest `Command failed with exit code None`.
All 10 provisioning execs now flow through `core.exec_stream` (bench init, 4 set-configs, new-site, 2 final configs, the ERPNext pair - the audited count), so the exit code is the contract's honest, bounded-polled one, and a lost stream raises the typed `CwcliError(DOCKER, "exec.stream_lost")` - the batch's one disclosed error-path hardening (both paths exit non-zero).
The ENOSPC hint ("No space left on device inside the container...") fires on BOTH consumption modes: a drained exec scans its full joined output, and a live-rendered one (`stream_output=True`, the verbose long execs) scans a retained 8KB tail of the streamed chunks (`_run_exec` in `core/init.py`) - restoring it there was the batch-3 audit's recorded non-item, since fixed (disk exhaustion is most likely during exactly the long streaming `bench build` where the hint used to be dead). The tail is bounded by slicing, never unbounded buffering.
The buffered probe/install helpers (pyenv, nvm, yarn, setuptools pin, `test -d`, `mkdir -p`) keep buffered `exec_run` argv calls; streaming was never their behavior.

### `init` compose `working_dir`: avoid a daemon-created root-owned host path (`fm/cwcli-pytest-root-tmp-p3`)

The downloaded devcontainer compose sets `working_dir: /workspace/development` - a path cwcli never creates (the bench lands at `/workspace/frappe-bench`). `/workspace` is a bind mount to `CWCLI_HOME/projects/<name>/`, so on `compose up` the DOCKER DAEMON (root, not the container process) creates that missing `working_dir` inside the bind mount as `root:root` - regardless of the container's `--user`. Matching the container uid to the host does NOT fix this, since the daemon creates the path, not the container process. Under the E2E harness, whose `CWCLI_HOME` sits inside pytest's `tmp_path` (shared `/tmp`), the leaked root-owned dir defeated a non-root `rmtree`/`cwcli rm` (unlink needs write on the parent) and re-broke a bare `pytest` for the next user on the box.
`core/init.py:init_instance` repoints it to `working_dir: /workspace` (the mount root, which always exists) before writing the compose file, so the daemon never creates anything there - this also makes a raw `docker exec -it <container> bash` (no explicit `-w`) land where cwcli actually puts the bench instead of a dead vestigial path.
Defense-in-depth for the harness itself lives in `tests/e2e/harness.py:reclaim_root_owned` (see the `cwcli-e2e-testing` skill) in case a future compose default reintroduces a similar daemon-created path.
Regression coverage: `tests/test_core_init.py` (`working_dir: /workspace` present, `/workspace/development` absent, in the written compose).

### `init` post-create dev-services auto-start (`fm/cwcli-init-autostart`)

After the bench+site is created, `cwcli init` and `cwcli axi init` both start the bench's dev services by default (`--start`/`--no-start`, default `--start`) rather than leaving a created-but-idle bench.
This is deliberately a FRONTEND epilogue, not a third `core/init.py` stage: both renderers call the UNCHANGED `core.start(project, bench_path=report.bench_path)` (the same primitive behind `cwcli start`/`cwcli axi start`) with the report's exact `bench_path`, so it never re-resolves the bench or forks to a multi-bench `select_bench` choice - the bench that was just created is the only one in play.
A start failure (any exception, not just `CwcliError`) degrades to a stderr warning and does NOT change `init`'s exit code or outcome: the bench was already created successfully, so a supervisor-launch failure is not an `init` failure - the human CLI prints `Dev services are not running for '<project>'.` and points at `cwcli start`; `axi init` prints the same warning to stderr and still emits its one TOON `InitReport` document unchanged (`InitReport` carries no services-running field).
`core.start` itself now blocks until the web server binds the bench's assigned web port (see the web-readiness note in the
`cwcli-lifecycle` skill's `start-status.md` reference) before it reports success, so `_start_services` threads
that honest `web_ready` signal back through: a launch that succeeds but times out waiting for that port still
prints `Dev services are not running for '<project>'.` (never the "running" success block) even though the
containers and supervisor did come up, because the web genuinely isn't serving yet.
The success block resolves the site's host URL from that assigned container port and Docker's live published binding, so a custom `--port` and every later bench advertise the address that is actually reachable from the host.
If either half cannot be read, it omits the address rather than guessing.
`--no-start` skips the call entirely (for automation/CI that wants a created-but-idle bench); it is orthogonal to `--auto-start`/container startup, which stage 1 always performs regardless of this flag.

### `init_instance` skips `compose pull`/`up -d` against an already-running instance (`fm/cwcli-bench-add-silently-stops-benches`)

`init_instance` used to run `docker compose pull` then `docker compose up -d` unconditionally on EVERY `cwcli init` call, including one that only adds a bench to an already-running instance (`cwcli init existing --bench second ...`).
`pull` re-fetches every image, including the downloaded devcontainer compose's UNPINNED `redis:alpine` tag and MariaDB's own tag (only the frappe image is pinned to a resolved semver by `init_instance` itself); without `--no-deps`/`--force-recreate`, `up -d` silently RECREATES any container whose freshly-pulled image no longer matches what is running.
For the frappe service that kills supervisord and every bench's process tree running under it, with nothing in the report to say so - a bench add is the ordinary, supported way to grow a multi-bench instance, and running concurrent tasks against ONE instance (each owning a bench) is the captain-directed norm, so this landed on the everyday path.
The trigger needs real upstream image drift between the instance's creation and the later `cwcli init` call (not reproducible on demand in a hermetic test), but the underlying operation - unconditionally re-pulling and `up -d`-ing an already-running instance's containers just to add a bench - was never necessary: bench provisioning happens entirely over `docker exec` in stage 2.
`core.init._running_compose_services` (distinct from the older, coarser `_project_containers_running` the port check uses) now gates these commands.
When this project's own frappe container and all three dependency services are confirmed running, `init_instance` skips pull and up entirely and emits an `instance.already_running` notice.
The human renderer shows that notice in verbose mode instead of silently dropping it.
When frappe is running but MariaDB or either Redis service is stopped, init still skips the image pull and starts only the missing siblings with `docker compose up -d --no-deps <services>`.
This recovers the dependency without allowing Compose to touch or recreate frappe.
A stopped frappe beside a running MariaDB or Redis still gets the normal pull and whole-stack up path.
Regression coverage: `tests/test_core_init.py::TestInitInstance` pins the healthy skip, partial-stack recovery, and stopped-frappe paths; `tests/test_init_characterization.py::TestAlreadyRunningNotice` pins the human verbose notice; `tests/e2e/test_init_bench_add_preserves_running_e2e.py` proves on real Docker that a bench add leaves the frappe container's id and `StartedAt` byte-identical and the first bench still answering, with no manual restart in between.

### `init` downloads a compose template with no ports block, and a fresh instance must still get one (`fm/cwcli-init-silent-portless-instance`)

The CONFIRMED root cause of the field incident (three CI runs that exited 0, printed "Successfully initialized", and produced instances with ZERO Docker port mappings, with no "already in use" text anywhere in the logs): the upstream `frappe_docker` devcontainer compose template stopped publishing development ports - it now expects an editor's `devcontainer.json` to forward them instead. cwcli runs the downloaded compose file directly with `docker compose up -d`, so once the template's `ports:` block disappeared upstream, the existing port-customization string replacement had nothing left to replace - a silent no-op - and `compose_path.write_text(content)` wrote out a compose file with NO port mappings at all. `docker compose up -d` succeeds on that file (there is nothing to bind, so nothing can conflict), so `init` completes normally and reports success on a container that is genuinely healthy INSIDE Docker but unreachable from the host - explaining every symptom at once: no conflict message (there was never a conflict to detect), exit 0, and a reliable CI recurrence (an ephemeral runner always downloads the CURRENT upstream template) alongside a clean interactive reproduction (an existing local `~/.cwcli` project directory whose compose file predates the upstream change short-circuits the download via `compose_path.exists()`, so a captain's manual repro against a pre-existing project never re-downloads the now-broken template).

`init_instance` (`core/init.py`) now detects when the downloaded template has NEITHER the web nor the socketio mapping after the usual replace (`web_mapping not in content and socketio_mapping not in content`) and, for a fresh instance only, inserts an explicit `ports:` block after `working_dir:` with cwcli's computed mappings. As a fail-closed backstop, if either mapping is STILL missing after that insertion attempt (an unanticipated future upstream shape), `init_instance` raises `CwcliError(PRECONDITION, "compose.ports_missing", ...)` rather than ever silently writing a portless compose file.
Regression coverage: `tests/test_core_init.py::TestInitInstance::test_portless_upstream_compose_gets_explicit_host_mappings` pins the live portless upstream shape (fixture `COMPOSE_UPSTREAM_PORTLESS`, matching the real template's current content) and the generated mappings; the real-Docker `tests/e2e` matrix downloads the actual live upstream template on every CI run, so a future upstream shape change surfaces here rather than shipping silently again.

### `init`'s port pre-check also retries a transient conflict, a separate hardening under the same incident (`fm/cwcli-init-silent-portless-instance`)

Also field-confirmed, independently of the root cause above: a just-removed instance's own port bindings can take a moment to fully release because of Docker's `docker-proxy` teardown lag.
A CI script that purges an instance and re-runs `init` within seconds can therefore see the same ports it just freed as still in use.
This is a genuine, transient conflict, not a bug in the availability check itself.
`check_ports_in_use` already detects a real listener regardless of interactivity, and both the pre-check and Docker's own Compose bind fail loudly for a persistent conflict.

`core.init_instance`'s port check (`core/init.py:_ports_still_in_use`) retries up to `_PORT_CHECK_ATTEMPTS` (5) times, one second apart, before raising.
An `InitNotice(code="ports.retry")` narrates each retry.
Both the human renderer and `cwcli axi init` surface that notice unconditionally so a caller knows the command is retrying.
A port still occupied after the full budget raises `CwcliError(CONFLICT, "ports.in_use", ...)` regardless of TTY.
Its hint names both remedies: wait and retry for a transient hold, or select a different range with `--port`.
The decision lives only in `core.init_instance`, so `cwcli init` and `cwcli axi init` share the same behavior.

Regression coverage: `tests/test_core_init.py::TestInitInstance::test_transient_port_conflict_clears_within_the_retry_budget` and `test_persistent_port_conflict_still_raises_after_the_retry_budget` pin the retry count, actionable hint, and loud failure after the budget.
`tests/e2e/test_init_e2e.py::test_noninteractive_init_refuses_loudly_on_an_occupied_port` proves against real Docker that a genuinely occupied socketio-range port causes a non-interactive run to exit nonzero and leave no containers behind.
Regression coverage: `tests/test_init_characterization.py::TestAutoStartServices` (the human CLI: exact `bench_path` passthrough, the running/not-running/`--no-start` completion messages, the failure-degrades-to-warning-not-exit case) and `tests/test_axi_init.py::TestAutoStartServices` (axi parity: exit 0 on a start failure, the stderr warning, `--no-start`).
