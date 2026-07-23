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
The authoritative validation policy lives in `docs/testing/guide.md#gate-policy`, and the always-loaded `AGENTS.md` "Captain standards" section applies it to prompting and destructive-delete commands.
CI owns post-validation E2E runs; use the manual recipe below only for one development instance when the behavior cannot otherwise be observed.

There is now an automated, CI-run E2E harness in `tests/e2e/` (the `e2e` / `e2e_p2p` / `e2e_pkg` marker tier) that codifies the manual recipe below; it drives the real `cwcli` binary against genuine throwaway Frappe instances, on a v14/v15/v16 matrix (`.github/workflows/e2e.yml`).

**Adding an E2E test: pick its CI group.** Each version leg runs as two jobs, split by the `standalone` marker.
A test that requests `session_instance` or `running_instance` belongs to the `shared` group and takes NO marker; a test that builds its own instance (via `port_allocator`) or needs no instance at all takes `@pytest.mark.standalone`.
`tests/e2e/conftest.py` checks this at collection and FAILS the run if the marker and the fixtures disagree either way, so a wrong choice is caught immediately rather than quietly making a job do the wrong work.
The split is what keeps the workflow's wall clock near half a leg: the `standalone` job never pays the session `cwcli init`, and the groups are isolated by being separate runners with separate Docker daemons, not by anything inside the suite.

**A shared-group test MUST restore whatever it mutates**, in a `finally`. This is the suite's oldest convention (`_ensure_serving` restores the running state "order-independently"; `test_workspace_persistence_e2e` leaves the instance healthy "regardless of collection order") and nothing enforces it, so it rots silently.
The app-install E2E once left an app on the shared site, and the shorter split exposed scheduler self-exits that could land inside a later sibling-process PID assertion.
Cleanup prevents that mutation from contaminating the rest of the job, but it does not eliminate the underlying scheduler problem: post-fix shared jobs still recorded 2 self-exits on v14, 1 on v15, and 2 on v16.
Those counts carry no timestamps, so attributing the remaining exits to the app-install window would be an inference.
Why the bench can report an app installed while the scheduler cannot import it remains undiagnosed.
When a shared-instance test fails on state it did not set up, look for an earlier test that mutated and did not restore, and do NOT reach for a longer settle or sleep: after a stop/start every supervised program has uptime 0 and a shared run stops the instance 6-14 times, so a threshold big enough to hide it costs 9-20 minutes per job.
`{bench}/logs/.cwcli-supervisord.log` is the tool for this - it distinguishes a program that self-exited (`exit status N`) from one that was signalled (`terminated by SIGxxx`), which is usually the whole question.
It is being filled in per command (see the open change `openspec/changes/rebuild-e2e-test-suite`); the harness contract - the `cwe2e-` name prefix, the `CWCLI_HOME` isolation seam plus temp `HOME`, the hard rails (`enforce_isolation`, name prefix) and the unconditional `sweep_cwe2e` backstop, a root-owned-path reclaim (`reclaim_root_owned`, run before a session's temp `HOME` is deleted so a Docker-daemon-created root-owned path can never survive teardown into shared `/tmp`), the port allocator (`CWE2E_PORT_BASE` override, since the harness also runs on operators' own machines), the `CWCLI_BIN` override to aim the harness at any binary (a runtime-deps-only `uv tool install .` build for the `e2e_pkg` leg below), real-readiness waits, and the `pexpect`/`ESC[?2004h` helper - lives in `tests/e2e/harness.py`; read `tests/README.md` before extending it.
The manual `docs/e2e/` worked runs remain the human-precedent reference the harness was built from (canonical examples: `docs/e2e/restore-inspect-e2e-r6.md`, `docs/e2e/bench-ux-m7-multi-bench.md`, `docs/e2e/restore-noninteractive-h2.md`).
When validating a behavior change by hand, follow the same procedure and point back to those runs.

