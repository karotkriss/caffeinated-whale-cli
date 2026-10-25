# `apps update <project> frappe` content checks: before and after

This is the worked reproduction behind the preflight's content tier (`_content_facts` and the in-container fetch in `core/update_preflight.py`).
The CI proof on a genuine instance is the frappe-major half of `tests/e2e/test_update_preflight_e2e.py`.

## Setup

`bench update --reset` backs up every site, hard-resets every app, and only then reinstalls every app.
The reinstall is where new code that needs newer tooling fails: `uv pip install -e` refuses a `requires-python` the bench's virtualenv does not meet, and `yarn install` refuses an `engines.node` the container's Node does not meet.
By then the bench is mixed (apps on new code, the environment on the old) and `maintenance_mode`/`pause_scheduler` are set bench-wide.

The run used real `frappe-bench` 5.31.0, the version cwcli instances run, and real `uv` and `yarn` 1.22.22, installed into a scratch directory.
The bench was synthetic but real where it matters, built the way `docs/e2e/update-frappe-preflight.md` describes:

- every app is a real git repository cloned `--depth 1 --origin upstream` from a local bare remote, the way `bench get-app` clones, with a real `pyproject.toml` and `package.json`;
- `env/` is a real Python 3.12 virtualenv, the container's Node is 22;
- four sites, whose backups a stub `frappe.utils.bench_helper` fakes at 2 s each;
- `sites/common_site_config.json` sets `shallow_clone: true`, as `bench init` writes it;
- per scenario, the frappe remote's tip raises `requires-python` to `>=3.14,<3.15`, raises `engines.node` to `>=24`, or bumps `__version__` to the next major.

cwcli's real `core.update` drove it, with Docker swapped for a local subprocess: the preflight script and `bench update --reset` itself ran unmodified.
The cache refresh, the post-run process resync, and the preflight's sites and migrate-lock tiers were stubbed, because they need a real Frappe.

## Before (develop at `b04e370`)

The preflight passed, and bench failed after every backup and every app reset, leaving the bench in maintenance mode.
`requires-python` raised on frappe's remote tip, seconds since start:

```text
  [UpdatePreflight] blocked=False 2 app(s), 4 site(s) on <bench> (0.1s)
    0.24 | Backing up sites...
    8.31 | backup ok: frappe --site site3.local backup
    8.31 | Updating apps source...
    8.32 | $ git reset --hard upstream/version-16
    8.32 | HEAD is now at 9876103 require python 3.14
    8.35 | $ git reset --hard upstream/main
    8.37 | Setting up requirements...
   10.70 | Installing frappe
   10.81 |   ╰─▶ Because the current Python version (3.12.13) does not satisfy
   10.81 |       Python>=3.14,<3.15 and frappe==16.0.0 depends on Python>=3.14,<3.15, we
   10.81 | Error occured during app install: uv pip install --quiet -e <bench>/apps/frappe ...
REPORT {"ok": false, "failed_apps": ["frappe"], "preflight_blocked": false, ...}
common_site_config: {"maintenance_mode":1,"pause_scheduler":1,"shallow_clone":true}
frappe HEAD:        9876103 require python 3.14
```

`engines.node` raised on frappe's remote tip failed the same way, one step later:

```text
    8.30 | Updating apps source...
    8.32 | HEAD is now at 7877b03 require node 24
    9.45 | Installing frappe
   11.28 | error frappe@16.0.0: The engine "node" is incompatible with this module. Expected version ">=24". Got "22.23.4"
common_site_config: {"maintenance_mode":1,"pause_scheduler":1,"shallow_clone":true}
```

On a real bench each stub backup is a real one, so the wait before either failure is every site's backup plus every app's fetch.

A frappe major bump already failed fast, but only after the preflight had passed it:

```text
    0.23 | This update will cause a major version change in Frappe/ERPNext from 16 to 17.
    0.23 | Do you want to continue? [y/N]: Aborted!
```

## After

The same three benches refuse in about 0.25 s with nothing run: the flags stay `0` and every checkout is where it was.

