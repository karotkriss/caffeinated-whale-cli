# Historical E2E evidence: `cwcli apps` on Frappe version-15

This records the original app-management validation before the verified post-mutation resynchronise step (`restart-processes`) was added.
For the current serving guarantee, see the `apps` contract in the README and `tests/e2e/test_apps_resync_e2e.py`.
The apps install and uninstall invocations shown here use the pre-`fm/cwcli-apps-single-positional` positional app-name syntax, superseded by the `--app` option; see `README.md` for current usage.

Real-instance end-to-end run of the new `cwcli apps` command group (task 7.2 of the
`add-app-management` change) on an **isolated** throwaway instance.
The captain's real projects and real `~/.cwcli` were never touched, and the instance
was torn down afterwards.

## Environment (isolation)

- Temporary `HOME` so `~/.cwcli` was a fresh SQLite DB.
- Unique docker-compose project `cwe2e-v15` on port 18000, dedicated volumes/network.
- Frappe **version-15** (`15.114.0`), built with `cwcli init cwe2e-v15 --frappe-branch version-15`.
- Two sites to exercise the multi-site default: `cwe2e.localhost` and `cwe2e2.localhost`.
- A local app `hello` (created with `bench new-app`) installed on `cwe2e.localhost` as fixture data, and a second app `widget` used for the git-URL install (served from a local git working tree, so no external network was needed for that step).
- Interactive prompts driven through a real pty (pexpect), awaiting prompt_toolkit's raw-mode readiness marker `ESC[?2004h` before each keystroke; non-interactive paths driven with a non-TTY stdin (`< /dev/null`).

## What it proves (both modes, honest exit codes)

### 1. `list` - live reads, `--json`, multi-site by default

`cwcli apps list cwe2e-v15 --installed --json` reports available apps and, with no `--site`, the installed apps for **every** site grouped by site (exit 0):

```json
{
  "project": "cwe2e-v15",
  "bench": "/workspace/frappe-bench",
  "available_apps": ["frappe", "hello"],
  "installed": {
    "cwe2e.localhost": ["frappe", "hello"],
    "cwe2e2.localhost": ["frappe"]
  }
}
```

### 2. `install` by git URL - real `get-app` + install, name derived from `apps/`

`cwcli apps install cwe2e-v15 file:///tmp/widget-src --site cwe2e2.localhost --json` ran a real `bench get-app <url>` then `bench --site cwe2e2.localhost install-app`, and exited 0. The installed name is **`widget`** (the app's real package name, detected from the new `apps/` directory after the fetch) - **not** `widget-src` (the URL basename) - proving the install-name is derived from the fetch result, not from parsing the URL:

```json
{ "results": [
    { "app": "file:///tmp/widget-src", "site": null, "action": "get-app", "ok": true },
    { "app": "widget", "site": "cwe2e2.localhost", "action": "install-app", "ok": true } ],
  "ok": true }
```

A follow-up `cwcli apps list cwe2e-v15 --site cwe2e2.localhost --json` shows `widget` now in both `available_apps` and the site's `installed` list, confirming the post-mutation cache refresh.

### 3. `install` failure - honest non-zero, no success banner

`cwcli apps install cwe2e-v15 no_such_app_zzz --site cwe2e.localhost` - `bench get-app` fails (`no_such_app_zzz not found under frappe or erpnext`), the command prints `✗ get-app no_such_app_zzz` / `Completed with errors.`, prints **no** success banner, and exits **1**.

### 4. `uninstall` destructive gate

- Non-interactive, non-TTY **without** `--yes` refuses:
  ```
  $ cwcli apps uninstall cwe2e-v15 hello --site cwe2e.localhost < /dev/null
  Error: Uninstall is destructive and no confirmation was given. Pass --yes to proceed.
  [exit=1]      # nothing uninstalled
  ```
- Non-interactive **with** `--yes` performs the real uninstall + cache refresh (exit 0):
  ```
  $ cwcli apps uninstall cwe2e-v15 hello --site cwe2e.localhost --yes
  Proceeding without confirmation (--yes).
  ...
  ✓ uninstall-app hello on cwe2e.localhost
  ✓ App(s) uninstalled.
  [exit=0]
  ```
  A subsequent `apps list --site cwe2e.localhost --json` shows `hello` gone from the site's `installed` list (cache refreshed).

### 5. `uninstall` interactive prompt (pexpect)

The confirm prompt is shown and its input is genuinely collected:

```
? Uninstall widget from 1 site(s) (cwe2e2.localhost)? This deletes their data. (y/N)
```

- Pressing **n** -> `Operation cancelled.` and exit **1** (nothing uninstalled).
- Pressing **y** -> `✓ uninstall-app widget on cwe2e2.localhost` / `✓ App(s) uninstalled.` and exit **0**.

### 6. `update` guards + `cwcli update` deprecation

```
$ cwcli apps update cwe2e-v15
Error: At least one app must be specified.
[exit=1]

$ cwcli update cwe2e-v15
Warning: 'cwcli update' is deprecated; use cwcli apps update instead.
Error: At least one app must be specified.
[exit=1]
```

## Result

Every `cwcli apps` behavior works end-to-end on a real Frappe version-15 instance in **both** interactive and non-interactive modes, with honest non-zero exit codes, multi-site fan-out, git-URL install (name from `apps/` diff), destructive gating, post-mutation cache refresh, and the `cwcli update` deprecation notice. See `apps-v14.md` for the version-14 run.
