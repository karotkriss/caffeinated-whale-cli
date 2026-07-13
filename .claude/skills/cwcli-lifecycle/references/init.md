# `init` command internals (sharp edges)

Each note guards a real shipped bug. Keep the root-cause "why" so a later change does not silently re-break the fix.

### `init` command: Frappe/bench version gating

The default Frappe branch is `version-16` (`DEFAULT_FRAPPE_BRANCH` in `commands/init.py`; `--erpnext-branch` defaults to `version-16` to match). Two mutually-exclusive flags pick the ref, resolved BEFORE any port/Docker work so a bad value fails fast:

- `--frappe-branch <ref>` - a raw git branch/tag (e.g. `version-16`, `v16.26.3`, `develop`), passed to `bench init` unchanged.
- `--version <value>` - a shape-resolving alias (`resolve_frappe_ref`): a bare integer `N` -> branch `version-N`; a full SemVer 2.0.0 `X.Y.Z` (`_SEMVER_RE`) -> tag `vX.Y.Z`; anything else (e.g. `16.26`, `latest`) raises `ValueError` -> `Exit(1)`. `_resolve_frappe_branch` rejects passing both flags (`Exit(1)`) and defaults to `DEFAULT_FRAPPE_BRANCH` when neither is given.

Version gating keys on the MAJOR version parsed from the resolved ref via `_frappe_major_version` (handles BOTH `version-16`->16 and the `v16.26.3`->16 tag form; `develop`->`None`), NOT exact branch strings - so a SemVer tag is gated the same as its branch equivalent:

- `bench new-site` MariaDB flag (`_select_mariadb_flag`): major `<= 14` (or a `v14.x.x`/`v13.x.x` tag) -> `--no-mariadb-socket`; else -> `--mariadb-user-host-login-scope=%` (this flag only exists in bench/Frappe 15+; passing it to bench 14 makes `bench new-site` fail). `develop`/unknown -> the 15+ flag.
- Python version (`branch_python`, keyed by int: 15->3.12, 14->3.10, 13->3.9), Node major (`branch_node`, keyed by int: 14->16, 13->14), and `setuptools<82` pinned only when major == 13. version-16 uses the container-default Python/Node (no dict entry).

Regression coverage: `tests/test_init_frappe_version.py` (resolver shapes, mutual exclusion, default, major extraction, and an end-to-end capture that the resolved ref reaches the real `bench init` command) and the tag-form cases in `tests/test_init_mariadb_flag.py`.

#### `init` existing-bench flow: decline must continue, not dead-end

The devcontainer image ships a `/workspace/frappe-bench`, so a fresh `cwcli init` usually finds an already-existing bench at the default `bench_parent/bench_name`.
`_resolve_bench_target(frappe_container, bench_parent_path, bench_name, reuse_bench=None)` in `init.py` owns this branch and returns `(bench_name, bench_full_path, bench_exists)`.

