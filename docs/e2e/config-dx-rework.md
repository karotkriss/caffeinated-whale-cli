# E2E evidence: the config DX rework (`rework-config-dx`)

Worked real-machine validation of the reworked `cwcli config` surface, run 2026-07-16 against the worktree's editable install (`uv run cwcli`), per the `cwcli-e2e-testing` protocol.
Two legs: a host-side leg under an isolated `CWCLI_HOME` (no Docker), and ONE throwaway real instance (`cwe2e-cfgdx`, frappe `version-16`, real `cwcli init`) used only for the auto-inspect daemon cycle, torn down at the end.
The captain's real `~/.cwcli` and real instances were never touched (all runs under scratch `CWCLI_HOME`/`HOME` dirs; `docker ps` showed no non-`cwe2e-` containers during the daemon leg).

## Leg 1: host-side surface (isolated `CWCLI_HOME`, no Docker)

44 scripted checks, all green (`HOST_E2E_FAIL=0`):

- `config show` fresh defaults (tips enabled, auto-inspect disabled/stopped, boot disabled); `show --json` parses as one object carrying `search_paths`, `show_tips`, `config_file`, `cache_db`, and the six `auto_inspect` keys.
- Substitution-safety at width 40: `$(cwcli config path)` and `$(cwcli config cache path)` each return the exact path, one line, no prose (F8 fixed).
- `paths` group: explicit empty state; add; trailing-slash dedup (`/opt/benches/` reports already-exists against `/opt/benches` - F9 fixed); `--json` = the exact list; relative add refused exit 2; remove matches the normalized form; absent remove exit 0.
- Alias byte-checks: `add-path`/`remove-path` stdout byte-identical to the pre-rework messages, deprecation warning on stderr ONLY; `add-path rel/garbage` now exit 2 (the one disclosed alias exception).
- `cache clear` rows: no target exit 2, NAME+`--all --yes` exit 2 clearing nothing (F4 fixed), non-TTY `--all` refusal exit 1, missing project exit 0; `cache list --json` empty = `[]`; seeded row visible, `--all --yes` clears it.
- `tips` enable/disable reflected in `show`; `tips status` alias renders the old table with the stderr warning.
- Auto-inspect host-safe rows: `enable --interval 30` exit 1 with the config file NOT mutated (F3 fixed - the file is never even created); `status`/`stop`/`logs` exit 0 with the pre-rework messages; the `start` alias refuses when disabled (exit 1) - the boot-unit guard.
- `config edit` with `EDITOR=true` exits 0 and creates the file first.

Interactive leg (pty via pexpect, awaiting prompt_toolkit's `ESC[?2004h` raw-mode marker before each keystroke, per the captain standard - both modes for the ONE prompting subcommand):

- `cache clear --all` DECLINE: prompt genuinely shown, `n` + Enter, exit 1, cache preserved.
- `cache clear --all` ACCEPT: `y` then Enter (`auto_enter=False`), exit 0, cache cleared.

## Leg 2: the auto-inspect daemon cycle (one real throwaway instance)

`cwe2e-cfgdx` built by a real non-interactive `cwcli init` (port 11000, `--frappe-branch version-16`, `--admin-password`, `--auto-start`), exit 0. 30 scripted checks, all green (`DAEMON_E2E_FAIL=0`):

- `enable -i 60`: ONE verb reaches running - enabled message, started message, pid file written, daemon process alive, `status` shows Running + PID. No "run start now" two-step hint anywhere (F2 fixed).
- Idempotent re-run: exit 0, "already running", same PID (no restart).
- **The daemon genuinely inspects**: starting from an empty cache, `cwcli config cache list` shows `cwe2e-cfgdx` within 90s, and `auto-inspect logs` carries `Inspecting cwe2e-cfgdx` + `Successfully inspected cwe2e-cfgdx`.
- `stop` honesty: exit 0, process really dead (`kill -0` fails), pid file removed, `enabled = true` preserved (daemon-only).
- Interval change restarts: `enable -i 120` over a running daemon reports the restart, a NEW pid is alive, `interval = 120` persisted.
- `disable` teardown honesty: reports the daemon stop and the disable, process dead, `enabled = false` persisted, `status` Stopped, and the frozen `start` alias then REFUSES (exit 1) - the stale-boot-hook guard verified end to end.

Teardown: `cwcli rm cwe2e-cfgdx --yes --volumes --no-backup` exit 0; `docker ps -a` and `docker volume ls` show zero `cwe2e-` leftovers; no daemon processes remain.

## The bug this run caught (and its fix)

The first daemon-leg run HUNG on `$(cwcli config auto-inspect enable -i 60)`: the POSIX fork child rebound `sys.stdin/stdout/stderr` but never `dup2`'d the REAL descriptors, so the detached daemon held the parent's stdout pipe open and every shell capture/pipe waited forever for EOF.
The old ten-verb surface masked it (`enable` never started a daemon; only `start` had it); the fused `enable` made it load-bearing.
Fixed in `utils/auto_inspect.py:start_daemon` (fds 0/1 to devnull, fd 2 to the log file, mirroring `_spawn_detached`), pinned by the mock-free `tests/test_auto_inspect.py::test_forked_daemon_releases_the_parents_stdout_pipe` (a real subprocess with a captured pipe, which fails as a 30s timeout against the unfixed code), and re-verified by the full daemon leg above.

Boot-hook (`--startup`) flows were validated at the unit level only, deliberately: `utils/startup.py` writes to the REAL `~/.config/systemd/user`, and installing a real boot unit on the captain's machine is not a throwaway action.
