//! The cwcli desktop shell.
//!
//! A thin Tauri 2 shell over the UNCHANGED Python core. It performs preflight,
//! supervises exactly one `cwcli serve` daemon over loopback, keeps single-
//! instance behaviour, restores window state, writes a log file, and points its
//! WebView at the daemon's own Console page. It contains NO Docker client, no
//! bench/site/app concept, no consent logic, and no job state: every domain
//! decision flows through the daemon and the Python core behind it.
//!
//! Bring-up sequence (all fallible steps render into the local splash, never a
//! blank page - Phase 0 finding P1-1):
//!   splash shown -> reserve a loopback port -> locate cwcli -> spawn the daemon
//!   -> wait until it answers HTTP 200 -> navigate the WebView to it.

mod shell;

use std::io;
use std::process::{Child, ExitStatus};
use std::sync::atomic::{AtomicBool, AtomicU16, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tauri::webview::PageLoadEvent;
use tauri::{AppHandle, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};

use shell::ShellError;

/// The daemon child, held so it can be ended when the app exits. WSL and Linux
/// both release the port once this dies (see `shell::stop_daemon`).
#[derive(Default)]
struct DaemonHandle {
    shutting_down: AtomicBool,
    child: Mutex<Option<Child>>,
}

impl DaemonHandle {
    fn register(&self, mut child: Child) -> bool {
        let Ok(mut guard) = self.child.lock() else {
            shell::stop_daemon(&mut child);
            return false;
        };
        if self.shutting_down.load(Ordering::Acquire) || guard.is_some() {
            shell::stop_daemon(&mut child);
            return false;
        }
        *guard = Some(child);
        true
    }

    fn try_wait(&self) -> io::Result<Option<ExitStatus>> {
        let mut guard = self
            .child
            .lock()
            .map_err(|_| io::Error::other("daemon handle lock is unavailable"))?;
        guard
            .as_mut()
            .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "daemon is not registered"))?
            .try_wait()
    }

    fn is_shutting_down(&self) -> bool {
        self.shutting_down.load(Ordering::Acquire)
    }

    fn shutdown(&self) {
        self.shutting_down.store(true, Ordering::Release);
        if let Ok(mut guard) = self.child.lock() {
            if let Some(mut child) = guard.take() {
                shell::stop_daemon(&mut child);
            }
        }
    }
}

/// Entry point invoked from `main`.
pub fn run() {
    tauri::Builder::default()
        // A second launch focuses the running window instead of starting a
        // second daemon fighting for a port.
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.show();
                let _ = w.unminimize();
                let _ = w.set_focus();
            }
        }))
        .plugin(tauri_plugin_window_state::Builder::default().build())
        .plugin(
            tauri_plugin_log::Builder::default()
                .level(log::LevelFilter::Info)
                .build(),
        )
        .manage(DaemonHandle::default())
        .setup(|app| {
            let handle = app.handle().clone();
            let port_slot = Arc::new(AtomicU16::new(0));
            let nav_slot = port_slot.clone();
            let bringup_slot = port_slot.clone();
            let bringup_started = Arc::new(AtomicBool::new(false));

            let window =
                WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                    .title("cwcli Console")
                    .inner_size(1180.0, 820.0)
                    .min_inner_size(440.0, 520.0)
                    // Linux implements zoom as a JS polyfill behind the
                    // `core:webview:allow-set-webview-zoom` IPC command (granted, on
                    // Linux only, to the loopback origin in capabilities/); Windows
                    // maps this to WebView2's native zoom needing no permission. WCAG
                    // 1.4.4 text resize either way.
                    .zoom_hotkeys_enabled(true)
                    .on_navigation(move |url| {
                        shell::navigation_allowed(url, nav_slot.load(Ordering::Relaxed))
                    })
                    .on_page_load(move |_window, payload| {
                        if claim_bringup(payload.event(), &bringup_started) {
                            let handle = handle.clone();
                            let port_slot = bringup_slot.clone();
                            std::thread::spawn(move || bringup(handle, port_slot));
                        }
                    })
                    .build()?;

            // Phase 0 finding: the window opened minimized on Windows. Show,
            // unminimize and focus explicitly rather than trusting the default.
            let _ = window.show();
            let _ = window.unminimize();
            let _ = window.set_focus();
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build the cwcli desktop shell")
        .run(|handle, event| {
            if let RunEvent::Exit = event {
                end_daemon(handle);
            }
        });
}

fn claim_bringup(event: PageLoadEvent, started: &AtomicBool) -> bool {
    matches!(event, PageLoadEvent::Finished)
        && started
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .is_ok()
}

/// Reserve a port, start the daemon, wait for it, then swap the WebView from the
/// local splash to the live Console. Any failure renders into the splash.
fn bringup(handle: AppHandle, port_slot: Arc<AtomicU16>) {
    let Some(daemon) = handle.try_state::<DaemonHandle>() else {
        return;
    };
    if daemon.is_shutting_down() {
        return;
    }
    let Some(window) = handle.get_webview_window("main") else {
        return;
    };
    let status = |msg: &str| {
        let _ = window.eval(format!("window.__cwcliStatus({})", js_string(msg)));
    };
    let fail = |e: &ShellError| {
        let payload = serde_json::to_string(e).unwrap_or_else(|_| "{}".into());
        let _ = window.eval(format!("window.__cwcliError({payload})"));
        log::error!("preflight failed [{}]: {}", e.code, e.message);
    };

    status("Reserving a local port\u{2026}");
    let port = match shell::pick_free_port() {
        Ok(p) => p,
        Err(e) => return fail(&e),
    };
    port_slot.store(port, Ordering::Relaxed);

    status("Locating cwcli\u{2026}");
    let argv = match shell::resolve_launch(port) {
        Ok(a) => a,
        Err(e) => return fail(&e),
    };

    status("Starting the cwcli daemon\u{2026}");
    if daemon.is_shutting_down() {
        return;
    }
    let child = match shell::spawn_daemon(&argv) {
        Ok(c) => c,
        Err(e) => return fail(&e),
    };
    if !daemon.register(child) {
        return;
    }

    status("Waiting for the daemon\u{2026}");
    if let Err(e) =
        shell::wait_until_ready(port, Duration::from_secs(40), || daemon.try_wait())
    {
        end_daemon(&handle); // spawned but never served: leave no orphan
        return fail(&e);
    }

    match tauri::Url::parse(&shell::daemon_url(port)) {
        Ok(u) => {
            let _ = window.navigate(u);
            log::info!(
                "cwcli daemon ready; navigated to {}",
                shell::daemon_url(port)
            );
        }
        Err(err) => {
            // Constructed from a numeric port, so this is unreachable in practice.
            log::error!("could not parse the daemon URL: {err}");
        }
    }
}

fn end_daemon(handle: &AppHandle) {
    if let Some(state) = handle.try_state::<DaemonHandle>() {
        state.shutdown();
    }
}

/// JSON-encode a string so it is safe to drop into an `eval`'d JS call.
fn js_string(s: &str) -> String {
    serde_json::to_string(s).unwrap_or_else(|_| "\"\"".into())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bringup_is_claimed_by_only_the_first_finished_load() {
        let started = AtomicBool::new(false);

        assert!(!claim_bringup(PageLoadEvent::Started, &started));
        assert!(claim_bringup(PageLoadEvent::Finished, &started));
        assert!(!claim_bringup(PageLoadEvent::Finished, &started));
    }
}
