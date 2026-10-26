// The Bench page's per-bench web chip. The fleet's FAST tier checks the web port
// with a bare TCP connect (no HTTP request, so nothing lands in the access log)
// and reports `web_listening`; an HTTP code appears only where one was measured.
// null means the web check was not asked for this cycle: show nothing.
export function webIndicator(health) {
  const code = health?.web_http_code
  if (code) return { label: `HTTP ${code}`, ok: code.startsWith('2') }
  if (health?.web_listening === true) return { label: 'Listening', ok: true }
  if (health?.web_listening === false) return { label: 'Not listening', ok: false }
  return null
}
