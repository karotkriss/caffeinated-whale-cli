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

- [x] 5.1 Fresh `cwcli init` (default `/workspace`): bench + site provision; `docker compose down` then `up`; confirm the bench and its `{bench}/logs/*.supervisor.log` survived on the host `{project}/data/`. Automated: `tests/e2e/test_workspace_persistence_e2e.py::test_default_parent_persists_bench_across_recreation` (reuses the session instance; also asserts the host-visible marker proving the bind mount).
- [x] 5.2 Fresh `cwcli init --bench-parent /opt/benches`: confirm the bench persists across a container recreation (the bug this fixes) and that `cwcli start`/`status`/`logs` see it. Automated: `::test_custom_bench_parent_persists_and_rm_removes_data` (status shows the custom parent; `axi logs` reads it; marker survives `down`/`up`).
- [x] 5.3 `cwcli rm` on a new instance removes the `{project}/data/` bench workspace (with the project dir). Automated: same test asserts the host project/data dir is gone after `cwcli rm --volumes`. (`mariadb-data` removal and the verified-backup gate are covered by the existing rm E2E; unchanged here.)
- [x] 5.4 Regression: a pre-existing frozen-compose instance (old whole-project `..:/workspace` mount) is neither rewritten nor allowed a mismatched re-init. Automated: `::test_preexisting_frozen_compose_is_not_rewritten_and_mismatch_refused` (real binary; USAGE refusal naming `/workspace`; compose byte-unchanged; no containers started). A matching re-init proceeds - covered by the unit `TestWorkspaceMount` frozen-compose case.
- [x] 5.5 BOTH modes per the captain standard: the mount change adds NO new prompt, so interactive init is unchanged and stays covered by `test_init_e2e`'s pty test; the new persistence tests drive the non-interactive path with flags.

## 6. Docs and memory

- [x] 6.1 `README.md`: make the "workspace volume" wording accurate - the bench workspace is a host bind mount from `{project}/data/`, with direct host access.
- [x] 6.2 CLAUDE.md ledger + `cwcli-lifecycle` skill: record the workspace bind-mount wiring and the `--bench-parent`-is-the-mount-point semantics.
- [x] 6.3 Persistence-across-recreation evidence added as the automated `tests/e2e/test_workspace_persistence_e2e.py` (the CI-run harness that codifies the manual `docs/e2e/` recipe), running on the v16 leg of `e2e.yml`.

## 7. Captain decision gate (settled)

- [x] 7.1 Decision 4 settled by the captain: host bind mount from `{project}/data/` (preserves host file access), NOT a named volume. Confirmed on the r&d review surface 2026-07-18.
