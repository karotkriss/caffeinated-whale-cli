//! The shell's product-free plumbing: preflight, daemon launch, health poll.
//!
//! This module holds the OS-integration half of the desktop shell and NOTHING
//! about Docker, Frappe, benches, sites, consent, or jobs. It resolves how to
//! start one `cwcli serve` process, starts it, waits for it to answer over
//! loopback, and offers a clean teardown. Every domain decision stays in the
//! Python core reached through that daemon (the feasibility architecture's hard
//! boundary: the Rust layer renders the Console, it does not reimplement it).

use std::io::{self, BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::Path;
use std::process::{Child, Command, ExitStatus, Stdio};
use std::time::Duration;

/// The daemon is started by running the serve MODULE as a script, never the
/// `cwcli serve` command - that command is deliberately withheld from the
/// released CLI/axi surfaces (unregistered in `main.py`). Running the module is
/// the sanctioned internal entry point: it adds no console-script binary and no
/// Typer verb, so nothing a `cwcli --help` listing can discover changes.
pub const DAEMON_MODULE: &str = "caffeinated_whale_cli.commands.serve";

const LOOPBACK: &str = "127.0.0.1";

/// A structured failure the local splash renders instead of a blank page.
///
/// This exists because the Phase 1 shape points the WebView at a remote origin,
/// so when preflight or the daemon fails there is no page to fall back to (Phase
/// 0 finding P1-1). The shell owns its own first-run/error surface, and this is
/// the payload it hands that surface.
#[derive(Debug, Clone, serde::Serialize)]
pub struct ShellError {
    pub code: String,
    pub message: String,
    pub hint: String,
}

impl ShellError {
    fn new(code: &str, message: impl Into<String>, hint: impl Into<String>) -> Self {
        ShellError {
            code: code.into(),
            message: message.into(),
            hint: hint.into(),
        }
    }
}

/// Pick a free loopback TCP port for the daemon.
///
/// ponytail: a bind-to-:0-then-release has a tiny TOCTOU window before the
/// daemon rebinds it. Acceptable here - the app is single-instance and the only
/// thing it launches, so the only racer is an unrelated process grabbing this
/// exact ephemeral port in the microsecond gap. The upgrade path, if it ever
/// bites, is to pass the pre-bound listener's fd to the child.
pub fn pick_free_port() -> Result<u16, ShellError> {
    TcpListener::bind((LOOPBACK, 0))
        .and_then(|l| l.local_addr())
        .map(|a| a.port())
        .map_err(|e| {
            ShellError::new(
                "port_unavailable",
                "Could not reserve a local port for the cwcli daemon.",
                format!("The loopback interface may be unavailable: {e}"),
            )
        })
}

/// The full argv used to launch the daemon on `port`, platform-resolved.
///
/// Linux: `<interpreter> -m <module> --host 127.0.0.1 --port <p>`, where the
/// interpreter is read from the installed `cwcli` console script's shebang - the
/// interpreter that runs `cwcli` is exactly the one that can import its package.
/// Windows: the same, wrapped in `wsl.exe -d <distro> -- ...`, because cwcli,
/// Docker and Frappe live inside WSL.
pub fn resolve_launch(port: u16) -> Result<Vec<String>, ShellError> {
    let mut argv = resolve_base()?;
    argv.push("--host".into());
    argv.push(LOOPBACK.into());
    argv.push("--port".into());
    argv.push(port.to_string());
    Ok(argv)
}

fn resolve_base() -> Result<Vec<String>, ShellError> {
    // Test-only seam, compiled OUT of release builds. It lets an E2E/dev run
    // point the shell at a fake, Docker-blocked daemon so the render and
    // supervision path can be exercised without touching any real instance.
    // A release binary has no such override and always resolves the real cwcli.
    #[cfg(debug_assertions)]
    if let Ok(raw) = std::env::var("CWCLI_DESKTOP_DAEMON_ARGV") {
        let argv: Vec<String> = serde_json::from_str(&raw).map_err(|e| {
            ShellError::new(
                "dev_override_invalid",
                "CWCLI_DESKTOP_DAEMON_ARGV is not a JSON array of strings.",
                e.to_string(),
            )
        })?;
        if argv.is_empty() {
            return Err(ShellError::new(
                "dev_override_invalid",
                "CWCLI_DESKTOP_DAEMON_ARGV is an empty array.",
                "Provide at least the interpreter path.",
            ));
        }
        return Ok(argv);
    }
    resolve_cwcli_base()
}

#[cfg(not(target_os = "windows"))]
fn resolve_cwcli_base() -> Result<Vec<String>, ShellError> {
    // A login shell so ~/.local/bin (where `uv tool install` puts cwcli) is on
    // PATH even when the app is launched from a desktop icon with a minimal env.
    let out = Command::new("bash")
        .args(["-lc", "command -v cwcli"])
        .output()
        .map_err(|e| {
            ShellError::new(
                "shell_unavailable",
                "Could not run a shell to locate cwcli.",
                e.to_string(),
            )
        })?;
    let path = String::from_utf8_lossy(&out.stdout).trim().to_string();
    if !out.status.success() || path.is_empty() {
        return Err(cwcli_missing());
    }
    let interpreter = read_shebang(&path).unwrap_or_else(|| "python3".to_string());
    Ok(vec![interpreter, "-m".into(), DAEMON_MODULE.into()])
}

#[cfg(target_os = "windows")]
fn resolve_cwcli_base() -> Result<Vec<String>, ShellError> {
    // 1. WSL present at all?
    let listed = Command::new("wsl.exe").args(["-l", "-q"]).output();
    let listed = match listed {
        Ok(o) if o.status.success() => o,
        _ => {
            return Err(ShellError::new(
                "wsl_missing",
                "Windows Subsystem for Linux is not available.",
                "Install it with 'wsl --install', then restart and reopen this app.",
            ))
        }
    };
    // wsl.exe emits its OWN output as UTF-16LE; child output would be raw bytes,
    // but `-l -q` is wsl.exe's own listing.
    let candidates: Vec<String> = decode_utf16le(&listed.stdout)
        .lines()
        .map(|l| l.trim().trim_end_matches('\r').to_string())
        .filter(|d| !d.is_empty() && !is_docker_distro(d))
        .collect();
    if candidates.is_empty() {
        return Err(ShellError::new(
            "no_distro",
            "No usable WSL distribution was found.",
            "Install a distro (for example 'wsl --install -d Ubuntu') that has cwcli.",
        ));
    }
    // 2. First candidate that actually has cwcli. One wsl.exe call resolves both
    //    presence and the interpreter, via the console script's shebang.
    for distro in &candidates {
        let probe = Command::new("wsl.exe")
            .args([
                "-d",
                distro,
                "--",
                "bash",
                "-lc",
                "head -1 \"$(command -v cwcli)\"",
            ])
            .output();
        if let Ok(o) = probe {
            let shebang = String::from_utf8_lossy(&o.stdout).trim().to_string();
            if o.status.success() && shebang.starts_with("#!") {
                let interpreter = shebang
                    .trim_start_matches("#!")
                    .split_whitespace()
                    .next()
                    .unwrap_or("python3")
                    .to_string();
                return Ok(vec![
                    "wsl.exe".into(),
                    "-d".into(),
                    distro.clone(),
                    "--".into(),
                    interpreter,
                    "-m".into(),
                    DAEMON_MODULE.into(),
                ]);
            }
        }
    }
    Err(cwcli_missing())
}

fn cwcli_missing() -> ShellError {
    ShellError::new(
        "cwcli_missing",
        "cwcli is not installed where this app can find it.",
        "Install it with 'uv tool install caffeinated-whale-cli', then reopen this app.",
    )
}

#[cfg(not(target_os = "windows"))]
fn read_shebang(script: &str) -> Option<String> {
    let first = std::fs::read_to_string(Path::new(script)).ok()?;
    let line = first.lines().next()?;
    let rest = line.strip_prefix("#!")?;
    rest.split_whitespace().next().map(str::to_string)
}

#[cfg(target_os = "windows")]
fn is_docker_distro(name: &str) -> bool {
    matches!(name, "docker-desktop" | "docker-desktop-data")
}

#[cfg(target_os = "windows")]
fn decode_utf16le(bytes: &[u8]) -> String {
    let units: Vec<u16> = bytes
        .chunks_exact(2)
        .map(|c| u16::from_le_bytes([c[0], c[1]]))
        .collect();
    String::from_utf16_lossy(&units)
}

/// Spawn the daemon. The child is held so the shell can end it on exit.
pub fn spawn_daemon(argv: &[String]) -> Result<Child, ShellError> {
    let mut child = Command::new(&argv[0])
        .args(&argv[1..])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| {
            ShellError::new(
                "daemon_spawn_failed",
                "The cwcli daemon could not be started.",
                e.to_string(),
            )
        })?;
    if let Some(stdout) = child.stdout.take() {
        log_daemon_stream("stdout", stdout, log::Level::Info);
    }
    if let Some(stderr) = child.stderr.take() {
        log_daemon_stream("stderr", stderr, log::Level::Warn);
    }
    Ok(child)
}

