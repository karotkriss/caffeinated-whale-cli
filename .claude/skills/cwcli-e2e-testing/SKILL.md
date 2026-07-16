---
name: cwcli-e2e-testing
description: >
  End-to-end testing protocol for cwcli against real Frappe/ERPNext Docker instances - the
  isolation recipe (CWCLI_HOME or a temp HOME, the cwe2e- project prefix, dedicated
  volumes/network), building genuine v14/v15/v16 benches instead of fixtures, driving interactive
  prompts through a real pty (the ESC[?2004h raw-mode marker), driving non-interactive paths with
  flags, teardown, and the tests/e2e/harness.py contract. Use this whenever you write, run, or
  extend E2E tests (anything under tests/e2e/), touch tests/e2e/harness.py, or hand-validate a
  cwcli behavior change on a real instance - even if the task just says "test it on a real
  instance" without naming E2E.
metadata:
  internal: true
---

# cwcli end-to-end testing

This is the detailed protocol for validating cwcli against genuine throwaway Frappe instances.
The standing captain-standards that motivate it (support+test BOTH modes; re-run E2E after no-mistakes/review fixes; full-lifecycle validation of dangerous-delete work) live in the always-loaded `AGENTS.md` "Captain standards" section - read those too when the change is a prompting or destructive-delete command.

There is now an automated, CI-run E2E harness in `tests/e2e/` (the `e2e` / `e2e_p2p` marker tier) that codifies the manual recipe below; it drives the real `cwcli` binary against genuine throwaway Frappe instances, on a v14/v15/v16 matrix (`.github/workflows/e2e.yml`).
It is being filled in per command (see the open change `openspec/changes/rebuild-e2e-test-suite`); the harness contract - the `cwe2e-` name prefix, the `CWCLI_HOME` isolation seam plus temp `HOME`, the hard rails (`enforce_isolation`, name prefix) and the unconditional `sweep_cwe2e` backstop, the port allocator, real-readiness waits, and the `pexpect`/`ESC[?2004h` helper - lives in `tests/e2e/harness.py`; read `tests/README.md` before extending it.
The manual `docs/e2e/` worked runs remain the human-precedent reference the harness was built from (canonical examples: `docs/e2e/restore-inspect-e2e-r6.md`, `docs/e2e/bench-ux-m7-multi-bench.md`, `docs/e2e/restore-noninteractive-h2.md`).
When validating a behavior change by hand, follow the same procedure and point back to those runs.

1. **Isolate everything.**
   Set `CWCLI_HOME` (preferred - relocates only cwcli's own state via `config_utils.cwcli_home()`, leaving `HOME` and HOME-derived tooling like git/ssh untouched) or a temporary `HOME` (isolates cwcli plus everything else that reads `HOME`) to a fresh throwaway directory so `~/.cwcli` is never touched, use unique docker-compose project names (for example `cwe2e-<something>`), and dedicated volumes/network.
   NEVER touch the captain's real projects or real `~/.cwcli`.
2. **Build genuine benches.**
   Use `cwcli init` + `bench init` to create real benches, not fixtures.
   Build BOTH Frappe `version-14` and `version-15` when the behavior is version-sensitive; some bugs only reproduce on v14 (for example the receive-mode bare-filename `Invalid path` bug, which v15's alternative-directory fallback masks).
3. **Drive interactive prompts through a real pty (pexpect).**
   Await prompt_toolkit's raw-mode readiness marker `ESC[?2004h` before each keystroke so nothing races the prompt.
   For a confirm-then-prompt flow, press `y` THEN Enter, the way a human types it, so the confirm consumes its own trailing Enter (see the `auto_enter=False` credential note in the `cwcli-lifecycle` skill (references/restore.md)).
4. **Drive non-interactive paths with flags.**
   Use `--yes`/`-y`, `--mariadb-root-username`/`--mariadb-root-password`, `--site`, and the backup selectors (`--latest`/`--backup-file`).
   A non-TTY without the needed flag must refuse with a non-zero exit, not hang or silently proceed.
5. **Tear down all throwaway instances afterward** (containers, volumes, network, temp `HOME`), and confirm the captain's real instances are untouched.
