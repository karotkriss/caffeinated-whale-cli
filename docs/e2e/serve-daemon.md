# E2E: `cwcli serve` - the three tiers, the quiet in between, and the WSL/Windows boundary

Real-instance validation of the streaming Console daemon (`core/fleet.py`, `commands/serve.py`).
The committed suite is `tests/e2e/test_serve_e2e.py`, which CI runs on the v14/v15/v16 matrix.
This document records the hand-driven run that produced the measurements, because two of the properties below cannot be observed from CI at all: the Windows-browser boundary needs a Windows host, and the reconnect re-bootstrap needs the event stream to be dropped on purpose.

Everything here ran against ONE throwaway Frappe v16 instance (`cwp2serve`, ports 24000/25000, site `p2.localhost`) built and destroyed inside this task, under an isolated `CWCLI_HOME=/tmp/cwp2/home`.
The five real instances on the box were stopped throughout and were never started, probed, or modified.

## Environment

WSL2 kernel `6.6.114.1-microsoft-standard-WSL2`, NAT networking (no `networkingMode=mirrored`), WSL IP `172.24.87.141`, Windows host `172.24.80.1`.
Daemon run as `cwcli serve --port 8765 --interval 2`.

## 1. The WSL-to-Windows boundary

The boundary was measured before anything was built on top of it, with a throwaway listener on 8765:

| Bind address in WSL | `http://localhost:8765` from Windows | `http://172.24.87.141:8765` from Windows |
| --- | --- | --- |
| `127.0.0.1` | reachable | not applicable |
| `0.0.0.0` | reachable | reachable |