```text
  [UpdatePreflight] blocked=True 2 app(s), 4 site(s) on <bench> (0.1s)
    blocker  tooling.python_too_old  frappe  needs Python >=3.14,<3.15 (requires-python) but the bench's environment runs Python 3.12.13; bench would fail reinstalling it, after backing up every site and resetting every app
REPORT {"ok": false, "failed_apps": [], "preflight_blocked": true, ...}
common_site_config: {"maintenance_mode":0,"pause_scheduler":0,"shallow_clone":true}
frappe HEAD:        62062a7 init

    blocker  tooling.node_too_old    frappe  needs Node >=24 (engines.node in package.json) but the container runs Node v22.23.4; yarn would refuse it, after bench has backed up every site and reset every app

    blocker  frappe.major_upgrade    frappe  the update moves frappe from version 16 to 17; bench stops to ask before a major upgrade, and cwcli's run cannot answer it
             fix: to upgrade on purpose, answer bench's prompt yourself: cwcli run repro -i update --reset
```

The human CLI renders the same refusal (the bench path shown as `<bench>`):

```text
  Blocking - bench update --reset would fail:
    ✗ frappe  the update moves frappe from version 16 to 17; bench stops to ask
              before a major upgrade, and cwcli's run cannot answer it
              fix: to upgrade on purpose, answer bench's prompt yourself: cwcli
              run repro --path <bench> -i update --reset

Refusing to start: 1 blocking problem(s). 'bench update --reset' did not run.
```

A bench path wider than the column now folds onto the next line in full.
Before, the renderer cut it to an ellipsis, which left every remedy carrying a long `--path` impossible to copy.

The clean twin, whose tips need nothing the bench lacks, passed the preflight and ran bench to completion: `ok: true`, both apps reinstalled, `yarn install` run, and the flags cleared.

## The version readers

Each requirement is compared in the dialect of the tool that enforces it: `requires-python` as PEP 440 (`packaging`, what uv and pip apply), `engines.node` as node-semver (yarn v1's engines check), and `[tool.bench.frappe-dependencies]` as semantic_version's `SimpleSpec` (what bench calls).
The two hand-written readers were compared with the real libraries on generated cases: semantic_version 2.10 from bench's own environment, and node-semver 7.7.4 from npm.

```text
SimpleSpec: 18885 cases, 0 mismatches
node-semver: 15521 cases, 0 mismatches, 11 unread (unverified)
```

The unread ranges carry a prerelease bound, which the reader reports as unverified rather than guessing.

## The CI leg's remote

The E2E needs a frappe whose upstream tip is the next major, on a shared instance that must be left exactly as found.
Its fixture makes a bare remote that borrows frappe's objects through `objects/info/alternates`, copies frappe's `shallow` file so the remote's history ends where the clone's does, and adds one commit on top of frappe's `HEAD` that only bumps `__version__`, built with `hash-object`, `read-tree`, `update-index` and `commit-tree` so nothing is copied or checked out.
frappe's `upstream` URL points at it for the test.

The fixture's exact shell was run against a synthetic shallow frappe clone: the preflight refused with `frappe.major_upgrade`, the checkout was unchanged, and `--no-preflight` let real bench stop at its own prompt (`Do you want to continue? [y/N]: Aborted!`) with the flags still `0`.
The `--no-preflight` leg lists every app in `sites/excluded_apps.txt` first.
bench's version check reads frappe regardless, so it still stops at the prompt (checked the same way), and were it ever to go on, there would be nothing for it to reset on the shared instance.
The exclusion also keeps the data-loss guard off frappe, so the leg never depends on the CI bench's frappe checkout having no local changes.
The fixture snapshots the files a fetch can change (`config`, `shallow`, `FETCH_HEAD`, `packed-refs`, and the remote-tracking ref and its reflog) and restores them.
Checked the same way, the fixture plus the preflight changed `FETCH_HEAD`, `config`, `shallow`, the tracking ref and its reflog, and after the restore every file under `.git` outside `objects/` was byte-identical to before.
The fetched objects are left: unreachable once the refs are back, and deleting object files risks ones a background repack moved.
