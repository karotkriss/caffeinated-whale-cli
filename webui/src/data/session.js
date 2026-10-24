// Sign-in and the protocol check.
//
// `cwcli ui` opens the browser on `/#login=<one-time code>`. The fragment never
// reaches a server; the page posts it once to /api/v1/session for the HttpOnly
// session cookie and strips it from history, so the link in a terminal's
// scrollback is dead the moment it has been used.
import { reactive } from 'vue'

export const PROTOCOL = 2 // the daemon's PROTOCOL_VERSION this page was built against

export const session = reactive({
  signedOut: false,
  meta: null, // { version, protocol }
  protocolMismatch: false,
})

export async function boot() {
  const code = new URLSearchParams(location.hash.replace(/^#/, '')).get('login')
  if (code) {
    history.replaceState(null, '', location.pathname + location.search)
    const res = await fetch('/api/v1/session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code }),
    })
    if (!res.ok) session.signedOut = true
  }
  try {
    const res = await fetch('/api/v1/meta')
    session.meta = await res.json()
    session.protocolMismatch = session.meta.protocol !== PROTOCOL
  } catch {
    session.meta = null
  }
}
