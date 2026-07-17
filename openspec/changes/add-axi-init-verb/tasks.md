## 1. The verb

- [ ] 1.1 Add an `axi_init` command to `commands/axi.py` taking `project` plus `--port`, `--bench`, `--site`, `--bench-parent`, `--frappe-branch`, `--version`, `--db-root-password`, `--admin-password`, `--reuse-bench/--no-reuse-bench`, `--install-erpnext`, `--erpnext-branch` (no `--verbose`, no `--auto-start`).
- [ ] 1.2 Resolve the admin password from `CWCLI_ADMIN_PASSWORD` env var then `--admin-password` (flag wins if both); refuse with a USAGE error (exit 2) naming both when neither is set. Never generate, never prompt.
- [ ] 1.3 Resolve the MariaDB root password from `CWCLI_DB_ROOT_PASSWORD` env var then `--db-root-password`, defaulting to `123`.
- [ ] 1.4 Render the `--frappe-branch` / `--version` mutual-exclusion as a frontend USAGE error (exit 2); resolve `--version` via `core.resolve_frappe_ref`, mapping its USAGE error through `emit_axi_error` (exit 2).
- [ ] 1.5 Call `core.init_instance` then `core.init_bench` in sequence; do NOT loop to re-invoke a stage (an unresolved choice is an error and the process exits).
- [ ] 1.6 Emit the final `InitReport` as ONE TOON document on stdout via `emit_result`, folding in warnings.

## 2. Progress narration (design question 1)

- [ ] 2.1 Wire `core.init`'s `on_event` to a stderr narrator that writes `InitStepStart.message` and `InitNotice.text` only; drop `InitOutput` and `InitTrace`. No secret is ever emitted.
- [ ] 2.2 Document in the verb's help/docstring that it blocks until done, emits one terminal document, and that `cwcli logs <project>` / `cwcli status <project>` give live/deeper progress from a second shell.

## 3. Choice surfaces become non-prompting errors (design question 2 continued)

- [ ] 3.1 Add a `confirm_reuse_bench` branch to `emit_axi_choice_as_usage_error` naming `--reuse-bench` / `--no-reuse-bench`; render an unresolved `confirm_reuse_bench` as exit 2.
- [ ] 3.2 Render `confirm_start` (stage-1 timeout or stage-2 race) as an operational error (exit 1) with the init-tailored message pointing at `cwcli status` / `cwcli logs`; do not re-invoke with `auto_start=True`.
- [ ] 3.3 Confirm the port conflict (`CwcliError(CONFLICT, "ports.in_use")`) and `bench.exists` conflict flow through the generic `emit_axi_error` to exit 1 with their existing hints; no special handling needed.

## 4. Exit codes

- [ ] 4.1 Exit 0 on `OK`/`WARNING`; exit 1 on any operational `CwcliError` and the `confirm_start` cases; exit 2 on any USAGE error and the unresolved `confirm_reuse_bench`.

## 5. Registry, skill, docs

- [ ] 5.1 Flip `tests/test_core_init.py:test_axi_registry_has_no_init_command` from an absence to a presence assertion; update its docstring to record the verb shipped.
- [ ] 5.2 Regenerate `skills/cwcli/SKILL.md` via `scripts/build_skill.py`; confirm `tests/test_axi_skill.py` (the `--check` gate) passes.
- [ ] 5.3 Flip the CLAUDE.md `init` ledger entry from "deliberately NO `axi init`" to a one-line description of the shipped verb and its two design-question resolutions.
- [ ] 5.4 Update `README.md` (axi verb list / "Making agents aware of the surface"), the `cwcli-lifecycle` and `cwcli-core-axi` skills, and `tests/README.md`.

## 6. Tests

- [ ] 6.1 Add `tests/test_axi_init.py`: admin password via env var and via argv (flag wins), missing-password USAGE exit 2, malformed `--version` exit 2, `--frappe-branch` + `--version` exit 2.
- [ ] 6.2 `confirm_reuse_bench` -> exit 2 naming both flags; `--no-reuse-bench` on an existing bench -> exit 1; `confirm_start` -> exit 1 with the tailored message; port conflict -> exit 1 with the `--port` hint.
- [ ] 6.3 Success emits one TOON `InitReport` on stdout with stdout kept pure (no narration on stdout); the stderr narrator carries no secret.
- [ ] 6.4 A soft provisioning failure surfaces as `WARNING` -> exit 0 with the warning in the TOON warnings block.

## 7. E2E

- [ ] 7.1 Real-instance E2E leg: a full agent-driven non-interactive `cwcli axi init` provisions a genuine bench + site, in both password transports, on an isolated throwaway instance; teardown after.
