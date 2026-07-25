# Historical E2E: `cwcli axi apps install`

This records the original v15 install validation before the verified post-mutation resynchronise step (`restart-processes`) was added.
For the current serving guarantee, see the `apps` contract in the README and `tests/e2e/test_axi_apps_install_e2e.py`.

Real-instance validation of the agent-surface install verb, run against a throwaway
Frappe v15 instance provisioned for this purpose and removed afterwards.

Environment: isolated `CWCLI_HOME`, project `cwe2e-axiinst`, port base `12200`, built
from the worktree's own editable install (`uv run cwcli`), never a published build.

## Setup

```
CWCLI_HOME=/tmp/cwe2e-axiinst-home CWCLI_ADMIN_PASSWORD=... \
  cwcli axi init cwe2e-axiinst --port 12200 --version 15
```

```
project: cwe2e-axiinst
bench_name: frappe-bench
bench_path: /workspace/frappe-bench
site_name: development.localhost
bench_created: true
site_created: true
erpnext_installed: false
```

The site starts with `frappe` only.

## The permitted path: an app the site does not have

```
cwcli axi apps install cwe2e-axiinst payments --site development.localhost --branch version-15
```

```
project: cwe2e-axiinst
bench_path: /workspace/frappe-bench
results[2]{app,site,action,ok}:
  payments,null,get-app,true
  payments,development.localhost,install-app,true
ok: true
warnings[1]:
  Using the only bench: /workspace/frappe-bench
```

Exit `0`.
One TOON document on stdout, one row per step, so an agent reads which step landed
rather than parsing prose.
`bench`'s own output (the clone, the `uv pip install`, the DocType progress bars) goes to
stderr, so stdout stays a single parseable document.

The state genuinely changed, read back through the cache the verb refreshed:

```
$ cwcli axi apps list cwe2e-axiinst --installed
available[2]: frappe,payments
installed:
  development.localhost[2]: frappe,payments
```

## The refused path: an app the site already has

Re-running the exact command that just succeeded:

```
$ cwcli axi apps install cwe2e-axiinst payments --site development.localhost --branch version-15
error: "App 'payments' is already installed on site 'development.localhost'. Installing it again would re-run its install hooks against that site's existing data."
help: "To move it to another ref: 'cwcli axi apps checkout <project> payments <ref>'. To pull and migrate it: 'cwcli axi apps update <project> payments --site development.localhost'. A genuine reinstall is the human 'cwcli apps install', which confirms first."
```

Exit `1`.
The refusal states why and names three specific ways forward, rather than failing bare.

Same refusal for `frappe`, which the site has had since it was created, and same refusal
for the git-URL spelling of an app already present, which confirms the name derivation
feeds the guard:

```
$ cwcli axi apps install cwe2e-axiinst https://github.com/frappe/payments.git --site development.localhost
error: "App 'payments' is already installed on site 'development.localhost'. ..."
```

Nothing is fetched on a refusal.
After all three refusals the bench still held exactly `frappe,payments` - the check runs
before `bench get-app`, so a refused install leaves the bench as it found it.

## Fail closed on an unreadable site

```
$ cwcli axi apps install cwe2e-axiinst hrms --site nosuch.localhost
error: "Could not read the installed apps on site 'nosuch.localhost' (it may not exist on this bench), so it cannot be confirmed that this install would not touch existing app data."
help: "Check the site exists and is readable: 'cwcli axi apps list <project> --site nosuch.localhost'."
```

Exit `1`, and `hrms` was not fetched.
An unreadable state is never treated as "nothing is installed there".

## Usage guards

```
$ cwcli axi apps install cwe2e-axiinst payments          # no --site
error: "Missing option '--site'."
help[1]:
  usage: cwcli axi apps install PROJECT APP [--site] [--bench] [--branch] [--help]
```

Exit `2`.
`--force` is likewise rejected as an unknown flag, which is the intended answer: there is
no bypass for the already-installed refusal.

## Teardown

The instance and its isolated `CWCLI_HOME` were removed.
No other instance on the daemon was touched and no prune was run.
