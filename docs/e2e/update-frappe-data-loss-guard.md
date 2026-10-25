# `apps update <project> frappe` data-loss guard: before and after

This is the worked reproduction behind the frappe-path data-loss guard (`update_preflight.local_work`, applied in `core/update.py:_frappe_checks`).
The CI proof on a genuine instance is the data-loss half of `tests/e2e/test_update_preflight_e2e.py`.

## Setup

`bench update --reset` hard-resets every app it updates to `<remote>/<branch>`.
That silently discards uncommitted changes to tracked files and commits that exist on no remote, branch or tag.

The run used real `frappe-bench` 5.31.0, the version cwcli instances run, installed into a scratch venv.
The bench was synthetic but real where it matters, built the way `docs/e2e/update-frappe-preflight.md` describes:

- every app is a real git repository cloned with `--origin upstream` from a local bare remote, the way `bench get-app` clones;
- the apps are `frappe`, a healthy `goodapp`, and `dirty`, which holds an unpushed commit (`dirty/committed.py`), an uncommitted edit to the tracked `dirty/__init__.py`, and an untracked `dirty/untracked.py`.

cwcli's real `core.update` drove it, with Docker swapped for a local subprocess: the preflight script, the guard, and `bench update --reset` itself all ran unmodified.
Only the cache refresh and the post-run process resync were stubbed, because both need a real instance.
The bench has no virtualenv, so every run that reaches bench's requirements step fails there.
That is after the git phase, so it changes nothing below.

## Before (develop at `0ca9db2`)

`cwcli apps update repro frappe` reported the loss as a warning, then ran bench anyway:

```text
  [UpdatePreflight] blocked=False 3 app(s), 0 site(s) on <bench> (0.1s)
    data_loss  git.uncommitted   dirty  1 uncommitted change(s) to tracked files would be DISCARDED by the reset
    data_loss  git.unpushed      dirty  1 commit(s) on 'main' exist nowhere else (no remote, branch or tag) and would be DISCARDED by the reset
  [UpdateStepStart] frappe_reset
    | $ git fetch --all
    | $ git reset --hard upstream/main
    | HEAD is now at d042b80 init
```

The `dirty` app before and after:

```text
before  HEAD 0e6736b unpushed work   status:  M dirty/__init__.py ?? dirty/untracked.py
after   HEAD d042b80 init            status: ?? dirty/untracked.py     (committed.py gone)
```

With `--no-preflight` the same work was discarded with no warning at all.

## After

The same command, same bench, refuses with nothing run, with and without `--no-preflight`:

```text
REPORT {"ok": false, "failed_apps": [], "preflight_blocked": false, "data_loss_refused": true, "data_loss_apps": ["dirty"]}
before  HEAD e985dd3 unpushed work   status:  M dirty/__init__.py ?? dirty/untracked.py
after   HEAD e985dd3 unpushed work   status:  M dirty/__init__.py ?? dirty/untracked.py
common_site_config.json: {"maintenance_mode":0,"pause_scheduler":0}   (bench never started)
```

The human CLI under `--no-preflight`, where only the guard runs:

```text
Local work check: 3 app(s), 0 site(s) on <bench> (0.1s)

  Data loss - bench update --reset discards this local work:
    ! dirty  1 uncommitted change(s) to tracked files would be DISCARDED by the reset
    ! dirty  1 commit(s) on 'main' exist nowhere else (no remote, branch or tag) and would be
             DISCARDED by the reset

Refusing to start: 'bench update --reset' would discard local work in dirty, so it did not run.
Commit and push that work, or re-run with --force to discard it; --no-preflight does not skip this
check.
```

It exits 1 the same way whether stdin is a terminal or not; there is no prompt.

`--force` accepts the loss.
The note prints before bench starts, and the report names the app:

```text
Note: --force: 'bench update --reset' will discard any uncommitted or unpushed work in dirty.
Updating the frappe framework with 'bench update --reset'
...
! --force let 'bench update --reset' discard local work in 1 app(s): dirty

REPORT {"ok": false, "failed_apps": ["frappe"], "preflight_blocked": false, "data_loss_refused": false, "data_loss_apps": ["dirty"]}
after   HEAD 3da77f4 init            status: ?? dirty/untracked.py     (committed.py gone)
```

`failed_apps: [frappe]` is the missing virtualenv on this synthetic bench, reached after the reset.

## The CI leg's stop

The E2E's `--force` leg must let real bench discard real work on the shared CI instance and then leave the instance exactly as found.
Left alone, bench goes on from the git phase to reinstall every app into the bench's environment, which cannot be undone exactly.
So the leg lists every other app in `sites/excluded_apps.txt` and plants an unreadable untracked directory in the dirty app.
That makes bench's own post-reset `find . -name "*.pyc" -delete` fail right after the reset.
Checked against real bench 5.31.0 with the same layout:

```text
Updating apps source...
Skipping pull for app frappe
Skipping pull for app goodapp
$ git fetch --all
$ git reset --hard upstream/main
$ find . -name "*.pyc" -delete
find: './locked': Permission denied
ERROR: find . -name "*.pyc" -delete
```

Bench stopped before `Setting up requirements...`.
The dirty app's work was gone, `frappe` was untouched, and the bench-wide flags were left set, which the test restores along with the config, the excluded list, the backups and frappe's tracking ref.
