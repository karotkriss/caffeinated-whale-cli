# cwcli desktop shell (Phase 1)

A cross-platform [Tauri 2](https://v2.tauri.app/) desktop shell that runs the existing cwcli **Console** GUI as a native window.

This is Phase 1 of the desktop app: the thin native shell over the unchanged Python core.
It performs preflight, supervises exactly one `cwcli serve` daemon over loopback, keeps single-instance behaviour, restores window state, writes a log file, and points its WebView at the daemon's own Console page.
The full architecture and the decisions behind it were ruled by the captain; this README records how the shell realises them and how to build and run it.

## What the shell owns, and what it must never do

The boundary is a hard rule each side obeys.

| Layer | Owns | Must never |
| --- | --- | --- |
| **Rust shell** (`src-tauri/`) | Preflight, starting and supervising one `cwcli serve`, single-instance, window state, native window lifecycle, the log file, and the local first-run/error surface. | Talk to Docker, parse a bench config, know what a site is, decide whether an action is safe, or hold any consent logic. |
| **Python core** (`src/caffeinated_whale_cli/`) | Every Docker, bench, site, app, supervisord and Frappe rule, consent, and the typed `Result`/`CwcliError` envelope. | Change for the desktop app. Phase 1 touches it in only two small, GUI-agnostic ways (below). |
| **`cwcli serve`** | The GUI's whole protocol surface (fleet model, SSE deltas, detail/logs/where/doctor/url reads, the Tier A action endpoint). | Grow GUI-only rules the CLI and `axi` do not share. |

The shell is a few hundred lines of Rust and contains **zero** Docker or Frappe business rules.
It deliberately does **not** depend on `bollard` (a Rust Docker client), which would fork cwcli's Docker knowledge into a second implementation, and it does **not** install `tauri-plugin-shell`, which would hand the frontend an arbitrary-command surface.

## How the daemon is launched (the internal entry point)

`cwcli serve` is deliberately **withheld** from the released `cwcli` and `cwcli axi` surfaces: it is not registered in `main.py`, because the Console was not ready to ship as a public command.
The desktop app is its sanctioned consumer, so the shell needs a way to start the daemon that does **not** re-expose `serve` on those surfaces.

The shell runs the serve **module** as a script:

```
<python> -m caffeinated_whale_cli.commands.serve --host 127.0.0.1 --port <p>
```

where `<python>` is the interpreter that runs the user's installed `cwcli`, read from the console script's shebang (the interpreter that runs `cwcli` is exactly the one that can import its package).
Running the module is an internal path: it adds no `[project.scripts]` binary and no Typer command, so nothing a `cwcli --help` or `cwcli axi` listing can discover changes, yet the shell has a real, stable launch path.
`serve.py` gained a `if __name__ == "__main__": typer.run(serve)` block for exactly this; `tests/test_axi.py::TestServeUnreachable` pins that the module entry works while `serve` stays absent from both public surfaces.

On Windows the same command is wrapped in `wsl.exe -d <distro> -- ...`, because cwcli, Docker and Frappe live inside WSL.

## Bring-up sequence and the local error surface

The window opens immediately on a bundled local splash (`dist/index.html`), then a background thread drives bring-up:

```
splash shown -> reserve a loopback port -> locate cwcli -> spawn the daemon
             -> wait until it answers HTTP 200 -> navigate the WebView to it
```

Every fallible step renders into the splash, never a blank page.
This is Phase 0 finding **P1-1**: in the remote-origin shape the whole UI *is* the daemon's page, so when the daemon is slow or absent there is no page to fall back to.
The shell owns its own branded first-run/error screen, and pushes progress and typed failures into it (`window.__cwcliStatus` / `window.__cwcliError`).

The window is shown, unminimized, and focused explicitly rather than trusting the default, because the Phase 0 Windows binary opened minimized (carry-in item 6).

## The content CSP lives in the daemon, not the config

In the Phase 1 shape the WebView loads the Console from the daemon's own loopback origin, which is a **remote** origin from Tauri's point of view.
Tauri can only inject a CSP into assets it serves itself, never into a remote server's response (Phase 0 report section 8.2), so `app.security.csp` in `tauri.conf.json` governs **only** the local splash.
The Console page's own CSP is therefore sent by the daemon, in `commands/serve.py` (`_CONSOLE_CSP`).
Its load-bearing clause is `connect-src 'self'`: it confines every `fetch` and `EventSource` to the daemon's origin, so a page rendering instance names, ports and log tails cannot be turned into an exfiltration channel.

## Linux text zoom: one narrowly scoped capability

Text resize (WCAG 1.4.4) works on both platforms, but the mechanism differs.
On Windows, `zoomHotkeysEnabled` maps to WebView2's native zoom and needs **no permission**, so Windows keeps a completely empty permission set (`capabilities/default.json`).
On Linux, Tauri implements zoom as a JS polyfill behind the `core:webview:allow-set-webview-zoom` IPC command, which a remote origin can only reach if a capability grants it (Phase 0 report section 6.3).

`capabilities/linux-zoom.json` grants **exactly** that one command to the loopback origin, on Linux only (`"platforms": ["linux"]`), and nothing else.
The Linux security posture is therefore honestly **one narrowly scoped capability**, not zero.

## Navigation guard

The WebView may load only the app's own bundled assets (the splash) or the exact loopback origin of *this* daemon.
Everything else is denied: external links, a different loopback port, and `file://` URLs.
The guard is origin-exact **including the port**, because Phase 0 proved a sibling loopback port is a real escape route (`shell::navigation_allowed`, unit-tested in `src/shell.rs`).

## Cross-platform

Both first-class targets match the Phase 0 evidence:

- **Linux / WebKitGTK** (native, and WSL2/WSLg): the app talks to a local `cwcli serve` over loopback with no WSL bridge.
- **Windows / WebView2**: the shell launches and supervises the daemon inside WSL through `wsl.exe`, and reaches it over WSL's `localhost` forwarding.

macOS is not regressed by any design choice here, but is not a Phase 1 validation target.

## Design system

`design/` (repo root) is the canonical Caffeinated Whale design system and the design authority for this shell: all Console/shell styling derives from its tokens, components, and guidelines.
Read `design/readme.md` (or invoke the `caffeinated-whale-design` skill) before styling any Console surface.

### Adherence lint

`design/_adherence.oxlintrc.json` is the maintainer-authored canonical adherence policy for frontend JSX.
Current [oxlint](https://oxc.rs/docs/guide/usage/linter) cannot load it because the policy uses `no-restricted-syntax`, which oxlint does not implement, so oxlint rejects the entire config and enforces none of its rules.
Enforcing any of the policy requires an ESLint-compatible runner that implements both `no-restricted-imports` and `no-restricted-syntax`.
It is intentionally **not** yet an installed desktop dependency or a CI gate: Phase 1 ships no bundled JS frontend (the Console page is served by the Python `cwcli serve` daemon), so there is no JSX in `desktop/` to lint.

When frontend JSX lands in the shell, select an ESLint-compatible runner for the policy.
Add the selected lint commands to `.github/workflows/desktop.yml` at that point with `working-directory: .`, overriding the workflow's `desktop/src-tauri` default.
Add frontend lint tooling as desktop dev dependencies only, never to the Python runtime deps.

## Build and run

Prerequisites: the Rust toolchain (MSRV 1.77.2), and on Linux the Tauri system dependencies (`libwebkit2gtk-4.1-dev`, `libgtk-3-dev`, `libayatana-appindicator3-dev`, `librsvg2-dev`, `libxdo-dev`, `libssl-dev`, `pkg-config`).

```bash
cd desktop/src-tauri
cargo build            # compile the shell
cargo test             # run the shell's unit tests
cargo run              # run it (needs a real cwcli install and a display)
```

`cargo run` (a debug build) resolves the real installed `cwcli` and starts a real daemon, so it lists your real instances.

### Test seam (debug builds only)

Debug builds honour `CWCLI_DESKTOP_DAEMON_ARGV`, a JSON array giving the daemon launch command, so an E2E or manual run can point the shell at a controlled daemon without touching any real Docker instance.
This override is compiled **out** of release builds (`#[cfg(debug_assertions)]` in `src/shell.rs`), so a shipped shell always resolves the real `cwcli`.

## CI

`.github/workflows/desktop.yml` proves the shell compiles, lints clean, and passes its unit tests on **both** targets (Linux and Windows) on every change that touches it.
It is compile + `cargo fmt` + `cargo clippy -D warnings` + `cargo test`, not a full installer bundle: bundling (with signing, the updater key, and multi-resolution icons) is Phase 2, so `bundle.active` is `false`.

## Validation evidence (Linux)

The shell was built and run on Linux (WebKitGTK 2.52.3, the engine Phase 0 cleared).
The bring-up pipeline was exercised end to end against a fake, Docker-blocked daemon (no real instance was touched):

- The shell reserved a port, resolved and spawned the daemon, waited for it to answer, and navigated the WebView to the live Console (`preflight` -> `cwcli daemon ready; navigated to http://127.0.0.1:<p>/` in the log).
- The Console rendered a multi-instance fleet with the master-detail layout and the action rail.
- The local error surface rendered a typed failure (branded screen, hint, and detail code) instead of a blank page.
- `<dialog>.showModal()` rendered a modal with its backdrop and focus ring.

### The Wayland early-check

The upstream Wayland/WSL scrolling defect ([tauri#14427](https://github.com/tauri-apps/tauri/issues/14427)) was checked as an early in-build step rather than a gate.

- **Wheel scrolling did not reproduce the defect.** Under the X11/XWayland path, wheel events scrolled the fleet tree (a nested scroll container) normally.
- **`<dialog>` behaviour** rendered correctly under the same path.
- **Under the Wayland backend** (`GDK_BACKEND=wayland` on the reachable WSLg `wayland-0` session), the app launched, initialised the WebView, and completed bring-up without crashing.
  WSLg provides no working Wayland input-injection or screen-copy tooling, so wheel and dialog **input** could not be driven under the Wayland backend directly; those interactions were exercised under XWayland on the same WebKitGTK 2.52.3 engine.
- **GPU compositing remains unexercised on WSL**: rendering fell back to software (MESA/libEGL DRI3/ZINK fallback), so a native desktop with real GPU compositing is a different path that this environment cannot represent.

No reproduction of the scroll defect was observed, so the check did not trigger a stop condition.
A native GNOME/Wayland desktop with GPU compositing remains the one Linux surface this environment cannot cover, as the Phase 0 report also noted.

## Deferred to later phases (not in Phase 1)

- Installers, code-signed auto-update, and the tray icon and native notifications (Phase 2).
- Bearer-token / bundled-page shape (Phase 2). Phase 1 binds the daemon to loopback with authentication off, which is the safe shipped default for a loopback-only listener.
- The long-running job backend with Detach-not-Cancel semantics (Phase 3, and Python-side).
- Expanding the action surface to new verbs (its own captain roadmap). Phase 1 ships exactly the Console's current Tier A actions and read surfaces.