Both routes work on this build.
The top-left cell is the load-bearing one and it is easy to misread: **a Windows browser DOES reach a WSL-only `127.0.0.1` listener**, because WSL's default NAT mode forwards `localhost` into the distro ([Microsoft's own networking documentation](https://learn.microsoft.com/en-us/windows/wsl/networking) says so, and the row above measures it).
`cwcli serve` was originally shipped with a `0.0.0.0` default on the reasoning that it is the only bind serving both routes; that default was later found to leave the action endpoint drivable, without credentials, from any host on the network, and the default is now `127.0.0.1`.
What this table actually justifies is the narrower claim: the WSL-IP route is what survives `localhostForwarding` being turned off, and it is what a browser on another machine needs, so `--host 0.0.0.0` remains available - now gated behind `CWCLI_SERVE_TOKEN`, which a non-loopback bind refuses to start without.
The startup banner prints the address it is reachable at, resolved by a UDP `connect` to TEST-NET-1 (which sends nothing and only makes the kernel pick a source interface).
Resolving the hostname - the obvious alternative - is wrong here: on this distro it answers `127.0.1.1` and nothing else.

## 2. A Windows browser, connected to the WSL daemon

Microsoft Edge on Windows, at `http://172.24.87.141:8765/?focus=cwp2serve`.
The WSL side shows the connections arriving from the Windows host rather than from loopback, which is what makes the attribution unambiguous:

```
$ ss -tnH state established '( sport = :8765 )'
  peer=172.24.80.1:51290
  peer=172.24.80.1:61096
```

Two connections: the page, and the `EventSource`.

**First capture, after 3.5 minutes connected to a steady instance:**

```
cwcli serve   open   focus=cwp2serve

instance     docker    health     web                       processes
cwp2serve    running   running    200 :8000 p2.localhost    schedule socketio watch w...
frappe15     exited    offline    -                         -
frappe16     exited    offline    -                         -
gcaa         exited    offline    -                         -
ners         exited    offline    -                         -
visa15       exited    offline    -                         -

9:35:13 PM SNAPSHOT 6 instances
```

One line. The fleet is visible and the feed is silent.

**Second capture, after the state changes in section 4 were driven:**

```
10:12:12 PM FAST     cwp2serve -> running
10:12:09 PM FAST     cwp2serve -> running
10:12:06 PM FAST     cwp2serve -> degraded
10:11:55 PM FAST     cwp2serve -> degraded
10:11:53 PM INSTANT  cwp2serve start(frappe) -> unknown
10:11:42 PM INSTANT  cwp2serve kill(frappe)  -> offline
10:11:40 PM FAST     cwp2serve -> unknown
10:11:28 PM FAST     cwp2serve -> running
10:11:26 PM FAST     cwp2serve -> running
10:11:23 PM FAST     cwp2serve -> degraded
10:10:39 PM FAST     cwp2serve -> running
10:10:37 PM FAST     cwp2serve -> running
10:10:34 PM FAST     cwp2serve -> degraded
10:07:06 PM FAST     cwp2serve -> running
10:07:03 PM SNAPSHOT 6 instances
```

Note the gap from 10:07:06 to 10:10:34: three and a half minutes of a running instance, zero deltas.
Note also `start(frappe) -> unknown` at 10:11:53 - the INSTANT tier reports the container came up and refuses to call it healthy.

## 3. Delta suppression on a real instance

25 seconds of a steady instance at a 2-second probe interval, recorded by an SSE client:

```
1784859057.345  HOST   A: 25s STEADY (delta suppression)
1784859054.385  SNAPSHOT n=6
1784859069.385  KEEPALIVE
1784859082.349  HOST   A: steady window closed
```

One snapshot, one keepalive, **zero deltas**, across roughly 12 probe cycles.

This is the test a fixture cannot fail.
Underneath it every process's `uptime_s` advanced by 2 each cycle, `cpu_pct` and `rss_kb` moved, and `probe_ms` differed on every single read (388.7, 407.3, 444.2, 517.9, 767.5, 768.2, 869.4, 954.2 ms were all observed).
Any one of those inside `health_key()` would have published a delta per tick.
The design spike found exactly that by accident, with `probe_ms` in the compared payload: 8 deltas in 8 cycles of an instance that never changed.

## 4. The three tiers against real state changes

Host actions and client receipts, interleaved on one clock.

### FAST - restarting ONE supervisord program

```
1784859082.353  HOST   restart --process web: begin
1784859083.488  DELTA  tier=fast  overall=degraded  http=None  probe_ms=444.2
1784859086.006  DELTA  tier=fast  overall=running   http=200   probe_ms=518.0
1784859087.347  HOST   restart --process web: returned
1784859088.413  DELTA  tier=fast  overall=running   http=200   probe_ms=407.3
```

**Zero INSTANT deltas.** Docker emits no event of any kind for a supervisord program restart, which is the whole reason the FAST tier exists.
The browser learned the program was healthy again at 086.006, **1.34 s before the CLI returned**.
The trailing delta at 088.413 is the program moving `STARTING` -> `RUNNING`, a genuine state change.

### INSTANT - container down and up

```
1784859099.353  HOST    docker stop frappe: begin
1784859100.485  DELTA   tier=fast     overall=unknown   running=True   err=supervisor.process_state_unknown
1784859102.361  HOST    docker stop frappe: returned
1784859102.402  DELTA   tier=instant  overall=offline   running=False  docker=exited  benches=0  cause=kill
1784859112.367  HOST    docker start frappe: begin
1784859113.117  DELTA   tier=instant  overall=unknown   running=True   docker=running benches=0  cause=start
1784859113.231  HOST    docker start frappe: returned
1784859115.441  DELTA   tier=fast     overall=degraded  running=True   http=None      probe_ms=954.2
1784859125.238  HOST    cwcli start: begin
1784859129.271  HOST    cwcli start: returned
1784859129.726  DELTA   tier=fast     overall=running   http=200       probe_ms=767.5
```

Four things this establishes:

- The delta at 100.485 is the honest-unknown rule firing on its own: the probe caught the container mid-teardown, could not read the supervisord state, and said **`unknown` with a `probe_error`** rather than reporting the last good token or guessing a bad one.
- The stop delta at 102.402 is internally consistent - `running=False`, `docker=exited`, `benches=0` - which is the fix described in section 6.
- The start delta at 113.117 arrived **114 ms before `docker start` returned**, and carries `overall=unknown` with an empty bench list. A container being up is not a bench serving.
- The FAST tier is what resolves it: `degraded` while supervisord was not yet relaunched, then `running` with HTTP 200 at 129.726, 0.45 s after `cwcli start` returned.

### LAZY - the on-demand detail read

```
$ curl -s http://127.0.0.1:8765/api/instance/cwp2serve/detail
{
  "project": "cwp2serve",
  "served_from": "partial",
  "degraded": false,
  "benches": [{"index": 0, "path": "/workspace/frappe-bench", "label": null,
    "current_site": null, "default_site": null, "available_apps": ["frappe"],
    "sites": [{"name": "p2.localhost",
               "installed_apps": ["frappe 16.28.0 version-16"],
               "installed_apps_verified": false,
               "has_site_config": true}]}]
}
```

`served_from: "partial"` (the T2 read-only drift check) and `installed_apps_verified: false` come through untouched - the tier's freshness labels are `core.inspect`'s own, not re-derived.

### Cost

| Endpoint | Median |
| --- | --- |
| `GET /api/snapshot` | under 10 ms (in-memory; no Docker call) |
| `GET /api/instance/<p>/detail` (T2 partial) | 0.70, 0.70, 0.75 s |
| FAST probe, per running instance, no web check | 361 ms |
| FAST probe, per running instance, web check on | 388-518 ms |

The 361 ms sits above the spike's 252 ms for `fused_probe` alone, and the difference is `core.status`'s own prologue around it - the container reload plus the `common_site_config.json` read for the port.
That is the daemon-side caching the spike listed as R3, deliberately not taken here: it is a second cache with its own invalidation, and 361 ms already fits a 2-second tick with room for six instances.

Stopped instances carry `probe_ms: null`. They were never probed, and the model says so.

## 5. Reconnect re-bootstrap, against real Docker

The rule under test is that a lost event stream RE-BOOTSTRAPS and never replays with `since=`.
Driving it needs the stream dropped on purpose, so this ran the shipped `core.fleet.event_loop` against the real Docker daemon with the reconnect held shut by a gate, and asserted inside the wrapper that no `since=`/`until=` is ever passed.

```
  0.020s  connect #1 filters=['start','stop','die','kill','restart','destroy']
  3.001s  --- dropping the event stream; the reconnect is GATED SHUT ---
  4.001s  --- stopping the container WHILE DISCONNECTED (this event reaches nobody) ---
  6.185s  docker stop returned (2.18s)
 11.184s  still disconnected: connects=1, model still says running=True
 11.184s  --- opening the gate: the daemon may now reconnect ---
 11.188s  connect #2
 11.293s  DELTA tier=instant overall=offline running=False
 11.384s  RECOVERED: overall=offline running=False benches=0
 11.384s  connects=2 deltas_after_drop=1
```

The container's `kill`/`die` events were emitted at 4-6 s to a stream nobody held, and five seconds later the model still believed the instance was running.
The reconnect at 11.188 s published the correct state **105 ms later**, from one `list_instances()` read, in exactly one delta.

A `since=` replay would have looked correct here and returned nothing: Docker's event buffer is a fixed 256-entry ring, and this daemon's own probe exec traffic flushes it in about 51 seconds.

## 6. Two defects this run found and fixed

**A self-contradicting row.** The first lifecycle run published, 54 ms before the stop event arrived, an instance carrying `container_running: true` beside `overall: offline` and an empty bench list.
The FAST probe had observed the container was gone and adopted `core.status`'s `offline`, while the model still held the container facts the event stream owns.
`Fleet.probe` now re-bootstraps when the probe finds the container gone: one Docker read, one consistent delta, and the event that follows is correctly suppressed as no change.
Pinned by `tests/test_core_fleet.py::TestProbeAndModelStayConsistent`.

**A probe timing on an instance nothing probed.** A stopped instance kept the `probe_ms`/`probed_at` of its last live read, date-stamping an answer no probe had produced. Both are now cleared with the bench rows.

On a failed read, retained bench and process rows keep the `probed_at` and `probe_ms` of the successful read that produced them.
The failed attempt has its own `probe_failed_at` timestamp alongside `overall: unknown` and `probe_error`.

## 7. Not a cwcli defect: `start`/`stop`/`restart` appearing to hang

Several times during this run, `cwcli start`, `cwcli stop` and `cwcli restart --process` hung indefinitely while `cwcli status` returned instantly.
Adding `< /dev/null` fixed it every time.
The cause is the harness's inherited stdin - a pipe that is neither a TTY nor closed, so a prompt blocks forever waiting on input that never arrives.
cwcli behaves correctly on a genuine non-TTY, which is what `tests/e2e/harness.py` drives (`stdin=subprocess.DEVNULL`).
Recorded here because the symptom looks exactly like a daemon deadlock and cost real time to attribute.

## What CI covers, and what it cannot

`tests/e2e/test_serve_e2e.py` covers the three tiers, delta suppression, the honest-unknown states, the focus-driven web-probe cadence, and the detail endpoint's freshness labels, on every leg of the matrix.

Two things stay here rather than in CI:

- **The Windows browser.** CI runners are Linux; there is no Windows host and no WSL boundary to cross.
- **The reconnect re-bootstrap.** Inducing a Docker event-stream drop means breaking the daemon's own socket, which no supported API exposes. Faking it in the E2E tier would test the fake, so the rule is pinned by unit tests instead (`tests/test_core_fleet.py::TestEventLoop` asserts one full rebuild per connect and the absence of `since=`/`until=` on the call), and measured against real Docker in section 5.

## Phase 4 tightening audit (2026-07-24)

An audit-then-fix pass over the shipped Console, driven hand-on against ONE throwaway Frappe v16 instance (`cwp4con`, ports 14000/15000, isolated `CWCLI_HOME`) built and destroyed inside the task.
Browser evidence is Linux Chrome via chrome-devtools-axi; Windows interop was unavailable on the box during this pass, so no Windows-browser claims are made here (section 2 above remains the Windows evidence).
One difference from the phase-2 run: the captain's `gcaa` instance was already running throughout, so the daemon passively health-probed it exactly as `cwcli serve` probes any running instance; no action, focus, or detail read ever targeted it.

**Daemon properties re-verified live, unchanged:**

- Delta suppression: 62 seconds connected to the steady instance produced ZERO deltas and 4 keepalives (the 15s cadence exactly), with the web probe running every cycle for the focused instance.
- Client disconnects: 30 rapid open/drop cycles against `/api/events` left the daemon healthy and the handler threads drained back to baseline (4) within two keepalive cycles; focus retracted (the closed tab's instance returned to `web_probed: false`).
- Action serialization: two simultaneous `restart_process` POSTs for the same project ran strictly one-after-the-other (4.7s and 10.0s wall for identical ~5s restarts), both 200.
- Reconnect: killing and restarting the daemon under an open page re-bootstrapped through a fresh full snapshot; the browser's own retry loop covers network-level failure.
- Honest unknowns rendered on real state: a running-but-never-`cwcli start`ed instance shows "never started" / "port unknown, not probed" / "processes unknown"; a bench whose supervisor died shows `degraded` with `site:port -> no answer` (`web_http_code: null` under `web_probed: true`); a cached app list shows "remembered - could not verify"; never 0, a dash, or a green pill from container-up alone.
- Keyboard walk under churn: tablist arrows wrap correctly at both edges; focus survived select-triggered re-renders, SSE delta re-renders mid-action, and the full stop -> container-only-start -> start round-trip driven entirely from the rail; the sr-only status region announced "in progress" and "accepted" exactly once each.

**Frontend gaps the audit found, fixed in this pass (each pinned by `tests/test_serve_frontend.py::TestSnapshot::test_the_console_ui_keeps_its_tightening_invariants`):**

- The app shell was `min-height: 100vh`, so the panel `overflow: auto` rules were dead: the page scrolled as a whole, the event log sat off-screen, and the action rail scrolled away.
  Now `height: 100vh` with explicit grid rows (page-scroll behavior is kept below 760px, and the mid-width rail band is capped at 42vh so it cannot squeeze the tree out).
- A lost connection was an 8px amber dot while every pill stayed brightly "live": stale data presented as live.
  Now a visible banner names the loss and the last-known-state timestamp, pills desaturate while stale, and a dedicated live region announces the loss and the recovery once each; a deliberate refocus reconnect shows none of this.
- A hard-closed `EventSource` (non-200/wrong content type; the browser does NOT retry those) left "reconnecting" on screen forever with nothing retrying.
  Now a closed source schedules a fresh `connectEvents()`, which is by construction a full re-bootstrap, never a replay.
- Nothing rendered before the first snapshot, and the empty-tree message claimed "No instances found" when the truth was "never asked".
  The shell now renders honest empty states at boot, and the no-instances claim requires a snapshot to back it.
- The event log was an `aria-live` region rewritten wholesale on every delta - a screen reader re-announced the whole log each time.
  It is a visual feed now; announcements go through the two dedicated status regions only.
- Tree twisties exposed no `aria-expanded`, and the selected tree row was styled but not exposed (`aria-current` now set).
- A removed instance could silently drop keyboard focus to `<body>`; the tree now falls back to its first row, guarded so it never steals focus that belongs to another pane.
