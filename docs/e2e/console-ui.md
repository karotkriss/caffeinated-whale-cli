# Historical E2E evidence: Console UI

Real-instance validation for Console phase 3.
This is historical evidence for the retained implementation; the Console entry in `AGENTS.md` owns its current surface-availability contract.
The retained implementation is covered directly by unit tests.
The test used the editable worktree build through `uv run cwcli`, a worktree-local `CWCLI_HOME`, one throwaway instance, and a Windows Edge browser reaching the daemon through WSL localhost forwarding.

## Environment

- Date: 2026-07-24.
- `CWCLI_HOME=.tmp/cwcli-console-p3/home`, so the captain's real cwcli home was not read or written.
- Throwaway instance: `cwe2e-console-p3`.
- Frappe: default version 16 via `cwcli init cwe2e-console-p3 --version 16 --port 28000 --site console.localhost`.
- Console daemon: `cwcli serve --host 0.0.0.0 --port 8777 --interval 1`, which was the implementation default at the time.
  That invocation now requires `CWCLI_SERVE_TOKEN` and the default bind is `127.0.0.1`; nothing else in this record changes, because the two reachability proofs immediately below both went through `localhost`, which loopback serves.
- Windows reachability: Windows PowerShell reached `http://localhost:8777/api/snapshot` with HTTP 200.
- Browser proof: Windows Edge opened `http://localhost:8777/?focus=cwe2e-console-p3`.

## Start transition from the action rail

The Console selected `cwe2e-console-p3` in the left tree and showed the approved master-detail layout: instance tree on the left, one detail pane in the center, and the narrow action rail on the right.
The rail contained only `Start instance`, `Stop instance`, `Restart instance`, and `Restart process`.

The Windows Edge page clicked `Stop instance` from the rail.
The selected detail pane changed to:

```
cwe2e-console-p3
offline
Docker exited - container stopped - probe never completed
```

The Windows Edge page then clicked `Start instance` from the rail.
The page stream showed the honest transition:

```
INSTANT cwe2e-console-p3 start(mariadb) -> unknown
INSTANT cwe2e-console-p3 start(frappe) -> unknown
FAST cwe2e-console-p3 -> degraded
FAST cwe2e-console-p3 -> running
```

The final selected detail pane showed `Health running`, `Docker running`, and the process table with `PROCESS web` running.
The CLI cross-check at the same point reported `web console.localhost:8000 -> 200`.

## Process restart from the action rail

To create the stuck-process condition without touching any real instance, only the throwaway's `web` supervisor program was stopped inside `cwe2e-console-p3-frappe-1`.
The CLI cross-check reported:

```
cwe2e-console-p3: degraded
web down state=STOPPED
```

The Windows Edge Console page then showed the same degraded state:

```
cwe2e-console-p3
degraded
/workspace/frappe-bench console.localhost:8000 -> no answer
PROCESS web pid unknown
STOPPED
```

The Windows Edge page selected `PROCESS web` in the left tree and clicked `Restart process` from the rail.
The selected process pane moved through:

```
PROCESS web
STOPPED
pid unknown

PROCESS web
STARTING
pid 4688

PROCESS web
STARTING
pid 4711

PROCESS web
RUNNING
pid 4711
```

The event log for that click showed:

```
FAST cwe2e-console-p3 -> degraded
FAST cwe2e-console-p3 -> running
ACTION restart process cwe2e-console-p3
```

## SSE regression found during browser proof

The first Windows Edge proof exposed a real stream bug.
The daemon's snapshot endpoint had the degraded `web STOPPED` state, but the open Edge page still had the previous healthy event log.
The cause was the SSE disconnect watcher reading from the request side of the socket.
Windows Edge can half-close that side while it is still reading the response, which made the hub unregister a live browser client.

The fix is to keep the SSE client registered until a response write fails.
A regression test now covers a raw SSE client that sends the request, shuts down only its write side, and still receives a later delta.

## Cleanup

The proof used `cwcli rm cwe2e-console-p3 --yes --volumes --no-backup` only for the disposable instance teardown.
No Docker prune was run.
Post-cleanup checks found no containers, no named volumes, and no networks with `com.docker.compose.project=cwe2e-console-p3`.
The dedicated Windows Edge temp profile `C:\Windows\Temp\cwcli-console-p3-edge` was removed.