1. **Isolate everything.**
   Set `CWCLI_HOME` (preferred - relocates only cwcli's own state via `config_utils.cwcli_home()`, leaving `HOME` and HOME-derived tooling like git/ssh untouched) or a temporary `HOME` (isolates cwcli plus everything else that reads `HOME`) to a fresh throwaway directory so `~/.cwcli` is never touched, use unique docker-compose project names (for example `cwe2e-<something>`), and dedicated volumes/network.
   NEVER touch the captain's real projects or real `~/.cwcli`.
2. **Build genuine benches.**
   Use `cwcli init` + `bench init` to create real benches, not fixtures.
   Local hand validation uses one throwaway Frappe v16 instance.
   For version-sensitive behavior, encode the coverage in a version-gated E2E test and let CI run the v14/v15/v16 matrix.
3. **Drive interactive prompts through a real pty (pexpect).**
   Await prompt_toolkit's raw-mode readiness marker `ESC[?2004h` before each keystroke so nothing races the prompt.
   For a confirm-then-prompt flow, press `y` THEN Enter, the way a human types it, so the confirm consumes its own trailing Enter (see the `auto_enter=False` credential note in the `cwcli-lifecycle` skill (references/restore.md)).
4. **Drive non-interactive paths with flags.**
   Use `--yes`/`-y`, `--mariadb-root-username`/`--mariadb-root-password`, `--site`, and the backup selectors (`--latest`/`--backup-file`).
   A non-TTY without the needed flag must refuse with a non-zero exit, not hang or silently proceed.
5. **Tear down all throwaway instances afterward** (containers, volumes, network, temp `HOME`), and confirm the captain's real instances are untouched.
6. **Add a runtime-deps-only clean-install smoke leg whenever the change touches imports or dependencies** (a new `import`, a new module wired into `main.py`'s command chain, or any `pyproject.toml` dependency edit).
   This is IN ADDITION to the editable-install behavior legs above, not a replacement - keep validating real behavior against the worktree's own editable install (`uv run cwcli`), never mocks and never a PyPI build.
   The reason it is its own leg: `uv run cwcli` runs in the dev env (`uv sync --all-extras`), where every dev/transitive dep is present, so it CANNOT catch a runtime dependency that is imported but not declared in `[project.dependencies]`. "In the user's shoes" is `uv tool install`, not `uv run`. cwcli shipped SHIPPED-BROKEN on develop exactly here: `commands/config.py` imported `click` directly while only `typer` was declared; typer 0.27 stopped supplying click transitively, so a real `uv tool install` had no click and EVERY command died at import with `ModuleNotFoundError`. Unit tests, no-mistakes, CI, and the 74-check `docs/e2e/config-dx-rework.md` E2E all ran in the dev env and stayed green.
   The leg (no Docker needed - purely an import/parse smoke):

   ```bash
   # Isolated tool dir so the install resolves ONLY from [project.dependencies]
   export UV_TOOL_DIR=$(mktemp -d) UV_TOOL_BIN_DIR=$(mktemp -d)
   uv tool install .                                   # the shipped shape: runtime deps only, no --all-extras
   uv pip list --python "$UV_TOOL_DIR"/caffeinated-whale-cli | grep -i pytest \
     && { echo "dev deps leaked - not a runtime-only install"; exit 1; } || true
   BIN="$UV_TOOL_BIN_DIR/cwcli"
   "$BIN" --help >/dev/null                            # exercises main.py's full `from .commands import ...` chain
   "$BIN" config --help >/dev/null                     # and the specific group you touched
   CWCLI_HOME=$(mktemp -d) "$BIN" config path >/dev/null   # one trivial no-Docker command that fully loads the tree
   ```

   Any undeclared runtime import surfaces here as a `ModuleNotFoundError` at import time.
   This is the per-feature discipline; `.github/workflows/test.yml`'s `Clean install smoke` job enforces the same leg mechanically on every push/PR (see `AGENTS.md` CI-gates).
   That job only exercises `--help`/`config` reads; `e2e.yml`'s `e2e-runtime-only` job goes deeper, driving a genuine `init` -> `apps list` -> `inspect` -> `backup` -> `rm` lifecycle against the same runtime-only binary via `CWCLI_BIN` (the `e2e_pkg` marker, `tests/e2e/test_pkg_lifecycle_e2e.py`), catching a dependency imported lazily inside a command body. See `docs/contributing/ci-cd.md` for the full job description.
