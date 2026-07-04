# E2E evidence: multi-bench labels, `--bench`, marker recovery, and `--yes`

Real-instance end-to-end run on an **isolated** throwaway environment (a temporary
`HOME` so `~/.cwcli` was a fresh DB, a unique docker-compose project
`cwcli-e2e-mbench`, and a dedicated network). Two genuine benches were built with
`bench init` (frappe `version-15`) inside one frappe container; `bench-b`'s frappe
`__version__` was set to a sentinel so `--bench` routing is unmistakable. The
captain's real projects and real `~/.cwcli` were never touched, and the instance
was torn down completely afterwards.

## What it proves
- **Genuine multi-bench + stable numeric labels** - `inspect` shows `[0] bench-a`,
  `[1] bench-b` in stable sorted order.
- **No silent guessing** - a bench op with no `--bench` on a multi-bench project
  errors and lists the benches (exit 1).
- **Label persists to BOTH stores** - `cwcli label ... 1 staging` writes the SQLite
  DB and the marker `/home/frappe/bench-b/.cwcli/.bench-label`
  (`{"schema": 1, "label": "staging"}`).
- **DB-loss recovery** - after `rm`-ing the SQLite cache, `inspect --update`
  rebuilds `staging` from the marker and re-persists it (shown twice).
- **`--bench` routing** - by index (`--bench 0` -> frappe 15.113.4, `--bench 1` ->
  14.0.0-BENCHB) and by label (`--bench staging` -> bench-b); unknown selector
  errors with the bench list.
- **Real dev workflows hit the selected bench** - `clear-cache` and `migrate`
  succeed on `--bench 0` (which owns `site-a.local`) and the same site-scoped
  command fails on `--bench staging` ("Site ... does not exist!"), proving routing.
- **`--yes`** - `start -y` proceeds; a stopped-container `run` refuses without `-y`
  in a non-TTY but auto-starts with `-y`; `config cache clear --all` refuses in a
  non-TTY without `-y` (cache preserved) and proceeds with `-y`.

Raw transcript follows.

---

Isolated HOME: /tmp/claude-1000/-home-cmckay--treehouse-cwcli-b7e614-2-cwcli/37b1217f-f6e7-46d4-a99e-a489bb6f07e8/scratchpad/e2e-home | project: cwcli-e2e-mbench | net: cwcli-e2e-net
### confirm 'label' command present in this build
│ inspect   Inspects a Project to find all Bench Instances, Sites, and Apps    │
│           within it. Caches the results for faster subsequent inspects.      │
│ label     Assign, clear, or list per-bench labels for a project.             │

### 1. inspect: genuine multi-bench instance, default numeric labels 0/1

$ cwcli inspect cwcli-e2e-mbench
Project cwcli-e2e-mbench
├── Bench [0] at /home/frappe/bench-a
│   ├── Available Apps (1)
│   │   └── frappe
│   └── Sites (0)
└── Bench [1] at /home/frappe/bench-b
    ├── Available Apps (1)
    │   └── frappe
    └── Sites (0)

$ cwcli inspect cwcli-e2e-mbench --json (indices + labels)
{
  "project_name": "cwcli-e2e-mbench",
  "bench_instances": [
    {
      "index": 0,
      "path": "/home/frappe/bench-a",
      "sites": [],
      "available_apps": [
        "frappe"
      ],
      "common_site_config": {
        "background_workers": 1,
        "file_watcher_port": 6787,
        "frappe_user": "frappe",
        "gunicorn_workers": 41,
        "live_reload": true,
        "rebase_on_pull": false,
        "redis_cache": "redis://127.0.0.1:13000",
        "redis_queue": "redis://127.0.0.1:11000",
        "redis_socketio": "redis://127.0.0.1:13000",
        "restart_supervisor_on_update": false,
        "restart_systemd_on_update": false,
        "serve_default_site": true,
        "shallow_clone": true,
        "socketio_port": 9000,
        "use_redis_auth": false,
        "webserver_port": 8000
      }
    },
    {
      "index": 1,
      "path": "/home/frappe/bench-b",
      "sites": [],
      "available_apps": [
        "frappe"
      ],
      "common_site_config": {
        "background_workers": 1,
        "file_watcher_port": 6788,
        "frappe_user": "frappe",
        "gunicorn_workers": 41,
        "live_reload": true,
        "rebase_on_pull": false,
        "redis_cache": "redis://127.0.0.1:13001",
        "redis_queue": "redis://127.0.0.1:11001",
        "redis_socketio": "redis://127.0.0.1:13001",
        "restart_supervisor_on_update": false,
        "restart_systemd_on_update": false,
        "serve_default_site": true,
        "shallow_clone": true,
        "socketio_port": 9001,
        "use_redis_auth": false,
        "webserver_port": 8001
      }
    }
  ]
}

