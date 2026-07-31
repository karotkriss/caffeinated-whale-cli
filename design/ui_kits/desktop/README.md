# UI kit — Caffeinated Whale Desktop (Console)

A click-through recreation of the Tauri desktop shell's main window. It is a **face on the CLI**, not a
separate product: every pane maps to a real `cwcli serve` endpoint and every action maps to a real verb.

Open `index.html`.

## What it recreates

| Region | Source of truth |
| --- | --- |
| Fleet tree (instance → bench → process) | `core/fleet.py` `InstanceState` → `status.BenchStatus` → `ProcessHealth` |
| Detail pane header + facts | `GET /api/instance/<project>/detail` (the LAZY tier) |
| Processes table | `core.status(..., fused=True)` — the FAST tier |
| Logs tab | `GET /api/instance/<project>/logs` — a **bounded** tail |
| Action rail | `POST /api/action` — the Tier A safe set only |
| Event stream footer | `GET /api/events` (SSE), tiers INSTANT / FAST |
| Status bar | the daemon the Rust shell supervises on loopback |

## What it does not recreate

The repo's own `commands/console.html` was deliberately **not** used as a visual reference (the brief said
to ignore all HTML in the repo). Its *information architecture* — master-detail with a narrow right-hand rail
— is documented in `desktop/README.md` and `docs/e2e/console-ui.md`, and that is what this kit follows.

## Things you can click

- Expand instances, benches and processes in the tree; selection drives the rail.
- **Start / Stop / Restart instance** — watch the honest transition land in the event log:
  the container event arrives first as `unknown`, then the FAST tier resolves it.
- Select a process, then **Restart process** — the row moves STOPPED → STARTING → RUNNING.
- **New** in the tree header — the init dialog, with the CLI's rotating tips during the long phase.
- **Remove instance** — a destructive dialog that names the exact command instead of pretending it is safe.
- Search filters instances by project, app and site, the same cache `cwcli where` reads.

## Files

- `data.js` — fixture shaped exactly like the fleet model's JSON.
- `TitleBar.jsx` — window chrome + status bar.
- `FleetTree.jsx` — the left tree.
- `DetailPane.jsx` — header, instance overview, bench tabs (processes / logs / apps / sites).
- `EventLog.jsx` — the SSE delta strip.
- `Dialogs.jsx` — init and remove.
- `App.jsx` — state, the fake action endpoint, and the layout.
