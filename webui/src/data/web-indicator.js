// The Bench page's per-bench web chip. The fleet's FAST tier checks the web port
// with a bare TCP connect (no HTTP request, so nothing lands in the access log)
// and reports `web_listening`; an HTTP code appears only where one was measured.
// null means the web check was not asked for this cycle: show nothing.
// While `stale` (the latest probe failed) any reading is the last good probe's,
// so it shows as unknown (ok: null) rather than as a live verdict.
export function webIndicator(health, { stale } = {}) {
  const code = health?.web_http_code
  let chip = null
  if (code) chip = { label: `HTTP ${code}`, ok: code.startsWith('2') }
  else if (health?.web_listening === true) chip = { label: 'Listening', ok: true }
  else if (health?.web_listening === false) chip = { label: 'Not listening', ok: false }
  return chip && stale ? { label: 'unknown', ok: null } : chip
}