### 2. multi-bench with NO --bench must ERROR (no silent guess)
$ cwcli run cwcli-e2e-mbench version   # expect error listing benches, exit!=0
Error: project 'cwcli-e2e-mbench' has multiple benches; specify one with --bench
<index|label>:
  [0] /home/frappe/bench-a
  [1] /home/frappe/bench-b
exit=1

### 3. assign a user label to bench 1 -> writes DB AND marker file
$ cwcli label cwcli-e2e-mbench 1 staging
✓ Labeled bench at /home/frappe/bench-b as 'staging'.
Benches in project cwcli-e2e-mbench:
  [0] (no label)  /home/frappe/bench-a
  [1] 'staging'  /home/frappe/bench-b

--- verify marker file INSIDE the bench (container-side) ---
$ docker exec ... cat /home/frappe/bench-b/.cwcli/.bench-label
{"schema": 1, "label": "staging"}

--- verify label in the SQLite DB (throwaway HOME cache) ---
$ sqlite3 $E2E_HOME/.cwcli/cache/cwc-cache.db 'select path,label from bench'
('/home/frappe/bench-a', None)
('/home/frappe/bench-b', 'staging')

### 4. DB-LOSS RECOVERY: delete the SQLite cache, inspect rebuilds label from marker
$ rm -f $E2E_HOME/.cwcli/cache/cwc-cache.db*  (simulate lost DB)
cache files after delete: 

$ cwcli inspect cwcli-e2e-mbench --update   # full inspect reads markers
{
  "project_name": "cwcli-e2e-mbench",
  "bench_instances": [
    {
      "index": 0,
      "path": "/home/frappe/bench-a",
      "sites": [],
      "available_apps": [
        "frappe"
      ],
      "common_site_config": {
        "background_workers": 1,
        "file_watcher_port": 6787,
        "frappe_user": "frappe",
        "gunicorn_workers": 41,
        "live_reload": true,
        "rebase_on_pull": false,
        "redis_cache": "redis://127.0.0.1:13000",
        "redis_queue": "redis://127.0.0.1:11000",
        "redis_socketio": "redis://127.0.0.1:13000",
        "restart_supervisor_on_update": false,
        "restart_systemd_on_update": false,
        "serve_default_site": true,
        "shallow_clone": true,
        "socketio_port": 9000,
        "use_redis_auth": false,
        "webserver_port": 8000
      }
    },
    {
      "index": 1,
      "path": "/home/frappe/bench-b",
      "sites": [],
      "available_apps": [
        "frappe"
      ],
      "label": "staging",
      "common_site_config": {
        "background_workers": 1,
        "file_watcher_port": 6788,
        "frappe_user": "frappe",
        "gunicorn_workers": 41,
        "live_reload": true,
        "rebase_on_pull": false,
        "redis_cache": "redis://127.0.0.1:13001",
        "redis_queue": "redis://127.0.0.1:11001",
        "redis_socketio": "redis://127.0.0.1:13001",
        "restart_supervisor_on_update": false,
        "restart_systemd_on_update": false,
        "serve_default_site": true,
        "shallow_clone": true,
        "socketio_port": 9001,
        "use_redis_auth": false,
        "webserver_port": 8001
      }
    }
  ]
}
RECOVERED benches: [(0, None, '/home/frappe/bench-a'), (1, 'staging', '/home/frappe/bench-b')]