The tri-state `reuse_bench: bool | None` flag (`init`'s `--reuse-bench/--no-reuse-bench`, default `None`) pre-answers the existing-bench question so the command is fully agent-drivable (issue #41).
When the bench exists, the resolver short-circuits BEFORE the interactive `while bench_exists` loop:
- `reuse_bench is True` (`--reuse-bench`): reuse with no prompt, return `bench_exists=True` (`bench init` skipped), exactly like an interactive Yes.
- `reuse_bench is False` (`--no-reuse-bench`): refuse to reuse; print an error naming the path and advising a different `--bench`, then `raise typer.Exit(1)`. It must NEVER enter the rename loop (a non-interactive loop would spin forever).
- `reuse_bench is None` and `sys.stdin.isatty()`: the unchanged interactive issue #20 loop below.
- `reuse_bench is None` and NOT a TTY: refuse with an honest `Exit(1)` naming both flags, BEFORE touching questionary. This replaces the old non-TTY hang (idle pipe) / `EOFError` crash (closed stdin); `.ask()` only catches `KeyboardInterrupt`, so the guard must be `sys.stdin.isatty()` checked up front.
When the bench does NOT exist the flag is irrelevant (returns `bench_exists=False`); `--no-reuse-bench` with a fresh name is a useful no-op that just asserts freshness ("create a NEW bench or fail").

Interactive loop (only reached when `reuse_bench is None` AND a TTY): "Reuse the existing bench ...?": Yes reuses it (`bench_exists=True`, `bench init` skipped); No prompts for a different bench name and loops, so site setup continues on a fresh bench (`bench_exists=False`).
A blank replacement name or a cancelled prompt (`.ask()` returns `None`) exits cleanly with code 0 and "No changes made." - this exit-0 interactive-cancel is the DELIBERATE issue #20 behavior; only the NON-TTY path (where "no changes" is a failure to do the requested job) exits 1.
This replaced the old behavior where declining reuse just `raise typer.Exit(0)` and aborted the whole command (issue #20).
The resolver runs after the setup spinner has exited, so its prompts own the terminal; keep it out of any `console.status`/`TipSpinner` block.
`inputs.bench_name` is reassigned from the resolver's return (so `bench init` and the site paths use the chosen name), which is valid because `InitInputs` is a plain mutable `@dataclass`.

Spinner-race fix (issue #41): `ensure_containers_running` used to be called INSIDE init's `TipSpinner` with `prompt=True`, so a slow container start with no `--auto-start` could paint a questionary confirm under the live spinner (the repo's known deadlock pattern).
Now `_wait_for_containers_running(project, ...)` runs a bounded SILENT poll (`ensure_containers_running(..., prompt=False, auto_start=False)`, ~10 x 0.5s) INSIDE the spinner - safe because it never prompts and absorbs normal startup latency (a container is often ~200ms from ready right after `compose up -d`, so a single check mis-reads it as down).
Only if that returns `False` does init call `ensure_containers_running(..., auto_start=auto_start)` (which prompts on a TTY or refuses on a non-TTY) OUTSIDE the spinner, then fetches the frappe container. `--reuse-bench` and `--auto-start` are separate axes (bench reuse vs container start); do not merge them.
Regression coverage is in `tests/test_init_reuse_bench.py` (flag/non-TTY resolver cases in `TestReuseBenchFlag`, poll behavior in `TestWaitForContainersRunning`).

### `init` secret handling: env-transport + admin-password generation

`bench new-site` needs two secrets - the admin password and the MariaDB root password - and both used to sit inline on the command's argv AND be echoed verbatim in `-v` mode (`init.py`).
The fix mirrors `restore.py`'s M5 pattern exactly (do NOT reinvent, do NOT use `cmd.replace(secret, "***")` masking):

- `_exec_in_container` gained an `environment: dict | None = None` param forwarded to `exec_create` (precedent already existed in the same file's pyenv step).
- `new_site_cmd` references the secrets as unexpanded `"$CWCLI_ADMIN_PASSWORD"` / `"$CWCLI_DB_ROOT_PASSWORD"` and supplies the values via `environment={...}`; the in-container `bash -lc` shell expands them at exec time. Non-secret interpolations (site name, path, mariadb flag) keep `shlex.quote`.
- **Admin password lifecycle:** generated with `secrets.token_urlsafe(18)` (helper `_generate_admin_password`) ONLY when `--admin-password` is omitted; used VERBATIM when supplied (no strength/non-empty check by captain decision - bench is the only backstop). A non-interactive session (`_is_interactive_session()` = both stdin AND stdout TTYs) with no `--admin-password` REFUSES up front (`Exit(1)`) rather than generate a secret into a captured log; an interactive run generates one and prints it ONCE.
- **The print is gated on `admin_password_generated and not site_exists`** (`init.py` success block). `bench new-site` is skipped when the site already exists, so an idempotent re-run sets NO password; printing a fresh one there would be a lie. Never echo a user-SUPPLIED password back.
- **`db_root_password` is NOT randomized** - its default `"123"` is coupled to the downloaded compose's hardcoded `MYSQL_ROOT_PASSWORD: 123` (`_customize_compose_ports` never rewrites it); it only gets the off-argv/off-echo env treatment.

Sharp edge (verified by real-instance E2E, do NOT re-assert a false guarantee): the env-transport keeps the secret off cwcli's `-v` echo and off the `bash -lc` wrapper argv / docker exec `Cmd` record, but it does NOT hide it from `docker top` of the LEAF process.
`bench new-site` accepts the password only as a flag, so the shell expands `$CWCLI_ADMIN_PASSWORD` into the child `frappe new-site --admin-password <plaintext>` python process's argv, which `docker top`/`/proc/<pid>/cmdline` show for the ~1-2 min the site is being created.
This residual exposure is inherent to bench's flag-only interface (bench reads no env/stdin password channel) and is identical to `restore`'s; it is out of scope to "fix" (would need a bench change). The real, testable win is the echo/log hygiene.
Regression coverage: `tests/test_init_admin_password.py` (env-ref not literal in the command, secrets ride in `environment=`, supplied-verbatim, generator non-empty/distinct/shell-safe, non-interactive refusal, print-once gated on site creation).
