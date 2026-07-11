# E2E evidence: uv distribution (`uv tool install` / `uvx` / `uv tool upgrade`)

Real end-to-end validation that a fresh user can install, run, and upgrade `cwcli`
entirely through uv tooling, on a **fully isolated** environment.
The captain's real `~/.cwcli` and real uv tools were never touched.

## Isolation

Every command below ran with a throwaway `HOME` and dedicated uv directories so
nothing leaked into the real environment:

```
HOME            = $SANDBOX/home            # so ~/.cwcli was a fresh, empty dir
UV_TOOL_DIR     = $SANDBOX/tooldir         # uv tool venvs
UV_TOOL_BIN_DIR = $SANDBOX/toolbin         # the installed `cwcli` symlink
UV_CACHE_DIR    = $SANDBOX/cache
XDG_DATA_HOME / XDG_CONFIG_HOME = $SANDBOX/...
```

Post-run isolation check confirmed:

- The captain's real `~/.cwcli` had no entries newer than the session (newest was
  `cache` at `2026-07-07`, predating the run).
- The captain's real uv tools directory contained **no** `caffeinated-whale-cli`.

## What it proves

### 1. `uv tool install` (persistent install → working `cwcli` on PATH)

Installed this branch's locally built wheel (`caffeinated_whale_cli-0.36.0-py3-none-any.whl`):

```
$ uv tool install <local wheel>
Installed 2 executables: caffeinated-whale-cli, cwcli
$ which cwcli
$SANDBOX/toolbin/cwcli
$ cwcli --version
Caffeinated Whale CLI Version: 0.36.0
```

The tool environment resolved **19 packages** and the runtime dependency set was
lean - `black` is **absent** (it moved to the `dev` extra in this change, since it
is only a formatter and is never imported or subprocessed at runtime):

```
docker  peewee  questionary  rich  toml  typer   # (+ their transitive deps) — no black
```

### 2. Read-only command run

```
$ cwcli --version        # exercises the full Typer app import chain
Caffeinated Whale CLI Version: 0.36.0
$ cwcli ls               # read-only docker scan, wrote only to the isolated HOME's cache
             Caffeinated Whale Instances
  Project Name   Status    Ports
  ...            ...       ...
```

`cwcli ls` is non-destructive (it only lists) and, with the isolated `HOME`, wrote
no cache to the captain's real `~/.cwcli`.

### 3. `uv tool upgrade` (genuine version advance)

To show the documented upgrade command actually advancing a version, `cwcli` was
installed **unpinned** from a local wheelhouse holding only `0.36.0`, then a
"new release" (`0.36.1`) was dropped in and the upgrade command run:

```
$ uv tool install caffeinated-whale-cli --find-links <wheelhouse>   # unpinned
$ cwcli --version
Caffeinated Whale CLI Version: 0.36.0

# ... 0.36.1 wheel added to the wheelhouse ...

$ uv tool upgrade caffeinated-whale-cli --find-links <wheelhouse>
Updated caffeinated-whale-cli v0.36.0 -> v0.36.1
 - caffeinated-whale-cli==0.36.0
 + caffeinated-whale-cli==0.36.1
$ cwcli --version
Caffeinated Whale CLI Version: 0.36.1
```

> Note on pins: installing with an exact version (`uv tool install caffeinated-whale-cli==0.34.0`)
> records a pin, and `uv tool upgrade` then reports "Nothing to upgrade" (verified
> against real PyPI releases 0.34.0 → 0.35.0). The README therefore documents the
> unpinned `uv tool install caffeinated-whale-cli` as the install command, so
> `uv tool upgrade` works as expected.

### 4. `uvx` ephemeral run (no persistent install)

Because the command name (`cwcli`) differs from the package name
(`caffeinated-whale-cli`), `uvx` needs `--from`:

```
$ uvx --from caffeinated-whale-cli cwcli --version
Caffeinated Whale CLI Version: 0.36.1
$ uvx --from caffeinated-whale-cli cwcli --help
 Usage: cwcli [OPTIONS] COMMAND [ARGS]...
 A command-line tool to help you create, manage, and back up your Frappe and ...
```

## Reproducing

The runtime/packaging pieces this relies on:

- Build backend: `setuptools.build_meta` with src-layout auto-discovery (unchanged).
- `[project.scripts]` exposes both `cwcli` and `caffeinated-whale-cli` →
  `caffeinated_whale_cli.main:cli`, so `uv tool install` puts `cwcli` on PATH and
  `uvx --from caffeinated-whale-cli cwcli` runs the same entry point.
- `black` lives in the `dev` optional-dependency group, not runtime `dependencies`.
