# `apps update <project> frappe` preflight: before and after

This is the worked reproduction behind the frappe-path preflight (`core/update_preflight.py`).
The CI proof on a genuine instance is `tests/e2e/test_update_preflight_e2e.py`.

## Setup

The run used real `frappe-bench` 5.31.0, the version cwcli instances run, inside the `frappe/bench:v5.31.0` image.
The container carried the compose labels cwcli resolves a project by (`com.docker.compose.project=cwe2e-pfrepro`, `com.docker.compose.service=frappe`), so cwcli drove it exactly as it drives an instance.
The bench at `/workspace/frappe-bench` was synthetic but real where it matters:

- every app is a real git repository cloned with `--origin upstream` from a local bare remote, the way `bench get-app` clones;
- `sites/common_site_config.json` is the one `bench init` writes, including `shallow_clone: true`;
- six sites, each with a stub `env/bin/python` that takes 2 s per `bench backup` and leaves a backup file;
- the apps are `frappe`, a healthy `goodapp`, and `localbranch`, checked out on `feature-x`, a branch its remote does not have.

A full Frappe build was not needed: every failure in scope happens in bench's own Python and git before any Frappe code runs.

## Before (develop at `b171105`)

`cwcli axi apps update cwe2e-pfrepro frappe` exited 1 after about 20 s.
The verbose run's timeline, in seconds since start:

```text
  4.38 backup ok: site1.localhost
  ...
 14.44 backup ok: site6.localhost
 14.53 Updating apps source...
 14.57 $ git fetch --depth=1 --no-tags upstream version-16
 14.69 $ git reset --hard upstream/version-16
 14.81 $ git gc --prune=all
 14.89 $ git fetch --depth=1 --no-tags upstream main
 15.02 $ git reset --hard upstream/main
 15.24 $ git fetch --depth=1 --no-tags upstream feature-x
 15.27 fatal: couldn't find remote ref feature-x
 15.31 ERROR: git fetch --depth=1 --no-tags upstream feature-x
```

Every site was backed up before any git problem could surface, and `frappe` and `goodapp` were already hard-reset and garbage-collected when the third app failed.
Afterwards `common_site_config.json` held `"maintenance_mode": 1` and `"pause_scheduler": 1`, so every site was down with its scheduler paused.
The report said only `failed_apps[1]: frappe`.
At 2 s per backup this is a 15 s wait; on a shared staging bench with real backups it is most of half an hour.

## After

The same command, same bench:

```text
exit=1 elapsed=3.82s
...
preflight[2]{severity,code,app,site,message,remedy}:
  blocker,git.branch_not_on_remote,localbranch,null,"branch 'feature-x' does not exist on remote 'upstream'",cwcli apps checkout cwe2e-pfrepro localbranch <published-branch>
  unverified,sites.unverified,null,null,"the bench's sites could not be checked (its read process left no readable record)",null
preflight_blocked: true
aborted: false
ok: false
```

`common_site_config.json` was byte-identical afterwards and no backup file existed: bench never started.
The 3.8 s includes interpreter startup; the checks took 1.4 s, of which the in-container script took 0.05 s and the rest was the per-site migrate-lock probes.
The `sites.unverified` row is the stub `env/bin/python`, which cannot open a site; on a real bench the sites tier opens every site.

The human command printed:

```text
Preflight: 3 app(s), 6 site(s) on /workspace/frappe-bench (1.4s)

  Blocking - bench update --reset would fail:
    ✗ localbranch  branch 'feature-x' does not exist on remote 'upstream'
                   fix: cwcli apps checkout cwe2e-pfrepro localbranch <published-branch>

  Could not verify:
    ? bench        the bench's sites could not be checked (its read process left no readable record)

Refusing to start: 1 blocking problem(s). Nothing was changed.
Re-run with --no-preflight to run bench anyway.
```

With `--no-preflight` the run went to bench and reproduced the late failure exactly: exit 1, six new backup files, and both flags left at 1.

## Does it mirror bench? One scenario at a time

Each row is a fresh single-problem bench (one site), the preflight's blockers, data loss and notices, then where real bench 5.31.0 stopped.
"Passed git phase" means bench got through every app's git step and stopped at the requirements step only because the synthetic bench has no virtualenv.

| Scenario | Preflight | bench |
| --- | --- | --- |
| app on a local-only branch | `git.branch_not_on_remote` | `fatal: couldn't find remote ref feature-x` |
| app remote unreachable | `git.remote_unreachable` | `fatal: '/workspace/remotes/gone.git' does not appear to be a git repository` |
| dead extra remote, `shallow_clone: true` | none | passed git phase |
| dead extra remote, `shallow_clone: false` | `git.remote_unreachable` | `fatal: '/workspace/remotes/gone.git' does not appear to be a git repository` |
| app on a detached HEAD | `git.detached_head` | `ERROR: Command 'git symbolic-ref -q --short HEAD' returned non-zero exit status 1.` |
| only remote renamed `old-upstream` | `git.remote_misdetected` | `fatal: 'upstream' does not appear to be a git repository` |
| app with no remote | `git.no_remote` (notice) | passed git phase |
| `frappe` remote renamed `origin` | `frappe.no_upstream` | `fatal: 'upstream' does not appear to be a git repository` |
| `frappe` on a detached HEAD | `git.detached_head` | `ERROR: Command 'git symbolic-ref -q --short HEAD' returned non-zero exit status 1.` |
| uncommitted change in an app | `git.uncommitted` (data loss) | passed git phase, change discarded |
| healthy | none | passed git phase |

The dead-extra-remote rows are why the preflight reads `shallow_clone`: with it set, bench fetches only the chosen remote, so an unused dead remote does not matter.