fn log_daemon_stream(
    stream_name: &'static str,
    stream: impl Read + Send + 'static,
    level: log::Level,
) {
    let thread = std::thread::Builder::new()
        .name(format!("cwcli-daemon-{stream_name}"))
        .spawn(move || {
            let mut reader = BufReader::new(stream);
            let mut line = Vec::new();
            loop {
                line.clear();
                match reader.read_until(b'\n', &mut line) {
                    Ok(0) => break,
                    Ok(_) => {
                        while matches!(line.last(), Some(b'\n' | b'\r')) {
                            line.pop();
                        }
                        log::log!(
                            target: "cwcli_daemon",
                            level,
                            "{stream_name}: {}",
                            String::from_utf8_lossy(&line)
                        );
                    }
                    Err(error) => {
                        log::warn!(
                            target: "cwcli_daemon",
                            "could not read daemon {stream_name}: {error}"
                        );
                        break;
                    }
                }
            }
        });
    if let Err(error) = thread {
        log::warn!(
            target: "cwcli_daemon",
            "could not start daemon {stream_name} logger: {error}"
        );
    }
}

/// Poll loopback until the daemon answers `HTTP 200` on `/`, bounded.
///
/// A plain TCP connect proves only the socket is open; this reads the status
/// line so "answers" means the handler is actually serving. `bootstrap()` runs
/// before the daemon binds its port, so an open port here is a served page.
pub fn wait_until_ready(
    port: u16,
    timeout: Duration,
    mut child_status: impl FnMut() -> io::Result<Option<ExitStatus>>,
) -> Result<(), ShellError> {
    let deadline = std::time::Instant::now() + timeout;
    while std::time::Instant::now() < deadline {
        match child_status() {
            Ok(Some(status)) => {
                return Err(ShellError::new(
                    "daemon_exited",
                    format!("The cwcli daemon exited before it began serving ({status})."),
                    "Check the app log for the daemon's output, then try reopening.",
                ));
            }
            Err(error) => {
                return Err(ShellError::new(
                    "daemon_status_failed",
                    "The cwcli daemon's status could not be checked.",
                    format!("{error}. Try reopening the app."),
                ));
            }
            Ok(None) => {}
        }
        if http_root_ok(port) {
            return Ok(());
        }
        std::thread::sleep(Duration::from_millis(250));
    }
    Err(ShellError::new(
        "daemon_unresponsive",
        "The cwcli daemon started but did not begin serving in time.",
        "Check the app log, or try reopening. Docker may be unreachable.",
    ))
}

