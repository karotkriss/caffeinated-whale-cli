# E2E evidence: `cwcli apps` command group on Frappe version-14

Companion to [`apps-v15.md`](apps-v15.md): the same `cwcli apps` behaviors, verified on a real Frappe **version-14** instance to confirm they are not version-sensitive (some Frappe bugs only reproduce on v14).
Isolated throwaway instance; the captain's real projects and `~/.cwcli` were never touched, and the instance was torn down afterwards.

## Environment (isolation)

- Temporary `HOME` so `~/.cwcli` was a fresh SQLite DB; unique project `cwe2e-v14` on port 18100, dedicated volumes/network.
- Frappe **version-14** (`14.101.1`), built with `cwcli init cwe2e-v14 --frappe-branch version-14` (which set up Python 3.10 + Node 16 + yarn per the v14 gating), site `cwe2e14.localhost`.
- App `wv14` (created with `bench new-app`) served from a local git working tree for the git-URL install (no external network needed for that step).
- Interactive prompt driven through a real pty (pexpect), awaiting `ESC[?2004h`; non-interactive paths driven with a non-TTY stdin.

## What it proves (both modes, honest exit codes)

### Non-interactive

| Command | Result |
|---|---|
| `apps list cwe2e-v14 --json` | `available_apps: ["frappe"]`, exit 0 |
| `apps install cwe2e-v14 file:///tmp/wv14-src --site cwe2e14.localhost --json` | real `get-app` + `install-app`; installed name **`wv14`** derived from the `apps/` diff (not the URL basename); `"ok": true`, exit 0 |
| `apps list cwe2e-v14 --installed --json` | `cwe2e14.localhost: ["frappe", "wv14"]` (cache refreshed after install), exit 0 |
| `apps install cwe2e-v14 no_such_app_v14 --site cwe2e14.localhost` | `bench get-app` fails → `✗ get-app`, no success banner, exit **1** |
| `apps uninstall cwe2e-v14 wv14 --site cwe2e14.localhost < /dev/null` | non-TTY without `--yes` → `Error: Uninstall is destructive ... Pass --yes`, exit **1**, nothing uninstalled |
| `apps uninstall cwe2e-v14 wv14 --site cwe2e14.localhost --yes` | real `uninstall-app` + cache refresh → `✓ App(s) uninstalled.`, exit 0; follow-up list shows `wv14` gone from the site's `installed` |
| `apps update cwe2e-v14` | `Error: At least one app must be specified.`, exit **1** |
| `cwcli update cwe2e-v14` | `Warning: 'cwcli update' is deprecated; use cwcli apps update instead.` then the no-app error, exit **1** |

### Interactive (pexpect)

```
? Uninstall wv14 from 1 site(s) (cwe2e14.localhost)? This deletes their data. (y/N)
```
Pressing **n** -> `Operation cancelled.` and exit **1** (nothing uninstalled). The prompt is shown and its input is genuinely collected.

## Result

Every `cwcli apps` behavior works identically on Frappe version-14, in both interactive and non-interactive modes, with the same honest exit codes, git-URL install (name from `apps/` diff), destructive gating, and cache refresh as version-15. No version-sensitive defect was found.
