# E2E: an unrecognised trailing flag on a variadic-project command

Real-instance validation for the fix that turns an unrecognised option token trailing a variadic `project_name` into a usage error instead of another project name.

- Instance: `cwe2e-argswallow`, a throwaway Frappe `version-16` instance built with `cwcli init`, ports 14000/15000.
- Isolation: `CWCLI_HOME=/tmp/cwe2e-argswallow-home`, binary is the worktree's own editable install (`uv run cwcli`).
- Date: 2026-07-22.
- Teardown: `cwcli rm cwe2e-argswallow --yes`, then the temp home removed; no other instance was touched.

## 1. The defect, before the fix

The instance was running (4 containers up) before the command, so the check below is not vacuous.

```
$ docker ps --filter label=com.docker.compose.project=cwe2e-argswallow
cwe2e-argswallow-redis-queue-1  Up 5 minutes
cwe2e-argswallow-redis-cache-1  Up 5 minutes
cwe2e-argswallow-frappe-1       Up 5 minutes
cwe2e-argswallow-mariadb-1      Up 5 minutes
running_count=4

$ cwcli stop cwe2e-argswallow --benhc 1
Attempting to stop 3 project(s)...
Instance 'cwe2e-argswallow' stopped.
Error: Project '--benhc' not found.
Error: Project '1' not found.

Stop command finished.

running_count_after=0
cwe2e-argswallow-redis-queue-1  Exited (0) 10 seconds ago
cwe2e-argswallow-redis-cache-1  Exited (0) 9 seconds ago
cwe2e-argswallow-frappe-1       Exited (137) 8 seconds ago
cwe2e-argswallow-mariadb-1      Exited (137) Less than a second ago
```

`--benhc` and `1` were absorbed as further project names, and the whole instance was stopped.
Nothing in the output says the flag was not understood.

## 2. The fixed behaviour

The instance was started again and confirmed running (4 containers up) before each command below.

Non-interactive (stdin is a pipe):

```
$ printf '' | cwcli stop cwe2e-argswallow --benhc 1
Error: No such option: --benhc
Run 'cwcli stop --help' to see the available options. Nothing was changed.
exit=2
running_count_after=4
```

Interactive (driven through a real pty, so `isatty()` is true):

```
Error: No such option: --benhc
Run 'cwcli stop --help' to see the available options. Nothing was changed.
isatty=True exit=2
running_count_after=4
```

The same refusal on the other three variadic-project commands, against the same live instance:

```
$ cwcli start cwe2e-argswallow --benhc 1
Error: No such option: --benhc
Run 'cwcli start --help' to see the available options. Nothing was changed.
exit=2

$ cwcli restart cwe2e-argswallow --typo
Error: No such option: --typo
Run 'cwcli restart --help' to see the available options. Nothing was changed.
exit=2

containers_still_up=4
```

## 3. The delete verb refuses without deleting

The instance, its project directory, and its named volume all existed first:

```
$ cwcli ls
cwe2e-argswallow  running  14000-14005, 15000-15005
/tmp/cwe2e-argswallow-home/projects/cwe2e-argswallow
volume cwe2e-argswallow_mariadb-data

$ cwcli rm cwe2e-argswallow --benhc 1 --yes
Error: No such option: --benhc
Run 'cwcli rm --help' to see the available options. Nothing was changed.
exit=2

$ cwcli ls
cwe2e-argswallow  running  14000-14005, 15000-15005
/tmp/cwe2e-argswallow-home/projects/cwe2e-argswallow
volume cwe2e-argswallow_mariadb-data
containers=4
```

## 4. A legitimate dash-leading value still works

A bench label may start with a dash, so the fix must not reject a value on the leading dash alone.
Bench 0 was labelled `-staging` and addressed by that label, in both the spaced and inline forms.

```
$ cwcli label cwe2e-argswallow 0 -- -staging
✓ Labeled bench at /workspace/frappe-bench as '-staging'.

$ cwcli status cwe2e-argswallow
bench 0  /workspace/frappe-bench ('-staging'): running (supervisor up)
  web development.localhost:8000 -> 200
  web/socketio/watch/schedule/worker  all up

$ cwcli stop cwe2e-argswallow --bench -staging
Bench '/workspace/frappe-bench' of 'cwe2e-argswallow' stopped
(schedule, socketio, watch, web, worker); other benches and containers are untouched.
exit=0  containers_still_up=4

$ cwcli status cwe2e-argswallow
bench 0  /workspace/frappe-bench ('-staging'): online (supervisor down)

$ cwcli start cwe2e-argswallow --bench=-staging
✓ Started bench (logs: /workspace/frappe-bench/logs)
exit=0
bench 0  /workspace/frappe-bench ('-staging'): running (supervisor up)
```

The dash-leading label resolved, acted on exactly that bench, and left the containers up.

## 5. The good paths are unchanged

```
$ cwcli stop cwe2e-argswallow
Attempting to stop 1 project(s)...
Instance 'cwe2e-argswallow' stopped.
exit=0  running_count_after=0

$ cwcli rm cwe2e-argswallow --yes
  Backed up 1 site(s) to .../archive/cwe2e-argswallow_20260722_231723/backups
  Configuration archived to .../archive/cwe2e-argswallow_20260722_231723
  Removed 1 named volume(s) for 'cwe2e-argswallow'
  Removed network 'cwe2e-argswallow_default' for 'cwe2e-argswallow'
  Removed project directory .../projects/cwe2e-argswallow
✓ Successfully removed 4 container(s)
exit=0
```

Afterwards zero containers and zero volumes carried the project label, and every other instance on the host was left in the state it started in.

## Observation, not a defect

`cwcli label <project> 0 -staging` is refused by the option parser (`No such option: -s`), because `new_label` is a plain positional argument; `--` is the standard escape and is what the run above used.
That is ordinary CLI behaviour and the opposite of the failure fixed here, where a bad token was accepted silently.