fn http_root_ok(port: u16) -> bool {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let Ok(mut stream) = TcpStream::connect_timeout(&addr, Duration::from_millis(500)) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(1000)));
    // HTTP/1.0 + Connection: close so the daemon replies and hangs up; reading
    // the status line is all we need.
    if stream
        .write_all(b"GET / HTTP/1.0\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        .is_err()
    {
        return false;
    }
    let mut buf = [0u8; 32];
    let n = stream.read(&mut buf).unwrap_or(0);
    String::from_utf8_lossy(&buf[..n]).contains(" 200")
}

/// The loopback origin the WebView is navigated to once the daemon answers.
pub fn daemon_url(port: u16) -> String {
    format!("http://{LOOPBACK}:{port}/")
}

/// Whether a navigation is allowed: the app's own bundled assets (the splash),
/// or the exact loopback origin of THIS daemon. Everything else - external
/// links, a different loopback port, file URLs - is denied.
///
/// Origin-exact, including the port: Phase 0 proved a sibling loopback port is a
/// real escape route, so "loopback is fine" is not the rule; "this daemon's
/// port" is.
pub fn navigation_allowed(url: &tauri::Url, daemon_port: u16) -> bool {
    let scheme = url.scheme();
    let host = url.host_str().unwrap_or("");
    let port = url.port();
    let bundled =
        (scheme == "tauri" && host == "localhost" && port.is_none())
            || (scheme == "http" && host == "tauri.localhost" && port.is_none());
    let daemon = daemon_port != 0
        && scheme == "http"
        && matches!(host, "127.0.0.1" | "localhost")
        && port == Some(daemon_port);
    bundled || daemon
}