--- confirm the recovered label is back in the fresh DB ---
('/home/frappe/bench-a', None)
('/home/frappe/bench-b', 'staging')

### 5. --bench routing: bench-a=frappe 15.113.4, bench-b(staging)=frappe 14.0.0-BENCHB

$ cwcli run cwcli-e2e-mbench --bench 0 version        # -> bench-a
frappe 15.113.4
exit=0

$ cwcli run cwcli-e2e-mbench --bench 1 version        # -> bench-b by index
frappe 14.0.0-BENCHB
exit=0

$ cwcli run cwcli-e2e-mbench --bench staging version  # -> bench-b by LABEL
frappe 14.0.0-BENCHB
exit=0

$ cwcli run cwcli-e2e-mbench --bench nope version     # unknown selector -> error
Error: No bench 'nope' in project 'cwcli-e2e-mbench'.
Available benches (address with --bench <index|label>):
  [0] /home/frappe/bench-a
  [1] 'staging' /home/frappe/bench-b
exit=1

### 6. real dev workflows on the SELECTED bench (bench-a has site-a.local; bench-b does not)

$ cwcli run cwcli-e2e-mbench --bench 0 -- --site site-a.local clear-cache   # real workflow on bench-a
exit=0

$ cwcli run cwcli-e2e-mbench --bench 0 -- --site site-a.local migrate        # real migrate on bench-a
Migrating site-a.local
Executing `after_migrate` hooks...

Queued rebuilding of search index for site-a.local

exit=0

$ cwcli run cwcli-e2e-mbench --bench staging -- --site site-a.local clear-cache  # bench-b has NO such site -> proves routing
Site site-a.local does not exist!
exit=1

### 7. --yes / -y on the real instance

$ cwcli start cwcli-e2e-mbench -y   # proceeds without prompting (containers already up)
✓ Started bench (logs: /tmp/bench-cwcli-e2e-mbench.log)
View logs with: cwcli logs cwcli-e2e-mbench

Start command finished.
exit=0

--- auto-start via -y: stop the frappe container, then run WITHOUT -y (non-TTY) vs WITH -y ---
frappe container stopped (exited)

$ cwcli run cwcli-e2e-mbench --bench 0 version   # no -y, non-TTY -> must NOT silently start
Warning: Frappe container for project 'cwcli-e2e-mbench' is not running.
Warning: Input is not a terminal (fd=0).

Aborted.
exit=1
frappe status after no-yes run: exited

$ cwcli run cwcli-e2e-mbench --bench 0 -y version   # -y -> auto-starts container, then runs
/home/frappe/bench-a. Select another with --bench <index|label>:
  [0] /home/frappe/bench-a
  [1] 'staging' /home/frappe/bench-b
✓ Containers started for 'cwcli-e2e-mbench'
View logs with: cwcli logs cwcli-e2e-mbench
frappe 15.113.4
exit=0
frappe status after -y run: running

### 7c. destructive 'config cache clear --all': non-TTY without -y must REFUSE
$ cwcli config cache clear --all      # no -y, non-TTY -> refuse, exit!=0, cache preserved
Error: Refusing to clear the entire cache without confirmation. Re-run with 
--yes to clear it non-interactively.
exit=1
benches still cached: 2

$ cwcli config cache clear --all -y   # -y -> proceeds
Proceeding without confirmation (--yes).
Entire cache has been cleared.
exit=0

--- final: after cache wipe, inspect recovers labels from markers AGAIN ---
$ cwcli inspect cwcli-e2e-mbench -u --json
benches: [(0, None, '/home/frappe/bench-a'), (1, 'staging', '/home/frappe/bench-b')]
### teardown
cwcli-e2e-frappe
cwcli-e2e-mariadb
cwcli-e2e-redis
cwcli-e2e-net
throwaway HOME removed
--- confirm nothing labeled with the E2E project remains, captain's 'ners' untouched ---
e2e containers left: 0
ners still running: 4 containers
