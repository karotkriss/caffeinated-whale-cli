// Sign-in and the protocol check.
//
// `cwcli ui` opens the browser on `/#login=<one-time code>`. The fragment never
// reaches a server; the page posts it once to /api/v1/session for the HttpOnly
// session cookie and strips it from history, so the link in a terminal's
// scrollback is dead the moment it has been used.
import { reactive } from 'vue'

export const PROTOCOL = 5 // the daemon's PROTOCOL_VERSION this page was built against

export const session = reactive({
  signedOut: false,
  meta: null, // { version, protocol, build }
  protocolMismatch: false,
  updating: false, // the daemon is restarting onto upgraded cwcli code
  updateStuck: false, // ...and did not come back
})

// Read and strip the code at module load: main.js imports this module before
// the router, whose history captures the location (fragment included) when it
// is created and writes it back on the first navigation.
const code = new URLSearchParams(location.hash.replace(/^#/, '')).get('login')
if (code) history.replaceState(null, '', location.pathname + location.search)

export async function boot() {
  if (code) {
    const res = await fetch('/api/v1/session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code }),
    })
    if (!res.ok) session.signedOut = true
  }
  await recheckMeta()
}

// Called at boot and whenever the event stream reconnects. A self-update
// replaces the daemon, which drops every stream; the reconnect that follows
// sees a new version or protocol and reloads the page, with no polling between.
export async function recheckMeta() {
  try {
    const res = await fetch('/api/v1/meta', { cache: 'no-store' })
    const meta = await res.json()
    const prior = session.meta
    if (
      prior &&
      (meta.protocol !== prior.protocol || meta.version !== prior.version || meta.build !== prior.build)
    ) {
      location.reload()
      return
    }
    session.meta = meta
    session.protocolMismatch = meta.protocol !== PROTOCOL
  } catch {
    // Mid-update the daemon is briefly gone; keep what the page knew so the
    // reconnect can tell the new code from the old.
    if (!session.updating) session.meta = null
  }
}

// The daemon announces an upgrade on the stream, then restarts itself onto the new
// code (same port, same session). Poll until it answers with a different build and
// reload; give up after a minute and say how to recover rather than spin forever.
export function daemonUpdating() {
  if (session.updating) return
  session.updating = true
  const started = Date.now()
  const timer = window.setInterval(async () => {
    try {
      const res = await fetch('/api/v1/meta', { cache: 'no-store' })
      const meta = await res.json()
      if (!meta.updating && meta.build !== session.meta?.build) location.reload()
    } catch {
      // still restarting
    }
    if (Date.now() - started > 60000) {
      window.clearInterval(timer)
      session.updateStuck = true
    }
  }, 1000)
}