/// End the daemon child. On Linux this terminates the process directly; on
/// Windows the child is `wsl.exe`, whose death SIGHUPs the Linux daemon (which
/// does not trap it) and, per WSL session teardown, tears the daemon down with
/// the launcher regardless. Either way the port is released.
pub fn stop_daemon(child: &mut Child) {
    let _ = child.kill();
    let _ = child.wait();
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Instant;

    fn url(s: &str) -> tauri::Url {
        tauri::Url::parse(s).unwrap()
    }

    #[test]
    fn allows_the_bundled_splash_and_the_exact_daemon_origin() {
        assert!(navigation_allowed(
            &url("tauri://localhost/index.html"),
            8765
        ));
        assert!(navigation_allowed(
            &url("http://tauri.localhost/index.html"),
            8765
        ));
        assert!(navigation_allowed(&url("http://127.0.0.1:8765/"), 8765));
        assert!(navigation_allowed(
            &url("http://localhost:8765/api/events"),
            8765
        ));
    }

    #[test]
    fn denies_external_and_sibling_ports_and_file_urls() {
        assert!(!navigation_allowed(&url("https://example.com/"), 8765));
        assert!(!navigation_allowed(&url("http://127.0.0.1:8766/"), 8765));
        assert!(!navigation_allowed(&url("file:///etc/passwd"), 8765));
        assert!(!navigation_allowed(
            &url("http://127.0.0.1.evil.com:8765/"),
            8765
        ));
        assert!(!navigation_allowed(
            &url("https://127.0.0.1:8765/"),
            8765
        ));
        assert!(!navigation_allowed(
            &url("tauri://elsewhere/index.html"),
            8765
        ));
        assert!(!navigation_allowed(
            &url("http://tauri.localhost:8765/index.html"),
            8765
        ));
        assert!(!navigation_allowed(
            &url("https://tauri.localhost/index.html"),
            8765
        ));
    }

    #[test]
    fn a_reserved_port_is_in_range_and_usable() {
        let p = pick_free_port().expect("a loopback port");
        assert!(p >= 1024);
        // Not answering yet: nothing is bound there.
        assert!(!http_root_ok(p));
    }

    #[test]
    fn resolve_launch_appends_the_loopback_host_and_port() {
        // Drive the resolver through the test-only override so the assertion does
        // not depend on cwcli being installed on the runner.
        std::env::set_var(
            "CWCLI_DESKTOP_DAEMON_ARGV",
            r#"["/usr/bin/python3","-m","x"]"#,
        );
        let argv = resolve_launch(31999).expect("argv");
        std::env::remove_var("CWCLI_DESKTOP_DAEMON_ARGV");
        assert_eq!(
            argv,
            vec![
                "/usr/bin/python3",
                "-m",
                "x",
                "--host",
                "127.0.0.1",
                "--port",
                "31999"
            ]
        );
    }

    #[test]
    fn readiness_fails_promptly_when_the_daemon_exits() {
        let p = pick_free_port().expect("a loopback port");
        #[cfg(not(target_os = "windows"))]
        let mut child = Command::new("sh")
            .args(["-c", "exit 23"])
            .spawn()
            .expect("short-lived child");
        #[cfg(target_os = "windows")]
        let mut child = Command::new("cmd")
            .args(["/C", "exit 23"])
            .spawn()
            .expect("short-lived child");

        let started = Instant::now();
        let error = wait_until_ready(p, Duration::from_secs(5), || child.try_wait())
            .expect_err("an exited daemon must fail readiness");

        assert_eq!(error.code, "daemon_exited");
        assert!(started.elapsed() < Duration::from_secs(2));
    }
}
