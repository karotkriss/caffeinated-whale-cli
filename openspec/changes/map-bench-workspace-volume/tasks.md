## 1. Compose rewrite (core)

- [x] 1.1 Thread `bench_parent` into `init_instance` (stage 1) so the compose mount can target it; the frontend passes the same value it already passes to `init_bench`.
- [x] 1.2 In the fresh-compose customization pass, rewrite the frappe service's workspace mount from `- ..:/workspace:cached` to `- ../data:{bench_parent}:cached` (host `{project}/data/` -> container `{bench_parent}`) and create the host `{project}/data/` directory. Leave `mariadb-data` and the `volumes:` block untouched (no new named volume).
- [x] 1.3 Gate the workspace-mount and `working_dir` rewrites on the fresh-download branch (`new_instance`) so an existing instance's frozen compose is never re-targeted; the ports/image rewrites stay unconditional (idempotent no-ops on an already-customized compose).
- [x] 1.4 Set the frappe service `working_dir` to `{bench_parent}` (replacing upstream `/workspace/development`) so it stays inside the mounted `data/` subtree for any parent and is never a root-owned dir.

## 2. Mount point as an instance property

- [x] 2.1 On a re-init of an existing instance, read the instance's mounted parent from its frozen compose file (the frappe workspace mount target) via `_mounted_bench_parent`, matching both the old `..:/workspace:cached` and new `../data:{parent}:cached` shapes.
- [x] 2.2 If `--bench-parent` does not match that mounted parent, raise `CwcliError(USAGE)` naming the mounted parent; a matching or default `--bench-parent` proceeds unchanged.

## 3. Frontends

- [x] 3.1 `commands/init.py`: pass `bench_parent` to `init_instance` (both the initial and the auto_start re-invoke) as well as `init_bench`; no new user-facing flag.
- [x] 3.2 `commands/axi.py` (`axi init`): same pass-through; no new flag; TOON output unchanged.

## 4. Tests

- [x] 4.1 Core test: `init_instance` with default and custom `--bench-parent` produces a compose whose frappe workspace mount resolves to `../data:{bench_parent}:cached` and whose `working_dir` is `{bench_parent}`.
- [x] 4.2 Core test: the `volumes:` block is unchanged (only `mariadb-data:`, no new named volume) and the host `{project}/data/` dir is created.
- [x] 4.3 Core test: `init_instance` on an existing instance does NOT re-target the frozen workspace mount (backward-compat boundary).
- [x] 4.4 Core test: a re-init with a mismatched `--bench-parent` raises `CwcliError(USAGE)` (`bench_parent.mismatch`) naming the mounted parent; a matching one proceeds.
- [x] 4.5 Keep every existing init suite green unchanged (updated the `init_reuse_bench` stub to accept the new `bench_parent` kwarg).

## 5. E2E (real instance - CI owns the matrix, not run locally)

- [ ] 5.1 Fresh `cwcli init` (default `/workspace`): bench + site provision; `docker compose down` then `up`; confirm the bench and its `{bench}/logs/*.supervisor.log` survived on the host `{project}/data/`.
- [ ] 5.2 Fresh `cwcli init --bench-parent /opt/benches`: confirm the bench persists across a container recreation (the bug this fixes) and that `cwcli start`/`status`/`logs` see it.
- [ ] 5.3 `cwcli rm` on a new instance removes the `{project}/data/` bench workspace (with the project dir) and `{project}_mariadb-data`, with the verified-backup gate intact.
- [ ] 5.4 Regression: an instance created before this change (whole-project bind mount) still inits benches, supervises, and rm-cleans exactly as before.
- [ ] 5.5 Validate BOTH interactive and non-interactive init paths per the captain standard (pty for prompts; flags for non-TTY).

## 6. Docs and memory

- [ ] 6.1 `README.md`: make the "workspace volume" wording accurate - the bench workspace is a host bind mount from `{project}/data/`, with direct host access.
- [ ] 6.2 CLAUDE.md ledger + `cwcli-lifecycle` skill: record the workspace bind-mount wiring and the `--bench-parent`-is-the-mount-point semantics.
- [ ] 6.3 `docs/e2e/`: add the persistence-across-recreation evidence.

## 7. Captain decision gate (settled)

- [x] 7.1 Decision 4 settled by the captain: host bind mount from `{project}/data/` (preserves host file access), NOT a named volume. Confirmed on the r&d review surface 2026-07-18.
