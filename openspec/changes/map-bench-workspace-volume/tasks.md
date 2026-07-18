## 1. Compose rewrite (core)

- [ ] 1.1 Thread `bench_parent` into `init_instance` (stage 1) so the compose mount can target it; the frontend passes the same value it already passes to `init_bench`.
- [ ] 1.2 In the existing port/image customization pass, rewrite the frappe service's workspace mount from `- ..:/workspace:cached` to a named volume mounted at the resolved `--bench-parent`, and add the volume to the top-level `volumes:` block (materializes as `{project}_workspace`). Leave `mariadb-data` untouched.
- [ ] 1.3 Keep the rewrite behind the pre-existing `if not compose_path.exists()` guard so existing instances are never rewritten.
- [ ] 1.4 (Optional tidy) set the frappe service `working_dir` to `--bench-parent` so it is valid for a non-`/workspace` parent; cwcli itself does not rely on it (execs `cd` explicitly).

## 2. Mount point as an instance property

- [ ] 2.1 On a re-init of an existing instance, read the instance's mounted parent from its frozen compose file (the frappe workspace mount target).
- [ ] 2.2 If `--bench-parent` does not match that mounted parent, raise `CwcliError(USAGE)` naming the mounted parent; a matching or default `--bench-parent` proceeds unchanged.

## 3. Frontends

- [ ] 3.1 `commands/init.py`: pass `bench_parent` to `init_instance` as well as `init_bench`; no new user-facing flag.
- [ ] 3.2 `commands/axi.py` (`axi init`): same pass-through; no new flag; TOON output unchanged.

## 4. Tests

- [ ] 4.1 Characterization test committed green against pre-change HEAD, pinning that today's compose customization touches only ports and image (no `volumes:` change), so the delta is visible.
- [ ] 4.2 Core test: `init_instance` with a custom `--bench-parent` produces a compose whose frappe service mounts the named workspace volume at that path and declares it in `volumes:`.
- [ ] 4.3 Core test: `init_instance` on an existing instance does NOT rewrite the frozen compose (backward-compat boundary).
- [ ] 4.4 Core test: a re-init with a mismatched `--bench-parent` raises `CwcliError(USAGE)` naming the mounted parent; a matching one proceeds.
- [ ] 4.5 `rm` test: assert `{project}_workspace` is enumerated by `get_project_volumes` and removed by `_remove_named_volumes` with no `rm` code change (label-based discovery).
- [ ] 4.6 Keep every existing init and rm suite green unchanged.

## 5. E2E (real instance, both version legs as applicable)

- [ ] 5.1 Fresh `cwcli init` (default `/workspace`): bench + site provision; `docker compose down` then `up`; confirm the bench and its `{bench}/logs/*.supervisor.log` survived on the workspace volume.
- [ ] 5.2 Fresh `cwcli init --bench-parent /opt/benches`: confirm the bench persists across a container recreation (the bug this fixes) and that `cwcli start`/`status`/`logs` see it.
- [ ] 5.3 `cwcli rm` on a new instance removes both `{project}_workspace` and `{project}_mariadb-data`, with the verified-backup gate intact.
- [ ] 5.4 Regression: an instance created before this change (bind mount) still inits benches, supervises, and rm-cleans exactly as before.
- [ ] 5.5 Validate BOTH interactive and non-interactive init paths per the captain standard (pty for prompts; flags for non-TTY).

## 6. Docs and memory

- [ ] 6.1 `README.md`: make the "workspace volume" wording literally accurate for new instances; document that the bench workspace is now a named volume and note the host-file-access change (pending captain's Decision 4).
- [ ] 6.2 CLAUDE.md ledger + `cwcli-lifecycle` skill: record the workspace-volume wiring and the `--bench-parent`-is-the-mount-point semantics.
- [ ] 6.3 `docs/e2e/`: add the persistence-across-recreation evidence.

## 7. Captain decision gate (blocks implementation)

- [ ] 7.1 Confirm Decision 4: named volume (recommended, loses host file access) vs re-targeted bind mount (keeps host access, keeps socket fragility). Implementation follows the approved option; the rest of the design is identical either way.
